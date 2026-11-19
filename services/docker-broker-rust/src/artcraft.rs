//! Fixed ArtCraft policy: download only pinned GitHub assets, verify bytes,
//! stage via Docker, and run the existing offline verifier in the Pi image.
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    io::{Seek, SeekFrom},
    sync::{Arc, LazyLock},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use axum::{
    Json,
    extract::State,
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
};
use futures_util::{Stream, StreamExt, stream};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::pin::Pin;
use tokio::io::AsyncWriteExt;

use crate::{
    Broker,
    packages::{InstallerError, short_hash, valid_revision},
    turns::RecoveryEngine,
    valid_docker_id,
};

const API: &str = "/v1.45";
const DEPLOY: &str = "nautionette.deployment";
const LABEL: &str = "nautionette.artcraft";
const HELPER: &str = "nautionette.artcraft.installer";
const MAX_ARCHIVE: usize = 256 * 1024 * 1024;
const OFFLINE: &[u8] =
    include_bytes!("../../docker-broker/nautionette_docker_broker/artcraft_offline.py");
static CATALOG: LazyLock<BTreeMap<String, Value>> = LazyLock::new(|| {
    serde_json::from_str(include_str!(
        "../../../images/pi-base/artcraft/catalog.json"
    ))
    .expect("checked-in ArtCraft catalog")
});
static VERSION: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:-[a-zA-Z0-9][a-zA-Z0-9.-]{0,40})?$").unwrap()
});

#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Release {
    app: String,
    version: String,
    arch: String,
    sha256: String,
}

impl Release {
    fn parse(value: Value) -> Result<Self, &'static str> {
        let release: Self =
            serde_json::from_value(value).map_err(|_| "Invalid ArtCraft release specification")?;
        if !CATALOG.contains_key(&release.app) {
            return Err("Unknown ArtCraft app");
        }
        if !VERSION.is_match(&release.version) {
            return Err("Use an exact ArtCraft release version");
        }
        if !matches!(std::env::consts::ARCH, "x86_64" | "aarch64")
            || release.arch != std::env::consts::ARCH
        {
            return Err("ArtCraft release architecture does not match this server");
        }
        if !valid_docker_id(&release.sha256) {
            return Err("ArtCraft releases require a SHA-256 digest");
        }
        Ok(release)
    }
    fn id(&self) -> String {
        // Python json.dumps(sort_keys=True) uses spaces after separators.
        let canonical = format!(
            r#"{{"app": "{}", "arch": "{}", "sha256": "{}", "version": "{}"}}"#,
            self.app, self.arch, self.sha256, self.version
        );
        short_hash(canonical.as_bytes(), 32)
    }
    fn name(&self) -> String {
        let cli = if CATALOG[&self.app]["cli_archive"] == true {
            "-cli"
        } else {
            ""
        };
        format!(
            "{}{cli}-{}-linux-{}.tar.gz",
            self.app, self.version, self.arch
        )
    }
    fn labels(&self, id: &str, namespace: &str) -> Value {
        json!({(DEPLOY):namespace,(LABEL):id,"nautionette.artcraft.app":self.app,"nautionette.artcraft.version":self.version,"nautionette.artcraft.arch":self.arch,"nautionette.artcraft.sha256":self.sha256})
    }
}

fn defaults() -> Result<Vec<Release>, &'static str> {
    let arch = std::env::consts::ARCH;
    CATALOG.iter().map(|(app,entry)|Release::parse(json!({"app":app,"version":entry["version"],"arch":arch,"sha256":entry["sha256"][arch]}))).collect()
}
fn volume(namespace: &str, id: &str) -> String {
    format!(
        "nautionette-artcraft-{}-{id}",
        short_hash(namespace.as_bytes(), 12)
    )
}
fn labels_match(actual: &Value, expected: &Value) -> bool {
    expected
        .as_object()
        .is_some_and(|labels| labels.iter().all(|(key, value)| actual[key] == *value))
}

