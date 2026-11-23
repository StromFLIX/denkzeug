//! Docker-backed fixed agent verbs for the local runtime.
use std::{
    collections::{BTreeMap, BTreeSet},
    convert::Infallible,
    path::{Path, PathBuf},
    pin::Pin,
    sync::{Arc, Mutex},
    time::Duration,
};

use axum::{
    Extension, Json,
    body::{Body, Bytes},
    extract::State,
    http::{Method, StatusCode, header},
    response::{IntoResponse, Response},
};
use futures_util::{Stream, StreamExt, stream};
use serde_json::{Value, json};
use tokio::time::{Instant, sleep};

use crate::{
    Broker, CHAT_LABEL, TURN_LABEL,
    credentials::{self, Batch, Delivery},
    job::{self, AgentPlan, JobContext},
    runner::{self, RunOptions, RunnerEngine},
    session::{Claim, ClaimError, ClaimKind, SessionRoot},
    stream::{Exit, RunnerLogs, watchdog_budget},
    turns::{RecoveryEngine, TurnKey},
    valid_docker_id,
};

pub const DEFAULT_MEMORY: &str = "4g";
pub const DEFAULT_WORKSPACE_SIZE: &str = "1g";

pub struct RuntimeConfig {
    /// Deployment-owned agent set to exact image tag. Never derived from a request.
    pub agent_sets: BTreeMap<String, String>,
    pub agent_env: BTreeMap<String, String>,
    pub workflow_network: String,
    pub chat_network: String,
    pub workspace_size: String,
    pub memory: String,
    pub run_timeout: u64,
    pub session_bytes: u64,
    pub docker_self: String,
    pub sessions_root: PathBuf,
    pub projects_root: PathBuf,
}

const API: &str = "/v1.45";
const DEPLOYMENT: &str = "nautionette.deployment";
const WAIT: Duration = Duration::from_secs(30);
const PREPARE: Duration = Duration::from_secs(45);
const EXECS: Duration = Duration::from_secs(8);
const STEER: &str = "const net = require('node:net');const client = net.connect('/tmp/nautionette-chat.sock');client.setTimeout(5000, () => process.exit(1));client.on('error', () => process.exit(1));client.on('connect', () => client.write(process.argv[1] + '\\n'));client.on('data', chunk => process.stdout.write(chunk));";

pub(crate) struct LiveCredential {
    id: String,
    scope: BTreeSet<String>,
    pub(crate) lock: tokio::sync::Mutex<()>,
}

impl LiveCredential {
    pub(crate) fn new(id: String, scope: BTreeSet<String>) -> Self {
        Self {
            id,
            scope,
            lock: tokio::sync::Mutex::new(()),
        }
    }
}

struct Driver {
    broker: Arc<Broker>,
    plan: AgentPlan,
    turn: Option<TurnKey>,
    image: String,
    memory: u64,
    projects: Vec<String>,
    credentials: Option<Batch>,
    scope: BTreeSet<String>,
    package_ids: BTreeSet<String>,
    artcraft: Vec<(String, crate::artcraft::Release)>,
    extra_mounts: Mutex<Vec<Value>>,
    claims: Mutex<Vec<Claim>>,
    session_preparation: tokio::sync::Mutex<Option<SessionPreparation>>,
}

type SessionPreparation = tokio::task::JoinHandle<Result<(Claim, Value), &'static str>>;
type DockerLogStream = Pin<Box<dyn Stream<Item = Result<Bytes, ()>> + Send>>;

