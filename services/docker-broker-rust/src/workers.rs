//! Stack-scoped Temporal-worker control. Never act on a label from another project.
use std::{
    collections::{BTreeMap, BTreeSet},
    io::Read,
    sync::Arc,
    time::Duration,
};

use axum::{
    Json,
    extract::State,
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};

use crate::{Broker, runtime, valid_docker_id};

const API: &str = "/v1.45";
const PROJECT: &str = "com.docker.compose.project";

pub struct WorkerConfig {
    pub docker_self: String,
    pub project_override: Option<String>,
    pub service_label: String,
    pub image: String,
    pub network: String,
    pub artifacts_volume: Option<String>,
    pub environment: BTreeMap<String, String>,
    pub replicas: usize,
    pub grace_seconds: u64,
    pub temporal_receipt_required: bool,
    pub app_version: String,
}

fn path_component(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 255
        && !matches!(value, "." | "..")
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
}

impl Broker {
    async fn worker_project(&self) -> Result<String, ()> {
        let config = self.config.workers.as_ref().ok_or(())?;
        if let Some(project) = &config.project_override {
            return path_component(project).then(|| project.clone()).ok_or(());
        }
        if !path_component(&config.docker_self) {
            return Err(());
        }
        let (status, own) = self
            .docker_json(
                Method::GET,
                &format!("{API}/containers/{}/json", config.docker_self),
                Value::Null,
                self.config.deadline,
            )
            .await?;
        if status != StatusCode::OK {
            return Err(());
        }
        own["Config"]["Labels"][PROJECT]
            .as_str()
            .filter(|v| path_component(v))
            .map(str::to_owned)
            .ok_or(())
    }

    async fn match_workers(&self, project: &str, all: bool) -> Result<Vec<(String, Value)>, ()> {
        let config = self.config.workers.as_ref().ok_or(())?;
        let (label, expected) = config
            .service_label
            .split_once('=')
            .filter(|(k, v)| !k.is_empty() && !v.is_empty())
            .ok_or(())?;
        let rows = self
            .list_containers(
                &[config.service_label.clone(), format!("{PROJECT}={project}")],
                all,
            )
            .await?;
        rows.into_iter()
            .filter(|row| row["Labels"][label] == expected && row["Labels"][PROJECT] == project)
            .map(|row| {
                let id = row["Id"]
                    .as_str()
                    .filter(|v| valid_docker_id(v))
                    .ok_or(())?
                    .to_owned();
                Ok((id, row))
            })
            .collect()
    }

    pub(crate) async fn list_containers(
        &self,
        labels: &[String],
        all: bool,
    ) -> Result<Vec<Value>, ()> {
        let filters = json!({"label":labels}).to_string();
        let query = form_urlencoded::Serializer::new(String::new())
            .append_pair("all", if all { "true" } else { "false" })
            .append_pair("filters", &filters)
            .finish();
        let data = self
            .docker_get(&format!("{API}/containers/json?{query}"), 8 * 1024 * 1024)
            .await?;
        serde_json::from_slice(&data).map_err(|_| ())
    }

    async fn inspect_worker(&self, id: &str) -> Result<Value, ()> {
        let (status, inspect) = self
            .docker_json(
                Method::GET,
                &format!("{API}/containers/{id}/json"),
                Value::Null,
                self.config.deadline,
            )
            .await?;
        if status != StatusCode::OK {
            return Err(());
        }
        Ok(inspect)
    }

    async fn do_worker_restart(&self, id: &str, seconds: u64) -> Result<(), ()> {
        let (status, _) = self
            .docker_payload(
                Method::POST,
                &format!("{API}/containers/{id}/restart?t={seconds}"),
                axum::body::Bytes::new(),
                None,
                1024,
                Duration::from_secs(seconds.saturating_add(30)),
            )
            .await?;
        (status == StatusCode::NO_CONTENT).then_some(()).ok_or(())
    }

    pub(crate) async fn workers_snapshot(&self) -> Value {
        let Some(config) = &self.config.workers else {
            return json!({"status":"degraded","ready":0,"error":"Worker checks are not configured"});
        };
        let check = async {
            let project = self.worker_project().await?;
            let mut rows = Vec::new();
            for (id, _) in self.match_workers(&project, true).await? {
                let worker = self.inspect_worker(&id).await?;
                let state = &worker["State"];
                let health = &state["Health"];
                let detail = health["Log"].as_array().and_then(|entries| entries.last()).and_then(|entry| entry["Output"].as_str()).map(|text| text.trim().chars().take(500).collect::<String>());
                rows.push(json!({"name":worker["Name"].as_str().map(|n| n.trim_start_matches('/')),"status":state["Status"].as_str().unwrap_or("unknown"),"health":health["Status"],"detail":detail}));
            }
            Ok::<_, ()>(rows)
        }.await;
        match check {
            Ok(rows) => {
                let ready = rows
                    .iter()
                    .filter(|row| row["status"] == "running" && row["health"] == "healthy")
                    .count();
                let error = self.worker_error.lock().unwrap().clone();
                json!({"status":if ready >= config.replicas && error.is_none() { "ready" } else { "degraded" },"desired":config.replicas,"ready":ready,"containers":rows,"error":error})
            }
            Err(()) => {
                json!({"status":"degraded","desired":config.replicas,"ready":0,"containers":[],"error":"Could not inspect deployment workers"})
            }
        }
    }

