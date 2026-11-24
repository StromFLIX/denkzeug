//! Local broker migration spike. Mutations are disabled by default.

mod artcraft;
pub mod credentials;
mod engine;
#[cfg(test)]
mod parity_tests;
pub use engine::{DockerEndpoint, EndpointError};
pub mod image_build;
mod images;
pub mod job;
pub mod maintenance;
pub mod packages;
mod registry;
mod retired;
pub mod runner;
pub mod runtime;
pub mod session;
pub mod stream;
pub mod telemetry;
pub mod turns;
pub mod workers;
pub use images::{Fingerprints, effective_fingerprints, fingerprints};

use std::{
    collections::{BTreeSet, HashMap},
    path::PathBuf,
    sync::Arc,
    time::Duration,
};

use axum::{
    Json, Router,
    body::Bytes,
    extract::{Query, Request, State},
    http::{HeaderMap, Method, StatusCode, header::CONTENT_TYPE},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use engine::{Engine, Payload};
use http_body_util::{BodyExt, Limited};
use serde::Deserialize;
use serde_json::{Value, json};
use subtle::ConstantTimeEq;
use turns::{RecoveryEngine, RemoveResult, TurnKey, TurnRegistry};

pub struct Config {
    engine: Engine,
    token: Vec<u8>,
    workflows_volume: String,
    deadline: Duration,
    experimental_cleanup: bool,
    runtime: Option<runtime::RuntimeConfig>,
    workers: Option<workers::WorkerConfig>,
    reconcile_interval: Option<Duration>,
    image_build: Option<image_build::ImageConfig>,
    negotiate_api: bool,
    docker_config: Option<PathBuf>,
    telemetry: Option<Arc<telemetry::Otlp>>,
}

impl Config {
    pub fn new(
        socket: PathBuf,
        token: &str,
        workflows_volume: &str,
        deadline: Duration,
    ) -> Result<Self, &'static str> {
        Self::for_endpoint(
            DockerEndpoint::Unix(socket),
            token,
            workflows_volume,
            deadline,
        )
    }

    pub fn for_endpoint(
        endpoint: DockerEndpoint,
        token: &str,
        workflows_volume: &str,
        deadline: Duration,
    ) -> Result<Self, &'static str> {
        let token = token.trim();
        if token.is_empty() || axum::http::HeaderValue::from_str(token).is_err() {
            return Err("a nonempty, valid INTERNAL_TOKEN header value is required");
        }
        if workflows_volume.trim().is_empty() || deadline.is_zero() {
            return Err("a workflows volume and positive Docker deadline are required");
        }
        let engine = Engine::new(endpoint, deadline).map_err(|error| error.0)?;
        Ok(Self {
            engine,
            token: token.as_bytes().to_owned(),
            workflows_volume: workflows_volume.to_owned(),
            deadline,
            experimental_cleanup: false,
            runtime: None,
            workers: None,
            reconcile_interval: None,
            image_build: None,
            negotiate_api: false,
            docker_config: None,
            telemetry: None,
        })
    }

    /// Auto-detect the daemon wire version. Session/project mounts still require 1.45.
    pub fn with_api_negotiation(mut self) -> Self {
        self.negotiate_api = true;
        self
    }

    /// Optional explicit daemon-side registry-auth configuration file.
    pub fn with_registry_config(mut self, path: PathBuf) -> Self {
        self.docker_config = Some(path);
        self
    }

    pub fn with_telemetry(mut self, telemetry: Arc<telemetry::Otlp>) -> Self {
        self.telemetry = Some(telemetry);
        self
    }

    pub fn with_local_runtime(mut self, runtime: runtime::RuntimeConfig) -> Self {
        self.runtime = Some(runtime);
        self.experimental_cleanup = true;
        self
    }

    pub fn with_local_workers(
        mut self,
        workers: workers::WorkerConfig,
        reconcile_interval: Option<Duration>,
    ) -> Self {
        self.workers = Some(workers);
        self.reconcile_interval = reconcile_interval.filter(|value| !value.is_zero());
        self
    }

    pub fn with_local_image_build(mut self, images: image_build::ImageConfig) -> Self {
        if let Some(runtime) = &mut self.runtime {
            runtime.agent_sets = images.agent_tags();
        }
        self.image_build = Some(images);
        self
    }

    /// Local tests only. Not a permission to attach to a live deployment.
    pub fn with_experimental_cleanup(mut self) -> Self {
        self.experimental_cleanup = true;
        self
    }
}