pub(super) async fn run(
    State(broker): State<Arc<Broker>>,
    trace: Option<Extension<crate::telemetry::TraceCarrier>>,
    Json(job): Json<Value>,
) -> Response {
    let Some(settings) = &broker.config.runtime else {
        return StatusCode::NOT_FOUND.into_response();
    };
    if !job.is_object() {
        return reject("agent job must be an object");
    }
    let set = match job.get("agent_set") {
        None | Some(Value::Null) => "default",
        Some(value) => match value.as_str() {
            Some("") => "default",
            Some(set) => set,
            None => return reject("Invalid agent set"),
        },
    };
    let Some(image) = settings.agent_sets.get(set).cloned() else {
        let set = set.chars().take(128).collect::<String>();
        return ndjson_error(format!("unknown agent set '{set}'"));
    };
    let set = set.to_owned();
    let chat = job["chat_id"].as_str().unwrap_or_default();
    let turn = job["turn_id"].as_str().unwrap_or_default();
    let turn = if chat.is_empty() {
        None
    } else {
        if !valid_chat_id(chat) || turn.is_empty() {
            return ndjson_error("Invalid chat or turn ID");
        }
        TurnKey::new(chat, turn)
    };
    let projects = match selected_projects(&job, turn.as_ref()) {
        Ok(value) => value,
        Err(error) => return ndjson_error(error),
    };
    let scope = match project_scope(&job, &projects) {
        Ok(value) => value,
        Err(error) => return ndjson_error(error),
    };
    let Some(memory) = size_bytes(&settings.memory) else {
        return ndjson_error("Invalid configured memory limit");
    };
    let Ok(watchdog) = watchdog_budget(&job, settings.run_timeout.max(1)) else {
        return ndjson_error("Invalid agent timeout");
    };
    let budget = PREPARE.max(broker.config.deadline);
    let carrier = trace.map(|Extension(carrier)| carrier).unwrap_or_default();
    let options = RunOptions {
        agent_set: set,
        image: image.clone(),
        memory: settings.memory.clone(),
        watchdog,
        operation_budget: budget,
        startup_trace: carrier.startup,
    };
    // Propagate the broker's own span across the detached NDJSON task.
    let trace = carrier.traceparent;
    let token = String::from_utf8_lossy(&broker.config.token).into_owned();
    let artcraft = job.get("artcraft").cloned().unwrap_or_else(|| json!([]));
    let member = job["member_mode"] == true;
    let context = JobContext {
        agent_env: settings.agent_env.clone(),
        token,
        worker_network: settings.workflow_network.clone(),
        chat_network: settings.chat_network.clone(),
        workflows_volume: broker.config.workflows_volume.clone(),
        workspace_size: settings.workspace_size.clone(),
        traced_parent: trace,
    };
    let mut plan = match job::plan_job(job, &context) {
        Ok(plan) => plan,
        Err(error) => return ndjson_error(error.to_string()),
    };
    let package_ids = match crate::packages::runtime_installations(&plan.package_runtime) {
        Ok(ids) => ids,
        Err(error) => return ndjson_error(error),
    };
    let artcraft = match crate::artcraft::selection(if member { json!([]) } else { artcraft }) {
        Ok(items) => items,
        Err(error) => return ndjson_error(error),
    };
    let credentials = if projects.is_empty() {
        None
    } else {
        match Batch::validate(std::mem::take(&mut plan.project_credentials), Some(&scope)) {
            Ok(batch) => Some(batch),
            Err(error) => return ndjson_error(error.to_string()),
        }
    };
    let driver = Arc::new(Driver {
        broker: broker.clone(),
        plan,
        turn: turn.clone(),
        image,
        memory,
        projects,
        credentials,
        scope,
        package_ids,
        artcraft,
        extra_mounts: Mutex::new(Vec::new()),
        claims: Mutex::new(Vec::new()),
        session_preparation: tokio::sync::Mutex::new(None),
    });
    let events = match turn {
        Some(turn) => runner::run_turn(broker.turns.clone(), turn, driver, options),
        None => runner::run_workflow(driver, options),
    };
    let body = Body::from_stream(stream::unfold(events, |mut events| async {
        events
            .recv()
            .await
            .map(|frame| (Ok::<_, Infallible>(frame), events))
    }));
    ([(header::CONTENT_TYPE, "application/x-ndjson")], body).into_response()
}

fn reject(message: &'static str) -> Response {
    error(StatusCode::UNPROCESSABLE_ENTITY, message)
}
fn error(status: StatusCode, message: &'static str) -> Response {
    (status, Json(json!({"detail":message}))).into_response()
}
fn ndjson_error(message: impl AsRef<str>) -> Response {
    (
        [(header::CONTENT_TYPE, "application/x-ndjson")],
        format!("{}\n", json!({"type":"error","message":message.as_ref()})),
    )
        .into_response()
}

fn valid_chat_id(chat: &str) -> bool {
    !chat.is_empty()
        && chat.len() <= 128
        && chat
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
}
fn lowercase_hex(value: &str, len: usize) -> bool {
    value.len() == len
        && value
            .bytes()
            .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}
