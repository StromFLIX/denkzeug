//! Real Docker daemon and a loopback-only, authenticated HTTPS registry.
use axum::{
    body::{Body, Bytes, to_bytes},
    http::{Request, Response, StatusCode},
};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use denkzeug_docker_broker_spike::{
    Config, broker, fingerprints, image_build::ImageConfig, runtime::RuntimeConfig,
};
use flate2::{Compression, write::GzEncoder};
use hyper::{body::Incoming, service::service_fn};
use hyper_util::rt::TokioIo;
use rcgen::{BasicConstraints, CertificateParams, CertifiedIssuer, IsCa, KeyPair};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    io::Write,
    path::PathBuf,
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio_rustls::{
    TlsAcceptor,
    rustls::{
        ServerConfig,
        pki_types::{PrivateKeyDer, PrivatePkcs8KeyDer},
    },
};
use tower::ServiceExt;

fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}
const CONFIG: &str = "application/vnd.docker.container.image.v1+json";
const LAYER: &str = "application/vnd.docker.image.rootfs.diff.tar.gzip";
const MANIFEST: &str = "application/vnd.docker.distribution.manifest.v2+json";
struct Registry {
    allowed: String,
    repository: String,
    revision: String,
    config: Bytes,
    layer: Bytes,
    manifest: Bytes,
    authorized_manifests: AtomicUsize,
}

