//! Behavioral ports of the Python SDK-mock scenarios against a Unix HTTP daemon.
//! Each mutation is exercised by the real Rust Engine adapter, not mocked methods.
mod admission_cases;
mod artifact_cases;
mod credential_cases;
mod fixture_artcraft_cases;
mod image_cases;
mod maintenance_cases;
mod project_cases;
mod retired_cases;
mod temporal_cases;
mod worker_cases;
use super::*;
use axum::{
    body::{Body, to_bytes},
    extract::State,
    http::Request,
    routing::any,
};
use serde_json::json;
use std::{collections::BTreeMap, sync::Mutex};
use tokio::net::UnixListener;
use tower::ServiceExt;

#[derive(Default)]
struct Daemon {
    calls: Vec<(String, String, Value)>,
    images: BTreeSet<String>,
    image_labels: BTreeMap<String, Value>,
    volumes: BTreeMap<String, Value>,
    networks: BTreeMap<String, Value>,
    containers: BTreeMap<String, Value>,
    next_id: u64,
    fail: Vec<(String, String, u16, usize)>,
    helper_exit: i64,
    helper_exits: std::collections::VecDeque<i64>,
    wait_gate: Option<Arc<tokio::sync::Barrier>>,
    helper_output: Option<Value>,
    worker_health: Option<&'static str>,
    pulls: usize,
    builds: usize,
    archives: Vec<Vec<u8>>,
    execs: BTreeMap<String, Value>,
    exec_exit: i64,
    exec_reply: Option<Value>,
    receipts: BTreeMap<String, Vec<u8>>,
}

fn next_id(d: &mut Daemon) -> String {
    d.next_id += 1;
    format!("{:064x}", d.next_id)
}
fn framed(raw: &[u8]) -> Vec<u8> {
    let mut frame = vec![1, 0, 0, 0];
    frame.extend((raw.len() as u32).to_be_bytes());
    frame.extend(raw);
    frame
}

fn matches_labels(labels: &Value, filter: Option<&String>) -> bool {
    let Some(filter) = filter else {
        return true;
    };
    let filters: Value = serde_json::from_str(filter).unwrap();
    filters["label"].as_array().is_none_or(|wanted| {
        wanted.iter().all(|wanted| {
            let wanted = wanted.as_str().unwrap();
            match wanted.split_once('=') {
                Some((key, value)) => labels[key] == value,
                None => labels.get(wanted).is_some(),
            }
        })
    })
}

