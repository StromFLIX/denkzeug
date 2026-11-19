//! Docker Engine transport, selected only by local operator configuration.
use std::{
    ffi::OsString,
    fs::File,
    io::{self, Read},
    path::{Path, PathBuf},
    pin::Pin,
    process::Stdio,
    task::{Context, Poll},
    time::Duration,
};

use axum::{
    body::{Body, Bytes},
    http::{
        HeaderMap, Method, Response,
        header::{CONTENT_LENGTH, HOST},
    },
};
use http_body_util::BodyExt;
use hyper_util::rt::TokioIo;
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::{
    io::{AsyncRead, AsyncWrite, ReadBuf},
    process::{Child, ChildStdin, ChildStdout, Command},
};

type Reply = Response<Body>;
const FILE_LIMIT: u64 = 1024 * 1024;

pub(crate) enum Payload {
    Bytes(Bytes),
    File(File, u64),
}

pub enum DockerEndpoint {
    Unix(PathBuf),
    Web {
        url: reqwest::Url,
        tls: Option<PathBuf>,
        require_identity: bool,
    },
    Ssh {
        target: String,
        port: Option<u16>,
    },
}

#[derive(Debug)]
pub struct EndpointError(pub &'static str);
impl std::fmt::Display for EndpointError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for EndpointError {}

impl DockerEndpoint {
    /// A broker-specific socket override wins, then Docker's host environment,
    /// then the selected CLI context, then the platform's default Unix socket.
    pub fn from_env(socket_override: Option<PathBuf>) -> Result<Self, EndpointError> {
        Self::resolve(socket_override, |key| std::env::var_os(key))
    }

    fn resolve(
        socket_override: Option<PathBuf>,
        get: impl Fn(&str) -> Option<OsString>,
    ) -> Result<Self, EndpointError> {
        if let Some(path) = socket_override {
            return Self::unix(path);
        }
        let env = |key| get(key).filter(|v| !v.is_empty());
        let root = env("DOCKER_CONFIG")
            .map(PathBuf::from)
            .or_else(|| env("HOME").map(|home| PathBuf::from(home).join(".docker")));
        if let Some(host) = env("DOCKER_HOST") {
            let host = host
                .into_string()
                .map_err(|_| EndpointError("invalid DOCKER_HOST"))?;
            let certs = env("DOCKER_CERT_PATH").map(PathBuf::from);
            let tls = get("DOCKER_TLS_VERIFY").is_some_and(|v| !v.is_empty()) || certs.is_some();
            let tls_path = if tls {
                Some(
                    certs
                        .or(root)
                        .ok_or(EndpointError("Docker TLS directory is unavailable"))?,
                )
            } else {
                None
            };
            return Self::parse(&host, tls_path, tls);
        }
        let name = if let Some(name) = env("DOCKER_CONTEXT") {
            Some(
                name.into_string()
                    .map_err(|_| EndpointError("invalid Docker context name"))?,
            )
        } else if let Some(root) = &root {
            match read_small(&root.join("config.json")) {
                Ok(bytes) => parse_json(&bytes)?["currentContext"]
                    .as_str()
                    .map(str::to_owned),
                Err(err) if err.kind() == io::ErrorKind::NotFound => None,
                Err(_) => return Err(EndpointError("cannot read Docker context settings")),
            }
        } else {
            None
        };
        let Some(name) = name.filter(|name| !name.is_empty() && name != "default") else {
            return Self::unix("/var/run/docker.sock".into());
        };
        if name.len() > 255 || name.chars().any(char::is_control) {
            return Err(EndpointError("invalid Docker context name"));
        }
        let root = root.ok_or(EndpointError("Docker context directory is unavailable"))?;
        let key = format!("{:x}", Sha256::digest(name.as_bytes()));
        let meta = read_small(&root.join("contexts/meta").join(&key).join("meta.json"))
            .map_err(|_| EndpointError("Docker context metadata is unavailable"))?;
        let meta = parse_json(&meta)?;
        if meta["Name"] != name {
            return Err(EndpointError("Docker context identity does not match"));
        }
        let docker = &meta["Endpoints"]["docker"];
        if docker["SkipTLSVerify"] == true {
            return Err(EndpointError(
                "unverified Docker TLS contexts are not supported",
            ));
        }
        let host = docker["Host"]
            .as_str()
            .ok_or(EndpointError("Docker context has no host"))?;
        let tls = root.join("contexts/tls").join(key).join("docker");
        Self::parse(host, tls.is_dir().then_some(tls), false)
    }

    fn unix(path: PathBuf) -> Result<Self, EndpointError> {
        if !path.is_absolute() {
            return Err(EndpointError("Docker Unix socket must be an absolute path"));
        }
        Ok(Self::Unix(path))
    }

    fn parse(
        host: &str,
        tls: Option<PathBuf>,
        require_identity: bool,
    ) -> Result<Self, EndpointError> {
        if let Some(path) = host.strip_prefix("unix://") {
            return Self::unix(path.into());
        }
        if let Some(host) = host.strip_prefix("ssh://") {
            let url = reqwest::Url::parse(&format!("ssh://{host}"))
                .map_err(|_| EndpointError("invalid Docker SSH endpoint"))?;
            if url.query().is_some()
                || url.fragment().is_some()
                || url.password().is_some()
                || !matches!(url.path(), "" | "/")
            {
                return Err(EndpointError("invalid Docker SSH endpoint"));
            }
            let host = url
                .host_str()
                .filter(|s| ssh_part(s))
                .ok_or(EndpointError("invalid Docker SSH host"))?;
            let username = url.username();
            if !username.is_empty() && !ssh_part(username) {
                return Err(EndpointError("invalid Docker SSH account"));
            }
            let target = if username.is_empty() {
                host.to_owned()
            } else {
                format!("{username}@{host}")
            };
            return Ok(Self::Ssh {
                target,
                port: url.port(),
            });
        }
        let url = if let Some(host) = host.strip_prefix("tcp://") {
            format!("{}://{host}", if tls.is_some() { "https" } else { "http" })
        } else {
            host.to_owned()
        };
        let mut url =
            reqwest::Url::parse(&url).map_err(|_| EndpointError("invalid Docker TCP endpoint"))?;
        if !matches!(url.scheme(), "http" | "https")
            || url.host().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err(EndpointError("invalid Docker TCP endpoint"));
        }
        if tls.is_some() {
            url.set_scheme("https")
                .map_err(|_| EndpointError("invalid Docker TLS endpoint"))?;
        }
        // Unix and SSH are authenticated by the operating system. Never send
        // registry credentials or privileged mutations to an unauthenticated remote TCP origin.
        if url.scheme() == "http"
            && !url.host_str().is_some_and(|host| {
                host == "localhost"
                    || host
                        .trim_matches(['[', ']'])
                        .parse::<std::net::IpAddr>()
                        .is_ok_and(|address| address.is_loopback())
            })
        {
            return Err(EndpointError("remote Docker TCP requires verified TLS"));
        }
        // Docker's default for bare tcp:// is 2375/2376, not HTTP's default port.
        if host.starts_with("tcp://") && url.port().is_none() {
            url.set_port(Some(if url.scheme() == "https" { 2376 } else { 2375 }))
                .map_err(|_| EndpointError("invalid Docker TCP port"))?;
        }
        Ok(Self::Web {
            url,
            tls,
            require_identity,
        })
    }
}