fn selected_projects(job: &Value, turn: Option<&TurnKey>) -> Result<Vec<String>, &'static str> {
    let Some(value) = job.get("project_ids") else {
        return Ok(Vec::new());
    };
    let ids = value
        .as_array()
        .filter(|v| v.len() <= 20)
        .ok_or("Select at most 20 projects")?;
    if !ids.is_empty() && !turn.is_some_and(|key| lowercase_hex(key.chat_id(), 12)) {
        return Err("Project worktrees require a valid chat ID");
    }
    ids.iter()
        .map(|value| {
            value
                .as_str()
                .filter(|id| lowercase_hex(id, 32))
                .map(str::to_owned)
                .ok_or("Invalid project ID")
        })
        .collect::<Result<BTreeSet<_>, _>>()
        .map(|set| set.into_iter().collect())
}
fn project_scope(job: &Value, projects: &[String]) -> Result<BTreeSet<String>, &'static str> {
    if projects.is_empty() {
        return Ok(BTreeSet::new());
    }
    let remotes = job["project_remotes"]
        .as_object()
        .filter(|map| map.len() == projects.len())
        .ok_or("Project credential repository scope does not match this turn")?;
    projects
        .iter()
        .map(|id| {
            remotes
                .get(id)
                .and_then(Value::as_str)
                .map(str::to_owned)
                .ok_or("Project credential repository scope does not match this turn")
        })
        .collect()
}
fn size_bytes(raw: &str) -> Option<u64> {
    let mut raw = raw.to_ascii_lowercase();
    if raw.ends_with('b')
        && raw
            .as_bytes()
            .get(raw.len().checked_sub(2)?)
            .is_some_and(u8::is_ascii_alphabetic)
    {
        raw.pop();
    }
    let (n, scale) = match raw.as_bytes().last()? {
        b'b' => (raw.get(..raw.len() - 1)?, 1),
        b'k' => (raw.get(..raw.len() - 1)?, 1024),
        b'm' => (raw.get(..raw.len() - 1)?, 1024_u64.pow(2)),
        b'g' => (raw.get(..raw.len() - 1)?, 1024_u64.pow(3)),
        b'0'..=b'9' => (raw.as_str(), 1),
        _ => return None,
    };
    let bytes = n.trim().parse::<f64>().ok()? * scale as f64;
    // Docker accepts decimal amounts; keep positive, finite, signed-64-bit
    // resource limits, never accidentally turn malformed input into unlimited.
    (bytes.is_finite() && bytes >= 1.0 && bytes < (i64::MAX as f64)).then_some(bytes as u64)
}
fn mount(volume: &str, target: &str, subpath: Option<&str>, read_only: bool) -> Value {
    let mut mount = json!({"Type":"volume","Source":volume,"Target":target,"ReadOnly":read_only});
    if let Some(subpath) = subpath {
        mount["VolumeOptions"] = json!({"Subpath":subpath});
    }
    mount
}
fn is_dir_without_symlink(path: &Path) -> bool {
    std::fs::symlink_metadata(path).is_ok_and(|m| m.is_dir())
}

impl Driver {
    async fn own_mounts(&self) -> Result<Value, &'static str> {
        let cfg = self
            .broker
            .config
            .runtime
            .as_ref()
            .expect("runtime configured");
        let name = &cfg.docker_self;
        if name.is_empty()
            || !name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
        {
            return Err("Invalid broker Docker identity");
        }
        let result = self
            .broker
            .docker_json(
                Method::GET,
                &format!("{API}/containers/{name}/json"),
                Value::Null,
                self.broker.config.deadline,
            )
            .await
            .map_err(|_| "Cannot inspect the broker's mounts")?;
        if result.0 != StatusCode::OK {
            return Err("Cannot inspect the broker's mounts");
        }
        Ok(result.1)
    }
    async fn wait_claim(root: PathBuf, chat: &str, kind: ClaimKind) -> Result<Claim, &'static str> {
        let root = SessionRoot::new(root);
        let deadline = Instant::now() + WAIT;
        loop {
            match root.try_claim(chat, kind) {
                Ok(claim) => return Ok(claim),
                Err(ClaimError::Busy)
                    if kind == ClaimKind::Worktree && Instant::now() < deadline =>
                {
                    sleep(Duration::from_millis(50)).await
                }
                Err(ClaimError::Busy) if kind == ClaimKind::Worktree => {
                    return Err("This chat's project worktrees are in use; retry shortly");
                }
                Err(ClaimError::Busy) => {
                    return Err("This chat's native session is already in use");
                }
                Err(_) => return Err("Could not acquire the chat storage claim"),
            }
        }
    }
    async fn put_archive(&self, id: &str, dest: &str, data: Bytes) -> Result<(), ()> {
        upload(&self.broker, id, dest, data).await
    }
}