async fn daemon(State(state): State<Arc<Mutex<Daemon>>>, request: Request<Body>) -> Response {
    let method = request.method().to_string();
    let path = request
        .uri()
        .path()
        .strip_prefix("/v1.45")
        .unwrap_or(request.uri().path())
        .to_owned();
    let query: BTreeMap<String, String> =
        form_urlencoded::parse(request.uri().query().unwrap_or("").as_bytes())
            .into_owned()
            .collect();
    let raw = to_bytes(request.into_body(), 64 * 1024 * 1024)
        .await
        .unwrap();
    let body = serde_json::from_slice::<Value>(&raw).unwrap_or(Value::Null);
    if method == "POST" && path.ends_with("/wait") {
        let gate = state.lock().unwrap().wait_gate.clone();
        if let Some(gate) = gate {
            gate.wait().await;
        }
    }
    let mut d = state.lock().unwrap();
    let recorded = if path == "/build" {
        json!({"query":query})
    } else {
        body.clone()
    };
    d.calls.push((method.clone(), path.clone(), recorded));
    let reply = |code: u16, value: Value| {
        (StatusCode::from_u16(code).unwrap(), Json(value)).into_response()
    };
    let empty = |code: u16| (StatusCode::from_u16(code).unwrap(), Body::empty()).into_response();
    if let Some((_, _, code, remaining)) = d
        .fail
        .iter_mut()
        .find(|(m, p, _, n)| *n > 0 && *m == method && path.starts_with(p.as_str()))
    {
        *remaining -= 1;
        return reply(*code, json!({"message":"synthetic-private-daemon-error"}));
    }
    match (method.as_str(), path.as_str()) {
        ("GET", "/version") => return reply(200, json!({"ApiVersion":"1.45"})),
        ("GET", "/_ping") => return (StatusCode::OK, "OK").into_response(),
        ("GET", "/containers/json") => {
            let rows:Vec<_>=d.containers.iter().filter(|(_,container)|matches_labels(&container["Config"]["Labels"],query.get("filters")))
                .filter(|(_,c)|query.get("all").is_some_and(|s|s=="true")||c["State"]["Status"]=="running")
                .map(|(id,c)|json!({"Id":id,"Names":[c["Name"]],"Labels":c["Config"]["Labels"],"State":c["State"]["Status"],"Mounts":c["Mounts"],"Created":c["Created"].as_u64().unwrap_or(1)})).collect();
            return reply(200, json!(rows));
        }
        ("POST", "/containers/create") => {
            let id = next_id(&mut d);
            let host = &body["HostConfig"];
            let mounts:Vec<_>=host["Mounts"].as_array().into_iter().flatten().map(|m|json!({"Type":m["Type"],"Name":m["Source"],"Destination":m["Target"]})).chain(host["Binds"].as_array().into_iter().flatten().filter_map(|s|{
                let mut parts=s.as_str()?.split(':');Some(json!({"Type":"volume","Name":parts.next()?,"Destination":parts.next()?}))
            })).collect();
            let name = query
                .get("name")
                .cloned()
                .unwrap_or_else(|| format!("helper-{id}"));
            let mut health = Value::Null;
            if let Some(status) = d.worker_health {
                health = json!({"Status":status});
            }
            let c = json!({"Id":id,"Name":format!("/{name}"),"Config":body,"HostConfig":host,"Mounts":mounts,"State":{"Status":"created","Running":false,"Health":health},"Created":1});
            d.containers.insert(id.clone(), c);
            return reply(201, json!({"Id":id}));
        }
        ("GET", "/volumes") => {
            let rows: Vec<_> = d
                .volumes
                .values()
                .filter(|v| matches_labels(&v["Labels"], query.get("filters")))
                .cloned()
                .collect();
            return reply(200, json!({"Volumes":rows}));
        }
        ("POST", "/volumes/create") => {
            let name = body["Name"].as_str().unwrap().to_owned();
            let row = json!({"Name":name,"Labels":body["Labels"]});
            d.volumes.entry(name).or_insert(row.clone());
            return reply(201, row);
        }
        ("POST", "/networks/create") => {
            let id = next_id(&mut d);
            let row = json!({"Id":id,"Name":body["Name"],"Labels":body["Labels"],"Containers":{},"Created":"2000-01-01T00:00:00Z"});
            d.networks.insert(id.clone(), row);
            return reply(201, json!({"Id":id}));
        }
        ("GET", "/networks") => {
            let rows: Vec<_> = d
                .networks
                .values()
                .filter(|v| matches_labels(&v["Labels"], query.get("filters")))
                .cloned()
                .collect();
            return reply(200, json!(rows));
        }
        ("POST", "/images/create") => {
            d.pulls += 1;
            let tag = format!(
                "{}:{}",
                query.get("fromImage").unwrap(),
                query.get("tag").unwrap()
            );
            d.images.insert(tag);
            return (StatusCode::OK, "{\"status\":\"Pulled\"}\n").into_response();
        }
        ("POST", "/build") => {
            d.builds += 1;
            d.images.insert(query["t"].clone());
            if let Some(labels) = query.get("labels") {
                d.image_labels
                    .insert(query["t"].clone(), serde_json::from_str(labels).unwrap());
            }
            return (StatusCode::OK, "{\"stream\":\"Built\"}\n").into_response();
        }
        ("GET", "/images/json") => {
            let rows: Vec<_> = d
                .image_labels
                .iter()
                .filter(|(_, labels)| matches_labels(labels, query.get("filters")))
                .map(|(id, labels)| json!({"Id":id,"Labels":labels}))
                .collect();
            return reply(200, json!(rows));
        }
        _ => (),
    }
    if let Some(rest) = path.strip_prefix("/images/") {
        if method == "GET"
            && let Some(tag) = rest.strip_suffix("/json")
        {
            let tag = form_urlencoded::parse(format!("tag={tag}").as_bytes())
                .next()
                .unwrap()
                .1
                .into_owned();
            return if d.images.contains(&tag) {
                if let Some(labels) = d.image_labels.get(&tag) {
                    return reply(
                        200,
                        json!({"Id":format!("sha256:{}", "a".repeat(64)),"Config":{"Labels":labels}}),
                    );
                }
                reply(200, json!({"Id":"sha256:fixture"}))
            } else {
                empty(404)
            };
        }
        if method == "DELETE" {
            d.image_labels.remove(rest);
            return reply(200, json!([]));
        }
        if method == "POST"
            && let Some(tag) = rest.strip_suffix("/tag")
        {
            if !d.images.contains(tag) {
                return empty(404);
            }
            d.images
                .insert(format!("{}:{}", query["repo"], query["tag"]));
            return empty(201);
        }
    }
    if let Some(name) = path.strip_prefix("/volumes/") {
        if method == "GET" {
            return d
                .volumes
                .get(name)
                .cloned()
                .map_or_else(|| empty(404), |v| reply(200, v));
        }
        if method == "DELETE" {
            let held = d.containers.values().any(|c| {
                c["Mounts"]
                    .as_array()
                    .is_some_and(|mounts| mounts.iter().any(|m| m["Name"] == name))
            });
            if held {
                return empty(409);
            }
            return if d.volumes.remove(name).is_some() {
                empty(204)
            } else {
                empty(404)
            };
        }
    }
    if let Some(id) = path.strip_prefix("/networks/") {
        if method == "GET" {
            return d
                .networks
                .get(id)
                .cloned()
                .map_or_else(|| empty(404), |v| reply(200, v));
        }
        if method == "DELETE" {
            return if d.networks.remove(id).is_some() {
                empty(204)
            } else {
                empty(404)
            };
        }
        if method == "POST" && id.ends_with("/disconnect") {
            let network = id.trim_end_matches("/disconnect");
            if let Some(net) = d.networks.get_mut(network) {
                net["Containers"]
                    .as_object_mut()
                    .unwrap()
                    .remove(body["Container"].as_str().unwrap());
                return empty(200);
            }
        }
    }
    if let Some(rest) = path.strip_prefix("/exec/") {
        let (id, action) = rest.split_once('/').unwrap();
        if !d.execs.contains_key(id) {
            return empty(404);
        }
        if method == "POST" && action == "start" {
            let data = d.exec_reply.clone().unwrap_or(json!({"ok":true}));
            return (StatusCode::OK, framed(data.to_string().as_bytes())).into_response();
        }
        if method == "GET" && action == "json" {
            return reply(200, json!({"Running":false,"ExitCode":d.exec_exit}));
        }
    }
    if let Some(rest) = path.strip_prefix("/containers/") {
        let (key, action) = rest.split_once('/').unwrap_or((rest, ""));
        let id = d
            .containers
            .iter()
            .find(|(id, c)| {
                id.as_str() == key
                    || c["Name"]
                        .as_str()
                        .is_some_and(|n| n.trim_start_matches('/') == key)
            })
            .map(|(id, _)| id.clone());
        let Some(id) = id else {
            return empty(404);
        };
        if method == "GET" && action == "json" {
            return reply(200, d.containers[&id].clone());
        }
        if method == "GET" && action == "archive" {
            return d.receipts.get(&id).map_or_else(
                || empty(404),
                |data| (StatusCode::OK, data.clone()).into_response(),
            );
        }
        if method == "DELETE" && action.is_empty() {
            d.containers.remove(&id);
            return empty(204);
        }
        if method == "PUT" && action == "archive" {
            d.archives.push(raw.to_vec());
            return empty(200);
        }
        if method == "POST" && action == "exec" {
            let exec = next_id(&mut d);
            d.execs.insert(exec.clone(), body);
            return reply(201, json!({"Id":exec}));
        }
        if method == "POST" && matches!(action, "start" | "unpause" | "restart") {
            d.containers.get_mut(&id).unwrap()["State"]["Status"] = json!("running");
            d.containers.get_mut(&id).unwrap()["State"]["Running"] = json!(true);
            return empty(204);
        }
        if method == "POST" && action == "update" {
            d.containers.get_mut(&id).unwrap()["HostConfig"]["RestartPolicy"] =
                body["RestartPolicy"].clone();
            return reply(200, json!({"Warnings":[]}));
        }
        if method == "POST" && action == "stop" {
            d.containers.get_mut(&id).unwrap()["State"]["Status"] = json!("exited");
            return empty(204);
        }
        if method == "POST" && action == "kill" {
            d.containers.get_mut(&id).unwrap()["State"]["Status"] = json!("exited");
            return empty(204);
        }
        if method == "POST" && action == "wait" {
            let exit = d.helper_exits.pop_front().unwrap_or(d.helper_exit);
            return reply(200, json!({"StatusCode":exit}));
        }
        if method == "GET" && action == "logs" {
            let output = if let Some(value) = &d.helper_output {
                value.clone()
            } else {
                let env = d.containers[&id]["Config"]["Env"].as_array();
                env.into_iter().flatten().find_map(|value|value.as_str()?.strip_prefix("ARTCRAFT_RELEASE=").and_then(|v|serde_json::from_str(v).ok())).unwrap_or(json!({"root":"pkg","resolved":"1.0.0","integrity":"sha512-fixture","resources":[]}))
            };
            return (StatusCode::OK, framed(output.to_string().as_bytes())).into_response();
        }
    }
    panic!("Unhandled fake Engine request {method} {path}");
}

