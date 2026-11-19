//! Opt in on a disposable Docker 26+ host with an already-loaded fixture image.
use std::{
    path::PathBuf,
    process::Command,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use axum::{
    body::{Body, to_bytes},
    http::{Request, StatusCode},
};
use denkzeug_docker_broker_spike::{Config, broker, runtime::RuntimeConfig};
use serde_json::{Value, json};
use tower::ServiceExt;

struct Fixtures {
    host: String,
    containers: Vec<String>,
    volumes: Vec<String>,
}

impl Fixtures {
    fn command(&self, args: &[&str]) -> Command {
        let mut command = Command::new("docker");
        command.args(["--host", &self.host]).args(args);
        command
    }

    fn checked(&self, args: &[&str]) {
        let output = self.command(args).output().expect("Docker CLI required");
        assert!(
            output.status.success(),
            "docker {:?}: {}",
            args,
            String::from_utf8_lossy(&output.stderr)
        );
    }

    fn volume(&mut self, name: &str) {
        self.checked(&["volume", "create", name]);
        self.volumes.push(name.to_owned());
    }

    fn agent(
        &mut self,
        name: &str,
        chat: &str,
        turn: &str,
        volume: &str,
        claimed: &str,
        image: &str,
    ) {
        self.checked(&[
            "create",
            "--pull=never",
            "--network=none",
            "--name",
            name,
            "--label",
            &format!("nautionette.chat={chat}"),
            "--label",
            &format!("nautionette.turn={turn}"),
            "--label",
            &format!("nautionette.deployment={claimed}"),
            "--mount",
            &format!("type=volume,source={volume},target=/workflows,readonly"),
            image,
        ]);
        self.containers.push(name.to_owned());
    }
}

impl Drop for Fixtures {
    fn drop(&mut self) {
        for name in &self.containers {
            let _ = self.command(&["rm", "-f", name]).output();
        }
        for name in &self.volumes {
            let _ = self.command(&["volume", "rm", name]).output();
        }
    }
}

#[tokio::test]
#[ignore = "requires Docker 26+, Docker CLI, and an existing local fixture image"]
async fn docker_named_mount_inventory_and_recovery_only_touch_owned_fixtures() {
    let socket = PathBuf::from(
        std::env::var_os("BROKER_DOCKER_SOCKET").unwrap_or_else(|| "/var/run/docker.sock".into()),
    );
    assert!(
        socket.is_absolute(),
        "expected a local absolute Unix socket path"
    );
    let image = std::env::var("BROKER_FIXTURE_IMAGE")
        .expect("set BROKER_FIXTURE_IMAGE to an already-loaded image, e.g. busybox:1.37");
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let scope = format!("dz-rust-{unique}");
    let staging = format!("{scope}-foreign");
    let mut fixtures = Fixtures {
        host: format!("unix://{}", socket.display()),
        containers: Vec::new(),
        volumes: Vec::new(),
    };
    fixtures.volume(&scope);
    fixtures.volume(&staging);
    fixtures.agent(
        &format!("{scope}-agent"),
        &scope,
        "own",
        &scope,
        &scope,
        &image,
    );
    fixtures.agent(
        &format!("{staging}-agent"),
        &scope,
        "foreign",
        &staging,
        &scope,
        &image,
    );

    let runtime = RuntimeConfig {
        agent_sets: [("default".into(), image)].into(),
        agent_env: Default::default(),
        workflow_network: "none".into(),
        chat_network: "none".into(),
        workspace_size: "32m".into(),
        memory: "256m".into(),
        run_timeout: 15,
        session_bytes: 4096,
        docker_self: String::new(),
        sessions_root: PathBuf::new(),
        projects_root: PathBuf::new(),
    };
    let app = broker(
        Config::new(socket, "local-test-token", &scope, Duration::from_secs(30))
            .unwrap()
            .with_local_runtime(runtime),
    );
    let request = Request::builder()
        .uri(format!("/agent/chats?chat_id={scope}"))
        .header("x-internal-token", "local-test-token")
        .body(Body::empty())
        .unwrap();
    let response = app.clone().oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = to_bytes(response.into_body(), 64 * 1024).await.unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&bytes).unwrap(),
        json!({"agents":[{"chat_id":scope,"turn_id":"own"}]})
    );
    for _ in 0..2 {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/agent/cleanup")
                    .header("x-internal-token", "local-test-token")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        json!({"chat_id":scope,"turn_id":"own"}).to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }
    let removed = fixtures
        .command(&["container", "inspect", &format!("{scope}-agent")])
        .output()
        .unwrap();
    assert!(!removed.status.success(), "exact fixture still exists");
    fixtures.checked(&["container", "inspect", &format!("{staging}-agent")]);
    fixtures.checked(&["volume", "inspect", &scope, &staging]);
    let launched = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/agent/run")
                .header("x-internal-token", "local-test-token")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"files":[{"fixture":"archive"}]}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(launched.status(), StatusCode::OK);
    let bytes = tokio::time::timeout(
        Duration::from_secs(90),
        to_bytes(launched.into_body(), 64 * 1024),
    )
    .await
    .unwrap()
    .unwrap();
    let frames: Vec<Value> = std::str::from_utf8(&bytes)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert!(
        frames.iter().all(|frame| frame["type"] != "error"),
        "{frames:?}"
    );
    assert_eq!(frames.first().unwrap()["type"], "started");
    assert_eq!(frames.last().unwrap()["type"], "closed");
    let listed = fixtures
        .command(&[
            "ps",
            "--all",
            "--quiet",
            "--filter",
            &format!("label=nautionette.deployment={scope}"),
        ])
        .output()
        .unwrap();
    assert!(listed.status.success());
    // The surviving foreign fixture also uses the copied label.
    assert_eq!(String::from_utf8_lossy(&listed.stdout).lines().count(), 1);
}

