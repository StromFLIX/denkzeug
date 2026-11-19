use std::{path::PathBuf, time::Duration};

use axum::{
    Router,
    body::{Body, to_bytes},
    http::{Request, StatusCode},
};
use denkzeug_docker_broker_spike::{Config, broker, fingerprints};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::UnixListener,
};
use tower::ServiceExt;

fn fixture_path() -> PathBuf {
    use std::sync::atomic::{AtomicU64, Ordering};
    static NEXT: AtomicU64 = AtomicU64::new(0);
    std::env::temp_dir().join(format!(
        "dz-broker-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ))
}

fn app(socket: PathBuf) -> Router {
    broker(
        Config::new(
            socket,
            "test-token",
            "local-workflows",
            Duration::from_millis(200),
        )
        .unwrap(),
    )
}

async fn call(app: Router, method: &str, path: &str, auth: bool) -> (StatusCode, Value) {
    call_body(app, method, path, auth, "{").await
}

async fn call_body(
    app: Router,
    method: &str,
    path: &str,
    auth: bool,
    body: &str,
) -> (StatusCode, Value) {
    let mut request = Request::builder()
        .method(method)
        .uri(path)
        .header("content-type", "application/json");
    if auth {
        request = request.header("x-internal-token", "test-token");
    }
    let response = app
        .oneshot(request.body(Body::from(body.to_owned())).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 64 * 1024).await.unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

struct FakeDocker {
    path: PathBuf,
    listener: UnixListener,
}
impl FakeDocker {
    fn new() -> Self {
        let path = fixture_path();
        let listener = UnixListener::bind(&path).unwrap();
        Self { path, listener }
    }
    async fn serve_one(&self, status: &str, body: &str) -> String {
        self.exchange(status, body.as_bytes()).await.0
    }

    async fn request(&self) -> (tokio::net::UnixStream, String, Vec<u8>) {
        let (socket, line, _, body) = self.request_with_headers().await;
        (socket, line, body)
    }

    async fn request_with_headers(&self) -> (tokio::net::UnixStream, String, String, Vec<u8>) {
        let (mut stream, _) = tokio::time::timeout(Duration::from_secs(2), self.listener.accept())
            .await
            .expect("broker never connected to the fake Docker daemon")
            .unwrap();
        let mut bytes = Vec::new();
        let mut chunk = [0; 1024];
        let body_start = loop {
            if let Some(end) = bytes.windows(4).position(|w| w == b"\r\n\r\n") {
                break end + 4;
            }
            let n = stream.read(&mut chunk).await.unwrap();
            assert_ne!(n, 0);
            bytes.extend_from_slice(&chunk[..n]);
        };
        let headers = std::str::from_utf8(&bytes[..body_start])
            .unwrap()
            .to_owned();
        let line = headers.lines().next().unwrap().to_owned();
        let content_length = headers
            .lines()
            .filter_map(|line| line.split_once(':'))
            .find(|(key, _)| key.eq_ignore_ascii_case("content-length"))
            .map_or(0, |(_, value)| value.trim().parse::<usize>().unwrap());
        while (bytes.len() - body_start) < content_length {
            let n = stream.read(&mut chunk).await.unwrap();
            assert_ne!(n, 0);
            bytes.extend_from_slice(&chunk[..n]);
        }
        (stream, line, headers, bytes.split_off(body_start))
    }

    async fn exchange(&self, status: &str, body: &[u8]) -> (String, Vec<u8>) {
        let (mut stream, line, request_body) = self.request().await;
        stream.write_all(format!("HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).as_bytes()).await.unwrap();
        stream.write_all(body).await.unwrap();
        (line, request_body)
    }
}
impl Drop for FakeDocker {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

#[tokio::test]
async fn only_health_is_exempt_and_auth_precedes_dispatch() {
    let app = app(fixture_path());
    for (method, path) in [
        ("GET", "/agent-sets"),
        ("POST", "/images/rebuild"),
        ("POST", "/agent/run"),
        ("POST", "/packages/install"),
        ("POST", "/artcraft/install"),
        ("POST", "/worker/restart"),
        ("GET", "/agent/chats"),
        ("POST", "/agent/cleanup"),
        ("POST", "/agent/project-credentials"),
        ("POST", "/agent/control"),
        ("POST", "/containers/create"),
    ] {
        assert_eq!(
            call(app.clone(), method, path, false).await,
            (StatusCode::UNAUTHORIZED, json!({"detail":"unauthorized"})),
            "{path}"
        );
    }
    assert_eq!(
        call(app.clone(), "POST", "/containers/create", true)
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        call(app, "GET", "/healthz", false).await,
        (
            StatusCode::OK,
            json!({"status":"degraded","docker":false,"error":"Docker unavailable"})
        )
    );
}

#[tokio::test]
async fn reachable_docker_does_not_make_an_incomplete_broker_ready() {
    let docker = FakeDocker::new();
    let (result, line) = tokio::join!(
        call(app(docker.path.clone()), "GET", "/healthz", false),
        docker.serve_one("200 OK", "OK")
    );
    assert_eq!(line, "GET /_ping HTTP/1.1");
    assert_eq!(result.0, StatusCode::OK);
    assert_eq!(result.1["status"], "degraded");
    assert_eq!(result.1["docker"], true);
}

#[tokio::test]
async fn inventory_is_scoped_by_named_mount_not_copied_labels_and_needs_no_inspects() {
    let docker = FakeDocker::new();
    let row = |chat, turn, name, kind, dest| json!({"Labels":{"nautionette.chat":chat,"nautionette.turn":turn,"nautionette.deployment":"local-workflows"}, "Mounts":[{"Type":kind,"Name":name,"Destination":dest}]}  );
    let data = json!([
        row("chat", "two", "local-workflows", "volume", "/workflows"),
        row("chat", "one", "local-workflows", "volume", "/workflows"),
        row("chat", "one", "local-workflows", "volume", "/workflows"),
        row("chat", "one", "other-workflows", "volume", "/workflows"),
        row("other", "four", "local-workflows", "volume", "/workflows"),
        row("chat", "", "local-workflows", "volume", "/workflows"),
        row("chat", "bad-type", "local-workflows", "bind", "/workflows"),
        row(
            "chat",
            "wrong-destination",
            "local-workflows",
            "volume",
            "/other"
        )
    ])
    .to_string();
    let (result, line) = tokio::join!(
        call(
            app(docker.path.clone()),
            "GET",
            "/agent/chats?chat_id=chat",
            true
        ),
        docker.serve_one("200 OK", &data)
    );
    assert_eq!(
        result,
        (
            StatusCode::OK,
            json!({"agents":[{"chat_id":"chat","turn_id":"one"},{"chat_id":"chat","turn_id":"two"}]})
        )
    );
    assert!(
        line.starts_with("GET /v1.45/containers/json?all=true&filters="),
        "{line}"
    );
    assert!(line.contains("nautionette.chat%3Dchat"), "{line}");
}

#[tokio::test]
async fn inventory_fails_closed_when_inspection_unavailable() {
    let docker = FakeDocker::new();
    let (result, _) = tokio::join!(
        call(app(docker.path.clone()), "GET", "/agent/chats", true),
        docker.serve_one("500 Error", "private daemon detail")
    );
    assert_eq!(
        result,
        (
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"detail":"Docker inventory unavailable"})
        )
    );
}

#[tokio::test]
async fn docker_deadline_covers_a_stalled_response_body() {
    let docker = FakeDocker::new();
    let app = app(docker.path.clone());
    let request = tokio::spawn(call(app, "GET", "/agent/chats", true));
    let (mut stream, _) = tokio::time::timeout(Duration::from_secs(2), docker.listener.accept())
        .await
        .unwrap()
        .unwrap();
    let mut request_start = [0; 16];
    stream.read_exact(&mut request_start).await.unwrap();
    stream
        .write_all(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n[",
        )
        .await
        .unwrap();
    // Keep the connection open: the declared body never finishes.
    let response = tokio::time::timeout(Duration::from_secs(2), request)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(response.0, StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn malformed_docker_inventory_is_not_an_empty_success() {
    let docker = FakeDocker::new();
    let (result, _) = tokio::join!(
        call(app(docker.path.clone()), "GET", "/agent/chats", true),
        docker.serve_one("200 OK", "{\"unexpected\":true}")
    );
    assert_eq!(result.0, StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn experimental_cleanup_removes_only_a_daemon_id_with_the_expected_turn_and_mount() {
    let docker = FakeDocker::new();
    let spike = broker(
        Config::new(
            docker.path.clone(),
            "test-token",
            "local-workflows",
            Duration::from_secs(2),
        )
        .unwrap()
        .with_experimental_cleanup(),
    );
    assert_eq!(
        call(spike.clone(), "POST", "/agent/cleanup", false).await.0,
        StatusCode::UNAUTHORIZED
    );
    for invalid in [
        "{",
        "{}",
        r#"{"chat_id":"chat","turn_id":123}"#,
        r#"{"chat_id":"chat","turn_id":""}"#,
    ] {
        assert_eq!(
            call_body(spike.clone(), "POST", "/agent/cleanup", true, invalid)
                .await
                .0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
    }
    let id = "a".repeat(64);
    let row = |id: &str, turn, volume| json!({"Id":id,"Labels":{"nautionette.chat":"chat","nautionette.turn":turn,"nautionette.deployment":"local-workflows"},"Mounts":[{"Type":"volume","Name":volume,"Destination":"/workflows"}]});
    let mixed = json!([
        row(&id, "turn", "local-workflows"),
        row(&"b".repeat(64), "turn", "elsewhere"),
        row(&"c".repeat(64), "stale", "local-workflows")
    ])
    .to_string();
    let observed = async {
        let list = docker.serve_one("200 OK", &mixed).await;
        assert!(
            list.contains("nautionette.chat%3Dchat") && list.contains("nautionette.turn%3Dturn")
        );
        let remove = docker.serve_one("404 Not Found", "already removed").await;
        assert_eq!(
            remove,
            format!("DELETE /v1.45/containers/{id}?force=true&v=false HTTP/1.1")
        );
        assert_eq!(list, docker.serve_one("200 OK", "[]").await);
    };
    let (result, _) = tokio::join!(
        call_body(
            spike,
            "POST",
            "/agent/cleanup",
            true,
            r#"{"chat_id":"chat","turn_id":"turn"}"#
        ),
        observed
    );
    assert_eq!(result, (StatusCode::OK, json!({"ok":true})));
}

#[tokio::test]
async fn experimental_cleanup_does_not_send_mutations_for_invalid_docker_ids() {
    let docker = FakeDocker::new();
    let spike = broker(
        Config::new(
            docker.path.clone(),
            "test-token",
            "local-workflows",
            Duration::from_secs(1),
        )
        .unwrap()
        .with_experimental_cleanup(),
    );
    let owned = json!([{"Id":"invalid-id","Labels":{"nautionette.chat":"chat","nautionette.turn":"turn"},"Mounts":[{"Type":"volume","Name":"local-workflows","Destination":"/workflows"}]}]).to_string();
    let observed = async {
        assert!(docker.serve_one("200 OK", &owned).await.starts_with("GET "));
        assert!(
            tokio::time::timeout(Duration::from_millis(100), docker.listener.accept())
                .await
                .is_err()
        );
    };
    let (result, _) = tokio::join!(
        call_body(
            spike,
            "POST",
            "/agent/cleanup",
            true,
            r#"{"chat_id":"chat","turn_id":"turn"}"#
        ),
        observed
    );
    assert_eq!(
        result,
        (
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"detail":"Old chat agent cleanup failed; retry"})
        )
    );
}

fn runtime_app(socket: PathBuf, root: &std::path::Path) -> Router {
    broker(runtime_config(socket, root))
}

fn runtime_config(socket: PathBuf, root: &std::path::Path) -> Config {
    use denkzeug_docker_broker_spike::runtime::RuntimeConfig;
    use std::collections::BTreeMap;
    Config::new(
        socket,
        "test-token",
        "local-workflows",
        Duration::from_secs(2),
    )
    .unwrap()
    .with_local_runtime(RuntimeConfig {
        agent_sets: BTreeMap::from([("default".into(), "local-agent:fixture".into())]),
        agent_env: BTreeMap::from([("BACKEND_URL".into(), "http://backend:8080".into())]),
        workflow_network: "control".into(),
        chat_network: "chat-only".into(),
        workspace_size: "768m".into(),
        memory: "4g".into(),
        run_timeout: 60,
        session_bytes: 4096,
        docker_self: "broker-test".into(),
        sessions_root: root.join("sessions"),
        projects_root: root.join("projects"),
    })
}

async fn run_job(app: Router, job: Value) -> axum::response::Response {
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/agent/run")
                .header("x-internal-token", "test-token")
                .header("content-type", "application/json")
                .body(Body::from(job.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()["content-type"], "application/x-ndjson");
    response
}

async fn events(response: axum::response::Response) -> Vec<Value> {
    let bytes = to_bytes(response.into_body(), 64 * 1024).await.unwrap();
    std::str::from_utf8(&bytes)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

fn frame(stream: u8, data: &[u8]) -> Vec<u8> {
    let mut payload = vec![stream, 0, 0, 0];
    payload.extend_from_slice(&(data.len() as u32).to_be_bytes());
    payload.extend_from_slice(data);
    payload
}

#[tokio::test]
async fn workflow_launch_uses_the_docker_wire_and_never_uses_caller_selected_container_options() {
    let docker = FakeDocker::new();
    let root = fixture_path();
    let app = runtime_app(docker.path.clone(), &root);
    let id = "d".repeat(64);
    let driver = async {
        assert_eq!(
            docker.serve_one("200 OK", "{}").await,
            "GET /v1.45/images/local-agent:fixture/json HTTP/1.1"
        );
        let (create, body) = docker
            .exchange("201 Created", json!({"Id":id}).to_string().as_bytes())
            .await;
        assert_eq!(create, "POST /v1.45/containers/create HTTP/1.1");
        let options: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(options["Image"], "local-agent:fixture");
        assert!(options.get("Cmd").is_none());
        assert_eq!(options["HostConfig"]["NetworkMode"], "control");
        assert_eq!(options["HostConfig"]["PidsLimit"], 512);
        assert_eq!(options["HostConfig"]["Memory"], 4_i64 << 30);
        assert_eq!(options["HostConfig"]["CapDrop"], json!(["ALL"]));
        assert_eq!(
            options["HostConfig"]["SecurityOpt"],
            json!(["no-new-privileges:true"])
        );
        assert_eq!(
            options["HostConfig"]["Mounts"],
            json!([{"Source":"local-workflows","Type":"volume","Target":"/workflows","ReadOnly":true}])
        );
        assert_eq!(
            options["HostConfig"]["Tmpfs"]["/workspace"],
            "size=768m,exec"
        );
        let (upload, archive) = docker.exchange("200 OK", b"").await;
        assert_eq!(
            upload,
            format!("PUT /v1.45/containers/{id}/archive?path=%2Ftmp HTTP/1.1")
        );
        let mut archive = tar::Archive::new(archive.as_slice());
        let mut entries = archive.entries().unwrap();
        let entry = entries.next().unwrap().unwrap();
        assert_eq!(entry.path().unwrap().to_str(), Some("nautionette-job.json"));
        assert_eq!(entry.header().mode().unwrap(), 0o600);
        assert_eq!(entry.header().uid().unwrap(), 0);
        assert_eq!(
            docker.serve_one("204 No Content", "").await,
            format!("POST /v1.45/containers/{id}/start HTTP/1.1")
        );
        let logs = frame(1, b"{\"type\":\"text\",\"text\":\"hello from Docker\"}\n");
        assert_eq!(
            docker.exchange("200 OK", &logs).await.0,
            format!(
                "GET /v1.45/containers/{id}/logs?stdout=true&stderr=false&follow=true HTTP/1.1"
            )
        );
        assert_eq!(
            docker.serve_one("200 OK", r#"{"StatusCode":0}"#).await,
            format!("POST /v1.45/containers/{id}/wait?condition=not-running HTTP/1.1")
        );
        assert_eq!(
            docker.serve_one("204 No Content", "").await,
            format!("DELETE /v1.45/containers/{id}?force=true&v=false HTTP/1.1")
        );
    };
    let result = async {
        events(
            run_job(
                app,
                json!({"files":[{"file":"fixture"}],"image":"wrong:ignored","command":["ignored"]}),
            )
            .await,
        )
        .await
    };
    let (result, _) = tokio::join!(result, driver);
    assert_eq!(
        result,
        [
            json!({"type":"started","agent_set":"default","image":"local-agent:fixture"}),
            json!({"type":"text","text":"hello from Docker"}),
            json!({"type":"closed"})
        ]
    );
}

#[tokio::test]
async fn chat_releases_its_session_claim_after_an_explicit_stop_even_with_no_logs() {
    use denkzeug_docker_broker_spike::session::{ClaimKind, SessionRoot};
    let docker = FakeDocker::new();
    let root = fixture_path();
    let app = runtime_app(docker.path.clone(), &root);
    let id = "e".repeat(64);
    let (started, ready) = tokio::sync::oneshot::channel();
    let driver = async {
        docker.serve_one("200 OK", "{}").await;
        let info = json!({"Mounts":[{"Type":"volume","Name":"fixture-sessions","Destination":root.join("sessions")}]});
        assert_eq!(
            docker.serve_one("200 OK", &info.to_string()).await,
            "GET /v1.45/containers/broker-test/json HTTP/1.1"
        );
        assert!(
            docker
                .serve_one("200 OK", "[]")
                .await
                .contains("nautionette.chat%3Dchat")
        );
        let (_, body) = docker
            .exchange("201 Created", json!({"Id":id}).to_string().as_bytes())
            .await;
        let options: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(options["User"], "10001:10001");
        assert_eq!(options["HostConfig"]["NetworkMode"], "chat-only");
        assert!(!options["Env"].as_array().unwrap().iter().any(|entry| {
            entry.as_str().unwrap().starts_with("INTERNAL_TOKEN=")
                || entry.as_str().unwrap().starts_with("BACKEND_URL=")
        }));
        assert_eq!(
            options["HostConfig"]["Mounts"][0],
            json!({"Type":"volume","Source":"fixture-sessions","Target":"/chat-session","ReadOnly":false,"VolumeOptions":{"Subpath":"chat"}})
        );
        docker.serve_one("204 No Content", "").await;
        let (mut silent, path, _) = docker.request().await;
        assert!(path.contains("/logs?"));
        silent
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
            .await
            .unwrap();
        started.send(()).unwrap();
        assert_eq!(
            docker.serve_one("204 No Content", "").await,
            format!("POST /v1.45/containers/{id}/kill HTTP/1.1")
        );
        assert_eq!(
            docker.serve_one("204 No Content", "").await,
            format!("DELETE /v1.45/containers/{id}?force=true&v=false HTTP/1.1")
        );
    };
    let result = async {
        let response = run_job(
            app.clone(),
            json!({"chat_id":"chat","turn_id":"turn","mode":"interactive","timeout_exempt":true}),
        )
        .await;
        ready.await.unwrap();
        assert!(
            SessionRoot::new(root.join("sessions"))
                .try_claim("chat", ClaimKind::Session)
                .is_err()
        );
        assert_eq!(
            call_body(
                app,
                "POST",
                "/agent/control",
                true,
                r#"{"chat_id":"chat","turn_id":"turn","command":{"type":"stop","id":"cmd"}}"#
            )
            .await,
            (StatusCode::OK, json!({"ok":true}))
        );
        events(response).await
    };
    let (result, _) = tokio::join!(result, driver);
    assert_eq!(result[1..], [json!({"type":"closed"})]);
    assert!(
        SessionRoot::new(root.join("sessions"))
            .try_claim("chat", ClaimKind::Session)
            .is_ok()
    );
    std::fs::remove_dir_all(&root).unwrap();
}

#[tokio::test]
async fn worker_restart_filters_for_its_own_compose_project_and_preserves_grace() {
    use denkzeug_docker_broker_spike::workers::WorkerConfig;
    let docker = FakeDocker::new();
    let settings = WorkerConfig {
        docker_self: "broker-test".into(),
        project_override: Some("stack".into()),
        service_label: "com.docker.compose.service=worker".into(),
        image: "worker:fixture".into(),
        network: "control".into(),
        artifacts_volume: None,
        environment: Default::default(),
        replicas: 1,
        grace_seconds: 50,
        temporal_receipt_required: true,
        app_version: "a".repeat(40),
    };
    let app = broker(
        Config::new(
            docker.path.clone(),
            "test-token",
            "local-workflows",
            Duration::from_secs(2),
        )
        .unwrap()
        .with_local_workers(settings, None),
    );
    let id = "f".repeat(64);
    let row = |id: &str, project, service| json!({"Id":id,"Names":["/stack-worker-1"],"Labels":{"com.docker.compose.project":project,"com.docker.compose.service":service}});
    let rows = json!([
        row(&id, "stack", "worker"),
        row(&"b".repeat(64), "foreign", "worker"),
        row(&"c".repeat(64), "stack", "backend")
    ])
    .to_string();
    let daemon = async {
        let list = docker.serve_one("200 OK", &rows).await;
        assert!(list.contains("all=false") && list.contains("com.docker.compose.project%3Dstack"));
        assert_eq!(
            docker.serve_one("204 No Content", "").await,
            format!("POST /v1.45/containers/{id}/restart?t=50 HTTP/1.1")
        );
    };
    let (result, _) = tokio::join!(call(app, "POST", "/worker/restart", true), daemon);
    assert_eq!(
        result,
        (
            StatusCode::OK,
            json!({"restarted":["stack-worker-1"],"grace_seconds":50})
        )
    );
}

#[tokio::test]
async fn package_install_publishes_only_an_immutable_artifact_with_a_fixed_helper() {
    let docker = FakeDocker::new();
    let app = runtime_app(docker.path.clone(), &fixture_path());
    let revision = "a".repeat(32);
    let container = "b".repeat(64);
    let network = "c".repeat(64);
    let daemon = async {
        docker.serve_one("200 OK", "{}").await;
        let lookup = docker.serve_one("404 Not Found", "").await;
        let name = lookup
            .split_whitespace()
            .nth(1)
            .unwrap()
            .strip_prefix("/v1.45/volumes/")
            .unwrap();
        assert!(name.starts_with("nautionette-package-") && name.ends_with(&revision));
        let labels =
            json!({"nautionette.deployment":"local-workflows","nautionette.package":revision});
        let (create, raw) = docker
            .exchange(
                "201 Created",
                json!({"Name":name,"Labels":labels}).to_string().as_bytes(),
            )
            .await;
        assert_eq!(create, "POST /v1.45/volumes/create HTTP/1.1");
        assert_eq!(
            serde_json::from_slice::<Value>(&raw).unwrap()["Labels"],
            labels
        );
        let (create, raw) = docker
            .exchange("201 Created", json!({"Id":network}).to_string().as_bytes())
            .await;
        assert_eq!(create, "POST /v1.45/networks/create HTTP/1.1");
        assert_eq!(
            serde_json::from_slice::<Value>(&raw).unwrap()["Options"]["com.docker.network.bridge.enable_icc"],
            "false"
        );
        let (create, raw) = docker
            .exchange(
                "201 Created",
                json!({"Id":container}).to_string().as_bytes(),
            )
            .await;
        assert_eq!(create, "POST /v1.45/containers/create HTTP/1.1");
        let options: Value = serde_json::from_slice(&raw).unwrap();
        assert_eq!(
            options["Entrypoint"],
            json!(["node", "/usr/local/lib/nautionette/package-install.mjs"])
        );
        assert_eq!(options["HostConfig"]["NetworkMode"], name);
        assert_eq!(options["HostConfig"]["ReadonlyRootfs"], true);
        assert_eq!(options["HostConfig"]["Mounts"].as_array().unwrap().len(), 1);
        assert!(
            options["Env"]
                .as_array()
                .unwrap()
                .iter()
                .all(|env| env.as_str().unwrap().starts_with("PACKAGE_"))
        );
        docker.serve_one("204 No Content", "").await;
        docker.serve_one("200 OK", r#"{"StatusCode":0}"#).await;
        docker.exchange("200 OK",&frame(1,br#"{"root":"pkg","resolved":"1.0.0","integrity":"sha512-fixture","resources":[]}"#)).await;
        assert_eq!(
            docker.serve_one("204 No Content", "").await,
            format!("DELETE /v1.45/containers/{container}?force=true&v=false HTTP/1.1")
        );
        assert_eq!(
            docker.serve_one("204 No Content", "").await,
            format!("DELETE /v1.45/networks/{network} HTTP/1.1")
        );
    };
    let body =
        json!({"installation_id":revision,"source":"npm:example@1.0.0","allow_scripts":false})
            .to_string();
    let (result, _) = tokio::join!(
        call_body(app, "POST", "/packages/install", true, &body),
        daemon
    );
    assert_eq!(result.0, StatusCode::OK);
    assert_eq!(result.1["root"], "pkg");
}

#[tokio::test]
async fn artifact_installer_inputs_fail_closed_before_docker() {
    let root = fixture_path();
    let app = runtime_app(fixture_path(), &root);
    for (path, body) in [
        (
            "/packages/install",
            json!({"installation_id":"a".repeat(32),"source":"https://example.test/owner/repo"}),
        ),
        (
            "/packages/install",
            json!({"installation_id":"a".repeat(32),"source":"npm:example","allow_scripts":"true"}),
        ),
        (
            "/artcraft/install",
            json!({"installation_id":"a".repeat(32),"release":{"app":"not-catalog","arch":std::env::consts::ARCH,"version":"1.0.0","sha256":"a".repeat(64)}}),
        ),
        (
            "/artcraft/install",
            json!({"installation_id":"a".repeat(32),"release":{"app":"photocraft","arch":std::env::consts::ARCH,"version":"latest","sha256":"a".repeat(64)}}),
        ),
    ] {
        assert_eq!(
            call_body(app.clone(), "POST", path, true, &body.to_string())
                .await
                .0,
            StatusCode::UNPROCESSABLE_ENTITY
        );
    }
}

#[tokio::test]
async fn forced_image_rebuild_is_single_flight_and_reports_a_streamed_failure() {
    use denkzeug_docker_broker_spike::image_build::ImageConfig;
    let docker = FakeDocker::new();
    let root = fixture_path();
    let images = root.join("images");
    for subpath in ["pi-base", "agent-sets/default"] {
        std::fs::create_dir_all(images.join(subpath)).unwrap();
        std::fs::write(images.join(subpath).join("Dockerfile"), b"FROM scratch\n").unwrap();
    }
    let config = ImageConfig {
        contexts: images.clone(),
        fingerprints: fingerprints(&images).unwrap(),
        local_prefix: "fixture/".into(),
        registry_prefix: "not-used/".into(),
        base_alias: "fixture/base:dev".into(),
        wait_seconds: 20,
    };
    use base64::{
        Engine as _,
        engine::general_purpose::{STANDARD, URL_SAFE},
    };
    let registry_config = root.join("docker-config.json");
    std::fs::write(&registry_config,json!({"auths":{"build-origin.invalid":{"auth":STANDARD.encode("user:offline-fixture:with-colon")},"another-origin.invalid":{"identitytoken":"synthetic-identity"}}}).to_string()).unwrap();
    let app = broker(
        runtime_config(docker.path.clone(), &root)
            .with_local_image_build(config)
            .with_registry_config(registry_config),
    );
    let (result, _) = tokio::join!(call(app.clone(), "POST", "/images/rebuild", true), async {
        let (mut socket, line, headers, body) = docker.request_with_headers().await;
        let auth = headers
            .lines()
            .filter_map(|line| line.split_once(':'))
            .find(|(key, _)| key.eq_ignore_ascii_case("x-registry-config"))
            .unwrap()
            .1
            .trim();
        let raw: Value = serde_json::from_slice(&URL_SAFE.decode(auth).unwrap()).unwrap();
        assert_eq!(
            raw["build-origin.invalid"]["password"],
            "offline-fixture:with-colon"
        );
        assert_eq!(
            raw["another-origin.invalid"]["IdentityToken"],
            "synthetic-identity"
        );
        assert!(!headers.to_lowercase().contains("x-registry-auth:"));
        assert!(line.starts_with("POST /v1.45/build?t=fixture%2Fbase%3A"));
        assert!(!line.contains("not-used"));
        let mut archive = tar::Archive::new(body.as_slice());
        assert_eq!(
            archive
                .entries()
                .unwrap()
                .next()
                .unwrap()
                .unwrap()
                .path_bytes()
                .as_ref(),
            b"Dockerfile"
        );
        let second = call(app.clone(), "POST", "/images/rebuild", true).await;
        assert_eq!(
            second,
            (
                StatusCode::OK,
                json!({"ok":true,"status":"already building"})
            )
        );
        let result = br#"{"error":"synthetic private daemon detail"}"#;
        socket
            .write_all(
                format!(
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    result.len()
                )
                .as_bytes(),
            )
            .await
            .unwrap();
        socket.write_all(result).await.unwrap();
    });
    assert_eq!(
        result,
        (StatusCode::OK, json!({"ok":true,"status":"building"}))
    );
    // The configuration remains discoverable while the Docker builder fails.
    let (status, sets) = call(app, "GET", "/agent-sets", true).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(sets["agent_sets"][0]["ready"], false);
    assert!(!sets.to_string().contains("private"));
    std::fs::remove_dir_all(root).unwrap();
}

#[tokio::test]
async fn docker_api_negotiation_retries_failed_discovery_and_uses_reported_version() {
    let docker = FakeDocker::new();
    let app = broker(
        Config::new(
            docker.path.clone(),
            "test-token",
            "local-workflows",
            Duration::from_secs(2),
        )
        .unwrap()
        .with_api_negotiation(),
    );
    let (result, line) = tokio::join!(
        call(app.clone(), "GET", "/healthz", false),
        docker.serve_one("500 Error", "internal daemon text")
    );
    assert_eq!(line, "GET /version HTTP/1.1");
    assert_eq!(result.1["docker"], false);
    let daemon = async {
        assert_eq!(
            docker
                .serve_one("200 OK", r#"{"ApiVersion":"1.49","MinAPIVersion":"1.24"}"#)
                .await,
            "GET /version HTTP/1.1"
        );
        assert_eq!(
            docker.serve_one("200 OK", "OK").await,
            "GET /v1.49/_ping HTTP/1.1"
        );
        assert!(
            docker
                .serve_one("200 OK", "[]")
                .await
                .starts_with("GET /v1.49/containers/json?")
        );
    };
    let application = async {
        assert_eq!(
            call(app.clone(), "GET", "/healthz", false).await.1["docker"],
            true
        );
        assert_eq!(
            call(app, "GET", "/agent/chats", true).await,
            (StatusCode::OK, json!({"agents":[]}))
        );
    };
    tokio::join!(application, daemon);
}

#[tokio::test]
async fn older_docker_keeps_ordinary_inventory_but_refuses_a_chat_subpath() {
    let docker = FakeDocker::new();
    let app = broker(runtime_config(docker.path.clone(), &fixture_path()).with_api_negotiation());
    let daemon = async {
        assert_eq!(
            docker.serve_one("200 OK", r#"{"ApiVersion":"1.44"}"#).await,
            "GET /version HTTP/1.1"
        );
        assert_eq!(
            docker.serve_one("200 OK", "{}").await,
            "GET /v1.44/images/local-agent:fixture/json HTTP/1.1"
        );
        assert!(
            docker
                .serve_one("200 OK", "[]")
                .await
                .starts_with("GET /v1.44/containers/json?")
        );
    };
    let application = async {
        let frames = events(
            run_job(
                app.clone(),
                json!({"chat_id":"chat","turn_id":"turn","mode":"interactive"}),
            )
            .await,
        )
        .await;
        assert_eq!(
            frames[1],
            json!({"type":"error","message":"Chat session isolation requires Docker Engine 26+ with API 1.45+"})
        );
        assert_eq!(
            call(app, "GET", "/agent/chats", true).await,
            (StatusCode::OK, json!({"agents":[]}))
        );
    };
    tokio::join!(application, daemon);
}

#[tokio::test]
async fn otlp_exports_three_redacted_signals_and_parents_the_agent_to_the_broker() {
    use axum::{
        body::Bytes,
        extract::{Path, State},
        routing::post,
    };
    use denkzeug_docker_broker_spike::telemetry::Otlp;
    use opentelemetry_proto::tonic::collector::{
        logs::v1::ExportLogsServiceRequest, metrics::v1::ExportMetricsServiceRequest,
        trace::v1::ExportTraceServiceRequest,
    };
    use prost::Message;
    use std::{
        collections::BTreeMap,
        sync::{Arc, Mutex},
    };
    type CapturedSignals = Arc<Mutex<BTreeMap<String, Vec<Bytes>>>>;
    let received = CapturedSignals::default();
    let collector = Router::new()
        .route(
            "/v1/{signal}",
            post(
                |Path(signal): Path<String>,
                 State(received): State<CapturedSignals>,
                 body: Bytes| async move {
                    received
                        .lock()
                        .unwrap()
                        .entry(signal)
                        .or_default()
                        .push(body);
                    ([("content-type", "application/x-protobuf")], Bytes::new())
                },
            ),
        )
        .with_state(received.clone());
    let server = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}", server.local_addr().unwrap());
    let collector = tokio::spawn(async move { axum::serve(server, collector).await.unwrap() });
    let telemetry = tokio::task::spawn_blocking(move || {
        Otlp::new(&endpoint, "unit-version", "unit-env", Default::default()).unwrap()
    })
    .await
    .unwrap();
    let docker = FakeDocker::new();
    let app = broker(
        runtime_config(docker.path.clone(), &fixture_path()).with_telemetry(telemetry.clone()),
    );
    let daemon = async {
        docker.serve_one("200 OK", "{}").await;
        let (_, body) = docker
            .exchange("500 Error", b"synthetic-private-docker-detail")
            .await;
        let options: Value = serde_json::from_slice(&body).unwrap();
        options["Env"]
            .as_array()
            .unwrap()
            .iter()
            .find_map(|s| s.as_str()?.strip_prefix("TRACEPARENT="))
            .unwrap()
            .to_owned()
    };
    let application = async {
        let response=app.clone().oneshot(Request::builder().method("POST").uri("/agent/run?q=synthetic-private-query")
            .header("content-type","application/json").header("x-internal-token","test-token")
            .header("traceparent","00-0123456789abcdef0123456789abcdef-0123456789abcdef-01")
            .body(Body::from(r#"{"_trace_context":{"traceparent":"untrusted-body-parent"},"prompt":"synthetic-private-job"}"#)).unwrap()).await.unwrap();
        let result = events(response).await;
        assert_eq!(result[1]["type"], "error");
        assert_eq!(
            call(app.clone(), "GET", "/synthetic-private-path", true)
                .await
                .0,
            StatusCode::NOT_FOUND
        );
    };
    let (_, parent) = tokio::join!(application, daemon);
    assert!(parent.starts_with("00-0123456789abcdef0123456789abcdef-"));
    assert!(!parent.ends_with("-0123456789abcdef-01"));
    assert!(!parent.contains("untrusted"));
    drop(app);
    tokio::task::spawn_blocking(move || telemetry.flush())
        .await
        .unwrap();
    collector.abort();
    let received = received.lock().unwrap();
    let trace =
        ExportTraceServiceRequest::decode(received["traces"].last().unwrap().clone()).unwrap();
    let spans: Vec<_> = trace
        .resource_spans
        .iter()
        .flat_map(|r| &r.scope_spans)
        .flat_map(|s| &s.spans)
        .collect();
    let launch = spans.iter().find(|s| s.name == "POST /agent/run").unwrap();
    let hex = |id: &[u8]| id.iter().map(|b| format!("{b:02x}")).collect::<String>();
    assert_eq!(hex(&launch.parent_span_id), "0123456789abcdef");
    assert_eq!(parent.split('-').nth(2).unwrap(), hex(&launch.span_id));
    assert!(spans.iter().any(|span| span.name == "GET request"));
    let metrics =
        ExportMetricsServiceRequest::decode(received["metrics"].last().unwrap().clone()).unwrap();
    assert!(
        metrics
            .resource_metrics
            .iter()
            .flat_map(|r| &r.scope_metrics)
            .flat_map(|s| &s.metrics)
            .any(|metric| metric.name == "http.server.request.duration")
    );
    let logs = ExportLogsServiceRequest::decode(received["logs"].last().unwrap().clone()).unwrap();
    assert!(
        logs.resource_logs
            .iter()
            .flat_map(|r| &r.scope_logs)
            .flat_map(|s| &s.log_records)
            .any(|log| log.span_id == launch.span_id)
    );
    for bodies in received.values() {
        for body in bodies {
            assert!(!String::from_utf8_lossy(body).contains("synthetic-private"));
        }
    }
}

#[tokio::test]
async fn artcraft_reuses_only_verified_owned_volumes_using_a_network_none_helper() {
    let docker = FakeDocker::new();
    let app = runtime_app(docker.path.clone(), &fixture_path());
    let artifact = "a".repeat(32);
    let helper = "b".repeat(64);
    let arch = std::env::consts::ARCH;
    let spec = json!({"app":"photocraft","version":"1.0.0","arch":arch,"sha256":"c".repeat(64)});
    let labels = json!({"nautionette.deployment":"local-workflows","nautionette.artcraft":artifact,"nautionette.artcraft.app":"photocraft","nautionette.artcraft.version":"1.0.0","nautionette.artcraft.arch":arch,"nautionette.artcraft.sha256":"c".repeat(64)});
    let daemon = async {
        assert_eq!(
            docker.serve_one("200 OK", "{}").await,
            "GET /v1.45/images/local-agent:fixture/json HTTP/1.1"
        );
        let lookup = docker
            .serve_one("200 OK", &json!({"Labels":labels}).to_string())
            .await;
        let volume = lookup
            .split_whitespace()
            .nth(1)
            .unwrap()
            .strip_prefix("/v1.45/volumes/")
            .unwrap();
        let (create, raw) = docker
            .exchange("201 Created", json!({"Id":helper}).to_string().as_bytes())
            .await;
        assert_eq!(create, "POST /v1.45/containers/create HTTP/1.1");
        let config: Value = serde_json::from_slice(&raw).unwrap();
        assert_eq!(
            config["Entrypoint"],
            json!([
                "python3",
                "/usr/local/lib/nautionette/artcraft/install.py",
                "--verify"
            ])
        );
        let host = &config["HostConfig"];
        assert_eq!(host["NetworkMode"], "none");
        assert_eq!(host["ReadonlyRootfs"], true);
        assert_eq!(
            host["Mounts"],
            json!([{"Source":volume,"Target":"/artifact","Type":"volume","ReadOnly":true}])
        );
        assert_eq!(
            docker.serve_one("204 No Content", "").await,
            format!("POST /v1.45/containers/{helper}/start HTTP/1.1")
        );
        docker.serve_one("200 OK", r#"{"StatusCode":0}"#).await;
        docker
            .exchange("200 OK", &frame(1, spec.to_string().as_bytes()))
            .await;
        assert_eq!(
            docker.serve_one("204 No Content", "").await,
            format!("DELETE /v1.45/containers/{helper}?force=true&v=false HTTP/1.1")
        );
    };
    let body = json!({"installation_id":artifact,"release":spec}).to_string();
    let (result, _) = tokio::join!(
        call_body(app.clone(), "POST", "/artcraft/install", true, &body),
        daemon
    );
    assert_eq!(result, (StatusCode::OK, spec));
    let daemon = async {
        docker.serve_one("200 OK", "{}").await;
        docker
            .serve_one(
                "200 OK",
                r#"{"Labels":{"nautionette.deployment":"foreign-fixture"}}"#,
            )
            .await;
        assert!(
            tokio::time::timeout(Duration::from_millis(100), docker.listener.accept())
                .await
                .is_err()
        );
    };
    let (rejected, _) = tokio::join!(
        call_body(app, "POST", "/artcraft/install", true, &body),
        daemon
    );
    assert_eq!(
        rejected,
        (
            StatusCode::UNPROCESSABLE_ENTITY,
            json!({"detail":"ArtCraft artifact ownership/release mismatch"})
        )
    );
}

#[test]
fn source_fingerprints_match_the_checked_in_python_lock() {
    let images = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../images");
    let expected: Value =
        serde_json::from_slice(&std::fs::read(images.join("source-fingerprints.json")).unwrap())
            .unwrap();
    assert_eq!(
        serde_json::to_value(fingerprints(&images).unwrap()).unwrap(),
        expected
    );
}

#[test]
fn missing_internal_token_is_rejected_at_configuration_time() {
    assert!(
        Config::new(
            fixture_path(),
            "  ",
            "local-workflows",
            Duration::from_secs(1)
        )
        .is_err()
    );
}