struct Broker {
    config: Config,
    turns: Arc<TurnRegistry>,
    live_credentials: tokio::sync::Mutex<HashMap<TurnKey, Arc<runtime::LiveCredential>>>,
    worker_ops: tokio::sync::Mutex<()>,
    worker_error: std::sync::Mutex<Option<String>>,
    package_slots: tokio::sync::Semaphore,
    installing_packages: std::sync::Mutex<BTreeSet<String>>,
    artcraft_slots: tokio::sync::Semaphore,
    installing_artcraft: std::sync::Mutex<BTreeSet<String>>,
    verified_artcraft: std::sync::Mutex<BTreeSet<String>>,
    build_state: std::sync::Mutex<image_build::BuildState>,
    retired_complete: std::sync::atomic::AtomicBool,
    daemon_api: tokio::sync::OnceCell<(u16, u16)>,
    #[cfg(test)]
    staged_artcraft_fixture: std::sync::Mutex<Option<Vec<u8>>>,
}

impl Broker {
    async fn docker_open(
        &self,
        method: Method,
        path: &str,
        body: Bytes,
        content_type: Option<&str>,
    ) -> Result<axum::http::Response<axum::body::Body>, ()> {
        self.docker_open_with_headers(method, path, body, request_headers(content_type)?)
            .await
    }

    async fn docker_open_with_headers(
        &self,
        method: Method,
        path: &str,
        body: Bytes,
        headers: HeaderMap,
    ) -> Result<axum::http::Response<axum::body::Body>, ()> {
        let path = self.docker_wire_path(path).await?;
        self.docker_send(method, &path, body, headers).await
    }

    async fn docker_send(
        &self,
        method: Method,
        path: &str,
        body: Bytes,
        headers: HeaderMap,
    ) -> Result<axum::http::Response<axum::body::Body>, ()> {
        self.config
            .engine
            .send(method, path, headers, Payload::Bytes(body))
            .await
    }

    async fn docker_api(&self) -> Result<(u16, u16), ()> {
        if !self.config.negotiate_api {
            return Ok((1, 45));
        }
        self.daemon_api
            .get_or_try_init(|| async {
                tokio::time::timeout(self.config.deadline, async {
                    let response = self
                        .docker_send(Method::GET, "/version", Bytes::new(), HeaderMap::new())
                        .await?;
                    if response.status() != StatusCode::OK {
                        return Err(());
                    }
                    let bytes = Limited::new(response.into_body(), 64 * 1024)
                        .collect()
                        .await
                        .map_err(|_| ())?
                        .to_bytes();
                    let info: Value = serde_json::from_slice(&bytes).map_err(|_| ())?;
                    let (major, minor) = info["ApiVersion"]
                        .as_str()
                        .ok_or(())?
                        .split_once('.')
                        .ok_or(())?;
                    let version = (
                        major.parse::<u16>().map_err(|_| ())?,
                        minor.parse::<u16>().map_err(|_| ())?,
                    );
                    (version >= (1, 24)).then_some(version).ok_or(())
                })
                .await
                .map_err(|_| ())?
            })
            .await
            .copied()
    }