fn inspected_path(fixtures: &Fixtures, name: &str) -> PathBuf {
    let output = fixtures
        .command(&["volume", "inspect", "--format", "{{.Mountpoint}}", name])
        .output()
        .unwrap();
    assert!(output.status.success());
    PathBuf::from(String::from_utf8(output.stdout).unwrap().trim())
}

async fn post(app: axum::Router, path: &str, data: Value) -> axum::response::Response {
    app.oneshot(
        Request::builder()
            .method("POST")
            .uri(path)
            .header("x-internal-token", "local-test-token")
            .header("content-type", "application/json")
            .body(Body::from(data.to_string()))
            .unwrap(),
    )
    .await
    .unwrap()
}

async fn as_json(response: axum::response::Response) -> Value {
    serde_json::from_slice(&to_bytes(response.into_body(), 64 * 1024).await.unwrap()).unwrap()
}

#[tokio::test]
#[ignore = "requires Docker 26+, local volume storage access, and the offline Node fixture image"]
async fn docker_project_mounts_steer_and_git_rotation_preserve_private_state() {
    use futures_util::StreamExt;
    let socket = PathBuf::from(
        std::env::var_os("BROKER_DOCKER_SOCKET").unwrap_or_else(|| "/var/run/docker.sock".into()),
    );
    let image = std::env::var("BROKER_NODE_FIXTURE_IMAGE")
        .expect("load an image with Node and docker_chat_fixture.cjs");
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let scope = format!("dz-rust-chat-{unique}");
    let (projects, sessions, identity) = (
        format!("{scope}-projects"),
        format!("{scope}-sessions"),
        format!("{scope}-broker"),
    );
    let mut fixtures = Fixtures {
        host: format!("unix://{}", socket.display()),
        containers: Vec::new(),
        volumes: Vec::new(),
    };
    for name in [&scope, &projects, &sessions] {
        fixtures.volume(name);
    }
    let project_root = inspected_path(&fixtures, &projects);
    let sessions_root = inspected_path(&fixtures, &sessions);
    fixtures.checked(&[
        "create",
        "--pull=never",
        "--network=none",
        "--name",
        &identity,
        "--mount",
        &format!(
            "type=volume,source={projects},target={}",
            project_root.display()
        ),
        "--mount",
        &format!(
            "type=volume,source={sessions},target={}",
            sessions_root.display()
        ),
        &image,
    ]);
    fixtures.containers.push(identity.clone());
    let project = "a".repeat(32);
    let chat = "c".repeat(12);
    let worktree = project_root.join(".sessions").join(&project).join(&chat);
    let repository = project_root.join(&project);
    std::fs::create_dir_all(&repository).unwrap();
    let git = |directory: &std::path::Path, args: &[&str]| {
        let result = Command::new("git")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .arg("-C")
            .arg(directory)
            .args(args)
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        result.stdout
    };
    git(&repository, &["init", "-b", "main"]);
    git(&repository, &["config", "user.name", "Offline fixture"]);
    git(
        &repository,
        &["config", "user.email", "offline@example.invalid"],
    );
    git(&repository, &["commit", "--allow-empty", "-m", "Initial"]);
    git(
        &repository,
        &[
            "worktree",
            "add",
            "--detach",
            worktree.to_str().unwrap(),
            "HEAD",
        ],
    );
    std::fs::write(worktree.join("committed.txt"), "saved local commit").unwrap();
    git(&worktree, &["add", "committed.txt"]);
    git(&worktree, &["commit", "-m", "Local work survives cleanup"]);
    std::fs::write(worktree.join("keep.txt"), "keep my changes").unwrap();
    let runtime = RuntimeConfig {
        agent_sets: [("default".into(), image)].into(),
        agent_env: [(
            "BACKEND_URL".into(),
            "http://private-backend.invalid".into(),
        )]
        .into(),
        workflow_network: "none".into(),
        chat_network: "none".into(),
        workspace_size: "64m".into(),
        memory: "512m".into(),
        run_timeout: 60,
        session_bytes: 4096,
        docker_self: identity,
        sessions_root: sessions_root.clone(),
        projects_root: project_root,
    };
    let app = broker(
        Config::new(socket, "local-test-token", &scope, Duration::from_secs(30))
            .unwrap()
            .with_api_negotiation()
            .with_local_runtime(runtime),
    );
    let credential = |repo, token| json!([{"full_name":repo,"token":token,"expires_at":if token=="offline-fixture-initial"{"2000-01-01T00:00:00Z"}else{"2099-01-01T00:00:00Z"}}]);
    let response=post(app.clone(),"/agent/run",json!({"chat_id":chat,"turn_id":"turn","mode":"interactive","project_ids":[project],"project_remotes":{(project):"owner/repo"},
        "project_credentials":credential("owner/repo","offline-fixture-initial")})).await;
    assert_eq!(response.status(), StatusCode::OK);
    let mut stream = response.into_body().into_data_stream();
    let ready = tokio::time::timeout(Duration::from_secs(30), async {
        let mut raw = Vec::new();
        while let Some(chunk) = stream.next().await {
            raw.extend(chunk.unwrap());
            if raw
                .windows(b"fixture_ready".len())
                .any(|x| x == b"fixture_ready")
            {
                break;
            }
        }
        raw
    })
    .await
    .unwrap();
    let reached = String::from_utf8(ready).unwrap();
    assert!(reached.contains("fixture_ready"), "{reached}");
    let renewal =
        |repo, token| json!({"chat_id":chat,"turn_id":"turn","credentials":credential(repo,token)});
    let denied = post(
        app.clone(),
        "/agent/project-credentials",
        renewal("owner/other", "offline-fixture-renewed"),
    )
    .await;
    assert_eq!(denied.status(), StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(
        as_json(
            post(
                app.clone(),
                "/agent/project-credentials",
                renewal("owner/repo", "offline-fixture-renewed")
            )
            .await
        )
        .await,
        json!({"ok":true})
    );
    for _ in 0..2 {
        let response=post(app.clone(),"/agent/control",json!({"chat_id":chat,"turn_id":"turn","command":{"type":"steer","id":"steer-id","text":"check renewed credential"}})).await;
        assert_eq!(as_json(response).await, json!({"ok":true}));
    }
    let stopped = post(
        app.clone(),
        "/agent/control",
        json!({"chat_id":chat,"turn_id":"turn","command":{"type":"stop","id":"stop-id"}}),
    )
    .await;
    assert_eq!(as_json(stopped).await, json!({"ok":true}));
    let remainder = tokio::time::timeout(Duration::from_secs(30), async {
        let mut all = Vec::new();
        while let Some(chunk) = stream.next().await {
            all.extend(chunk.unwrap());
        }
        all
    })
    .await
    .unwrap();
    let output = String::from_utf8(remainder).unwrap();
    assert!(output.contains("closed"), "{output}");
    assert!(
        !output.contains("offline-fixture-renewed") && !output.contains("offline-fixture-initial")
    );
    assert_eq!(
        std::fs::read_to_string(sessions_root.join(&chat).join("session-fixture.txt")).unwrap(),
        "persistent"
    );
    assert_eq!(
        std::fs::read_to_string(worktree.join("keep.txt")).unwrap(),
        "keep my changes"
    );
    assert_eq!(
        git(&worktree, &["show", "HEAD:committed.txt"]),
        b"saved local commit"
    );
    assert_eq!(
        String::from_utf8(git(&worktree, &["rev-parse", "--abbrev-ref", "HEAD"]))
            .unwrap()
            .trim(),
        "HEAD"
    );
    let retry = post(
        app,
        "/agent/cleanup",
        json!({"chat_id":chat,"turn_id":"turn"}),
    )
    .await;
    assert_eq!(as_json(retry).await, json!({"ok":true}));
}

#[tokio::test]
#[ignore = "requires Docker 26+, local volume storage access, and an offline BusyBox image with /bin/sh"]
async fn docker_worker_drain_and_temporal_receipt_are_scoped_and_fail_closed() {
    use denkzeug_docker_broker_spike::workers::WorkerConfig;
    let socket = PathBuf::from(
        std::env::var_os("BROKER_DOCKER_SOCKET").unwrap_or_else(|| "/var/run/docker.sock".into()),
    );
    let image =
        std::env::var("BROKER_FIXTURE_IMAGE").expect("load an offline BusyBox fixture image");
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let scope = format!("dz-rust-workers-{unique}");
    let (own, foreign, controller, receipt) = (
        format!("{scope}-own"),
        format!("{scope}-foreign"),
        format!("{scope}-controller"),
        format!("{scope}-receipt"),
    );
    let mut fixtures = Fixtures {
        host: format!("unix://{}", socket.display()),
        containers: Vec::new(),
        volumes: Vec::new(),
    };
    for name in [&scope, &receipt] {
        fixtures.volume(name);
    }
    let own_dir = inspected_path(&fixtures, &scope);
    fixtures.checked(&["create","--pull=never","--network=none","--name",&own,
        "--label",&format!("com.docker.compose.project={scope}"),"--label","com.docker.compose.service=worker",
        "--mount",&format!("type=volume,source={scope},target=/workflows"),"--health-cmd=/bin/busybox true","--health-interval=1s",
        &image,"/bin/sh","-c","trap '/bin/busybox touch /workflows/drained; exit 0' TERM; /bin/busybox touch /workflows/started; while :; do /bin/busybox sleep 1; done"]);
    fixtures.containers.push(own.clone());
    fixtures.checked(&[
        "create",
        "--pull=never",
        "--network=none",
        "--name",
        &foreign,
        "--label",
        "com.docker.compose.project=foreign-fixture",
        "--label",
        "com.docker.compose.service=worker",
        &image,
        "/bin/busybox",
        "sleep",
        "100",
    ]);
    fixtures.containers.push(foreign.clone());
    fixtures.checked(&["start", &own, &foreign]);
    tokio::time::timeout(Duration::from_secs(20), async {
        while !own_dir.join("started").exists() {
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .unwrap();
    let started_at = || {
        fixtures
            .command(&["inspect", "--format", "{{.State.StartedAt}}", &foreign])
            .output()
            .unwrap()
            .stdout
    };
    let before = started_at();
    let commit = "a".repeat(40);
    let config = WorkerConfig {
        docker_self: "unused".into(),
        project_override: Some(scope.clone()),
        service_label: "com.docker.compose.service=worker".into(),
        image: image.clone(),
        network: "none".into(),
        artifacts_volume: None,
        environment: Default::default(),
        replicas: 1,
        grace_seconds: 8,
        temporal_receipt_required: true,
        app_version: commit.clone(),
    };
    let app = broker(
        Config::new(socket, "local-test-token", &scope, Duration::from_secs(30))
            .unwrap()
            .with_api_negotiation()
            .with_local_workers(config, None),
    );
    let health = || {
        app.clone().oneshot(
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
    };
    assert_eq!(
        as_json(health().await.unwrap()).await["temporal_migration"]["phase"],
        "receipt_missing"
    );
    let restart = post(app.clone(), "/worker/restart", json!({})).await;
    assert_eq!(
        as_json(restart).await,
        json!({"restarted":[own],"grace_seconds":8})
    );
    assert!(own_dir.join("drained").exists());
    assert_eq!(before, started_at());
    fixtures.checked(&[
        "create",
        "--pull=never",
        "--network=none",
        "--name",
        &controller,
        "--restart=no",
        "--label",
        &format!("nautionette.temporal.project={scope}"),
        "--label",
        &format!("nautionette.temporal.commit={commit}"),
        "--mount",
        &format!("type=volume,source={receipt},target=/opt/maintenance"),
        &image,
    ]);
    fixtures.containers.push(controller.clone());
    let published = inspected_path(&fixtures, &receipt).join("completed.json");
    std::fs::write(
        &published,
        json!({"status":"completed","commit":commit}).to_string(),
    )
    .unwrap();
    assert_eq!(
        as_json(health().await.unwrap()).await["temporal_migration"]["phase"],
        "cleanup_pending"
    );
    fixtures.checked(&["start", "-a", &controller]);
    assert_eq!(
        as_json(health().await.unwrap()).await["temporal_migration"],
        json!({"status":"ready","phase":"completed"})
    );
    std::fs::write(published, b"invalid receipt").unwrap();
    let health = as_json(health().await.unwrap()).await;
    assert_eq!(health["temporal_migration"]["phase"], "inspection_failed");
    assert!(!health.to_string().contains("invalid receipt"));
}

fn native_pi_fixture(with_steering: bool) -> (Fixtures, axum::Router, String, PathBuf) {
    let socket = PathBuf::from(
        std::env::var_os("BROKER_DOCKER_SOCKET").unwrap_or_else(|| "/var/run/docker.sock".into()),
    );
    let native =
        std::env::var("BROKER_PI_FIXTURE_IMAGE").expect("load the native Pi fixture image");
    let mut agent_sets = std::collections::BTreeMap::from([("default".into(), native.clone())]);
    if with_steering {
        let steer = std::env::var("BROKER_PI_STEER_FIXTURE_IMAGE")
            .expect("load the original Pi steering fixture image");
        agent_sets.insert("steer".into(), steer);
    }
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let scope = format!("dz-rust-native-pi-{unique}");
    let (sessions, identity) = (format!("{scope}-sessions"), format!("{scope}-broker"));
    let mut fixtures = Fixtures {
        host: format!("unix://{}", socket.display()),
        containers: Vec::new(),
        volumes: Vec::new(),
    };
    for name in [&scope, &sessions] {
        fixtures.volume(name);
    }
    let sessions_root = inspected_path(&fixtures, &sessions);
    fixtures.checked(&[
        "create",
        "--pull=never",
        "--network=none",
        "--name",
        &identity,
        "--mount",
        &format!(
            "type=volume,source={sessions},target={}",
            sessions_root.display()
        ),
        &native,
    ]);
    fixtures.containers.push(identity.clone());
    let runtime = RuntimeConfig {
        agent_sets,
        agent_env: [
            ("AGENTGATEWAY_URL".into(), "http://127.0.0.1:9".into()),
            ("PI_OFFLINE".into(), "1".into()),
            ("PI_SKIP_VERSION_CHECK".into(), "1".into()),
            ("PI_TELEMETRY".into(), "0".into()),
            ("OTEL_SDK_DISABLED".into(), "true".into()),
        ]
        .into(),
        workflow_network: "none".into(),
        chat_network: "none".into(),
        workspace_size: "128m".into(),
        memory: "512m".into(),
        run_timeout: 150,
        session_bytes: 16 * 1024 * 1024,
        docker_self: identity,
        sessions_root: sessions_root.clone(),
        projects_root: PathBuf::new(),
    };
    let app = broker(
        Config::new(socket, "local-test-token", &scope, Duration::from_secs(38))
            .unwrap()
            .with_api_negotiation()
            .with_local_runtime(runtime),
    );
    (fixtures, app, scope, sessions_root)
}

#[tokio::test]
#[ignore = "requires Docker 26+, local volume storage access, and the offline pinned Pi 1.5.2 base"]
async fn docker_real_pi_native_session_replay() {
    let (_fixtures, app, _, sessions_root) = native_pi_fixture(false);
    let secret = sessions_root.join("other-chat");
    std::fs::create_dir(&secret).unwrap();
    std::fs::write(secret.join("unrelated.txt"), b"leave this session alone").unwrap();
    let mut session_ids = Vec::new();
    for turn in ["initial", "resumed"] {
        let response=post(app.clone(),"/agent/run",json!({"chat_id":"native-chat","turn_id":turn,"mode":"interactive","prompt":"/compact","model":"test/model","tools":[]})).await;
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = tokio::time::timeout(
            Duration::from_secs(90),
            to_bytes(response.into_body(), 1024 * 1024),
        )
        .await
        .unwrap()
        .unwrap();
        let log = std::str::from_utf8(&bytes).unwrap();
        assert!(!log.contains("EACCES"), "{log}");
        let events: Vec<Value> = log
            .lines()
            .filter_map(|line| serde_json::from_str(line).ok())
            .collect();
        let ready = events
            .iter()
            .find(|frame| frame["type"] == "session_ready")
            .unwrap_or_else(|| panic!("no real Pi session frame: {log}"));
        assert_eq!(events.last().unwrap()["type"], "closed");
        session_ids.push(ready["id"].clone());
    }
    eprintln!("production Pi runner opened the same durable session in two disposable containers");
    let file = std::fs::read_to_string(sessions_root.join("native-chat/session.jsonl")).unwrap();
    let header: Value = serde_json::from_str(file.lines().next().unwrap()).unwrap();
    assert_eq!(session_ids, [header["id"].clone(), header["id"].clone()]);
    assert_eq!(
        std::fs::read(secret.join("unrelated.txt")).unwrap(),
        b"leave this session alone"
    );
}

#[tokio::test]
#[ignore = "requires Docker 26+, local volume storage access, pinned Pi 1.5.2 and the original steering fixture"]
async fn docker_real_pi_original_tool_steering_and_stop() {
    use futures_util::StreamExt;
    let (fixtures, app, scope, _) = native_pi_fixture(true);
    for stop in [false, true] {
        let (chat, turn) = (
            if stop { "native-stop" } else { "native-steer" },
            "tool-turn",
        );
        let response=post(app.clone(),"/agent/run",json!({"chat_id":chat,"turn_id":turn,"mode":"interactive","agent_set":"steer","model":"test-model","prompt":"Run the blocking tool"})).await;
        let mut stream = response.into_body().into_data_stream();
        let ready = tokio::time::timeout(Duration::from_secs(75), async {
            let mut raw = Vec::new();
            while let Some(chunk) = stream.next().await {
                raw.extend(chunk.unwrap());
                if raw
                    .windows(b"\"type\":\"tool\"".len())
                    .any(|x| x == b"\"type\":\"tool\"")
                {
                    break;
                }
            }
            raw
        })
        .await
        .unwrap();
        let ready = String::from_utf8(ready).unwrap();
        assert!(ready.contains("\"type\":\"tool\""), "{ready}");
        eprintln!("original Pi fixture reached its blocking tool (stop={stop})");
        let target = fixtures
            .command(&[
                "ps",
                "--quiet",
                "--filter",
                &format!("label=nautionette.deployment={scope}"),
                "--filter",
                &format!("label=nautionette.chat={chat}"),
                "--filter",
                &format!("label=nautionette.turn={turn}"),
            ])
            .output()
            .unwrap();
        assert!(target.status.success());
        let ids = String::from_utf8(target.stdout).unwrap();
        let id = ids.trim();
        assert_eq!(ids.lines().count(), 1);
        if stop {
            let stopped = post(
                app.clone(),
                "/agent/control",
                json!({"chat_id":chat,"turn_id":turn,"command":{"type":"stop","id":"stop"}}),
            )
            .await;
            assert_eq!(as_json(stopped).await, json!({"ok":true}));
        } else {
            for _ in 0..2 {
                let queued=post(app.clone(),"/agent/control",json!({"chat_id":chat,"turn_id":turn,"command":{"type":"steer","id":"queued","text":"queued instruction"}})).await;
                assert_eq!(as_json(queued).await, json!({"ok":true}));
            }
            fixtures.checked(&[
                "exec",
                id,
                "node",
                "-e",
                "require('fs').writeFileSync('/tmp/release-tool','yes')",
            ]);
        }
        let remainder = tokio::time::timeout(Duration::from_secs(85), async {
            let mut all = Vec::new();
            while let Some(chunk) = stream.next().await {
                all.extend(chunk.unwrap());
            }
            all
        })
        .await
        .unwrap();
        let tail = std::str::from_utf8(&remainder).unwrap();
        let frames: Vec<Value> = tail
            .lines()
            .filter_map(|line| serde_json::from_str(line).ok())
            .collect();
        assert_eq!(frames.last().unwrap()["type"], "closed");
        if !stop {
            let ids: Vec<_> = frames
                .iter()
                .filter(|f| f["type"] == "input_consumed")
                .map(|f| f["id"].clone())
                .collect();
            assert_eq!(ids, [json!("queued")]);
            let result = frames
                .iter()
                .find(|f| f["type"] == "result")
                .unwrap_or_else(|| panic!("no Pi result: {tail}"));
            assert_eq!(result["ok"], true);
            assert_eq!(result["text"], "Received queued instruction after tool");
        }
        let gone = fixtures
            .command(&["container", "inspect", id])
            .output()
            .unwrap();
        assert!(!gone.status.success(), "disposed agent still exists");
    }
}

#[tokio::test]
#[ignore = "requires Docker 26+, local volume storage access, and a prepared pinned GridCraft artifact"]
async fn docker_real_artcraft_probe_reuses_release_and_preserves_a_referenced_failed_volume() {
    use sha2::{Digest, Sha256};
    let socket = PathBuf::from(
        std::env::var_os("BROKER_DOCKER_SOCKET").unwrap_or_else(|| "/var/run/docker.sock".into()),
    );
    let artifact = PathBuf::from(
        std::env::var_os("BROKER_ARTCRAFT_ARTIFACT_PATH").expect("set the prepared artifact path"),
    );
    let image = std::env::var("BROKER_ARTCRAFT_VERIFY_IMAGE")
        .expect("load a Pi image with the production Python verifier");
    let scope = format!(
        "dz-rust-gridcraft-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    );
    let id = "c".repeat(32);
    let prefix = format!("{:x}", Sha256::digest(scope.as_bytes()));
    let name = format!("nautionette-artcraft-{}-{id}", &prefix[..12]);
    let mut fixtures = Fixtures {
        host: format!("unix://{}", socket.display()),
        containers: Vec::new(),
        volumes: Vec::new(),
    };
    let catalog: Value = serde_json::from_str(include_str!(
        "../../../images/pi-base/artcraft/catalog.json"
    ))
    .unwrap();
    let arch = std::env::consts::ARCH;
    let release = json!({"app":"gridcraft","version":catalog["gridcraft"]["version"],"arch":arch,"sha256":catalog["gridcraft"]["sha256"][arch]});
    let actual_manifest: Value =
        serde_json::from_slice(&std::fs::read(artifact.join("manifest.json")).unwrap()).unwrap();
    assert_eq!(actual_manifest, release);
    let label = |key: &str| {
        format!(
            "nautionette.artcraft.{key}={}",
            release[key].as_str().unwrap()
        )
    };
    fixtures.checked(&[
        "volume",
        "create",
        "--label",
        &format!("nautionette.deployment={scope}"),
        "--label",
        &format!("nautionette.artcraft={id}"),
        "--label",
        &label("app"),
        "--label",
        &label("version"),
        "--label",
        &label("arch"),
        "--label",
        &label("sha256"),
        &name,
    ]);
    fixtures.volumes.push(name.clone());
    let mount = inspected_path(&fixtures, &name);
    std::fs::create_dir_all(mount.join("bin")).unwrap();
    std::fs::copy(
        artifact.join("bin/gridcraft-cli"),
        mount.join("bin/gridcraft-cli"),
    )
    .unwrap();
    std::fs::copy(artifact.join("manifest.json"), mount.join("manifest.json")).unwrap();
    let before = std::fs::metadata(mount.join("bin/gridcraft-cli"))
        .unwrap()
        .len();
    let runtime = RuntimeConfig {
        agent_sets: [("default".into(), image.clone())].into(),
        agent_env: Default::default(),
        workflow_network: "none".into(),
        chat_network: "none".into(),
        workspace_size: "64m".into(),
        memory: "512m".into(),
        run_timeout: 60,
        session_bytes: 4096,
        docker_self: String::new(),
        sessions_root: PathBuf::new(),
        projects_root: PathBuf::new(),
    };
    let app = broker(
        Config::new(socket, "local-test-token", &scope, Duration::from_secs(38))
            .unwrap()
            .with_api_negotiation()
            .with_local_runtime(runtime),
    );
    let request = json!({"installation_id":id,"release":release});
    let verified = post(app.clone(), "/artcraft/install", request.clone()).await;
    assert_eq!(
        verified.status(),
        StatusCode::OK,
        "{}",
        as_json(verified).await
    );
    let referencing = format!("{scope}-reference");
    fixtures.checked(&[
        "create",
        "--pull=never",
        "--network=none",
        "--name",
        &referencing,
        "--mount",
        &format!("type=volume,source={name},target=/fixture-held,readonly"),
        "--entrypoint",
        "/bin/sh",
        &image,
        "-c",
        ":",
    ]);
    fixtures.containers.push(referencing);
    std::fs::write(
        mount.join("manifest.json"),
        b"{\"app\":\"gridcraft\",\"version\":\"invalid fixture\"}",
    )
    .unwrap();
    let refused = post(app, "/artcraft/install", request).await;
    assert_eq!(refused.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(
        !as_json(refused)
            .await
            .to_string()
            .contains("invalid fixture")
    );
    assert_eq!(
        std::fs::metadata(mount.join("bin/gridcraft-cli"))
            .unwrap()
            .len(),
        before
    );
    fixtures.checked(&["volume", "inspect", &name]);
}