pub(crate) fn selection(value: Value) -> Result<Vec<(String, Release)>, &'static str> {
    let items = value
        .as_array()
        .filter(|v| v.len() <= CATALOG.len())
        .ok_or("Invalid ArtCraft runtime selection")?;
    let mut seen = BTreeSet::new();
    items
        .iter()
        .map(|item| {
            if !item.as_object().is_some_and(|item| {
                item.keys()
                    .all(|key| matches!(key.as_str(), "app" | "release" | "installation_id"))
            }) {
                return Err("Invalid ArtCraft runtime entry");
            }
            let spec = Release::parse(item["release"].clone())?;
            if item["app"] != spec.app {
                return Err("ArtCraft app/release mismatch");
            }
            if !seen.insert(spec.app.clone()) {
                return Err("Duplicate ArtCraft runtime entry");
            }
            let id = match item.get("installation_id") {
                None => spec.id(),
                Some(value) => value
                    .as_str()
                    .filter(|id| valid_revision(id))
                    .ok_or("Invalid ArtCraft installation ID")?
                    .to_owned(),
            };
            Ok((id, spec))
        })
        .collect()
}

fn https_client() -> Result<reqwest::Client, InstallerError> {
    use reqwest::redirect::{Action, Policy};
    reqwest::Client::builder()
        .user_agent("Nautionette-ArtCraft")
        .connect_timeout(Duration::from_secs(30))
        .timeout(Duration::from_secs(60))
        .redirect(Policy::custom(|redirect| {
            let url = redirect.url();
            if redirect.previous().len() >= 5
                || url.scheme() != "https"
                || !matches!(
                    url.host_str(),
                    Some(
                        "github.com"
                            | "api.github.com"
                            | "release-assets.githubusercontent.com"
                            | "objects.githubusercontent.com"
                    )
                )
                || url.port().is_some()
            {
                return redirect.error("Unexpected ArtCraft asset redirect");
            }
            // Return the previous policy result only for these fixed origins.
            let action: Action = redirect.follow();
            action
        }))
        .build()
        .map_err(|_| InstallerError::Daemon)
}

struct AssetReply {
    status: u16,
    body: Pin<Box<dyn Stream<Item = Result<axum::body::Bytes, ()>> + Send>>,
}
trait AssetHttp: Sync {
    fn get(&self, url: &str, binary: bool) -> impl Future<Output = Result<AssetReply, ()>> + Send;
}
struct ReleaseClient(reqwest::Client);
impl AssetHttp for ReleaseClient {
    async fn get(&self, url: &str, binary: bool) -> Result<AssetReply, ()> {
        let mut request = self.0.get(url).header("User-Agent", "Nautionette-ArtCraft");
        if binary {
            request = request.header("Accept", "application/octet-stream");
        }
        let response = request.send().await.map_err(|_| ())?;
        Ok(AssetReply {
            status: response.status().as_u16(),
            body: Box::pin(response.bytes_stream().map(|chunk| chunk.map_err(|_| ()))),
        })
    }
}

async fn asset_api_url(client: &impl AssetHttp, spec: &Release) -> Result<String, InstallerError> {
    let base = format!(
        "https://api.github.com/repos/storytold/{}/releases",
        spec.app
    );
    let res = client
        .get(&format!("{base}/tags/v{}", spec.version), false)
        .await
        .map_err(|_| InstallerError::Daemon)?;
    if !(200..300).contains(&res.status) {
        return Err(InstallerError::Daemon);
    }
    let mut stream = res.body;
    let mut buffer = Vec::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|_| InstallerError::Daemon)?;
        if buffer.len() + chunk.len() > 2 * 1024 * 1024 {
            return Err(InstallerError::Invalid(
                "ArtCraft release metadata exceeds 2 MiB",
            ));
        }
        buffer.extend_from_slice(&chunk);
    }
    pinned_asset_api_url(spec, &buffer)
}

fn pinned_asset_api_url(spec: &Release, bytes: &[u8]) -> Result<String, InstallerError> {
    if bytes.len() > 2 * 1024 * 1024 {
        return Err(InstallerError::Invalid(
            "ArtCraft release metadata exceeds 2 MiB",
        ));
    }
    let base = format!(
        "https://api.github.com/repos/storytold/{}/releases",
        spec.app
    );
    let metadata: Value = serde_json::from_slice(bytes).map_err(|_| InstallerError::Daemon)?;
    if metadata["tag_name"] != format!("v{}", spec.version) {
        return Err(InstallerError::Invalid(
            "ArtCraft release metadata does not match the pinned version",
        ));
    }
    let expected = spec.name();
    let id = metadata["assets"]
        .as_array()
        .and_then(|assets| {
            assets.iter().find_map(|asset| {
                if asset["name"] == expected {
                    asset["id"].as_u64().filter(|id| *id > 0)
                } else {
                    None
                }
            })
        })
        .ok_or(InstallerError::Invalid(
            "Pinned ArtCraft release asset is unavailable",
        ))?;
    Ok(format!("{base}/assets/{id}"))
}