fn ssh_part(value: &str) -> bool {
    !value.is_empty()
        && !value.starts_with('-')
        && value.bytes().all(|b| {
            b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_' | b':' | b'[' | b']')
        })
}

fn read_small(path: &Path) -> io::Result<Vec<u8>> {
    let mut buffer = Vec::new();
    File::open(path)?
        .take(FILE_LIMIT + 1)
        .read_to_end(&mut buffer)?;
    if buffer.len() as u64 > FILE_LIMIT {
        return Err(io::Error::other("Docker configuration is too large"));
    }
    Ok(buffer)
}
fn parse_json(bytes: &[u8]) -> Result<Value, EndpointError> {
    serde_json::from_slice(bytes).map_err(|_| EndpointError("invalid Docker context JSON"))
}

pub(crate) enum Engine {
    Http {
        client: reqwest::Client,
        base: String,
    },
    Ssh {
        target: String,
        port: Option<u16>,
    },
}

impl Engine {
    pub(crate) fn new(endpoint: DockerEndpoint, deadline: Duration) -> Result<Self, EndpointError> {
        let builder = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .http1_only()
            .connect_timeout(deadline);
        let (builder, base) = match endpoint {
            DockerEndpoint::Unix(path) => {
                (builder.unix_socket(path), "http://localhost".to_owned())
            }
            DockerEndpoint::Web {
                url,
                tls,
                require_identity,
            } => {
                let mut builder = builder;
                if let Some(path) = tls {
                    let ca = read_small(&path.join("ca.pem"));
                    match ca {
                        Ok(ca) => {
                            let certs = reqwest::Certificate::from_pem_bundle(&ca)
                                .map_err(|_| EndpointError("invalid Docker CA certificate"))?;
                            if certs.is_empty() {
                                return Err(EndpointError("Docker CA certificate is empty"));
                            }
                            builder = builder.tls_built_in_root_certs(false);
                            for cert in certs {
                                builder = builder.add_root_certificate(cert);
                            }
                        }
                        Err(err) if err.kind() == io::ErrorKind::NotFound && !require_identity => {}
                        Err(_) => {
                            return Err(EndpointError("Docker CA certificate is unavailable"));
                        }
                    }
                    let cert = read_small(&path.join("cert.pem"));
                    let key = read_small(&path.join("key.pem"));
                    match (cert, key) {
                        (Ok(mut cert), Ok(key)) => {
                            cert.push(b'\n');
                            cert.extend(key);
                            builder = builder
                                .identity(reqwest::Identity::from_pem(&cert).map_err(|_| {
                                    EndpointError("invalid Docker client identity")
                                })?);
                        }
                        (Err(c), Err(k))
                            if !require_identity
                                && c.kind() == io::ErrorKind::NotFound
                                && k.kind() == io::ErrorKind::NotFound => {}
                        _ => {
                            return Err(EndpointError(
                                "Docker client certificate/key pair is unavailable",
                            ));
                        }
                    }
                }
                (builder, url.as_str().trim_end_matches('/').to_owned())
            }
            DockerEndpoint::Ssh { target, port } => return Ok(Self::Ssh { target, port }),
        };
        let client = builder
            .build()
            .map_err(|_| EndpointError("cannot initialize Docker Engine transport"))?;
        Ok(Self::Http { client, base })
    }