    async fn docker_wire_path<'a>(&self, path: &'a str) -> Result<std::borrow::Cow<'a, str>, ()> {
        if !self.config.negotiate_api {
            return Ok(path.into());
        }
        let suffix = if path == "/_ping" {
            path
        } else if let Some(path) = path.strip_prefix("/v1.45/") {
            return self
                .docker_api()
                .await
                .map(|(major, minor)| format!("/v{major}.{minor}/{path}").into());
        } else {
            return Ok(path.into());
        };
        self.docker_api()
            .await
            .map(|(major, minor)| format!("/v{major}.{minor}{suffix}").into())
    }

    async fn docker_file(
        &self,
        method: Method,
        path: &str,
        file: std::fs::File,
        budget: Duration,
    ) -> Result<StatusCode, ()> {
        tokio::time::timeout(budget, async {
            let size = file.metadata().map_err(|_| ())?.len();
            let path = self.docker_wire_path(path).await?;
            let response = self
                .config
                .engine
                .send(
                    method,
                    &path,
                    request_headers(Some("application/x-tar"))?,
                    Payload::File(file, size),
                )
                .await?;
            Ok(response.status())
        })
        .await
        .map_err(|_| ())?
    }

    async fn docker_payload(
        &self,
        method: Method,
        path: &str,
        body: Bytes,
        content_type: Option<&str>,
        max_bytes: usize,
        budget: Duration,
    ) -> Result<(StatusCode, Bytes), ()> {
        self.docker_payload_with_headers(
            method,
            path,
            body,
            request_headers(content_type)?,
            max_bytes,
            budget,
        )
        .await
    }

    async fn docker_payload_with_headers(
        &self,
        method: Method,
        path: &str,
        body: Bytes,
        headers: HeaderMap,
        max_bytes: usize,
        budget: Duration,
    ) -> Result<(StatusCode, Bytes), ()> {
        tokio::time::timeout(budget, async {
            let response = self
                .docker_open_with_headers(method, path, body, headers)
                .await?;
            let status = response.status();
            // Never retain error response bodies from the privileged daemon.
            if !status.is_success() {
                return Ok((status, Bytes::new()));
            }
            let body = Limited::new(response.into_body(), max_bytes)
                .collect()
                .await
                .map_err(|_| ())?;
            Ok((status, body.to_bytes()))
        })
        .await
        .map_err(|_| ())?
    }

    async fn docker_request(
        &self,
        method: Method,
        path: &str,
        max_bytes: usize,
    ) -> Result<(StatusCode, Bytes), ()> {
        self.docker_payload(
            method,
            path,
            Bytes::new(),
            None,
            max_bytes,
            self.config.deadline,
        )
        .await
    }

    async fn docker_json(
        &self,
        method: Method,
        path: &str,
        body: Value,
        budget: Duration,
    ) -> Result<(StatusCode, Value), ()> {
        let data = if body.is_null() {
            Bytes::new()
        } else {
            Bytes::from(serde_json::to_vec(&body).map_err(|_| ())?)
        };
        let (status, result) = self
            .docker_payload(
                method,
                path,
                data,
                Some("application/json"),
                8 * 1024 * 1024,
                budget,
            )
            .await?;
        if result.is_empty() {
            return Ok((status, Value::Null));
        }
        Ok((status, serde_json::from_slice(&result).map_err(|_| ())?))
    }

    async fn docker_get(&self, path: &str, max_bytes: usize) -> Result<Bytes, ()> {
        let (status, body) = self.docker_request(Method::GET, path, max_bytes).await?;
        if !status.is_success() {
            return Err(());
        }
        Ok(body)
    }
}

fn request_headers(content_type: Option<&str>) -> Result<HeaderMap, ()> {
    let mut headers = HeaderMap::new();
    if let Some(content_type) = content_type {
        headers.insert(CONTENT_TYPE, content_type.parse().map_err(|_| ())?);
    }
    Ok(headers)
}