fn archive_url(spec: &Release) -> String {
    format!(
        "https://github.com/storytold/{}/releases/download/v{}/{}",
        spec.app,
        spec.version,
        spec.name()
    )
}

struct DownloadIntegrity {
    size: usize,
    digest: Sha256,
    limit: usize,
}
impl DownloadIntegrity {
    fn new(limit: usize) -> Self {
        Self {
            size: 0,
            digest: Sha256::new(),
            limit,
        }
    }
    fn push(&mut self, bytes: &[u8]) -> Result<(), InstallerError> {
        self.size = self
            .size
            .checked_add(bytes.len())
            .ok_or(InstallerError::Invalid("Release archive exceeds 256 MiB"))?;
        if self.size > self.limit {
            return Err(InstallerError::Invalid("Release archive exceeds 256 MiB"));
        }
        self.digest.update(bytes);
        Ok(())
    }
    fn verify(self, expected: &str) -> Result<(), InstallerError> {
        if format!("{:x}", self.digest.finalize()) != expected {
            return Err(InstallerError::Invalid("Release checksum mismatch"));
        }
        Ok(())
    }
}

async fn download(spec: &Release) -> Result<File, InstallerError> {
    download_using(spec, &ReleaseClient(https_client()?), true).await
}

async fn download_using(
    spec: &Release,
    client: &impl AssetHttp,
    delay: bool,
) -> Result<File, InstallerError> {
    let mut url = archive_url(spec);
    let mut use_api = false;
    for attempt in 0..3 {
        let result = async {
            let response = client
                .get(&url, use_api)
                .await
                .map_err(|_| InstallerError::Daemon)?;
            if matches!(response.status, 400 | 401 | 403 | 404 | 410) {
                return Err(InstallerError::Invalid(
                    "Pinned ArtCraft release archive is unavailable",
                ));
            }
            if !(200..300).contains(&response.status) {
                return Err(InstallerError::Daemon);
            }
            let file = tempfile::tempfile().map_err(|_| InstallerError::Daemon)?;
            let mut file = tokio::fs::File::from_std(file);
            let mut integrity = DownloadIntegrity::new(MAX_ARCHIVE);
            let mut stream = response.body;
            while let Some(chunk) = stream.next().await {
                let chunk = chunk.map_err(|_| InstallerError::Daemon)?;
                integrity.push(&chunk)?;
                file.write_all(&chunk)
                    .await
                    .map_err(|_| InstallerError::Daemon)?;
            }
            integrity.verify(&spec.sha256)?;
            file.flush().await.map_err(|_| InstallerError::Daemon)?;
            Ok(file.into_std().await)
        }
        .await;
        match result {
            Err(InstallerError::Daemon) if attempt < 2 => (),
            other => return other,
        }
        if !use_api {
            match asset_api_url(client, spec).await {
                Ok(fallback) => {
                    url = fallback;
                    use_api = true;
                }
                Err(InstallerError::Invalid(reason)) => {
                    return Err(InstallerError::Invalid(reason));
                }
                Err(InstallerError::Daemon) => (),
            }
        }
        if delay {
            tokio::time::sleep(Duration::from_secs(attempt + 1)).await;
        }
    }
    Err(InstallerError::Daemon)
}

async fn stage(spec: &Release) -> Result<File, InstallerError> {
    let release = download(spec).await?;
    tokio::task::spawn_blocking(move || archive_release(release))
        .await
        .map_err(|_| InstallerError::Daemon)?
        .map_err(|_| InstallerError::Daemon)
}

fn archive_release(mut release: File) -> std::io::Result<File> {
    release.seek(SeekFrom::Start(0))?;
    let mut tar = tar::Builder::new(tempfile::tempfile()?);
    let header = |size, mode| {
        let mut header = tar::Header::new_ustar();
        header.set_mode(mode);
        header.set_uid(0);
        header.set_gid(0);
        header.set_size(size);
        header.set_entry_type(tar::EntryType::Regular);
        header
    };
    let mut file = header(release.metadata()?.len(), 0o600);
    tar.append_data(&mut file, ".release.tar.gz", &mut release)?;
    let mut script = header(OFFLINE.len() as u64, 0o644);
    tar.append_data(&mut script, ".install.py", OFFLINE)?;
    let mut staging = tar.into_inner()?;
    staging.seek(SeekFrom::Start(0))?;
    Ok(staging)
}