    pub(crate) async fn reconcile_workers(&self) {
        let Some(config) = &self.config.workers else {
            return;
        };
        let _lock = self.worker_ops.lock().await;
        let work = async {
            let project = self.worker_project().await?;
            let rows = self.match_workers(&project, true).await?;
            let mut available = rows.len();
            let mut names = BTreeSet::new();
            let mut failed = false;
            for (id, _) in rows {
                let Ok(worker) = self.inspect_worker(&id).await else { failed = true; continue; };
                names.insert(worker["Name"].as_str().unwrap_or_default().trim_start_matches('/').to_owned());
                let status = &worker["State"]["Status"];
                let action = match status.as_str() {
                    Some("dead") => Some((Method::DELETE, "")),
                    Some("created" | "exited") => Some((Method::POST, "/start")),
                    Some("paused") => Some((Method::POST, "/unpause")),
                    Some("running") if worker["State"]["Health"]["Status"] == "unhealthy" => {
                        failed |= self.do_worker_restart(&id, config.grace_seconds).await.is_err(); None
                    }
                    _ => None,
                };
                if let Some((method,suffix)) = action {
                    if self.docker_request(method, &format!("{API}/containers/{id}{suffix}"),1024).await.is_ok_and(|(status,_)| status == StatusCode::NO_CONTENT) {
                        if suffix.is_empty() { available -= 1; }
                    } else { failed = true; }
                }
            }
            let mut number = 1;
            while available < config.replicas {
                let name = format!("{project}-worker-{number}");
                number += 1;
                if names.contains(&name) { continue; }
                let (label,label_value) = config.service_label.split_once('=').ok_or(())?;
                let env: Vec<_> = config.environment.iter().map(|(key,value)| format!("{key}={value}")).collect();
                let fallback = format!("{project}_artifacts");
                let body = json!({"Image":config.image,"Env":env,"Labels":{(label):label_value,(PROJECT):project,"com.docker.compose.container-number":(number-1).to_string()},
                    "HostConfig":{"Binds":[format!("{}:/workflows:rw",self.config.workflows_volume),format!("{}:/artifacts:rw",config.artifacts_volume.as_deref().unwrap_or(&fallback))],"NetworkMode":config.network,
                        "RestartPolicy":{"Name":"unless-stopped"},"LogConfig":{"Type":"json-file","Config":{"max-size":"10m","max-file":"3"}}}});
                let (status,result) = self.docker_json(Method::POST,&format!("{API}/containers/create?name={name}"),body,self.config.deadline).await?;
                if status != StatusCode::CREATED { return Err(()); }
                let id = result["Id"].as_str().filter(|v| valid_docker_id(v)).ok_or(())?;
                let (status,_) = self.docker_request(Method::POST,&format!("{API}/containers/{id}/start"),1024).await?;
                if status != StatusCode::NO_CONTENT { return Err(()); }
                available += 1;
            }
            if failed { return Err(()); }
            Ok(())
        }.await;
        *self.worker_error.lock().unwrap() = work
            .err()
            .map(|_| "Worker reconciliation failed; retrying".to_owned());
    }

    pub(crate) async fn temporal_snapshot(&self) -> Value {
        let Some(cfg) = &self.config.workers else {
            return json!({"status":"degraded","phase":"unknown_project"});
        };
        let Ok(project) = self.worker_project().await else {
            return json!({"status":"degraded","phase":"unknown_project"});
        };
        let phase =
            |phase, ready| json!({"phase":phase,"status":if ready { "ready" } else { "degraded" }});
        let Ok(jobs) = self
            .list_containers(&[format!("nautionette.temporal.project={project}")], true)
            .await
        else {
            return phase("inspection_failed", false);
        };
        let mut jobs = jobs
            .iter()
            .filter(|job| job["Labels"]["nautionette.temporal.project"] == project);
        let Some(job) = jobs.next() else {
            return if cfg.temporal_receipt_required {
                phase("receipt_missing", false)
            } else {
                phase("not_required", true)
            };
        };
        if jobs.next().is_some() {
            return phase("controller_unavailable", false);
        }
        let Some(id) = job["Id"].as_str().filter(|v| valid_docker_id(v)) else {
            return phase("inspection_failed", false);
        };
        let Some(commit) = job["Labels"]["nautionette.temporal.commit"]
            .as_str()
            .filter(|value| {
                value.len() == 40 && value.bytes().all(|b| matches!(b,b'0'..=b'9'|b'a'..=b'f'))
            })
        else {
            return phase("inspection_failed", false);
        };
        let Ok(info) = self.inspect_worker(id).await else {
            return phase("inspection_failed", false);
        };
        let result = self
            .docker_request(
                Method::GET,
                &format!(
                    "{API}/containers/{id}/archive?path=%2Fopt%2Fmaintenance%2Fcompleted.json"
                ),
                64 * 1024,
            )
            .await;
        let received = match result {
            Ok((StatusCode::OK, tar)) => parse_receipt(&tar),
            Ok((StatusCode::NOT_FOUND, _)) if info["State"]["Status"] == "running" => {
                match runtime::fixed_exec(
                    self,
                    id,
                    None,
                    &[
                        "python",
                        "/opt/maintenance/temporal_deploy_job.py",
                        "status",
                    ],
                )
                .await
                {
                    Ok((0, data)) if data.len() <= 16 * 1024 => {
                        runtime::raw_docker_payload(&data, 1)
                            .and_then(|raw| serde_json::from_str::<Value>(&raw).map_err(|_| ()))
                    }
                    _ => Err(()),
                }
            }
            _ => Err(()),
        };
        let Ok(result) = received else {
            return phase("inspection_failed", false);
        };
        if result["commit"] != commit || !result["status"].is_string() {
            return phase("inspection_failed", false);
        }
        let complete = result["status"] == "completed" && commit == cfg.app_version;
        if complete
            && cfg.temporal_receipt_required
            && (info["HostConfig"]["RestartPolicy"]["Name"] != "no"
                || info["State"]["Status"] != "exited")
        {
            return phase("cleanup_pending", false);
        }
        if complete {
            phase("completed", true)
        } else {
            phase("verification_pending", false)
        }
    }
}

