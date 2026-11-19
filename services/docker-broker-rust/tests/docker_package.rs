//! Real immutable package publication using the unchanged production Node installer.
use axum::{
    body::{Body, to_bytes},
    http::{Request, StatusCode},
};
use denkzeug_docker_broker_spike::{Config, broker, runtime::RuntimeConfig};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    path::PathBuf,
    process::Command,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tower::ServiceExt;

async fn call(app: axum::Router, body: Value) -> (StatusCode, Value) {
    let res = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/packages/install")
                .header("content-type", "application/json")
                .header("x-internal-token", "fixture-internal")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = res.status();
    let bytes = to_bytes(res.into_body(), 1024 * 1024).await.unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

#[tokio::test]
#[ignore = "requires a disposable Docker daemon with internet egress and a production Node/npm installer image"]
async fn public_npm_install_is_immutable_reaped_and_mounted_readonly() {
    let socket = PathBuf::from(
        std::env::var_os("BROKER_DOCKER_SOCKET").expect("set disposable local socket"),
    );
    let image = std::env::var("BROKER_PACKAGE_FIXTURE_IMAGE")
        .expect("load production package installer fixture");
    let source = std::env::var("NAUTIONETTE_PACKAGE_TEST_SOURCE")
        .expect("choose one pinned public Pi package");
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let namespace = format!("dz-rust-npm-{now}");
    let revision = format!("{now:032x}");
    let hash = format!("{:x}", Sha256::digest(namespace.as_bytes()));
    let volume = format!("nautionette-package-{}-{revision}", &hash[..12]);
    struct Cleanup {
        host: String,
        volume: String,
        namespace: String,
        networks: Vec<String>,
        images: Vec<String>,
    }
    impl Cleanup {
        fn command(&self, args: &[&str]) -> std::process::Output {
            Command::new("docker")
                .args(["--host", &self.host])
                .args(args)
                .output()
                .unwrap()
        }
    }
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let ids = self.command(&[
                "ps",
                "--all",
                "--quiet",
                "--filter",
                &format!("label=nautionette.deployment={}", self.namespace),
            ]);
            for id in String::from_utf8_lossy(&ids.stdout).lines() {
                let _ = self.command(&["rm", "-f", id]);
            }
            let _ = self.command(&["volume", "rm", &self.volume]);
            let _ = self.command(&["network", "rm", &self.volume]);
            for network in &self.networks {
                let _ = self.command(&["network", "rm", network]);
            }
            for image in &self.images {
                let _ = self.command(&["image", "rm", image]);
            }
        }
    }
    let mut cleanup = Cleanup {
        host: format!("unix://{}", socket.display()),
        volume: volume.clone(),
        namespace: namespace.clone(),
        networks: Vec::new(),
        images: Vec::new(),
    };
    let runtime = RuntimeConfig {
        agent_sets: [("default".into(), image.clone())].into(),
        agent_env: Default::default(),
        workflow_network: "none".into(),
        chat_network: "none".into(),
        workspace_size: "64m".into(),
        memory: "512m".into(),
        run_timeout: 300,
        session_bytes: 4096,
        docker_self: String::new(),
        sessions_root: PathBuf::new(),
        projects_root: PathBuf::new(),
    };
    let app = broker(
        Config::new(
            socket,
            "fixture-internal",
            &namespace,
            Duration::from_secs(30),
        )
        .unwrap()
        .with_api_negotiation()
        .with_local_runtime(runtime),
    );
    let body = json!({"installation_id":revision,"source":source,"allow_scripts":false});
    let (status, result) = call(app.clone(), body.clone()).await;
    assert_eq!(status, StatusCode::OK, "{result}");
    let root = result["root"]
        .as_str()
        .filter(|root| !root.is_empty())
        .unwrap();
    assert!(result["resolved"].is_string());
    let retry = call(app, body).await;
    assert_eq!(retry.0, StatusCode::UNPROCESSABLE_ENTITY);
    assert!(
        retry.1["detail"]
            .as_str()
            .unwrap()
            .contains("never overwritten")
    );
    for (resource, key) in [
        ("ps", "nautionette.package"),
        ("network", "nautionette.package"),
    ] {
        let args = if resource == "ps" {
            vec!["ps", "--all", "--quiet", "--filter"]
        } else {
            vec!["network", "ls", "--quiet", "--filter"]
        };
        let filter = format!("label={key}={revision}");
        let mut full = args;
        full.push(&filter);
        let output = cleanup.command(&full);
        assert!(output.status.success());
        assert!(
            output.stdout.is_empty(),
            "installer resources survived publication"
        );
    }
    let script = format!(
        "const fs=require('node:fs'); if(!fs.statSync('/artifact/{root}').isDirectory()) process.exit(2); try{{fs.writeFileSync('/artifact/mutation','bad');process.exit(3)}}catch(e){{if(e.code!=='EROFS')throw e}}"
    );
    let read = cleanup.command(&[
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--user=10001:10001",
        "--read-only",
        "--cap-drop=ALL",
        "--entrypoint=node",
        "--mount",
        &format!("type=volume,source={volume},target=/artifact,readonly"),
        &image,
        "-e",
        &script,
    ]);
    assert!(
        read.status.success(),
        "{}",
        String::from_utf8_lossy(&read.stderr)
    );
    assert!(read.stdout.is_empty());

    // Exercise the sandbox path too: a member-mode agent starts directly on
    // its configured egress network, can resolve the fixed gateway, and cannot
    // resolve a backend that only belongs to the control network.
    let sandbox = format!("{namespace}-sandbox");
    let control = format!("{namespace}-control");
    for name in [&sandbox, &control] {
        let output = cleanup.command(&["network", "create", name]);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        cleanup.networks.push(name.clone());
    }
    for (name, network, alias) in [
        ("gateway", &sandbox, "agentgateway"),
        ("backend", &control, "backend"),
    ] {
        let node=cleanup.command(&["run","-d","--pull=never","--network",network,"--network-alias",alias,"--label",&format!("nautionette.deployment={namespace}"),"--entrypoint=node",&image,"-e","require('http').createServer((q,s)=>s.end('fixture-ok')).listen(4000,'0.0.0.0',()=>console.log('fixture-ready'))"]);
        assert!(
            node.status.success(),
            "{}",
            String::from_utf8_lossy(&node.stderr)
        );
        let id = String::from_utf8(node.stdout).unwrap();
        tokio::time::timeout(Duration::from_secs(30), async {
            loop {
                let log = cleanup.command(&["logs", id.trim()]);
                if String::from_utf8_lossy(&log.stdout).contains("fixture-ready") {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        })
        .await
        .unwrap_or_else(|_| panic!("{name} never listened"));
    }
    let probe = format!("{namespace}-network-probe");
    let code = "const assert=require('assert/strict');(async()=>{assert.equal(await (await fetch('http://agentgateway:4000',{signal:AbortSignal.timeout(10000)})).text(),'fixture-ok');assert.equal((await fetch('https://registry.npmjs.org/@oscaner-skills/kairos/1.7.0',{signal:AbortSignal.timeout(20000)})).status,200);await assert.rejects(()=>fetch('http://backend:4000',{signal:AbortSignal.timeout(3000)}));assert.ok(!process.env.INTERNAL_TOKEN&&!process.env.BACKEND_URL);console.log(JSON.stringify({type:'network_probe',ok:true}));})().catch(e=>{console.error(e);process.exit(1)})";
    let dockerfile = format!(
        "FROM {image}\nENTRYPOINT {}\n",
        serde_json::to_string(&["node", "-e", code]).unwrap()
    );
    use std::io::Write;
    let mut child = Command::new("docker")
        .args([
            "--host",
            &cleanup.host,
            "build",
            "--network=none",
            "--pull=false",
            "-t",
            &probe,
            "-",
        ])
        .env("DOCKER_BUILDKIT", "0")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(dockerfile.as_bytes())
        .unwrap();
    let built = child.wait_with_output().unwrap();
    assert!(
        built.status.success(),
        "{}",
        String::from_utf8_lossy(&built.stderr)
    );
    cleanup.images.push(probe.clone());
    let runtime = RuntimeConfig {
        agent_sets: [("default".into(), probe)].into(),
        agent_env: [("BACKEND_URL".into(), "http://backend:4000".into())].into(),
        workflow_network: control,
        chat_network: sandbox,
        workspace_size: "64m".into(),
        memory: "256m".into(),
        run_timeout: 60,
        session_bytes: 4096,
        docker_self: String::new(),
        sessions_root: PathBuf::new(),
        projects_root: PathBuf::new(),
    };
    let cfg = Config::new(
        PathBuf::from(std::env::var_os("BROKER_DOCKER_SOCKET").unwrap()),
        "fixture-internal",
        &namespace,
        Duration::from_secs(30),
    )
    .unwrap()
    .with_api_negotiation()
    .with_local_runtime(runtime);
    let response = broker(cfg)
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/agent/run")
                .header("content-type", "application/json")
                .header("x-internal-token", "fixture-internal")
                .body(Body::from(r#"{"member_mode":true,"mode":"interactive"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    let bytes = tokio::time::timeout(
        Duration::from_secs(90),
        to_bytes(response.into_body(), 1024 * 1024),
    )
    .await
    .unwrap()
    .unwrap();
    let events: Vec<Value> = std::str::from_utf8(&bytes)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert!(
        events
            .iter()
            .any(|e| e["type"] == "network_probe" && e["ok"] == true),
        "{events:?}"
    );
    assert!(!events.iter().any(|e| e["type"] == "error"), "{events:?}");
}