impl Broker {
    async fn artcraft_volume(
        &self,
        id: &str,
        spec: &Release,
    ) -> Result<Option<String>, InstallerError> {
        let name = volume(&self.config.workflows_volume, id);
        let Some(info) = self.inspect_volume(&name).await? else {
            return Ok(None);
        };
        if !labels_match(
            &info["Labels"],
            &spec.labels(id, &self.config.workflows_volume),
        ) {
            return Err(InstallerError::Invalid(
                "ArtCraft artifact ownership/release mismatch",
            ));
        }
        Ok(Some(name))
    }

    async fn staged_release(&self, spec: &Release) -> Result<File, InstallerError> {
        #[cfg(test)]
        if let Some(bytes) = self.staged_artcraft_fixture.lock().unwrap().as_ref() {
            use std::io::Write;
            let mut file = tempfile::tempfile().map_err(|_| InstallerError::Daemon)?;
            file.write_all(bytes).map_err(|_| InstallerError::Daemon)?;
            file.seek(SeekFrom::Start(0))
                .map_err(|_| InstallerError::Daemon)?;
            return Ok(file);
        }
        stage(spec).await
    }

    async fn artcraft_helper(
        &self,
        id: &str,
        spec: &Release,
        image: &str,
        verify: bool,
    ) -> Result<(), InstallerError> {
        let staging = if verify {
            None
        } else {
            Some(self.staged_release(spec).await?)
        };
        let mut labels = spec.labels(id, &self.config.workflows_volume);
        labels[HELPER] = json!("true");
        let entry = if verify {
            json!([
                "python3",
                "/usr/local/lib/nautionette/artcraft/install.py",
                "--verify"
            ])
        } else {
            json!(["python3", "/artifact/.install.py"])
        };
        let body = json!({"Image":image,"Entrypoint":entry,"Env":[format!("ARTCRAFT_RELEASE={}",serde_json::to_string(spec).map_err(|_|())?)],"Labels":labels,
            "HostConfig":{"Mounts":[{"Type":"volume","Source":volume(&self.config.workflows_volume,id),"Target":"/artifact","ReadOnly":verify}],"NetworkMode":"none","ReadonlyRootfs":true,"Tmpfs":{"/tmp":"size=768m,exec,mode=1777"},
            "Memory":2147483648u64,"NanoCpus":1000000000,"PidsLimit":128,"CapDrop":["ALL"],"SecurityOpt":["no-new-privileges:true"],"LogConfig":{"Type":"json-file","Config":{"max-size":"1m","max-file":"1"}}}});
        let helper = self.create_helper(body).await?;
        let result = async {
            if let Some(file) = staging {
                let path = format!("{API}/containers/{helper}/archive?path=%2Fartifact");
                if self
                    .docker_file(Method::PUT, &path, file, Duration::from_secs(90))
                    .await?
                    != StatusCode::OK
                {
                    return Err(InstallerError::Invalid(
                        "Could not stage the verified ArtCraft archive",
                    ));
                }
            }
            let (exit, reply) = self.helper_output(&helper).await?;
            if exit != 0 || serde_json::to_value(spec).map_err(|_| ())? != reply {
                return Err(InstallerError::Invalid(
                    "ArtCraft release failed checksum/extraction/headless MCP verification",
                ));
            }
            Ok(())
        }
        .await;
        let removed = self.force_remove(&helper).await;
        if removed.is_err() && result.is_ok() {
            return Err(InstallerError::Daemon);
        }
        result
    }

