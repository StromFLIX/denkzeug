//! Source-tagged image acquisition and readiness. Mutations use only local policy.
use std::{
    io,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};

use axum::{
    Json,
    body::Bytes,
    extract::State,
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
};
use serde_json::{Value, json};
use tokio::{
    sync::mpsc,
    time::{Instant, sleep},
};

use crate::{Broker, Fingerprints, packages::InstallerError};

pub struct ImageConfig {
    pub contexts: PathBuf,
    pub fingerprints: Fingerprints,
    pub local_prefix: String,
    pub registry_prefix: String,
    pub base_alias: String,
    pub wait_seconds: u64,
}

impl ImageConfig {
    pub fn agent_tags(&self) -> std::collections::BTreeMap<String, String> {
        self.fingerprints
            .agent_sets
            .iter()
            .map(|(name, hash)| {
                (
                    name.clone(),
                    format!("{}agent-{name}:{hash}", self.local_prefix),
                )
            })
            .collect()
    }
}

#[derive(Default, Clone)]
pub(crate) struct BuildState {
    pub(crate) status: &'static str,
    pub(crate) error: Option<&'static str>,
    images: std::collections::BTreeMap<String, String>,
}
const API: &str = "/v1.45";
const IMAGE_OP: Duration = Duration::from_secs(900);
const BODY_LIMIT: usize = 16 * 1024 * 1024;

impl Broker {
    pub(crate) fn image_build_configured(&self) -> bool {
        self.config.image_build.is_some()
    }

    pub(crate) async fn has_image(&self, tag: &str) -> bool {
        self.docker_get(&format!("{API}/images/{tag}/json"), 4 * 1024 * 1024)
            .await
            .is_ok()
    }

    pub(crate) async fn images_snapshot(&self) -> Value {
        let Some(config) = &self.config.image_build else {
            return json!({"status":"pending","images":{},"missing":[],"error":null});
        };
        let state = self.build_state.lock().unwrap().clone();
        let images = state.images;
        let mut missing = Vec::new();
        let mut status = state.status;
        let mut error = state.error;
        if status == "ready" {
            for tag in images.values().chain([&config.base_alias]) {
                if !self.has_image(tag).await {
                    missing.push(tag.clone());
                }
            }
            if !missing.is_empty() {
                status = "missing";
            } else if !self.artcraft_ready().await {
                status = "missing";
                error = Some(
                    "Default creative-app artifacts are not ready; preparing verified volumes",
                );
            }
        }
        json!({"status":if status.is_empty(){"pending"}else{status},"images":images,"missing":missing,"error":error})
    }

    pub(crate) fn start_build(self: &Arc<Self>, force: bool) -> bool {
        if self.config.image_build.is_none() {
            return false;
        }
        {
            let mut state = self.build_state.lock().unwrap();
            if state.status == "building" {
                return false;
            }
            state.status = "building";
            state.error = None;
        }
        let broker = self.clone();
        tokio::spawn(async move {
            let mut completed = BuildOnDrop {
                broker: broker.clone(),
                completed: false,
            };
            let result = broker.ensure_images(force).await;
            let mut state = broker.build_state.lock().unwrap();
            match result {
                Ok(images) => {
                    state.status = "ready";
                    state.error = None;
                    state.images = images;
                }
                Err(err) => {
                    state.status = "failed";
                    state.error = Some(match err {
                        InstallerError::Invalid(error) => error,
                        InstallerError::Daemon => {
                            "Agent image or artifact preparation failed; see Docker status and broker configuration"
                        }
                    });
                }
            }
            completed.completed = true;
        });
        true
    }