fn new_state(config: Config) -> Arc<Broker> {
    let run = config
        .runtime
        .as_ref()
        .map_or(3600, |runtime| runtime.run_timeout.max(1));
    let build = config
        .image_build
        .as_ref()
        .map_or(900, |images| images.wait_seconds.max(1));
    let fence = Duration::from_secs(run.saturating_add(build));
    Arc::new(Broker {
        config,
        turns: Arc::new(TurnRegistry::new(fence)),
        live_credentials: tokio::sync::Mutex::new(HashMap::new()),
        worker_ops: tokio::sync::Mutex::new(()),
        worker_error: std::sync::Mutex::new(None),
        package_slots: tokio::sync::Semaphore::new(2),
        installing_packages: std::sync::Mutex::new(BTreeSet::new()),
        artcraft_slots: tokio::sync::Semaphore::new(2),
        installing_artcraft: std::sync::Mutex::new(BTreeSet::new()),
        verified_artcraft: std::sync::Mutex::new(BTreeSet::new()),
        build_state: std::sync::Mutex::new(image_build::BuildState::default()),
        retired_complete: std::sync::atomic::AtomicBool::new(false),
        daemon_api: tokio::sync::OnceCell::new(),
        #[cfg(test)]
        staged_artcraft_fixture: std::sync::Mutex::new(None),
    })
}

pub fn broker(config: Config) -> Router {
    router_for_state(new_state(config))
}

/// The executable uses explicit lifecycle ownership, like the Python app's
/// lifespan: drain HTTP, stop/join the monitor, then flush telemetry.
pub struct BrokerShutdown {
    monitor: Option<(
        tokio::sync::watch::Sender<bool>,
        tokio::task::JoinHandle<()>,
    )>,
    telemetry: Option<Arc<telemetry::Otlp>>,
}

impl BrokerShutdown {
    pub async fn finish(self) {
        if let Some((stopping, task)) = self.monitor {
            stopping.send_replace(true);
            let _ = task.await;
        }
        if let Some(telemetry) = self.telemetry {
            let _ = tokio::task::spawn_blocking(move || telemetry.shutdown()).await;
        }
    }
}

pub fn managed_broker(mut config: Config) -> (Router, BrokerShutdown) {
    let interval = config.reconcile_interval.take();
    let telemetry = config.telemetry.clone();
    let state = new_state(config);
    let monitor = interval.map(|interval| {
        let weak = Arc::downgrade(&state);
        let (stopping, mut stopped) = tokio::sync::watch::channel(false);
        let task = tokio::spawn(async move {
            loop {
                if *stopped.borrow() {
                    return;
                }
                let Some(broker) = weak.upgrade() else {
                    return;
                };
                // Do not cancel an in-flight privileged mutation halfway
                // through a monitor pass; wait for its bounded Engine call.
                broker.reconcile_pass().await;
                drop(broker);
                tokio::select! {
                    _=tokio::time::sleep(interval)=>(),
                    result=stopped.changed()=>{if result.is_err(){return;}},
                }
            }
        });
        (stopping, task)
    });
    (
        router_for_state(state),
        BrokerShutdown { monitor, telemetry },
    )
}

impl Broker {
    async fn reconcile_pass(self: &Arc<Self>) {
        self.reconcile_retired().await;
        if self.image_build_configured()
            && !matches!(
                self.images_snapshot().await["status"].as_str(),
                Some("ready" | "building")
            )
        {
            self.start_build(false);
        }
        self.reconcile_workers().await;
        if self.config.runtime.is_some() {
            self.reap_packages().await;
            self.reap_artcraft().await;
        }
    }
}