struct Fixture {
    root: tempfile::TempDir,
    daemon: Arc<Mutex<Daemon>>,
    task: tokio::task::JoinHandle<()>,
    socket: PathBuf,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        self.task.abort();
    }
}
impl Fixture {
    async fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let socket = root.path().join("docker.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let state = Arc::new(Mutex::new(Daemon::default()));
        let router = Router::new()
            .fallback(any(daemon))
            .with_state(state.clone());
        let task = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        Self {
            root,
            daemon: state,
            task,
            socket,
        }
    }
    fn settings(&self) -> Config {
        Config::new(
            self.socket.clone(),
            "test-token",
            "fixture-workflows",
            Duration::from_secs(2),
        )
        .unwrap()
        .with_local_runtime(runtime::RuntimeConfig {
            agent_sets: BTreeMap::from([("default".into(), "fixture-agent:v1".into())]),
            agent_env: BTreeMap::new(),
            workflow_network: "control".into(),
            chat_network: "sandbox".into(),
            workspace_size: "1g".into(),
            memory: "4g".into(),
            run_timeout: 3600,
            session_bytes: 4096,
            docker_self: "broker".into(),
            sessions_root: self.root.path().join("sessions"),
            projects_root: self.root.path().join("projects"),
        })
    }
    fn state(&self, config: Config) -> Arc<Broker> {
        let state = new_state(config);
        *state.staged_artcraft_fixture.lock().unwrap() = Some(Vec::new());
        state
    }
    fn worker_settings(&self) -> Config {
        self.settings().with_local_workers(
            workers::WorkerConfig {
                docker_self: "broker".into(),
                project_override: Some("stack".into()),
                service_label: "com.docker.compose.service=worker".into(),
                image: "fixture-worker:v1".into(),
                network: "control".into(),
                artifacts_volume: Some("artifacts".into()),
                environment: BTreeMap::from([("TEMPORAL_ADDRESS".into(), "temporal:7233".into())]),
                replicas: 1,
                grace_seconds: 50,
                temporal_receipt_required: false,
                app_version: "a".repeat(40),
            },
            None,
        )
    }
    fn worker(&self, status: &str, health: Option<&str>) -> String {
        let mut d = self.daemon.lock().unwrap();
        let id = next_id(&mut d);
        d.containers.insert(id.clone(),json!({"Id":id,"Name":"/stack-worker-1","Config":{"Labels":{"com.docker.compose.project":"stack","com.docker.compose.service":"worker"}},"Mounts":[],"State":{"Status":status,"Running":status=="running","Health":health.map(|h|json!({"Status":h}))}}));
        id
    }
    fn image_settings(&self, registry: bool) -> Config {
        let root = self.root.path().join("images");
        for sub in ["pi-base", "agent-sets/default"] {
            std::fs::create_dir_all(root.join(sub)).unwrap();
            if !root.join(sub).join("Dockerfile").exists() {
                std::fs::write(root.join(sub).join("Dockerfile"), b"FROM scratch\n").unwrap();
            }
        }
        let images = image_build::ImageConfig {
            fingerprints: fingerprints(&root).unwrap(),
            contexts: root,
            local_prefix: "fixture/".into(),
            registry_prefix: if registry {
                "registry.invalid/cache/".into()
            } else {
                String::new()
            },
            base_alias: "fixture/base:dev".into(),
            wait_seconds: 2,
        };
        self.settings().with_local_image_build(images)
    }
    fn seed_artcraft(&self) -> Vec<(String, Value)> {
        let catalog: Value = serde_json::from_str(include_str!(
            "../../../images/pi-base/artcraft/catalog.json"
        ))
        .unwrap();
        let arch = std::env::consts::ARCH;
        let mut result = Vec::new();
        for (app, item) in catalog.as_object().unwrap() {
            let spec = json!({"app":app,"version":item["version"],"arch":arch,"sha256":item["sha256"][arch]});
            let canonical = format!(
                r#"{{"app": "{app}", "arch": "{arch}", "sha256": "{}", "version": "{}"}}"#,
                spec["sha256"].as_str().unwrap(),
                spec["version"].as_str().unwrap()
            );
            let id = packages::short_hash(canonical.as_bytes(), 32);
            let name = format!(
                "nautionette-artcraft-{}-{id}",
                packages::short_hash(b"fixture-workflows", 12)
            );
            let labels = json!({"nautionette.deployment":"fixture-workflows","nautionette.artcraft":id,"nautionette.artcraft.app":app,"nautionette.artcraft.version":spec["version"],"nautionette.artcraft.arch":arch,"nautionette.artcraft.sha256":spec["sha256"]});
            self.daemon
                .lock()
                .unwrap()
                .volumes
                .insert(name.clone(), json!({"Name":name,"Labels":labels}));
            result.push((name, spec));
        }
        result
    }
    async fn build(&self, state: &Arc<Broker>, force: bool) -> Value {
        assert!(state.start_build(force));
        tokio::time::timeout(Duration::from_secs(8), async {
            loop {
                if state.build_state.lock().unwrap().status != "building" {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        state.images_snapshot().await
    }
    async fn request(&self, app: Router, path: &str, data: Value) -> (StatusCode, Value) {
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(path)
                    .header("content-type", "application/json")
                    .header("x-internal-token", "test-token")
                    .body(Body::from(data.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = response.status();
        let body = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
        (status, serde_json::from_slice(&body).unwrap())
    }
}