    pub(crate) async fn artcraft_mounts(
        self: &Arc<Self>,
        items: &[(String, Release)],
    ) -> Result<Vec<Value>, &'static str> {
        let mut mounts = Vec::new();
        for (id, spec) in items {
            let name = self
                .artcraft_volume(id, spec)
                .await
                .map_err(|_| "ArtCraft artifact ownership/release mismatch")?
                .ok_or("ArtCraft artifact unavailable")?;
            let verified = self.verified_artcraft.lock().unwrap().contains(&name);
            if !verified {
                let broker = self.clone();
                let (id, spec, verified_name) = (id.clone(), spec.clone(), name.clone());
                // The caller can time out while verification is running. It
                // must not abandon the helper before its Docker cleanup.
                tokio::spawn(async move {
                    let _slot = broker.artcraft_slots.try_acquire().map_err(
                        |_| "Two ArtCraft validations are already running; retry shortly",
                    )?;
                    let image = broker
                        .get_pinned_image("default")
                        .await
                        .map_err(|_| "ArtCraft verifier image unavailable")?;
                    broker
                        .artcraft_helper(&id, &spec, image, true)
                        .await
                        .map_err(|_| "ArtCraft artifact verification failed")?;
                    broker
                        .verified_artcraft
                        .lock()
                        .unwrap()
                        .insert(verified_name);
                    Ok::<_, &'static str>(())
                })
                .await
                .map_err(|_| "ArtCraft artifact verification failed")??;
            }
            mounts.push(json!({"Type":"volume","Source":name,"Target":format!("/opt/artcraft/{}",spec.app),"ReadOnly":true}));
        }
        Ok(mounts)
    }

    pub(crate) async fn artcraft_ready(&self) -> bool {
        let Ok(items) = defaults() else {
            return false;
        };
        for spec in items {
            let id = spec.id();
            let name = volume(&self.config.workflows_volume, &id);
            if !self.verified_artcraft.lock().unwrap().contains(&name)
                || !matches!(self.artcraft_volume(&id, &spec).await, Ok(Some(_)))
            {
                return false;
            }
        }
        true
    }

    pub(crate) async fn prepare_artcraft_defaults(
        &self,
        image: &str,
    ) -> Result<(), InstallerError> {
        let items = defaults().map_err(InstallerError::Invalid)?;
        // Drive all spawned installs to completion, including cleanup on error.
        let results = stream::iter(items)
            .map(|spec| async move {
                let id = spec.id();
                let name = volume(&self.config.workflows_volume, &id);
                if self.verified_artcraft.lock().unwrap().contains(&name)
                    && matches!(self.artcraft_volume(&id, &spec).await, Ok(Some(_)))
                {
                    return Ok(());
                }
                self.install_artcraft(&id, &spec, image).await
            })
            .buffer_unordered(2)
            .collect::<Vec<_>>()
            .await;
        results.into_iter().collect()
    }

    async fn install_artcraft(
        &self,
        id: &str,
        spec: &Release,
        image: &str,
    ) -> Result<(), InstallerError> {
        let name = volume(&self.config.workflows_volume, id);
        let _slot = self.artcraft_slots.try_acquire().map_err(|_| {
            InstallerError::Invalid("Two ArtCraft installations are already running; retry shortly")
        })?;
        if !self
            .installing_artcraft
            .lock()
            .unwrap()
            .insert(name.clone())
        {
            return Err(InstallerError::Invalid(
                "This ArtCraft artifact is already being prepared; retry shortly",
            ));
        }
        let mut created = false;
        let result = async {
            if self.artcraft_volume(id, spec).await?.is_some() {
                match self.artcraft_helper(id, spec, image, true).await {
                    Ok(()) => return Ok(()),
                    Err(InstallerError::Invalid(_)) => {
                        self.delete_volume(&name).await?;
                        self.verified_artcraft.lock().unwrap().remove(&name);
                    }
                    Err(err) => return Err(err),
                }
            }
            let filter =
                json!({"label":[format!("{DEPLOY}={}",self.config.workflows_volume),LABEL]})
                    .to_string();
            let query = form_urlencoded::Serializer::new(String::new())
                .append_pair("filters", &filter)
                .finish();
            let (status, result) = self
                .docker_json(
                    Method::GET,
                    &format!("{API}/volumes?{query}"),
                    Value::Null,
                    self.config.deadline,
                )
                .await?;
            if status != StatusCode::OK {
                return Err(InstallerError::Daemon);
            }
            if result["Volumes"]
                .as_array()
                .is_some_and(|volumes| volumes.len() >= 128)
            {
                return Err(InstallerError::Invalid(
                    "ArtCraft artifact storage limit reached; reclaim unused installations first",
                ));
            }
            self.create_volume(&name, &spec.labels(id, &self.config.workflows_volume))
                .await?;
            created = true;
            self.artcraft_helper(id, spec, image, false).await
        }
        .await;
        if result.is_ok() {
            self.verified_artcraft.lock().unwrap().insert(name.clone());
        } else if created {
            let _ = self.delete_volume(&name).await;
        }
        self.installing_artcraft.lock().unwrap().remove(&name);
        result
    }

    pub(crate) async fn reap_artcraft(&self) {
        let deployment = &self.config.workflows_volume;
        let Ok(rows) = self
            .list_containers(
                &[format!("{DEPLOY}={deployment}"), format!("{HELPER}=true")],
                true,
            )
            .await
        else {
            return;
        };
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        for row in rows {
            if row["Labels"][DEPLOY] != *deployment
                || row["Labels"][HELPER] != "true"
                || row["Created"]
                    .as_u64()
                    .is_none_or(|created| now.saturating_sub(created) < 420)
            {
                continue;
            }
            if let Some(id) = row["Id"].as_str().filter(|id| valid_docker_id(id)) {
                let _ = self.force_remove(id).await;
            }
        }
        // Pre-offline installers created bridge networks. Inspect ownership,
        // age and endpoint count again before removing an obsolete one.
        let Ok(networks) = self
            .list_docker_resources("networks", &[format!("{DEPLOY}={deployment}")])
            .await
        else {
            return;
        };
        let legacy = |row: &Value| {
            row["Labels"][DEPLOY] == *deployment
                && (row["Labels"][LABEL].as_str().is_some_and(|s| !s.is_empty())
                    || row["Labels"]["nautionette.artcraft.installer_network"] == "true")
        };
        if let Some(networks) = networks.as_array() {
            for row in networks.iter().filter(|row| legacy(row)) {
                let Some(id) = row["Id"].as_str().filter(|id| valid_docker_id(id)) else {
                    continue;
                };
                let path = format!("{API}/networks/{id}");
                let Ok((StatusCode::OK, network)) = self
                    .docker_json(Method::GET, &path, Value::Null, self.config.deadline)
                    .await
                else {
                    continue;
                };
                if !legacy(&network)
                    || !network["Containers"]
                        .as_object()
                        .is_some_and(|v| v.is_empty())
                {
                    continue;
                }
                let Some(created) = network["Created"]
                    .as_str()
                    .and_then(|date| chrono::DateTime::parse_from_rfc3339(date).ok())
                else {
                    continue;
                };
                if now.saturating_sub(created.timestamp().max(0) as u64) >= 420 {
                    let _ = self.docker_request(Method::DELETE, &path, 1024).await;
                }
            }
        }
    }
}