    async fn ensure_images(
        &self,
        force: bool,
    ) -> Result<std::collections::BTreeMap<String, String>, InstallerError> {
        let cfg = self.config.image_build.as_ref().ok_or(())?;
        let base = format!("{}base:{}", cfg.local_prefix, cfg.fingerprints.base);
        if force || (!self.has_image(&base).await && self.pull_pinned(&base).await.is_err()) {
            self.build_context(&cfg.contexts.join("pi-base"), &base, None)
                .await?;
        }
        self.tag_image(&base, &cfg.base_alias).await?;
        let mut tags = cfg.agent_tags();
        for (name, tag) in &tags {
            if force || (!self.has_image(tag).await && self.pull_pinned(tag).await.is_err()) {
                self.build_context(
                    &cfg.contexts.join("agent-sets").join(name),
                    tag,
                    Some(&base),
                )
                .await?;
            }
        }
        self.prepare_artcraft_defaults(tags.get("default").ok_or(())?)
            .await?;
        tags.insert("base".into(), base);
        Ok(tags)
    }

    async fn tag_image(&self, from: &str, to: &str) -> Result<(), ()> {
        let (repository, tag) = to.rsplit_once(':').ok_or(())?;
        let flags = form_urlencoded::Serializer::new(String::new())
            .append_pair("repo", repository)
            .append_pair("tag", tag)
            .finish();
        let (status, _) = self
            .docker_request(
                Method::POST,
                &format!("{API}/images/{from}/tag?{flags}"),
                1024,
            )
            .await?;
        (status == StatusCode::CREATED).then_some(()).ok_or(())
    }

    async fn pull_pinned(&self, tag: &str) -> Result<(), ()> {
        let cfg = self.config.image_build.as_ref().ok_or(())?;
        if cfg.registry_prefix.is_empty() {
            return Err(());
        }
        let remote = format!(
            "{}{}",
            cfg.registry_prefix,
            tag.strip_prefix(&cfg.local_prefix).ok_or(())?
        );
        let (repo, version) = remote.rsplit_once(':').ok_or(())?;
        let flags = form_urlencoded::Serializer::new(String::new())
            .append_pair("fromImage", repo)
            .append_pair("tag", version)
            .finish();
        let headers = crate::registry::pull_headers(
            self.config.docker_config.as_deref(),
            &remote,
            Default::default(),
        )
        .await?;
        let (status, body) = self
            .docker_payload_with_headers(
                Method::POST,
                &format!("{API}/images/create?{flags}"),
                Bytes::new(),
                headers,
                BODY_LIMIT,
                IMAGE_OP,
            )
            .await?;
        if status != StatusCode::OK || !image_messages_succeeded(&body) {
            return Err(());
        }
        self.tag_image(&remote, tag).await
    }

    async fn build_context(&self, path: &Path, tag: &str, base: Option<&str>) -> Result<(), ()> {
        let path = path.to_owned();
        let tar = tokio::task::spawn_blocking(move || context_tar(&path))
            .await
            .map_err(|_| ())?
            .map_err(|_| ())?;
        let args = base
            .map_or_else(|| json!({}), |value| json!({"BASE_IMAGE":value}))
            .to_string();
        let flags = form_urlencoded::Serializer::new(String::new())
            .append_pair("t", tag)
            .append_pair("version", "1")
            .append_pair("pull", "false")
            .append_pair("rm", "true")
            .append_pair("buildargs", &args)
            .finish();
        let headers = crate::registry::build_headers(
            self.config.docker_config.as_deref(),
            crate::request_headers(Some("application/x-tar"))?,
        )
        .await?;
        let (status, body) = self
            .docker_payload_with_headers(
                Method::POST,
                &format!("{API}/build?{flags}"),
                tar.into(),
                headers,
                BODY_LIMIT,
                IMAGE_OP,
            )
            .await?;
        if status != StatusCode::OK
            || !image_messages_succeeded(&body)
            || !self.has_image(tag).await
        {
            return Err(());
        }
        Ok(())
    }