    pub(crate) async fn send(
        &self,
        method: Method,
        path: &str,
        mut headers: HeaderMap,
        payload: Payload,
    ) -> Result<Reply, ()> {
        if !path.starts_with('/') || path.starts_with("//") {
            return Err(());
        }
        match self {
            Engine::Http { client, base } => {
                let body = match payload {
                    Payload::Bytes(data) => reqwest::Body::from(data),
                    Payload::File(file, size) => {
                        headers.insert(CONTENT_LENGTH, size.into());
                        reqwest::Body::wrap_stream(tokio_util::io::ReaderStream::new(
                            tokio::fs::File::from_std(file),
                        ))
                    }
                };
                let res = client
                    .request(method, format!("{base}{path}"))
                    .headers(headers)
                    .body(body)
                    .send()
                    .await
                    .map_err(|_| ())?;
                let status = res.status();
                let mut reply = Response::new(Body::from_stream(res.bytes_stream()));
                *reply.status_mut() = status;
                Ok(reply)
            }
            Engine::Ssh { target, port } => {
                let mut cmd = Command::new("ssh");
                cmd.args([
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-T",
                ]);
                if let Some(port) = port {
                    cmd.args(["-p", &port.to_string()]);
                }
                let mut child = cmd
                    .arg(target)
                    .args(["docker", "system", "dial-stdio"])
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::null())
                    .kill_on_drop(true)
                    .spawn()
                    .map_err(|_| ())?;
                let channel = SshPipe {
                    stdin: child.stdin.take().ok_or(())?,
                    stdout: child.stdout.take().ok_or(())?,
                    _child: child,
                };
                let (mut client, connection) =
                    hyper::client::conn::http1::handshake(TokioIo::new(channel))
                        .await
                        .map_err(|_| ())?;
                tokio::spawn(async move {
                    let _ = connection.await;
                });
                headers.insert(HOST, "localhost".parse().map_err(|_| ())?);
                headers.insert("connection", "close".parse().map_err(|_| ())?);
                let body = match payload {
                    Payload::Bytes(data) => http_body_util::Full::new(data)
                        .map_err(|never| match never {})
                        .boxed_unsync(),
                    Payload::File(file, size) => {
                        use futures_util::StreamExt;
                        headers.insert(CONTENT_LENGTH, size.into());
                        let frames =
                            tokio_util::io::ReaderStream::new(tokio::fs::File::from_std(file))
                                .map(|res| res.map(hyper::body::Frame::data));
                        http_body_util::StreamBody::new(frames).boxed_unsync()
                    }
                };
                let mut builder = hyper::Request::builder().method(method).uri(path);
                *builder.headers_mut().ok_or(())? = headers;
                let res = client
                    .send_request(builder.body(body).map_err(|_| ())?)
                    .await
                    .map_err(|_| ())?;
                Ok(res.map(Body::new))
            }
        }
    }
}