fn own_volume<'a>(info: &'a Value, path: &Path) -> Option<&'a str> {
    let path = path.to_str()?;
    let mut mounts = info["Mounts"]
        .as_array()?
        .iter()
        .filter(|mount| mount["Destination"] == path);
    let mount = mounts.next()?;
    if mounts.next().is_some() || mount["Type"] != "volume" {
        return None;
    }
    mount["Name"].as_str().filter(|name| !name.is_empty())
}

impl RunnerEngine for Driver {
    type Logs = DockerLogStream;

    async fn ready(&self, output: &tokio::sync::mpsc::Sender<String>) -> Result<(), String> {
        self.broker.ensure_image_for_turn(&self.image, output).await
    }

    async fn prepare(&self) -> Result<(), &'static str> {
        let mut extra = self.broker.package_mounts(&self.package_ids).await?;
        extra.extend(self.broker.artcraft_mounts(&self.artcraft).await?);
        let Some(turn) = &self.turn else {
            *self.extra_mounts.lock().unwrap() = extra;
            return Ok(());
        };
        let api = self
            .broker
            .docker_api()
            .await
            .map_err(|_| "Docker API version is unavailable")?;
        if api < (1, 45) {
            return Err(if self.projects.is_empty() {
                "Chat session isolation requires Docker Engine 26+ with API 1.45+"
            } else {
                "Project isolation requires Docker Engine 26+ with API 1.45+"
            });
        }
        let cfg = self
            .broker
            .config
            .runtime
            .as_ref()
            .expect("runtime configured");
        let chat = turn.chat_id();
        let mounts = self.own_mounts().await?;
        if !self.projects.is_empty() {
            let projects_volume = own_volume(&mounts, &cfg.projects_root)
                .ok_or("The broker requires a named Docker volume mounted at PROJECTS_DIR")?;
            let worktree =
                Self::wait_claim(cfg.sessions_root.clone(), chat, ClaimKind::Worktree).await?;
            self.claims.lock().unwrap().push(worktree);
            for id in &self.projects {
                self.ensure_project_unclaimed(id, chat, projects_volume)
                    .await?;
                let root = cfg.projects_root.join(id);
                let sessions = cfg.projects_root.join(".sessions");
                let project_sessions = sessions.join(id);
                if ![
                    root.clone(),
                    root.join(".git"),
                    sessions,
                    project_sessions.clone(),
                    project_sessions.join(chat),
                ]
                .iter()
                .all(|path| is_dir_without_symlink(path))
                {
                    return Err("Project worktree is unavailable");
                }
                extra.push(mount(
                    projects_volume,
                    &format!("/project-repositories/{id}"),
                    Some(&format!("{id}/.git")),
                    false,
                ));
                let source = format!(".sessions/{id}/{chat}");
                extra.push(mount(
                    projects_volume,
                    &format!("/projects/{source}"),
                    Some(&source),
                    false,
                ));
            }
        }
        let session = Self::wait_claim(cfg.sessions_root.clone(), chat, ClaimKind::Session).await?;
        if !self
            .broker
            .owned_agents(chat, "")
            .await
            .map_err(|_| "Could not inspect previous chat agents")?
            .is_empty()
        {
            return Err("A previous chat agent still exists; clean it up before resuming");
        }
        let volume = own_volume(&mounts, &cfg.sessions_root)
            .ok_or("The broker requires a named Docker volume at CHAT_SESSIONS_DIR")?
            .to_owned();
        let limit = cfg.session_bytes;
        let prepared = {
            let mut active = self.session_preparation.lock().await;
            *active = Some(tokio::task::spawn_blocking(move || {
                let sub = session.prepare_session(limit).map_err(|error| match error {
                ClaimError::Quota => "Chat session storage limit reached; export/delete the chat or raise CHAT_SESSION_MAX_BYTES",
                ClaimError::Symlink => "Symlinks are not allowed in durable chat state",
                _ => "Could not prepare durable chat state",
            })?;
                let payload = mount(&volume, sub.target, Some(sub.subpath), false);
                Ok::<_, &'static str>((session, payload))
            }));
            // Await by reference, retaining the handle if the admission budget
            // cancels this future. Release must join the filesystem worker
            // before acknowledging that all ownership claims are gone.
            let result = active
                .as_mut()
                .expect("session preparation registered")
                .await;
            active.take();
            result
        };
        let (session, mount) = prepared.map_err(|_| "Could not prepare durable chat state")??;
        extra.push(mount);
        self.claims.lock().unwrap().push(session);
        *self.extra_mounts.lock().unwrap() = extra;
        Ok(())
    }

    async fn create(&self) -> Result<String, ()> {
        let mut mounts = self.extra_mounts.lock().unwrap().clone();
        if let Some(volume) = &self.plan.workflows_volume {
            mounts.push(mount(volume, "/workflows", None, true));
        }
        let mut labels = BTreeMap::new();
        labels.insert(
            DEPLOYMENT.to_owned(),
            self.broker.config.workflows_volume.clone(),
        );
        let (chat, turn) = self
            .turn
            .as_ref()
            .map_or(("", ""), |key| (key.chat_id(), key.turn_id()));
        labels.insert(CHAT_LABEL.to_owned(), chat.to_owned());
        labels.insert(TURN_LABEL.to_owned(), turn.to_owned());
        for id in &self.projects {
            labels.insert(format!("nautionette.project.{id}"), chat.to_owned());
        }
        let env: Vec<_> = self
            .plan
            .environment
            .iter()
            .map(|(key, value)| format!("{key}={value}"))
            .collect();
        let body = json!({"Image":self.image,"Tty":false,"User":self.plan.user.unwrap_or(""),"Env":env,"Labels":labels,
            "HostConfig":{"Mounts":mounts,"NetworkMode":self.plan.network,"Tmpfs":self.plan.tmpfs,
                "Memory":self.memory,"PidsLimit":512,"CapDrop":["ALL"],"SecurityOpt":["no-new-privileges:true"]}});
        let (status, result) = self
            .broker
            .docker_json(
                Method::POST,
                &format!("{API}/containers/create"),
                body,
                self.broker.config.deadline,
            )
            .await?;
        if status != StatusCode::CREATED {
            return Err(());
        }
        result["Id"]
            .as_str()
            .filter(|id| valid_docker_id(id))
            .map(str::to_owned)
            .ok_or(())
    }

    async fn stage(&self, id: &str) -> Result<(), ()> {
        for archive in &self.plan.archives {
            self.put_archive(
                id,
                archive.destination(),
                Bytes::copy_from_slice(archive.contents()),
            )
            .await?;
        }
        if let Some(batch) = &self.credentials {
            let (path, data) = batch.archive(Delivery::Initial).map_err(|_| ())?;
            self.put_archive(id, path, data.into()).await?;
        }
        Ok(())
    }

    async fn start(&self, id: &str) -> Result<(), ()> {
        let (status, _) = self
            .broker
            .docker_request(Method::POST, &format!("{API}/containers/{id}/start"), 1024)
            .await?;
        if status != StatusCode::NO_CONTENT {
            return Err(());
        }
        if let Some(turn) = &self.turn
            && self.credentials.is_some()
        {
            self.broker.live_credentials.lock().await.insert(
                turn.clone(),
                Arc::new(LiveCredential::new(id.into(), self.scope.clone())),
            );
        }
        Ok(())
    }

    async fn logs(&self, id: &str) -> Result<Self::Logs, ()> {
        let response = self
            .broker
            .docker_open(
                Method::GET,
                &format!("{API}/containers/{id}/logs?stdout=true&stderr=false&follow=true"),
                Bytes::new(),
                None,
            )
            .await?;
        if response.status() != StatusCode::OK {
            return Err(());
        }
        Ok(Box::pin(
            response
                .into_body()
                .into_data_stream()
                .map(|frame| frame.map_err(|_| ())),
        ))
    }

    async fn exit(&self, id: &str) -> Result<Exit, ()> {
        let (status, result) = self
            .broker
            .docker_json(
                Method::POST,
                &format!("{API}/containers/{id}/wait?condition=not-running"),
                Value::Null,
                WAIT,
            )
            .await?;
        if status != StatusCode::OK {
            return Err(());
        }
        let code = result["StatusCode"].as_i64().ok_or(())?;
        if code == 0 {
            return Ok(Exit {
                code,
                oom_killed: false,
                stderr: String::new(),
            });
        }
        let (status, inspect) = self
            .broker
            .docker_json(
                Method::GET,
                &format!("{API}/containers/{id}/json"),
                Value::Null,
                self.broker.config.deadline,
            )
            .await?;
        if status != StatusCode::OK {
            return Err(());
        }
        let oom_killed = inspect["State"]["OOMKilled"] == true;
        let stderr = if !oom_killed && code != 137 {
            let raw = self
                .broker
                .docker_get(
                    &format!("{API}/containers/{id}/logs?stderr=true&stdout=false&tail=100"),
                    512 * 1024,
                )
                .await
                .unwrap_or_default();
            raw_docker_payload(&raw, 2).unwrap_or_default()
        } else {
            String::new()
        };
        Ok(Exit {
            code,
            oom_killed,
            stderr,
        })
    }

    async fn kill(&self, id: &str) -> Result<(), ()> {
        kill(&self.broker, id).await
    }
    async fn remove(&self, id: &str) -> Result<(), ()> {
        self.broker.force_remove(id).await.map(|_| ())
    }
    async fn release(&self) {
        if let Some(turn) = &self.turn {
            self.broker.live_credentials.lock().await.remove(turn);
        }
        if let Some(task) = self.session_preparation.lock().await.take() {
            let _ = task.await;
        }
        self.claims.lock().unwrap().clear();
    }
}