pub(super) async fn install(
    State(broker): State<Arc<Broker>>,
    Json(body): Json<Value>,
) -> Response {
    let fail = |status, message| (status, Json(json!({"detail":message}))).into_response();
    if !body.as_object().is_some_and(|v| {
        v.len() == 2 && v.contains_key("installation_id") && v.contains_key("release")
    }) {
        return fail(
            StatusCode::UNPROCESSABLE_ENTITY,
            "Provide an installation ID and fixed ArtCraft release",
        );
    }
    let Some(id) = body["installation_id"]
        .as_str()
        .filter(|v| valid_revision(v))
        .map(str::to_owned)
    else {
        return fail(
            StatusCode::UNPROCESSABLE_ENTITY,
            "Invalid ArtCraft installation ID",
        );
    };
    let spec = match Release::parse(body["release"].clone()) {
        Ok(v) => v,
        Err(e) => return fail(StatusCode::UNPROCESSABLE_ENTITY, e),
    };
    if !broker.artcraft_ready().await && broker.image_build_configured() {
        return fail(
            StatusCode::SERVICE_UNAVAILABLE,
            "Default app artifacts are being prepared; queued updates will retry",
        );
    }
    match tokio::spawn(async move {
        let image = broker.get_pinned_image("default").await?;
        broker.install_artcraft(&id, &spec, image).await?;
        Ok::<_, InstallerError>(spec)
    })
    .await
    {
        Ok(Ok(reply)) => Json(reply).into_response(),
        Ok(Err(InstallerError::Invalid(msg))) => fail(StatusCode::UNPROCESSABLE_ENTITY, msg),
        _ => fail(
            StatusCode::SERVICE_UNAVAILABLE,
            "ArtCraft installer unavailable or timed out; previous version unchanged",
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn downloader_fixed_urls_bounded_digest_and_fallback_cannot_substitute_a_version_or_origin() {
        let spec = defaults()
            .unwrap()
            .into_iter()
            .find(|spec| spec.app == "cadcraft")
            .unwrap();
        assert_eq!(
            archive_url(&spec),
            format!(
                "https://github.com/storytold/cadcraft/releases/download/v{}/cadcraft-{}-linux-{}.tar.gz",
                spec.version, spec.version, spec.arch
            )
        );
        let expected = format!("{:x}", Sha256::digest(b"archive"));
        let mut valid = DownloadIntegrity::new(1024);
        valid.push(b"arc").unwrap_or_else(|_| panic!());
        valid.push(b"hive").unwrap_or_else(|_| panic!());
        assert!(valid.verify(&expected).is_ok());
        let mut wrong = DownloadIntegrity::new(1024);
        wrong.push(b"archive").unwrap_or_else(|_| panic!());
        assert!(wrong.verify(&spec.sha256).is_err());
        let mut oversized = DownloadIntegrity::new(2);
        assert!(oversized.push(b"archive").is_err());
        let metadata = json!({"tag_name":format!("v{}",spec.version),"assets":[{"id":12345,"name":spec.name(),"browser_download_url":"https://untrusted.invalid/ignored"}]});
        assert_eq!(
            pinned_asset_api_url(&spec, metadata.to_string().as_bytes())
                .ok()
                .unwrap(),
            "https://api.github.com/repos/storytold/cadcraft/releases/assets/12345"
        );
        let mut wrong = metadata.clone();
        wrong["tag_name"] = json!("v0.0.0");
        assert!(pinned_asset_api_url(&spec, wrong.to_string().as_bytes()).is_err());
        wrong = metadata.clone();
        wrong["assets"][0]["id"] = json!("12345");
        assert!(pinned_asset_api_url(&spec, wrong.to_string().as_bytes()).is_err());
        assert!(pinned_asset_api_url(&spec, &vec![b'a'; 2 * 1024 * 1024 + 1]).is_err());
    }

    struct HttpStep {
        url: String,
        binary: bool,
        status: u16,
        body: Vec<u8>,
    }
    struct ScriptedHttp(std::sync::Mutex<std::collections::VecDeque<HttpStep>>);
    impl AssetHttp for ScriptedHttp {
        async fn get(&self, url: &str, binary: bool) -> Result<AssetReply, ()> {
            let next = self
                .0
                .lock()
                .unwrap()
                .pop_front()
                .expect("unexpected release request");
            assert_eq!(next.url, url);
            assert_eq!(next.binary, binary);
            let chunks = next
                .body
                .chunks(3)
                .map(axum::body::Bytes::copy_from_slice)
                .map(Ok)
                .collect::<Vec<_>>();
            Ok(AssetReply {
                status: next.status,
                body: Box::pin(stream::iter(chunks)),
            })
        }
    }

    #[tokio::test]
    async fn release_504_uses_only_the_matching_github_asset_api_and_keeps_the_pinned_digest() {
        use std::io::Read;
        let mut spec = defaults()
            .unwrap()
            .into_iter()
            .find(|spec| spec.app == "cadcraft")
            .unwrap();
        spec.sha256 = format!("{:x}", Sha256::digest(b"archive"));
        let direct = archive_url(&spec);
        let metadata_url = format!(
            "https://api.github.com/repos/storytold/cadcraft/releases/tags/v{}",
            spec.version
        );
        let metadata=json!({"tag_name":format!("v{}",spec.version),"assets":[{"id":12345,"name":spec.name(),"browser_download_url":"https://untrusted.invalid/ignored"}]}).to_string();
        let steps = || {
            std::collections::VecDeque::from([
                HttpStep {
                    url: direct.clone(),
                    binary: false,
                    status: 504,
                    body: Vec::new(),
                },
                HttpStep {
                    url: metadata_url.clone(),
                    binary: false,
                    status: 200,
                    body: metadata.as_bytes().to_vec(),
                },
                HttpStep {
                    url: "https://api.github.com/repos/storytold/cadcraft/releases/assets/12345"
                        .into(),
                    binary: true,
                    status: 200,
                    body: b"archive".to_vec(),
                },
            ])
        };
        let client = ScriptedHttp(std::sync::Mutex::new(steps()));
        let mut file = download_using(&spec, &client, false)
            .await
            .unwrap_or_else(|_| panic!("fixture release did not download"));
        file.seek(SeekFrom::Start(0)).unwrap();
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes).unwrap();
        assert_eq!(bytes, b"archive");
        assert!(client.0.lock().unwrap().is_empty());
        spec.sha256 = "0".repeat(64);
        let client = ScriptedHttp(std::sync::Mutex::new(steps()));
        assert!(matches!(
            download_using(&spec, &client, false).await,
            Err(InstallerError::Invalid("Release checksum mismatch"))
        ));
        assert!(client.0.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn unavailable_assets_do_not_retry_and_bad_tag_metadata_cannot_switch_the_version() {
        let spec = defaults().unwrap().remove(0);
        for status in [400, 401, 403, 404, 410] {
            let client = ScriptedHttp(std::sync::Mutex::new(std::collections::VecDeque::from([
                HttpStep {
                    url: archive_url(&spec),
                    binary: false,
                    status,
                    body: Vec::new(),
                },
            ])));
            assert!(matches!(
                download_using(&spec, &client, false).await,
                Err(InstallerError::Invalid(
                    "Pinned ArtCraft release archive is unavailable"
                ))
            ));
            assert!(client.0.lock().unwrap().is_empty());
        }
        let client = ScriptedHttp(std::sync::Mutex::new(std::collections::VecDeque::from([
            HttpStep {
                url: archive_url(&spec),
                binary: false,
                status: 504,
                body: Vec::new(),
            },
            HttpStep {
                url: format!(
                    "https://api.github.com/repos/storytold/{}/releases/tags/v{}",
                    spec.app, spec.version
                ),
                binary: false,
                status: 200,
                body: br#"{"tag_name":"v0.0.0"}"#.to_vec(),
            },
        ])));
        assert!(matches!(
            download_using(&spec, &client, false).await,
            Err(InstallerError::Invalid(
                "ArtCraft release metadata does not match the pinned version"
            ))
        ));
    }

    #[test]
    fn staging_archive_has_only_verified_bytes_and_the_unchanged_root_owned_offline_helper() {
        use std::io::{Read, Write};
        let mut file = tempfile::tempfile().unwrap();
        file.write_all(b"archive").unwrap();
        let staging = archive_release(file).unwrap();
        let mut tar = tar::Archive::new(staging);
        let mut entries = tar.entries().unwrap();
        let mut archive = entries.next().unwrap().unwrap();
        assert_eq!(archive.path_bytes().as_ref(), b".release.tar.gz");
        assert_eq!(
            (
                archive.header().uid().unwrap(),
                archive.header().gid().unwrap(),
                archive.header().mode().unwrap()
            ),
            (0, 0, 0o600)
        );
        let mut bytes = Vec::new();
        archive.read_to_end(&mut bytes).unwrap();
        assert_eq!(bytes, b"archive");
        let mut helper = entries.next().unwrap().unwrap();
        assert_eq!(helper.path_bytes().as_ref(), b".install.py");
        assert_eq!(
            (
                helper.header().uid().unwrap(),
                helper.header().gid().unwrap()
            ),
            (0, 0)
        );
        bytes.clear();
        helper.read_to_end(&mut bytes).unwrap();
        assert_eq!(bytes, OFFLINE);
        assert!(entries.next().is_none());
    }

    #[tokio::test]
    #[ignore = "downloads a pinned public GridCraft release from GitHub"]
    async fn real_gridcraft_archive_is_bounded_and_matches_the_packaged_digest() {
        let spec = defaults()
            .unwrap()
            .into_iter()
            .find(|spec| spec.app == "gridcraft")
            .unwrap();
        let mut file = download(&spec).await.unwrap_or_else(|error| match error {
            InstallerError::Invalid(reason) => panic!("pinned release rejected: {reason}"),
            InstallerError::Daemon => panic!("pinned release could not be downloaded"),
        });
        let size = file.metadata().unwrap().len();
        assert!(size > 0 && size <= MAX_ARCHIVE as u64);
        file.seek(SeekFrom::Start(0)).unwrap();
        let mut digest = Sha256::new();
        std::io::copy(&mut file, &mut digest).unwrap();
        assert_eq!(format!("{:x}", digest.finalize()), spec.sha256);
    }
}