fn router_for_state(state: Arc<Broker>) -> Router {
    let experimental_cleanup = state.config.experimental_cleanup;
    let local_runtime = state.config.runtime.is_some();
    let local_workers = state.config.workers.is_some();
    if let Some(interval) = state.config.reconcile_interval {
        let weak = Arc::downgrade(&state);
        tokio::spawn(async move {
            loop {
                let Some(broker) = weak.upgrade() else {
                    return;
                };
                broker.reconcile_pass().await;
                drop(broker);
                tokio::time::sleep(interval).await;
            }
        });
    }
    let mut routes = Router::new()
        .route("/healthz", get(health))
        .route("/agent/chats", get(chat_inventory));
    if experimental_cleanup {
        routes = routes.route("/agent/cleanup", post(cleanup_turn));
    }
    if local_workers {
        routes = routes.route("/worker/restart", post(workers::restart));
    }
    if local_runtime {
        routes = routes
            .route(
                "/agent/run",
                post(runtime::run).layer(axum::extract::DefaultBodyLimit::disable()),
            )
            .route("/agent/control", post(runtime::control))
            .route("/packages/install", post(packages::install))
            .route("/artcraft/install", post(artcraft::install))
            .route("/agent-sets", get(image_build::agent_sets))
            .route("/images/rebuild", post(image_build::rebuild))
            .route(
                "/agent/project-credentials",
                post(runtime::refresh_credentials),
            );
    }
    routes
        .layer(middleware::from_fn_with_state(state.clone(), authenticate))
        .layer(middleware::from_fn_with_state(state.clone(), instrument))
        .with_state(state)
}

async fn instrument(State(state): State<Arc<Broker>>, request: Request, next: Next) -> Response {
    if let Some(otel) = &state.config.telemetry {
        telemetry::instrument(otel, request, next).await
    } else {
        next.run(request).await
    }
}

async fn authenticate(State(state): State<Arc<Broker>>, request: Request, next: Next) -> Response {
    if request.uri().path() != "/healthz" {
        let mut tokens = request.headers().get_all("x-internal-token").iter();
        let valid = tokens
            .next()
            .is_some_and(|value| bool::from(value.as_bytes().ct_eq(&state.config.token)))
            && tokens.next().is_none();
        if !valid {
            return (
                StatusCode::UNAUTHORIZED,
                Json(json!({"detail":"unauthorized"})),
            )
                .into_response();
        }
    }
    let response = next.run(request).await;
    if matches!(
        response.status(),
        StatusCode::BAD_REQUEST | StatusCode::UNSUPPORTED_MEDIA_TYPE
    ) {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail":"Invalid request body"})),
        )
            .into_response();
    }
    response
}

async fn health(State(state): State<Arc<Broker>>) -> Json<Value> {
    let docker = tokio::time::timeout(Duration::from_secs(3), state.docker_get("/_ping", 1024))
        .await
        .is_ok_and(|result| result.is_ok_and(|body| body == "OK"));
    let error = if docker {
        "local spike: readiness checks not implemented"
    } else {
        "Docker unavailable"
    };
    if docker && (state.config.workers.is_some() || state.image_build_configured()) {
        let (workers, maintenance, images) = tokio::join!(
            state.workers_snapshot(),
            state.temporal_snapshot(),
            state.images_snapshot()
        );
        let ready = [&workers, &maintenance, &images]
            .iter()
            .all(|value| value["status"] == "ready");
        return Json(
            json!({"status":if ready {"ok"} else {"degraded"},"implementation":"rust","docker":true,"error":images["error"],"workers":workers,"temporal_migration":maintenance,
            "images":images["images"],"image_status":images["status"],"missing_images":images["missing"]}),
        );
    }
    Json(json!({"status":"degraded", "docker":docker, "error":error}))
}