impl Driver {
    async fn ensure_project_unclaimed(
        &self,
        project: &str,
        chat: &str,
        volume: &str,
    ) -> Result<(), &'static str> {
        let key = format!("nautionette.project.{project}");
        let filter = json!({"label":[format!("{key}={chat}")]}).to_string();
        let query = form_urlencoded::Serializer::new(String::new())
            .append_pair("all", "true")
            .append_pair("filters", &filter)
            .finish();
        let raw = self
            .broker
            .docker_get(&format!("{API}/containers/json?{query}"), 8 * 1024 * 1024)
            .await
            .map_err(|_| "Could not inspect project worktree ownership")?;
        let containers: Vec<Value> = serde_json::from_slice(&raw)
            .map_err(|_| "Could not inspect project worktree ownership")?;
        for found in containers {
            if found["Labels"][&key] != chat
                || !found["Mounts"].as_array().is_some_and(|mounts| {
                    mounts
                        .iter()
                        .any(|m| m["Type"] == "volume" && m["Name"] == volume)
                })
            {
                continue;
            }
            if !matches!(found["State"].as_str(), Some("exited" | "dead")) {
                return Err("This chat's project worktree is still in use by another agent");
            }
            let id = found["Id"]
                .as_str()
                .filter(|id| valid_docker_id(id))
                .ok_or("Could not inspect project worktree ownership")?;
            let (status, _) = self
                .broker
                .docker_request(
                    Method::DELETE,
                    &format!("{API}/containers/{id}?v=false"),
                    1024,
                )
                .await
                .map_err(|_| "Could not remove a retired project agent")?;
            if !matches!(status, StatusCode::NO_CONTENT | StatusCode::NOT_FOUND) {
                return Err("Could not remove a retired project agent");
            }
        }
        Ok(())
    }
}