    pub(crate) async fn ensure_image_for_turn(
        self: &Arc<Self>,
        image: &str,
        output: &mpsc::Sender<String>,
    ) -> Result<(), String> {
        let Some(cfg) = &self.config.image_build else {
            return self
                .has_image(image)
                .await
                .then_some(())
                .ok_or("The pinned agent image is unavailable".to_owned());
        };
        if self.images_snapshot().await["status"] == "ready" {
            return Ok(());
        }
        self.start_build(false);
        let start = Instant::now();
        let messages = [
            "Setting things up",
            "Building the agent image",
            "Installing the agent's tools",
            "Almost ready",
        ];
        loop {
            let state = self.build_state.lock().unwrap().clone();
            match state.status {
                "ready" => {
                    return if self.has_image(image).await && self.artcraft_ready().await {
                        Ok(())
                    } else {
                        Err(
                            "The pinned agent image or bundled artifacts are unavailable"
                                .to_owned(),
                        )
                    };
                }
                "failed" => {
                    return Err(format!(
                        "the agent image failed to build: {}",
                        state.error.unwrap_or("unknown error")
                    ));
                }
                _ => (),
            }
            let seconds = start.elapsed().as_secs();
            if seconds >= cfg.wait_seconds {
                return Err(format!("{image} was still building after {seconds}s"));
            }
            let status = json!({"type":"status","state":"building","message":messages[((seconds/20) as usize).min(3)],"seconds":seconds});
            if output.send(format!("{status}\n")).await.is_err() {
                return Ok(());
            }
            sleep(Duration::from_secs(3)).await;
        }
    }
}

struct BuildOnDrop {
    broker: Arc<Broker>,
    completed: bool,
}
impl Drop for BuildOnDrop {
    fn drop(&mut self) {
        if !self.completed {
            let mut state = self.broker.build_state.lock().unwrap();
            state.status = "failed";
            state.error = Some("Image build task ended unexpectedly");
        }
    }
}

pub(crate) fn image_messages_succeeded(body: &[u8]) -> bool {
    body.split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
        .try_fold(false, |_, line| {
            let value: Value = serde_json::from_slice(line).ok()?;
            if !value["error"].is_null() || !value["errorDetail"].is_null() {
                None
            } else {
                Some(true)
            }
        })
        == Some(true)
}

fn context_tar(root: &Path) -> io::Result<Vec<u8>> {
    fn append(builder: &mut tar::Builder<Vec<u8>>, dir: &Path, root: &Path) -> io::Result<()> {
        let mut entries = std::fs::read_dir(dir)?.collect::<Result<Vec<_>, _>>()?;
        entries.sort_by_key(|entry| entry.file_name());
        for item in entries {
            if matches!(item.file_name().to_str(), Some("target" | "__pycache__")) {
                continue;
            }
            let path = item.path();
            let kind = item.file_type()?;
            if kind.is_symlink() {
                return Err(io::Error::other(
                    "Symlinks not allowed in bundled Docker build contexts",
                ));
            }
            let relative = path.strip_prefix(root).map_err(io::Error::other)?;
            if kind.is_dir() {
                builder.append_dir(relative, &path)?;
                append(builder, &path, root)?;
            } else if kind.is_file()
                && !matches!(
                    path.extension().and_then(|v| v.to_str()),
                    Some("pyc" | "pyo")
                )
            {
                builder.append_path_with_name(&path, relative)?;
            }
        }
        Ok(())
    }
    let mut tar = tar::Builder::new(Vec::new());
    append(&mut tar, root, root)?;
    tar.into_inner()
}

pub(super) async fn agent_sets(State(broker): State<Arc<Broker>>) -> Json<Value> {
    let mut agent_sets = Vec::new();
    if let Some(config) = &broker.config.runtime {
        let ready = broker.images_snapshot().await["status"] == "ready";
        for (name, tag) in &config.agent_sets {
            agent_sets.push(json!({"name":name,"image":tag,"ready":if broker.image_build_configured(){ready}else{broker.has_image(tag).await}}));
        }
    }
    Json(json!({"agent_sets":agent_sets}))
}

pub(super) async fn rebuild(State(broker): State<Arc<Broker>>) -> Response {
    if !broker.image_build_configured() {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"detail":"Image building is not configured"})),
        )
            .into_response();
    }
    let status = if broker.start_build(true) {
        "building"
    } else {
        "already building"
    };
    Json(json!({"ok":true,"status":status})).into_response()
}