impl Registry {
    fn serve(&self, request: Request<Incoming>) -> Response<Body> {
        let respond = |status, content_type: &str, data: Bytes| {
            let sum = digest(&data);
            let len = data.len();
            Response::builder()
                .status(status)
                .header("Docker-Distribution-Api-Version", "registry/2.0")
                .header("Docker-Content-Digest", sum)
                .header("Content-Type", content_type)
                .header("Content-Length", len)
                .body(if request.method() == "HEAD" {
                    Body::empty()
                } else {
                    Body::from(data)
                })
                .unwrap()
        };
        if request
            .headers()
            .get("authorization")
            .and_then(|value| value.to_str().ok())
            != Some(self.allowed.as_str())
        {
            let mut denied = respond(
                StatusCode::UNAUTHORIZED,
                "application/json",
                Bytes::from_static(br#"{"errors":[{"code":"UNAUTHORIZED"}]}"#),
            );
            denied.headers_mut().insert(
                "WWW-Authenticate",
                "Basic realm=\"local Docker registry fixture\""
                    .parse()
                    .unwrap(),
            );
            return denied;
        }
        let path = request.uri().path();
        if path == "/v2/" {
            return respond(
                StatusCode::OK,
                "application/json",
                Bytes::from_static(b"{}"),
            );
        }
        let base = format!("/v2/{}/", self.repository);
        if path == format!("{base}manifests/{}", self.revision)
            || path == format!("{base}manifests/{}", digest(&self.manifest))
        {
            self.authorized_manifests.fetch_add(1, Ordering::Relaxed);
            return respond(StatusCode::OK, MANIFEST, self.manifest.clone());
        }
        if path == format!("{base}blobs/{}", digest(&self.config)) {
            return respond(StatusCode::OK, CONFIG, self.config.clone());
        }
        if path == format!("{base}blobs/{}", digest(&self.layer)) {
            return respond(StatusCode::OK, LAYER, self.layer.clone());
        }
        respond(
            StatusCode::NOT_FOUND,
            "application/json",
            Bytes::from_static(br#"{"errors":[{"code":"MANIFEST_UNKNOWN"}]}"#),
        )
    }
}

#[tokio::test]
#[ignore = "requires a disposable local Docker daemon and access to /etc/docker/certs.d"]
async fn docker_pull_uses_configured_auth_against_an_actual_tls_registry_and_restores_alias() {
    let socket = PathBuf::from(
        std::env::var_os("BROKER_DOCKER_SOCKET").unwrap_or_else(|| "/var/run/docker.sock".into()),
    );
    let id = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let scope = format!("dz-rust-{id}");
    let sources = tempfile::tempdir().unwrap();
    for subdir in ["pi-base", "agent-sets/default"] {
        let dir = sources.path().join(subdir);
        std::fs::create_dir_all(&dir).unwrap();
        // Force a local fallback failure after the base image was authenticated
        // and retagged, without involving GitHub or launching artifact helpers.
        std::fs::write(
            dir.join("Dockerfile"),
            b"FROM scratch\nUNSUPPORTED_FIXTURE_INSTRUCTION\n",
        )
        .unwrap();
    }
    let fingerprint = fingerprints(sources.path()).unwrap();
    let revision = serde_json::to_value(&fingerprint).unwrap()["base"]
        .as_str()
        .unwrap()
        .to_owned();
    let mut tar = tar::Builder::new(Vec::new());
    let mut header = tar::Header::new_ustar();
    let marker = b"authenticated registry evidence";
    header.set_mode(0o644);
    header.set_size(marker.len() as u64);
    tar.append_data(&mut header, "marker.txt", marker.as_slice())
        .unwrap();
    let raw = tar.into_inner().unwrap();
    let mut gzip = GzEncoder::new(Vec::new(), Compression::fast());
    gzip.write_all(&raw).unwrap();
    let layer = Bytes::from(gzip.finish().unwrap());
    let arch = match std::env::consts::ARCH {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        _ => panic!("fixture architecture"),
    };
    let config=Bytes::from(json!({"os":"linux","architecture":arch,"config":{},"rootfs":{"type":"layers","diff_ids":[digest(&raw)]},"history":[{"created_by":"broker fixture"}]}).to_string());
    let config_id = digest(&config);
    let manifest=Bytes::from(json!({"schemaVersion":2,"mediaType":MANIFEST,"config":{"mediaType":CONFIG,"size":config.len(),"digest":config_id},"layers":[{"mediaType":LAYER,"size":layer.len(),"digest":digest(&layer)}]}).to_string());
    let basic = STANDARD.encode("fixture-broker:fixture-private-token");
    let registry = Arc::new(Registry {
        allowed: format!("Basic {basic}"),
        repository: format!("{scope}/pi-base"),
        revision: revision.clone(),
        config,
        layer,
        manifest,
        authorized_manifests: AtomicUsize::new(0),
    });
    let mut ca_params = CertificateParams::new(Vec::new()).unwrap();
    ca_params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    let ca = CertifiedIssuer::self_signed(ca_params, KeyPair::generate().unwrap()).unwrap();
    let params = CertificateParams::new(vec!["127.0.0.1".to_owned()]).unwrap();
    let key = KeyPair::generate().unwrap();
    let cert = params.signed_by(&key, &ca).unwrap();
    let cfg = ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(
            vec![cert.der().clone()],
            PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(key.serialize_der())),
        )
        .unwrap();
    let acceptor = TlsAcceptor::from(Arc::new(cfg));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let trust = PathBuf::from(format!("/etc/docker/certs.d/{addr}"));
    std::fs::create_dir_all(trust.parent().unwrap()).unwrap();
    std::fs::create_dir(&trust).expect("isolated fixture needs a unique registry trust directory");
    struct Trust(PathBuf);
    impl Drop for Trust {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(self.0.join("ca.crt"));
            let _ = std::fs::remove_dir(&self.0);
        }
    }
    let trust = Trust(trust);
    std::fs::write(trust.0.join("ca.crt"), ca.pem()).unwrap();
    let registry_server = registry.clone();
    let server = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let acceptor = acceptor.clone();
            let registry = registry_server.clone();
            tokio::spawn(async move {
                let Ok(stream) = acceptor.accept(stream).await else {
                    return;
                };
                let _ = hyper::server::conn::http1::Builder::new()
                    .serve_connection(
                        TokioIo::new(stream),
                        service_fn(|request| async {
                            Ok::<_, std::convert::Infallible>(registry.serve(request))
                        }),
                    )
                    .await;
            });
        }
    });
    let auth_file = sources.path().join("config.json");
    std::fs::write(
        &auth_file,
        json!({"auths":{(addr.to_string()):{"auth":basic}}}).to_string(),
    )
    .unwrap();
    let tag = format!("{scope}/pi-base:{revision}");
    let alias = format!("{scope}/pi-base:dev");
    let images = ImageConfig {
        contexts: sources.path().to_owned(),
        fingerprints: fingerprint,
        local_prefix: format!("{scope}/pi-"),
        registry_prefix: format!("{addr}/{scope}/pi-"),
        base_alias: alias.clone(),
        wait_seconds: 120,
    };
    let runtime = RuntimeConfig {
        agent_sets: images.agent_tags(),
        agent_env: Default::default(),
        workflow_network: "none".into(),
        chat_network: "none".into(),
        workspace_size: "64m".into(),
        memory: "256m".into(),
        run_timeout: 60,
        session_bytes: 4096,
        docker_self: String::new(),
        sessions_root: PathBuf::new(),
        projects_root: PathBuf::new(),
    };
    let cfg = Config::new(
        socket.clone(),
        "local-test-token",
        &scope,
        Duration::from_secs(12),
    )
    .unwrap()
    .with_api_negotiation()
    .with_registry_config(auth_file)
    .with_local_runtime(runtime)
    .with_local_image_build(images);
    struct Images {
        host: String,
        tags: Vec<String>,
    }
    impl Drop for Images {
        fn drop(&mut self) {
            for tag in &self.tags {
                let _ = Command::new("docker")
                    .args(["--host", &self.host, "image", "rm", tag])
                    .output();
            }
        }
    }
    let cleanup = Images {
        host: format!("unix://{}", socket.display()),
        tags: vec![
            alias.clone(),
            tag.clone(),
            format!("{addr}/{scope}/pi-base:{revision}"),
        ],
    };
    let response = broker(cfg)
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/agent/run")
                .header("x-internal-token", "local-test-token")
                .header("content-type", "application/json")
                .body(Body::from("{}"))
                .unwrap(),
        )
        .await
        .unwrap();
    let frames = tokio::time::timeout(
        Duration::from_secs(150),
        to_bytes(response.into_body(), 1024 * 1024),
    )
    .await
    .unwrap()
    .unwrap();
    let result = std::str::from_utf8(&frames).unwrap();
    assert!(
        result.contains("the agent image failed to build"),
        "{result}"
    );
    assert!(!result.contains("fixture-private-token"));
    assert!(
        registry.authorized_manifests.load(Ordering::Relaxed) > 0,
        "Docker never obtained the authenticated manifest"
    );
    for tag in [tag, alias] {
        let output = Command::new("docker")
            .args([
                "--host",
                &cleanup.host,
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                &tag,
            ])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "missing source tag or alias: {tag}"
        );
        assert_eq!(String::from_utf8_lossy(&output.stdout).trim(), config_id);
    }
    server.abort();
}