struct SshPipe {
    stdin: ChildStdin,
    stdout: ChildStdout,
    _child: Child,
}
impl AsyncRead for SshPipe {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stdout).poll_read(cx, buffer)
    }
}
impl AsyncWrite for SshPipe {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.stdin).poll_write(cx, buffer)
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stdin).poll_flush(cx)
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.stdin).poll_shutdown(cx)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        Router,
        body::to_bytes,
        http::{Request, StatusCode},
        routing::get,
    };
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
    };
    use tower::ServiceExt;

    #[test]
    fn env_and_cli_context_precedence_use_pinned_metadata_and_fail_closed() {
        let root = tempfile::tempdir().unwrap();
        let name = "dev-context";
        let digest = format!("{:x}", Sha256::digest(name));
        let meta = root.path().join("contexts/meta").join(digest);
        std::fs::create_dir_all(&meta).unwrap();
        std::fs::write(
            root.path().join("config.json"),
            format!(r#"{{"currentContext":"{name}"}}"#),
        )
        .unwrap();
        let data = |host: &str, skip: bool| {
            serde_json::json!({"Name":name,"Endpoints":{"docker":{"Host":host,"SkipTLSVerify":skip}}}).to_string()
        };
        std::fs::write(
            meta.join("meta.json"),
            data("ssh://fixture@example.invalid:2222", false),
        )
        .unwrap();
        let vars = |key: &str| match key {
            "DOCKER_CONFIG" => Some(root.path().as_os_str().to_owned()),
            _ => None,
        };
        assert!(
            matches!(DockerEndpoint::resolve(None,vars),Ok(DockerEndpoint::Ssh{target,port:Some(2222)}) if target=="fixture@example.invalid")
        );
        let host = |key: &str| {
            if key == "DOCKER_HOST" {
                Some("unix:///tmp/engine.sock".into())
            } else {
                vars(key)
            }
        };
        assert!(
            matches!(DockerEndpoint::resolve(None,host),Ok(DockerEndpoint::Unix(path)) if path==Path::new("/tmp/engine.sock"))
        );
        assert!(
            matches!(DockerEndpoint::resolve(Some("/tmp/override.sock".into()),host),Ok(DockerEndpoint::Unix(path)) if path==Path::new("/tmp/override.sock"))
        );
        std::fs::write(
            meta.join("meta.json"),
            data("https://example.invalid:2376", true),
        )
        .unwrap();
        assert!(DockerEndpoint::resolve(None, vars).is_err());
        assert!(DockerEndpoint::parse("tcp://example.invalid:2375", None, false).is_err());
        assert!(DockerEndpoint::parse("ssh://-oProxyCommand@local.invalid", None, false).is_err());
    }

    #[tokio::test]
    async fn negotiates_engine_api_over_loopback_tcp_instead_of_using_a_socket() {
        let server = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = server.local_addr().unwrap();
        let daemon = Router::new()
            .route(
                "/version",
                get(|| async { axum::Json(serde_json::json!({"ApiVersion":"1.47"})) }),
            )
            .route("/v1.47/_ping", get(|| async { "OK" }));
        let server = tokio::spawn(async move { axum::serve(server, daemon).await.unwrap() });
        let endpoint = DockerEndpoint::parse(&format!("tcp://{addr}"), None, false).unwrap();
        let app = crate::broker(
            crate::Config::for_endpoint(endpoint, "synthetic", "volume", Duration::from_secs(2))
                .unwrap()
                .with_api_negotiation(),
        );
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/healthz")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let state: Value =
            serde_json::from_slice(&to_bytes(response.into_body(), 1024).await.unwrap()).unwrap();
        assert_eq!(state["docker"], true);
        server.abort();
    }

    #[tokio::test]
    async fn verified_docker_tls_uses_the_client_identity_and_never_ignores_hostname() {
        use rcgen::{
            BasicConstraints, CertificateParams, CertifiedIssuer, ExtendedKeyUsagePurpose, IsCa,
            KeyPair,
        };
        use std::sync::Arc;
        use tokio_rustls::{
            TlsAcceptor,
            rustls::{
                RootCertStore, ServerConfig,
                pki_types::{PrivateKeyDer, PrivatePkcs8KeyDer},
                server::WebPkiClientVerifier,
            },
        };
        let mut ca_params = CertificateParams::new(Vec::new()).unwrap();
        ca_params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        let ca = CertifiedIssuer::self_signed(ca_params, KeyPair::generate().unwrap()).unwrap();
        let server_key = KeyPair::generate().unwrap();
        let mut server_params = CertificateParams::new(vec!["localhost".to_owned()]).unwrap();
        server_params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
        let server_cert = server_params.signed_by(&server_key, &ca).unwrap();
        let client_key = KeyPair::generate().unwrap();
        let mut client_params =
            CertificateParams::new(vec!["broker.fixture.invalid".to_owned()]).unwrap();
        client_params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ClientAuth];
        let client_cert = client_params.signed_by(&client_key, &ca).unwrap();
        let secrets = tempfile::tempdir().unwrap();
        std::fs::write(secrets.path().join("ca.pem"), ca.pem()).unwrap();
        std::fs::write(secrets.path().join("cert.pem"), client_cert.pem()).unwrap();
        std::fs::write(secrets.path().join("key.pem"), client_key.serialize_pem()).unwrap();
        let mut roots = RootCertStore::empty();
        roots.add(ca.der().clone()).unwrap();
        let verifier = WebPkiClientVerifier::builder(Arc::new(roots))
            .build()
            .unwrap();
        let tls = ServerConfig::builder()
            .with_client_cert_verifier(verifier)
            .with_single_cert(
                vec![server_cert.der().clone()],
                PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(server_key.serialize_der())),
            )
            .unwrap();
        let acceptor = TlsAcceptor::from(Arc::new(tls));
        let socket = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = socket.local_addr().unwrap().port();
        let server = tokio::spawn(async move {
            for i in 0..2 {
                let (stream, _) = socket.accept().await.unwrap();
                let handshake = acceptor.accept(stream).await;
                if i == 1 {
                    assert!(handshake.is_err());
                    continue;
                }
                let mut stream = handshake.unwrap();
                let mut request = [0; 512];
                let n = stream.read(&mut request).await.unwrap();
                assert!(request[..n].starts_with(b"GET /_ping HTTP/1.1"));
                assert!(stream.get_ref().1.peer_certificates().is_some());
                stream
                    .write_all(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK",
                    )
                    .await
                    .unwrap();
                stream.shutdown().await.unwrap();
            }
        });
        let load = |host: &str| {
            Engine::new(
                DockerEndpoint::parse(
                    &format!("tcp://{host}:{port}"),
                    Some(secrets.path().to_owned()),
                    true,
                )
                .unwrap(),
                Duration::from_secs(2),
            )
            .unwrap()
        };
        let reply = load("localhost")
            .send(
                Method::GET,
                "/_ping",
                HeaderMap::new(),
                Payload::Bytes(Bytes::new()),
            )
            .await
            .unwrap();
        assert_eq!(to_bytes(reply.into_body(), 16).await.unwrap(), "OK");
        let fail = load("127.0.0.1")
            .send(
                Method::GET,
                "/_ping",
                HeaderMap::new(),
                Payload::Bytes(Bytes::new()),
            )
            .await;
        assert!(fail.is_err());
        tokio::time::timeout(Duration::from_secs(2), server)
            .await
            .unwrap()
            .unwrap();
    }
}