pub(crate) fn parse_receipt(bytes: &[u8]) -> Result<Value, ()> {
    let mut tar = tar::Archive::new(bytes);
    let mut entries = tar.entries().map_err(|_| ())?;
    let entry = entries.next().ok_or(())?.map_err(|_| ())?;
    if entry.path_bytes().as_ref() != b"completed.json"
        || !entry.header().entry_type().is_file()
        || entry.size() > 16 * 1024
    {
        return Err(());
    }
    let mut raw = Vec::new();
    entry
        .take(16 * 1024 + 1)
        .read_to_end(&mut raw)
        .map_err(|_| ())?;
    if entries.next().is_some() || raw.len() > 16 * 1024 {
        return Err(());
    }
    let parsed: Value = serde_json::from_slice(&raw).map_err(|_| ())?;
    (parsed["status"] == "completed")
        .then_some(parsed)
        .ok_or(())
}

pub(super) async fn restart(State(state): State<Arc<Broker>>) -> Response {
    // Detach from the HTTP consumer: a timed-out caller cannot drop the drain lock.
    let result = tokio::spawn(async move {
        let _lock = state.worker_ops.lock().await;
        let cfg = state.config.workers.as_ref().expect("workers configured");
        let project = state.worker_project().await.map_err(|_| (StatusCode::SERVICE_UNAVAILABLE,"refusing to restart: this broker cannot tell which stack it belongs to"))?;
        let items = state.match_workers(&project,false).await.map_err(|_| (StatusCode::BAD_GATEWAY,"docker unavailable"))?;
        let mut restarted = Vec::new();
        for (id,item) in items {
            state.do_worker_restart(&id,cfg.grace_seconds).await.map_err(|_| (StatusCode::BAD_GATEWAY,"worker restart failed; retry"))?;
            restarted.push(item["Names"].as_array().and_then(|names| names.first()).and_then(Value::as_str).map_or(id, |name| name.trim_start_matches('/').to_owned()));
        }
        if restarted.is_empty() {
            Ok(json!({"restarted":[],"detail":format!("no container matched ['{}', '{PROJECT}={project}']",cfg.service_label)}))
        } else { Ok(json!({"restarted":restarted,"grace_seconds":cfg.grace_seconds})) }
    }).await;
    match result {
        Ok(Ok(response)) => Json(response).into_response(),
        Ok(Err((status, message))) => (status, Json(json!({"detail":message}))).into_response(),
        Err(_) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"detail":"Worker restart did not complete"})),
        )
            .into_response(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn archive(entries: &[(&str, &[u8])]) -> Vec<u8> {
        let mut archive = tar::Builder::new(Vec::new());
        for (name, data) in entries {
            let mut header = tar::Header::new_ustar();
            header.set_entry_type(tar::EntryType::Regular);
            header.set_size(data.len() as u64);
            archive.append_data(&mut header, name, *data).unwrap();
        }
        archive.into_inner().unwrap()
    }

    #[test]
    fn accepts_one_small_completed_receipt_and_nothing_else() {
        let receipt =
            br#"{"status":"completed","commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#;
        assert_eq!(
            parse_receipt(&archive(&[("completed.json", receipt)])).unwrap()["status"],
            "completed"
        );
        assert!(parse_receipt(&archive(&[("different.json", receipt)])).is_err());
        assert!(
            parse_receipt(&archive(&[
                ("completed.json", receipt),
                ("extra.json", receipt)
            ]))
            .is_err()
        );
        assert!(parse_receipt(&archive(&[("completed.json", b"invalid-json")])).is_err());
        assert!(
            parse_receipt(&archive(&[("completed.json", b"{\"status\":\"running\"}")])).is_err()
        );
        assert!(
            parse_receipt(&archive(&[("completed.json", &vec![b'a'; 16 * 1024 + 1])])).is_err()
        );
    }
}