async fn upload(broker: &Broker, id: &str, dest: &str, data: Bytes) -> Result<(), ()> {
    let query = form_urlencoded::Serializer::new(String::new())
        .append_pair("path", dest)
        .finish();
    let (status, _) = broker
        .docker_payload(
            Method::PUT,
            &format!("{API}/containers/{id}/archive?{query}"),
            data,
            Some("application/x-tar"),
            1024,
            broker.config.deadline,
        )
        .await?;
    (status == StatusCode::OK).then_some(()).ok_or(())
}
async fn kill(broker: &Broker, id: &str) -> Result<(), ()> {
    let (status, _) = broker
        .docker_request(Method::POST, &format!("{API}/containers/{id}/kill"), 1024)
        .await?;
    matches!(
        status,
        StatusCode::NO_CONTENT | StatusCode::NOT_FOUND | StatusCode::CONFLICT
    )
    .then_some(())
    .ok_or(())
}

pub(crate) fn raw_docker_payload(mut frames: &[u8], wanted: u8) -> Result<String, ()> {
    let mut bytes = Vec::new();
    while !frames.is_empty() {
        let header = frames.get(..8).ok_or(())?;
        let n = u32::from_be_bytes(header[4..8].try_into().map_err(|_| ())?) as usize;
        let (payload, rest) = frames[8..].split_at_checked(n).ok_or(())?;
        if header[0] == wanted {
            bytes.extend_from_slice(payload);
        }
        frames = rest;
    }
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

pub(crate) async fn fixed_exec(
    broker: &Broker,
    id: &str,
    user: Option<&str>,
    command: &[&str],
) -> Result<(i64, Bytes), ()> {
    let budget = EXECS.max(broker.config.deadline);
    let mut body = json!({"AttachStdout":true,"AttachStderr":false,"Tty":false,"Cmd":command});
    if let Some(user) = user {
        body["User"] = json!(user);
    }
    let (status, res) = broker
        .docker_json(
            Method::POST,
            &format!("{API}/containers/{id}/exec"),
            body,
            budget,
        )
        .await?;
    if status != StatusCode::CREATED {
        return Err(());
    }
    let exec_id = res["Id"]
        .as_str()
        .filter(|id| valid_docker_id(id))
        .ok_or(())?;
    let (status, output) = broker
        .docker_payload(
            Method::POST,
            &format!("{API}/exec/{exec_id}/start"),
            Bytes::from_static(br#"{"Detach":false,"Tty":false}"#),
            Some("application/json"),
            64 * 1024,
            budget,
        )
        .await?;
    if status != StatusCode::OK {
        return Err(());
    }
    let (status, res) = broker
        .docker_json(
            Method::GET,
            &format!("{API}/exec/{exec_id}/json"),
            Value::Null,
            budget,
        )
        .await?;
    if status != StatusCode::OK || res["Running"] == true {
        return Err(());
    }
    Ok((res["ExitCode"].as_i64().ok_or(())?, output))
}

fn key(body: &Value) -> Option<TurnKey> {
    TurnKey::new(body["chat_id"].as_str()?, body["turn_id"].as_str()?)
}

pub(super) async fn control(
    State(broker): State<Arc<Broker>>,
    Json(body): Json<Value>,
) -> Response {
    let command = &body["command"];
    let mode = command["type"].as_str();
    if command["id"].as_str().is_none_or(str::is_empty)
        || !matches!(mode, Some("stop" | "steer"))
        || (mode == Some("steer")
            && command["text"]
                .as_str()
                .is_none_or(|text| text.trim().is_empty()))
    {
        return reject("Valid chat, turn and command are required");
    }
    let Some(turn) = key(&body) else {
        return reject("Valid chat, turn and command are required");
    };
    if mode == Some("stop") && broker.turns.request_stop(&turn) {
        return Json(json!({"ok":true})).into_response();
    }
    let Ok(ids) = broker.exact_turn_containers(&turn).await else {
        return error(
            StatusCode::SERVICE_UNAVAILABLE,
            "Agent control unavailable; retry",
        );
    };
    let mut delivered = false;
    for id in ids {
        match is_running(&broker, &id).await {
            Ok(false) => continue,
            Ok(true) => (),
            Err(()) => {
                return error(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "Agent control unavailable; retry",
                );
            }
        }
        let result = if mode == Some("stop") {
            kill(&broker, &id).await.is_ok()
        } else {
            let data = command.to_string();
            let Ok((0, output)) =
                fixed_exec(&broker, &id, None, &["node", "-e", STEER, &data]).await
            else {
                continue;
            };
            let mut parsed = None;
            let mut logs = RunnerLogs::new(64 * 1024);
            let mut line = |line: String| {
                parsed = serde_json::from_str::<Value>(&line).ok();
            };
            logs.push(&output, &mut line).is_ok()
                && logs.finish(line).is_ok()
                && parsed.is_some_and(|value| value["ok"] == true)
        };
        delivered |= result;
    }
    Json(json!({"ok":delivered})).into_response()
}

async fn is_running(broker: &Broker, id: &str) -> Result<bool, ()> {
    let (status, info) = broker
        .docker_json(
            Method::GET,
            &format!("{API}/containers/{id}/json"),
            Value::Null,
            broker.config.deadline,
        )
        .await?;
    match status {
        StatusCode::NOT_FOUND => Ok(false),
        StatusCode::OK => info["State"]["Running"].as_bool().ok_or(()),
        _ => Err(()),
    }
}

pub(super) async fn refresh_credentials(
    State(broker): State<Arc<Broker>>,
    Json(body): Json<Value>,
) -> Response {
    let Some(turn) = key(&body) else {
        return reject("chat_id and turn_id are required");
    };
    let payload = body["credentials"].clone();
    if let Err(error) = Batch::validate(payload.clone(), None) {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail": error.to_string()})),
        )
            .into_response();
    }
    let live = broker.live_credentials.lock().await.get(&turn).cloned();
    let Some(live) = live else {
        return Json(json!({"ok":false})).into_response();
    };
    let _renewal = live.lock.lock().await;
    if !broker.turns.running(&turn)
        || !broker
            .live_credentials
            .lock()
            .await
            .get(&turn)
            .is_some_and(|current| Arc::ptr_eq(current, &live))
    {
        return Json(json!({"ok":false})).into_response();
    }
    let batch = match Batch::validate(payload, Some(&live.scope)) {
        Ok(batch) => batch,
        Err(error) => {
            return (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(json!({"detail": error.to_string()})),
            )
                .into_response();
        }
    };
    let failure = || {
        error(
            StatusCode::SERVICE_UNAVAILABLE,
            "Git credential delivery failed; retry",
        )
    };
    let Ok(ids) = broker.exact_turn_containers(&turn).await else {
        return failure();
    };
    if ids.as_slice() != [live.id.as_str()] {
        return Json(json!({"ok":false})).into_response();
    }
    match is_running(&broker, &live.id).await {
        Ok(true) => (),
        Ok(false) => return Json(json!({"ok":false})).into_response(),
        Err(()) => return failure(),
    }
    let Ok((path, data)) = batch.archive(Delivery::Renewal) else {
        return failure();
    };
    if upload(&broker, &live.id, path, data.into()).await.is_err() {
        return if matches!(is_running(&broker, &live.id).await, Ok(false)) {
            Json(json!({"ok":false})).into_response()
        } else {
            failure()
        };
    }
    let (user, command) = credentials::activation();
    match fixed_exec(&broker, &live.id, Some(user), &command).await {
        Ok((0, _)) => Json(json!({"ok":true})).into_response(),
        Err(()) if matches!(is_running(&broker, &live.id).await, Ok(false)) => {
            Json(json!({"ok":false})).into_response()
        }
        _ => failure(),
    }
}

#[cfg(test)]
mod ownership_tests {
    use super::*;

    #[test]
    fn configured_memory_sizes_match_docker_sdk_units_and_preserve_explicit_overrides() {
        assert_eq!(DEFAULT_MEMORY, "4g");
        assert_eq!(DEFAULT_WORKSPACE_SIZE, "1g");
        for (text, bytes) in [
            ("4g", 4_u64 << 30),
            ("6gb", 6 << 30),
            ("768m", 768 << 20),
            ("1.5GB", 1536 << 20),
            ("1024", 1024),
            ("1b", 1),
        ] {
            assert_eq!(size_bytes(text), Some(bytes), "{text}");
        }
        for text in ["", "-1g", "not-memory", "NaNg", "infg"] {
            assert_eq!(size_bytes(text), None, "{text}");
        }
    }

    #[tokio::test]
    async fn cancellation_waits_for_a_blocking_session_preparation_before_releasing_ownership() {
        let root = tempfile::tempdir().unwrap();
        let sessions = root.path().join("sessions");
        let claim = SessionRoot::new(sessions.clone())
            .try_claim("chat", ClaimKind::Session)
            .unwrap();
        let (ready, wait) = std::sync::mpsc::channel();
        let task = tokio::task::spawn_blocking(move || {
            wait.recv().unwrap();
            Ok((claim, Value::Null))
        });
        let broker = crate::new_state(
            crate::Config::new(
                root.path().join("none.sock"),
                "fixture",
                "volume",
                Duration::from_secs(1),
            )
            .unwrap(),
        );
        let context = JobContext {
            agent_env: BTreeMap::new(),
            token: String::new(),
            worker_network: "none".into(),
            chat_network: "none".into(),
            workflows_volume: "fixture".into(),
            workspace_size: "1g".into(),
            traced_parent: None,
        };
        let driver = Driver {
            broker,
            plan: job::plan_job(json!({}), &context).unwrap(),
            turn: None,
            image: "fixture".into(),
            memory: 1,
            projects: Vec::new(),
            credentials: None,
            scope: BTreeSet::new(),
            package_ids: BTreeSet::new(),
            artcraft: Vec::new(),
            extra_mounts: Mutex::new(Vec::new()),
            claims: Mutex::new(Vec::new()),
            session_preparation: tokio::sync::Mutex::new(Some(task)),
        };
        // Simulate cancellation while prepare awaited the blocking filesystem scan.
        assert!(
            tokio::time::timeout(Duration::from_millis(10), async {
                let mut active = driver.session_preparation.lock().await;
                let _ = active.as_mut().unwrap().await;
            })
            .await
            .is_err()
        );
        let releasing = driver.release();
        tokio::pin!(releasing);
        assert!(
            tokio::time::timeout(Duration::from_millis(10), &mut releasing)
                .await
                .is_err()
        );
        assert!(matches!(
            SessionRoot::new(sessions.clone()).try_claim("chat", ClaimKind::Session),
            Err(ClaimError::Busy)
        ));
        ready.send(()).unwrap();
        releasing.await;
        assert!(
            SessionRoot::new(sessions)
                .try_claim("chat", ClaimKind::Session)
                .is_ok()
        );
    }
}