#[derive(Deserialize)]
struct InventoryQuery {
    #[serde(default)]
    chat_id: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct ContainerSummary {
    id: Option<String>,
    labels: Option<HashMap<String, String>>,
    mounts: Option<Vec<Mount>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct Mount {
    r#type: String,
    name: Option<String>,
    destination: String,
}

const CHAT_LABEL: &str = "nautionette.chat";
const TURN_LABEL: &str = "nautionette.turn";

fn label_filter(key: &str, value: &str) -> String {
    if value.is_empty() {
        key.to_owned()
    } else {
        format!("{key}={value}")
    }
}

impl Broker {
    async fn owned_agents(
        &self,
        chat: &str,
        turn: &str,
    ) -> Result<Vec<(TurnKey, Option<String>)>, ()> {
        let filters =
            json!({"label": [label_filter(CHAT_LABEL, chat), label_filter(TURN_LABEL, turn)]})
                .to_string();
        let query = form_urlencoded::Serializer::new(String::new())
            .append_pair("all", "true")
            .append_pair("filters", &filters)
            .finish();
        // Use the Docker 26 API floor already required for session subpaths.
        let raw = self
            .docker_get(&format!("/v1.45/containers/json?{query}"), 8 * 1024 * 1024)
            .await?;
        let summaries: Vec<ContainerSummary> = serde_json::from_slice(&raw).map_err(|_| ())?;
        Ok(summaries
            .into_iter()
            .filter_map(|container| {
                let labels = container.labels.as_ref()?;
                let chat_id = labels.get(CHAT_LABEL)?;
                let turn_id = labels.get(TURN_LABEL)?;
                if (!chat.is_empty() && chat_id != chat) || (!turn.is_empty() && turn_id != turn) {
                    return None;
                }
                let owner = TurnKey::new(chat_id, turn_id)?;
                container
                    .mounts
                    .as_ref()?
                    .iter()
                    .any(|mount| {
                        mount.r#type == "volume"
                            && mount.name.as_deref() == Some(self.config.workflows_volume.as_str())
                            && mount.destination == "/workflows"
                    })
                    .then_some((owner, container.id))
            })
            .collect())
    }
}

impl RecoveryEngine for Broker {
    async fn exact_turn_containers(&self, turn: &TurnKey) -> Result<Vec<String>, ()> {
        let agents = self.owned_agents(turn.chat_id(), turn.turn_id()).await?;
        agents
            .into_iter()
            .map(|(_, id)| id.filter(|id| valid_docker_id(id)).ok_or(()))
            .collect()
    }

    async fn force_remove(&self, id: &str) -> Result<RemoveResult, ()> {
        if !valid_docker_id(id) {
            return Err(());
        }
        // Only a daemon-issued ID; never a supplied name. Never remove volumes.
        let (status, _) = self
            .docker_request(
                Method::DELETE,
                &format!("/v1.45/containers/{id}?force=true&v=false"),
                1024,
            )
            .await?;
        match status {
            StatusCode::NO_CONTENT => Ok(RemoveResult::Removed),
            StatusCode::NOT_FOUND => Ok(RemoveResult::AlreadyAbsent),
            _ => Err(()),
        }
    }
}

fn valid_docker_id(id: &str) -> bool {
    id.len() == 64 && id.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

async fn chat_inventory(
    State(state): State<Arc<Broker>>,
    Query(query): Query<InventoryQuery>,
) -> Response {
    let Ok(agents) = state.owned_agents(&query.chat_id, "").await else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"detail":"Docker inventory unavailable"})),
        )
            .into_response();
    };
    let agents: BTreeSet<_> = agents
        .into_iter()
        .map(|(turn, _)| turn)
        .chain(state.turns.inventory(&query.chat_id))
        .collect();
    Json(json!({"agents":agents})).into_response()
}

async fn cleanup_turn(
    State(state): State<Arc<Broker>>,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let turn = body.ok().and_then(|Json(value)| {
        TurnKey::new(
            value.get("chat_id")?.as_str()?,
            value.get("turn_id")?.as_str()?,
        )
    });
    let Some(turn) = turn else {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"detail":"chat_id and turn_id are required"})),
        )
            .into_response();
    };
    if state
        .turns
        .cleanup(state.as_ref(), &turn, Duration::from_secs(30))
        .await
        .is_err()
    {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"detail":"Old chat agent cleanup failed; retry"})),
        )
            .into_response();
    }
    Json(json!({"ok":true})).into_response()
}
