use std::{
    collections::BTreeMap,
    env, io,
    net::{Ipv4Addr, SocketAddrV4},
    path::{Path, PathBuf},
    time::Duration,
};

use denkzeug_docker_broker_spike::{
    Config, DockerEndpoint, effective_fingerprints, fingerprints, image_build::ImageConfig,
    managed_broker, runtime::RuntimeConfig, workers::WorkerConfig,
};

fn setting(key: &str, default: &str) -> String {
    env::var(key).unwrap_or_else(|_| default.to_owned())
}
fn integer(key: &str, default: u64) -> Result<u64, Box<dyn std::error::Error>> {
    match env::var(key) {
        Ok(value) => value
            .parse()
            .map_err(|_| format!("Invalid integer setting {key}").into()),
        _ => Ok(default),
    }
}

fn local_runtime(config: Config, token: &str) -> Result<Config, Box<dyn std::error::Error>> {
    let self_name = env::var("BROKER_DOCKER_SELF").or_else(|_| env::var("HOSTNAME"))?;
    if self_name.is_empty() {
        return Err("BROKER_DOCKER_SELF must identify the local broker container".into());
    }
    let contexts = PathBuf::from(setting("AGENT_IMAGES_DIR", "/agent-images"));
    let fingerprint_path = PathBuf::from(setting(
        "BROKER_IMAGE_FINGERPRINTS",
        "/etc/nautionette/image-fingerprints.json",
    ));
    let image = ImageConfig {
        fingerprints: effective_fingerprints(&contexts, &fingerprint_path)?,
        contexts,
        local_prefix: setting("IMAGE_PREFIX", "nautionette/pi-"),
        registry_prefix: setting("AGENT_IMAGE_REGISTRY_PREFIX", "").trim().to_owned(),
        base_alias: setting("BASE_IMAGE", "nautionette/pi-base:dev"),
        wait_seconds: integer("AGENT_IMAGE_BUILD_TIMEOUT_SECONDS", 900)?.max(1),
    };
    let target = setting("TARGET_NETWORK", "nautionette-internal");
    let backend = setting("AGENT_BACKEND_URL", "http://backend:8080");
    let telemetry: BTreeMap<_, _> = [
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_SDK_DISABLED",
        "APP_ENVIRONMENT",
        "APP_VERSION",
    ]
    .into_iter()
    .filter_map(|key| env::var(key).ok().map(|value| (key.to_owned(), value)))
    .collect();
    let agent_env = [
        (
            "AGENTGATEWAY_URL",
            setting("AGENT_AGENTGATEWAY_URL", "http://agentgateway:4000"),
        ),
        ("BACKEND_URL", backend.clone()),
        ("PI_OFFLINE", "1".into()),
        ("PI_SKIP_VERSION_CHECK", "1".into()),
        ("PI_TELEMETRY", "0".into()),
    ]
    .into_iter()
    .map(|(key, value)| (key.to_owned(), value))
    .chain(telemetry.clone())
    .collect();
    let runtime = RuntimeConfig {
        agent_sets: image.agent_tags(),
        agent_env,
        workflow_network: target.clone(),
        chat_network: setting("AGENT_NETWORK", "nautionette-agent-sandboxes"),
        workspace_size: setting(
            "AGENT_WORKSPACE_SIZE",
            denkzeug_docker_broker_spike::runtime::DEFAULT_WORKSPACE_SIZE,
        ),
        memory: setting(
            "AGENT_MEMORY_LIMIT",
            denkzeug_docker_broker_spike::runtime::DEFAULT_MEMORY,
        ),
        run_timeout: integer("AGENT_RUN_TIMEOUT_SECONDS", 3600)?.max(1),
        session_bytes: integer("CHAT_SESSION_MAX_BYTES", 268435456)?,
        docker_self: self_name.clone(),
        sessions_root: PathBuf::from(setting("CHAT_SESSIONS_DIR", "/chat-sessions")),
        projects_root: PathBuf::from(setting("PROJECTS_DIR", "/projects")),
    };
    let worker_env = [
        ("INTERNAL_TOKEN", token.trim().to_owned()),
        (
            "TEMPORAL_ADDRESS",
            setting("TEMPORAL_ADDRESS", "temporal:7233"),
        ),
        (
            "TEMPORAL_NAMESPACE",
            setting("TEMPORAL_NAMESPACE", "default"),
        ),
        (
            "TEMPORAL_TASK_QUEUE",
            setting("TEMPORAL_TASK_QUEUE", "nautionette"),
        ),
        ("BACKEND_URL", backend),
        (
            "MCP_URL",
            setting("WORKER_MCP_URL", "http://agentgateway:4000/mcp"),
        ),
        ("WORKFLOWS_DIR", "/workflows".into()),
        ("ARTIFACTS_DIR", "/artifacts".into()),
        (
            "WORKER_GRACE_SECONDS",
            setting("WORKER_GRACE_SECONDS", "50"),
        ),
    ]
    .into_iter()
    .map(|(key, value)| (key.to_owned(), value))
    .chain(telemetry)
    .collect();
    let workers = WorkerConfig {
        docker_self: self_name,
        project_override: env::var("COMPOSE_PROJECT")
            .ok()
            .map(|v| v.trim().to_owned())
            .filter(|v| !v.is_empty()),
        service_label: setting("WORKER_SERVICE_LABEL", "com.docker.compose.service=worker"),
        image: setting("WORKER_IMAGE", "nautionette/worker:dev"),
        network: target,
        artifacts_volume: env::var("ARTIFACTS_VOLUME").ok().filter(|v| !v.is_empty()),
        environment: worker_env,
        replicas: integer("WORKER_REPLICAS", 1)?.max(1).try_into()?,
        grace_seconds: integer("WORKER_STOP_GRACE_SECONDS", 60)?,
        temporal_receipt_required: !setting("TEMPORAL_MAINTENANCE_APP", "").is_empty(),
        app_version: setting("APP_VERSION", ""),
    };
    let interval = Duration::from_secs(integer("CONTAINER_RECONCILE_SECONDS", 30)?.max(1));
    Ok(config
        .with_local_runtime(runtime)
        .with_local_image_build(image)
        .with_local_workers(workers, Some(interval)))
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = env::args_os().skip(1).collect();
    let local=match args.as_slice() {
        [command] if command == "maintenance-launcher-version" => {
            println!("1");
            return Ok(());
        }
        [command, packed] if command == "maintenance-launcher" => {
            let packed = packed.to_str().ok_or("Invalid maintenance payload encoding")?;
            let self_name = env::var("HOSTNAME")?;
            // This is a trusted Coolify exec, never a network administration API.
            // Only the local Unix socket is accepted for maintenance.
            let socket = PathBuf::from(setting("BROKER_DOCKER_SOCKET", "/var/run/docker.sock"));
            if !socket.is_absolute() { return Err("Maintenance requires an absolute local Docker socket path".into()); }
            let config = Config::new(socket, &env::var("INTERNAL_TOKEN")?, "maintenance", Duration::from_secs(240))?
                .with_api_negotiation();
            let config = if let Some(path) = env::var_os("BROKER_DOCKER_CONFIG") { config.with_registry_config(path.into()) } else { config };
            denkzeug_docker_broker_spike::maintenance::launch(config, &self_name, packed).await?;
            return Ok(());
        }
        [command, root] if command == "fingerprints" => {
            println!("{}",serde_json::to_string_pretty(&fingerprints(Path::new(root))?)?);
            return Ok(());
        }
        [command, root, lock] if command == "fingerprints" => {
            println!("{}",serde_json::to_string_pretty(&effective_fingerprints(Path::new(root),Path::new(lock))?)?);
            return Ok(());
        }
        [command] if command == "healthcheck" => {
            let port=integer("BROKER_PORT",19100)?;
            let client=reqwest::Client::builder().no_proxy().timeout(Duration::from_secs(4)).build()?;
            let response=client.get(format!("http://127.0.0.1:{port}/healthz")).send().await?.error_for_status()?;
            let body:serde_json::Value=serde_json::from_slice(&response.bytes().await?)?;
            if body["status"]!="ok" {return Err("broker is not ready".into());}
            return Ok(());
        }
        [command] if command == "--local-runtime" => true,
        [] => false,
        _ => return Err("usage: denkzeug-docker-broker-spike [--local-runtime | healthcheck | fingerprints IMAGE_CONTEXT [PACKAGED_LOCK]]".into()),
    };
    let token = env::var("INTERNAL_TOKEN").unwrap_or_default();
    let socket = env::var_os("BROKER_DOCKER_SOCKET")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from);
    if local
        && socket.is_none()
        && ["DOCKER_HOST", "DOCKER_CONTEXT"]
            .iter()
            .all(|key| env::var_os(key).is_none_or(|value| value.is_empty()))
    {
        return Err(
            "--local-runtime requires an explicit disposable Docker socket, host, or context"
                .into(),
        );
    }
    let config = Config::for_endpoint(
        DockerEndpoint::from_env(socket)?,
        &token,
        &setting("WORKFLOWS_VOLUME", "nautionette-workflows"),
        Duration::from_secs(
            integer("BROKER_DOCKER_TIMEOUT_SECONDS", if local { 60 } else { 3 })?.max(1),
        ),
    )
    .map_err(io::Error::other)?;
    let config = config.with_api_negotiation();
    let config = if let Some(path) = env::var_os("BROKER_DOCKER_CONFIG") {
        config.with_registry_config(path.into())
    } else {
        config
    };
    let config = if local {
        local_runtime(config, &token)?
    } else {
        config
    };
    let config =
        match tokio::task::spawn_blocking(denkzeug_docker_broker_spike::telemetry::Otlp::from_env)
            .await?
        {
            Ok(Some(otel)) => config.with_telemetry(otel),
            Ok(None) => config,
            Err(_) => {
                eprintln!("Broker OTLP configuration is unavailable; continuing without export");
                config
            }
        };
    let port = integer("BROKER_PORT", 19100)?.try_into()?;
    let bind = setting("BROKER_BIND_ADDR", "127.0.0.1").parse::<Ipv4Addr>()?;
    if !local && !bind.is_loopback() {
        return Err("non-loopback binding is only supported by the explicit Docker runtime".into());
    }
    let listener = tokio::net::TcpListener::bind(SocketAddrV4::new(bind, port)).await?;
    eprintln!(
        "{} broker candidate listening on {}",
        if local {
            "Docker-backed local"
        } else {
            "Read-only"
        },
        listener.local_addr()?
    );
    let (router, shutdown) = managed_broker(config);
    let result = axum::serve(listener, router)
        .with_graceful_shutdown(shutdown_signal())
        .await;
    shutdown.finish().await;
    result?;
    Ok(())
}

async fn shutdown_signal() {
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .expect("register SIGTERM handler");
    tokio::select! {
        _=terminate.recv()=>(),
        _=tokio::signal::ctrl_c()=>(),
    }
}
