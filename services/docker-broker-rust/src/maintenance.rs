//! Trusted deployment-only launcher. Never registered as an HTTP route.
//! The controller retains the reviewed Python migration/restore implementation.
use std::{collections::BTreeMap, io::Read, time::Duration};

use axum::{
    body::Bytes,
    http::{Method, StatusCode},
};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::{Broker, Config, new_state, runtime, valid_docker_id, workers::parse_receipt};

const API: &str = "/v1.45";
const LABEL: &str = "nautionette.temporal.project";
const MAX_PACKED: usize = 100_000;
const MAX_SOURCE: u64 = 8 * 1024 * 1024;
const RECEIPT_PATH: &str = "/opt/maintenance/completed.json";

#[derive(Debug)]
pub struct LauncherError(&'static str);
impl std::fmt::Display for LauncherError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for LauncherError {}
type Result<T> = std::result::Result<T, LauncherError>;
fn error(message: &'static str) -> LauncherError {
    LauncherError(message)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Payload {
    app: String,
    target: String,
    commit: String,
    shards: String,
    mode: String,
    files: BTreeMap<String, String>,
}
fn commit(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}
fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 63
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}
impl Payload {
    fn decode(packed: &str) -> Result<Self> {
        if packed.len() > MAX_PACKED {
            return Err(error("Maintenance payload exceeds its size limit"));
        }
        let raw = STANDARD
            .decode(packed)
            .map_err(|_| error("Invalid maintenance payload encoding"))?;
        let mut decoder = flate2::bufread::GzDecoder::new(std::io::Cursor::new(&raw));
        let mut source = Vec::new();
        decoder
            .by_ref()
            .take(MAX_SOURCE + 1)
            .read_to_end(&mut source)
            .map_err(|_| error("Invalid maintenance payload compression"))?;
        if source.len() as u64 > MAX_SOURCE || decoder.into_inner().position() != raw.len() as u64 {
            return Err(error(
                "Maintenance payload exceeds its size limit or contains trailing data",
            ));
        }
        let payload: Self =
            serde_json::from_slice(&source).map_err(|_| error("Invalid maintenance payload"))?;
        payload.validate()?;
        Ok(payload)
    }
    fn validate(&self) -> Result<()> {
        if !identifier(&self.app)
            || !commit(&self.commit)
            || !matches!(self.target.as_str(), "staging" | "production")
            || !matches!(self.mode.as_str(), "prepare" | "finish")
            || self.shards.parse::<u32>().ok().is_none_or(|n| n == 0)
            || !self.shards.bytes().all(|b| b.is_ascii_digit())
        {
            return Err(error(
                "Invalid maintenance identity, target, mode or shard count",
            ));
        }
        if let Some(contract) = self.files.get("context/launcher-contract.json") {
            let contract: Value = serde_json::from_str(contract)
                .map_err(|_| error("Invalid maintenance launcher protocol"))?;
            if contract.as_object().is_none_or(|v| v.len() != 1)
                || contract["protocol"].as_u64() != Some(1)
            {
                return Err(error("Unsupported maintenance launcher protocol"));
            }
        }
        if self.files.is_empty() || self.files.len() > 128 {
            return Err(error("Invalid maintenance build context"));
        }
        for path in self.files.keys() {
            if path.len() > 255
                || path.split('/').any(|part| {
                    part.is_empty()
                        || part == "."
                        || part == ".."
                        || !part
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
                })
            {
                return Err(error("Invalid maintenance build context path"));
            }
        }
        if !self.files.contains_key("context/Dockerfile.controller")
            || !self.files.contains_key("temporal_deploy_job.py")
        {
            return Err(error("Missing maintenance controller source"));
        }
        let plan: Value = serde_json::from_str(
            self.files
                .get("context/upgrade-plan.json")
                .ok_or(error("Missing maintenance plan"))?,
        )
        .map_err(|_| error("Invalid maintenance plan"))?;
        if plan["target"].as_str().is_none()
            || plan["releases"]
                .as_array()
                .and_then(|rows| rows.last())
                .is_none_or(|last| last["version"] != plan["target"])
        {
            return Err(error("Invalid maintenance plan"));
        }
        Ok(())
    }
    fn archive(&self) -> Result<Vec<u8>> {
        let mut archive = tar::Builder::new(Vec::new());
        for (path, source) in &self.files {
            let mut header = tar::Header::new_ustar();
            header.set_size(source.len() as u64);
            header.set_mode(0o644);
            header.set_cksum();
            archive
                .append_data(&mut header, path, source.as_bytes())
                .map_err(|_| error("Cannot package maintenance source"))?;
        }
        archive
            .into_inner()
            .map_err(|_| error("Cannot package maintenance source"))
    }
}

struct Launcher {
    broker: std::sync::Arc<Broker>,
    payload: Payload,
}
impl Launcher {
    async fn inspect(&self, name: &str) -> Result<Option<Value>> {
        let (status, value) = self
            .broker
            .docker_json(
                Method::GET,
                &format!("{API}/containers/{name}/json"),
                Value::Null,
                self.broker.config.deadline,
            )
            .await
            .map_err(|_| error("Cannot inspect maintenance containers"))?;
        match status {
            StatusCode::NOT_FOUND => Ok(None),
            StatusCode::OK if value["Id"].as_str().is_some_and(valid_docker_id) => Ok(Some(value)),
            _ => Err(error("Cannot inspect maintenance containers")),
        }
    }
    fn owned(&self, container: &Value) -> Result<String> {
        if container["Config"]["Labels"][LABEL] != self.payload.app {
            return Err(error(
                "Maintenance controller belongs to a different application",
            ));
        }
        container["Id"]
            .as_str()
            .filter(|id| valid_docker_id(id))
            .map(str::to_owned)
            .ok_or(error("Invalid maintenance container identity"))
    }
    async fn receipt(&self, id: &str) -> Result<Option<Value>> {
        let (status, tar) = self
            .broker
            .docker_request(
                Method::GET,
                &format!("{API}/containers/{id}/archive?path={RECEIPT_PATH}"),
                64 * 1024,
            )
            .await
            .map_err(|_| error("Cannot read maintenance completion receipt"))?;
        match status {
            StatusCode::NOT_FOUND => Ok(None),
            StatusCode::OK => parse_receipt(&tar)
                .map(Some)
                .map_err(|_| error("Invalid maintenance completion receipt")),
            _ => Err(error("Cannot read maintenance completion receipt")),
        }
    }
    async fn state(&self, id: &str) -> Result<Value> {
        let container = self
            .inspect(id)
            .await?
            .ok_or(error("Maintenance controller disappeared"))?;
        self.owned(&container)?;
        let value = if let Some(receipt) = self.receipt(id).await? {
            receipt
        } else {
            if container["State"]["Status"] != "running" {
                return Err(error(
                    "Stopped maintenance controller has no completion receipt",
                ));
            }
            let (code, output) = runtime::fixed_exec(
                &self.broker,
                id,
                None,
                &[
                    "python",
                    "/opt/maintenance/temporal_deploy_job.py",
                    "status",
                ],
            )
            .await
            .map_err(|_| error("Cannot inspect durable maintenance state"))?;
            let raw = runtime::raw_docker_payload(&output, 1)
                .map_err(|_| error("Invalid maintenance state framing"))?;
            if code != 0 || raw.len() > 16 * 1024 {
                return Err(error("Cannot inspect durable maintenance state"));
            }
            serde_json::from_str(&raw).map_err(|_| error("Invalid maintenance state"))?
        };
        let label = container["Config"]["Labels"]["nautionette.temporal.commit"]
            .as_str()
            .filter(|s| commit(s))
            .ok_or(error("Invalid maintenance revision"))?;
        if value["commit"] != label
            || value["status"]
                .as_str()
                .is_none_or(|s| s.is_empty() || s.len() > 128)
            || value.get("safe_to_retry").is_some_and(|v| !v.is_boolean())
        {
            return Err(error("Maintenance state does not match its controller"));
        }
        Ok(value)
    }
    async fn mutate(
        &self,
        id: &str,
        operation: &str,
        body: Value,
        statuses: &[StatusCode],
    ) -> Result<()> {
        let (status, _) = self.broker.docker_json(Method::POST, &format!("{API}/containers/{id}/{operation}"), body, self.broker.config.deadline).await
            .map_err(|_| error("Maintenance Docker operation failed; inspect existing controller before retrying"))?;
        if !statuses.contains(&status) {
            return Err(error(
                "Maintenance Docker operation failed; inspect existing controller before retrying",
            ));
        }
        Ok(())
    }
    async fn stop(&self, id: &str) -> Result<()> {
        self.mutate(
            id,
            "stop?t=5",
            Value::Null,
            &[StatusCode::NO_CONTENT, StatusCode::NOT_MODIFIED],
        )
        .await
    }
    async fn retire(&self, id: &str) -> Result<()> {
        if self
            .receipt(id)
            .await?
            .is_none_or(|v| v["commit"] != self.payload.commit)
        {
            return Err(error(
                "Cannot retire maintenance without a matching completion receipt",
            ));
        }
        self.mutate(
            id,
            "update",
            json!({"RestartPolicy":{"Name":"no"}}),
            &[StatusCode::OK],
        )
        .await?;
        if self
            .inspect(id)
            .await?
            .ok_or(error("Maintenance controller disappeared"))?["State"]["Status"]
            == "running"
        {
            self.stop(id).await?;
        }
        let info = self
            .inspect(id)
            .await?
            .ok_or(error("Maintenance controller disappeared"))?;
        if info["State"]["Status"] != "exited"
            || info["HostConfig"]["RestartPolicy"]["Name"] != "no"
            || self
                .receipt(id)
                .await?
                .is_none_or(|v| v["commit"] != self.payload.commit)
        {
            return Err(error("Maintenance controller cleanup is incomplete"));
        }
        Ok(())
    }
    async fn build_image(&self) -> Result<String> {
        let tag = format!(
            "nautionette/temporal-controller:{}-{}",
            self.payload.app, self.payload.commit
        );
        let flags = form_urlencoded::Serializer::new(String::new())
            .append_pair("dockerfile", "context/Dockerfile.controller")
            .append_pair("t", &tag)
            .append_pair("rm", "1")
            .append_pair(
                "labels",
                &json!({LABEL:self.payload.app,"nautionette.temporal.commit":self.payload.commit})
                    .to_string(),
            )
            .finish();
        let headers = crate::registry::build_headers(
            self.broker.config.docker_config.as_deref(),
            crate::request_headers(Some("application/x-tar"))
                .map_err(|_| error("Invalid build headers"))?,
        )
        .await
        .map_err(|_| error("Cannot prepare maintenance build authentication"))?;
        let (status, output) = self
            .broker
            .docker_payload_with_headers(
                Method::POST,
                &format!("{API}/build?{flags}"),
                Bytes::from(self.payload.archive()?),
                headers,
                32 * 1024 * 1024,
                Duration::from_secs(900),
            )
            .await
            .map_err(|_| error("Maintenance image build did not complete"))?;
        if status != StatusCode::OK || !crate::image_build::image_messages_succeeded(&output) {
            return Err(error("Maintenance image build failed"));
        }
        let image_path = form_urlencoded::byte_serialize(tag.as_bytes()).collect::<String>();
        let (status, image) = self
            .broker
            .docker_json(
                Method::GET,
                &format!("{API}/images/{image_path}/json"),
                Value::Null,
                self.broker.config.deadline,
            )
            .await
            .map_err(|_| error("Cannot inspect built maintenance image"))?;
        let image_id = image["Id"]
            .as_str()
            .filter(|id| id.strip_prefix("sha256:").is_some_and(valid_docker_id))
            .ok_or(error("Invalid built maintenance image identity"))?;
        if status != StatusCode::OK
            || image["Config"]["Labels"][LABEL] != self.payload.app
            || image["Config"]["Labels"]["nautionette.temporal.commit"] != self.payload.commit
        {
            return Err(error("Built maintenance image ownership mismatch"));
        }
        Ok(image_id.to_owned())
    }
    async fn create_and_start(&self, name: &str, image_id: &str) -> Result<String> {
        let mut nonce = [0u8; 4];
        std::fs::File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(&mut nonce))
            .map_err(|_| error("Cannot generate maintenance run identity"))?;
        let nonce = nonce.iter().map(|b| format!("{b:02x}")).collect::<String>();
        let run_id = format!(
            "{}-{nonce}-{}",
            &self.payload.commit[..12],
            self.payload.app
        );
        let body = json!({"Image":image_id,"Cmd":["python","/opt/maintenance/temporal_deploy_job.py","serve"],"Labels":{LABEL:self.payload.app,"nautionette.temporal.commit":self.payload.commit},
            "Env":[format!("MAINTENANCE_APP={}",self.payload.app),format!("MAINTENANCE_COMMIT={}",self.payload.commit),format!("MAINTENANCE_ENVIRONMENT={}",self.payload.target),format!("MAINTENANCE_RUN_ID={run_id}"),format!("MAINTENANCE_SHARDS={}",self.payload.shards)],
            "HostConfig":{"RestartPolicy":{"Name":"unless-stopped"},"SecurityOpt":["no-new-privileges:true"],"Binds":["/var/run/docker.sock:/var/run/docker.sock:rw","/var/lib/nautionette-temporal:/var/lib/nautionette-temporal:rw","/var/lib/nautionette-refresh:/var/lib/nautionette-refresh:rw"],"LogConfig":{"Type":"json-file","Config":{"max-size":"10m","max-file":"3"}}}});
        let (status, created) = self
            .broker
            .docker_json(
                Method::POST,
                &format!("{API}/containers/create?name={name}"),
                body,
                self.broker.config.deadline,
            )
            .await
            .map_err(|_| {
                error("Controller creation uncertain; inspect existing controller before retrying")
            })?;
        if status != StatusCode::CREATED {
            return Err(error(
                "Controller creation failed; inspect existing controller before retrying",
            ));
        }
        let id = created["Id"]
            .as_str()
            .filter(|id| valid_docker_id(id))
            .ok_or(error("Invalid created controller identity"))?
            .to_owned();
        self.mutate(&id, "start", Value::Null, &[StatusCode::NO_CONTENT])
            .await?;
        Ok(id)
    }
    async fn run(&self, self_name: &str) -> Result<()> {
        if self_name.is_empty()
            || self_name.len() > 255
            || !self_name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
        {
            return Err(error("Invalid broker container identity"));
        }
        let own = self
            .inspect(self_name)
            .await?
            .ok_or(error("Cannot identify the hook broker"))?;
        if own["Config"]["Labels"]["com.docker.compose.project"] != self.payload.app
            || own["Config"]["Labels"]["com.docker.compose.service"] != "docker-broker"
        {
            return Err(error(
                "Temporal hook must run in the target application's broker",
            ));
        }
        let name = format!("nautionette-temporal-{}", self.payload.app);
        let existing = self.inspect(&name).await?;
        if self.payload.mode == "finish" {
            let info = existing.ok_or(error("Missing candidate maintenance controller"))?;
            let id = self.owned(&info)?;
            if info["Config"]["Labels"]["nautionette.temporal.commit"] != self.payload.commit {
                return Err(error("Missing candidate maintenance controller"));
            }
            if info["State"]["Status"] == "running" {
                let (code, _) = runtime::fixed_exec(
                    &self.broker,
                    &id,
                    None,
                    &[
                        "python",
                        "/opt/maintenance/temporal_deploy_job.py",
                        "finish",
                    ],
                )
                .await
                .map_err(|_| error("Temporal post-deployment verification failed"))?;
                if code != 0 {
                    return Err(error("Temporal post-deployment verification failed"));
                }
            }
            if self.state(&id).await?["status"] != "completed" {
                return Err(error("Temporal post-deployment verification failed"));
            }
            self.retire(&id).await?;
            println!(
                "Temporal deployment verified; maintenance controller stopped and receipt retained."
            );
            return Ok(());
        }
        let id = if let Some(info) = existing {
            let id = self.owned(&info)?;
            let previous = self.state(&id).await?;
            if previous["commit"] != self.payload.commit || previous["safe_to_retry"] == true {
                if previous["status"] != "completed" && previous["safe_to_retry"] != true {
                    return Err(error(
                        "Unfinished Temporal migration; recover the existing controller first",
                    ));
                }
                // A failed build must not erase the live application's last
                // completed receipt. Prepare the replacement before retiring it.
                let image_id = self.build_image().await?;
                if info["State"]["Status"] == "running" {
                    self.stop(&id).await?;
                }
                let (status, _) = self
                    .broker
                    .docker_request(
                        Method::DELETE,
                        &format!("{API}/containers/{id}?force=false&v=false"),
                        1024,
                    )
                    .await
                    .map_err(|_| error("Cannot retire previous maintenance controller"))?;
                if status != StatusCode::NO_CONTENT {
                    return Err(error("Cannot retire previous maintenance controller"));
                }
                self.create_and_start(&name, &image_id).await?
            } else {
                id
            }
        } else {
            let image_id = self.build_image().await?;
            self.create_and_start(&name, &image_id).await?
        };
        let deadline = tokio::time::Instant::now() + Duration::from_secs(105 * 60);
        loop {
            let state = self.state(&id).await?;
            if state["commit"] != self.payload.commit {
                return Err(error(
                    "Maintenance controller has a different candidate revision",
                ));
            }
            match state["status"].as_str().unwrap_or_default() {
                "prepared" | "current" | "completed" => {
                    println!(
                        "Temporal pre-deployment checks passed; ready for container replacement."
                    );
                    return Ok(());
                }
                "failed" | "needs_recovery" => {
                    return Err(error(
                        "Temporal migration failed; inspect private controller state/backups",
                    ));
                }
                _ => (),
            }
            if tokio::time::Instant::now() >= deadline {
                return Err(error(
                    "Temporal migration still active; inspect existing controller, do not relaunch",
                ));
            }
            tokio::time::sleep(Duration::from_secs(5)).await;
        }
    }
}

/// Local operator execution only. The normal broker runtime is not started.
pub async fn launch(config: Config, self_name: &str, packed: &str) -> Result<()> {
    let payload = Payload::decode(packed)?;
    Launcher {
        broker: new_state(config),
        payload,
    }
    .run(self_name)
    .await
}
