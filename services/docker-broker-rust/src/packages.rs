//! A fixed, isolated installer for immutable package volumes, plus mount validation.
use std::{
    collections::BTreeSet,
    sync::{Arc, LazyLock},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use axum::{
    Json,
    extract::State,
    http::{Method, StatusCode},
    response::{IntoResponse, Response},
};
use regex::Regex;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::{Broker, runtime, turns::RecoveryEngine, valid_docker_id};

const API: &str = "/v1.45";
const DEPLOYMENT: &str = "nautionette.deployment";
const PACKAGE: &str = "nautionette.package";
const SOURCE_ERROR: &str = "Only npm packages and public HTTPS GitHub repositories are supported; no credentials or local paths";
static NPM: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"^npm:(@?[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)?)(?:@([A-Za-z0-9.*^~+_-]+))?$",
    )
    .unwrap()
});
static GITHUB: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?:git:)?(?:https://)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:@([A-Za-z0-9_./-]+))?$").unwrap()
});
static ENV: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Z][A-Z0-9_]{0,79}$").unwrap());
static FILE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^agent/[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\.(?:json|yaml|yml|toml|txt)$").unwrap()
});
const RESERVED_ENV: &[&str] = &[
    "HOME",
    "PATH",
    "SHELL",
    "USER",
    "LOGNAME",
    "PWD",
    "TMPDIR",
    "TMP",
    "TEMP",
    "BASH_ENV",
    "ENV",
    "IFS",
    "CDPATH",
    "SHELLOPTS",
    "BASHOPTS",
    "PYTHONPATH",
    "PYTHONHOME",
    "BACKEND_URL",
    "MCP_URL",
    "INTERNAL_TOKEN",
    "AGENTGATEWAY_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CURL_CA_BUNDLE",
];
const BLOCKED_PREFIXES: &[&str] = &[
    "PI_",
    "NODE_",
    "NPM_",
    "GIT_",
    "NAUTIONETTE_",
    "AGENT_",
    "LD_",
    "DYLD_",
];
const RESERVED_FILES: &[&str] = &[
    "agent/settings.json",
    "agent/auth.json",
    "agent/models.json",
    "agent/trust.json",
];

pub(crate) enum InstallerError {
    Invalid(&'static str),
    Daemon,
}
impl From<()> for InstallerError {
    fn from(_: ()) -> Self {
        Self::Daemon
    }
}

pub fn installation_source(source: &str) -> Result<Value, &'static str> {
    if source.chars().count() > 300 {
        return Err(SOURCE_ERROR);
    }
    let source = source.trim();
    if let Some(matches) = NPM.captures(source) {
        let name = &matches[1];
        if name.starts_with('@') != name.contains('/') {
            return Err("Invalid npm package name");
        }
        let spec = matches
            .get(2)
            .map_or_else(|| name.to_owned(), |v| format!("{name}@{}", v.as_str()));
        return Ok(json!({"kind":"npm","name":name,"spec":spec}));
    }
    if let Some(matches) = GITHUB.captures(source) {
        let (owner, repo) = (&matches[1], &matches[2]);
        let reference = matches.get(3).map_or("HEAD", |value| value.as_str());
        if matches!(owner, "." | "..")
            || matches!(repo, "." | "..")
            || reference.starts_with(['-', '/'])
            || reference.contains("..")
            || reference.contains("//")
        {
            return Err("Invalid GitHub repository or ref");
        }
        return Ok(
            json!({"kind":"git","url":format!("https://github.com/{owner}/{repo}.git"),"ref":reference}),
        );
    }
    Err(SOURCE_ERROR)
}

pub(crate) fn valid_revision(id: &str) -> bool {
    id.len() == 32 && id.bytes().all(|b| matches!(b,b'0'..=b'9'|b'a'..=b'f'))
}
pub(crate) fn short_hash(bytes: &[u8], chars: usize) -> String {
    let hex: String = Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    hex[..chars].to_owned()
}
fn volume_name(namespace: &str, id: &str) -> String {
    format!(
        "nautionette-package-{}-{id}",
        short_hash(namespace.as_bytes(), 12)
    )
}
fn valid_path(path: &str) -> bool {
    !path.is_empty() && !path.starts_with('/') && !path.split('/').any(|part| part == "..")
}

pub(crate) fn runtime_installations(value: &Value) -> Result<BTreeSet<String>, &'static str> {
    let mut ids = BTreeSet::new();
    let items = value
        .as_array()
        .filter(|v| v.len() <= 20)
        .ok_or("Invalid package runtime configuration")?;
    for item in items {
        let id = item["installation_id"]
            .as_str()
            .filter(|v| valid_revision(v))
            .ok_or("Invalid package revision ID")?;
        let configuration = item["configuration"]
            .as_object()
            .ok_or("Configuration accepts env and files objects")?;
        let mut bytes = 0;
        for (kind, values) in configuration {
            let valid: fn(&str) -> bool = match kind.as_str() {
                "env" => |key: &str| {
                    ENV.is_match(key)
                        && !RESERVED_ENV.contains(&key)
                        && !BLOCKED_PREFIXES
                            .iter()
                            .any(|prefix| key.starts_with(prefix))
                },
                "files" => |key: &str| FILE.is_match(key) && !RESERVED_FILES.contains(&key),
                _ => return Err("Configuration accepts env and files objects"),
            };
            let values = values
                .as_object()
                .filter(|v| v.len() <= 50)
                .ok_or("Each kind must contain at most 50 entries")?;
            for (key, value) in values {
                if !valid(key) {
                    return Err("Unsupported package configuration key/path");
                }
                bytes += value
                    .as_str()
                    .filter(|s| !s.contains('\0'))
                    .ok_or("Invalid package configuration value")?
                    .len();
            }
        }
        if bytes > 64_000 {
            return Err("Package configuration must fit in 64 KB");
        }
        let filters = item["filters"]
            .as_object()
            .ok_or("Invalid package resource filters")?;
        for (kind, patterns) in filters {
            if !matches!(
                kind.as_str(),
                "extensions" | "skills" | "prompts" | "themes"
            ) {
                return Err("Invalid package resource filters");
            }
            for pattern in patterns
                .as_array()
                .filter(|v| v.len() <= 100)
                .ok_or("Invalid package resource patterns")?
            {
                let pattern = pattern
                    .as_str()
                    .filter(|p| p.chars().count() <= 300)
                    .ok_or("Invalid package resource patterns")?;
                let path = pattern.trim_start_matches(['!', '+', '-']);
                if !valid_path(path) || path.starts_with('~') || path.contains('\\') {
                    return Err("Resource filters must stay inside the package");
                }
            }
        }
        if !item["root"].as_str().is_some_and(valid_path) {
            return Err("Invalid package artifact root");
        }
        ids.insert(id.into());
    }
    Ok(ids)
}

impl Broker {
    pub(crate) async fn inspect_volume(&self, name: &str) -> Result<Option<Value>, ()> {
        let (status, result) = self
            .docker_json(
                Method::GET,
                &format!("{API}/volumes/{name}"),
                Value::Null,
                self.config.deadline,
            )
            .await?;
        match status {
            StatusCode::OK => Ok(Some(result)),
            StatusCode::NOT_FOUND => Ok(None),
            _ => Err(()),
        }
    }

    pub(crate) async fn create_volume(&self, name: &str, labels: &Value) -> Result<(), ()> {
        let (status, result) = self
            .docker_json(
                Method::POST,
                &format!("{API}/volumes/create"),
                json!({"Name":name,"Labels":labels}),
                self.config.deadline,
            )
            .await?;
        (status == StatusCode::CREATED && result["Name"] == name && result["Labels"] == *labels)
            .then_some(())
            .ok_or(())
    }

    pub(crate) async fn delete_volume(&self, name: &str) -> Result<(), ()> {
        let (status, _) = self
            .docker_request(Method::DELETE, &format!("{API}/volumes/{name}"), 1024)
            .await?;
        matches!(status, StatusCode::NO_CONTENT | StatusCode::NOT_FOUND)
            .then_some(())
            .ok_or(())
    }

    pub(crate) async fn get_pinned_image(&self, set: &str) -> Result<&str, ()> {
        let tag = self
            .config
            .runtime
            .as_ref()
            .and_then(|cfg| cfg.agent_sets.get(set))
            .ok_or(())?;
        self.docker_get(&format!("{API}/images/{tag}/json"), 4 * 1024 * 1024)
            .await?;
        Ok(tag)
    }

    pub(crate) async fn create_helper(&self, body: Value) -> Result<String, ()> {
        let (status, result) = self
            .docker_json(
                Method::POST,
                &format!("{API}/containers/create"),
                body,
                self.config.deadline,
            )
            .await?;
        if status != StatusCode::CREATED {
            return Err(());
        }
        result["Id"]
            .as_str()
            .filter(|v| valid_docker_id(v))
            .map(str::to_owned)
            .ok_or(())
    }

    pub(crate) async fn helper_output(&self, id: &str) -> Result<(i64, Value), ()> {
        let (started, _) = self
            .docker_request(Method::POST, &format!("{API}/containers/{id}/start"), 1024)
            .await?;
        if started != StatusCode::NO_CONTENT {
            return Err(());
        }
        let (status, result) = self
            .docker_json(
                Method::POST,
                &format!("{API}/containers/{id}/wait?condition=not-running"),
                Value::Null,
                Duration::from_secs(300),
            )
            .await?;
        if status != StatusCode::OK {
            return Err(());
        }
        let exit = result["StatusCode"].as_i64().ok_or(())?;
        let frame = self
            .docker_get(
                &format!("{API}/containers/{id}/logs?stdout=true&stderr=false&tail=1"),
                1024 * 1024,
            )
            .await?;
        let line = runtime::raw_docker_payload(&frame, 1)?;
        Ok((exit, serde_json::from_str(&line).unwrap_or(Value::Null)))
    }

    pub(crate) async fn package_mounts(
        &self,
        ids: &BTreeSet<String>,
    ) -> Result<Vec<Value>, &'static str> {
        let mut result = Vec::new();
        for id in ids {
            let name = volume_name(&self.config.workflows_volume, id);
            let volume = self
                .inspect_volume(&name)
                .await
                .map_err(|_| "Package artifact unavailable")?
                .ok_or("Package artifact unavailable")?;
            if volume["Labels"][PACKAGE] != *id
                || volume["Labels"][DEPLOYMENT] != self.config.workflows_volume
            {
                return Err("Package artifact ownership mismatch");
            }
            result.push(json!({"Type":"volume","Source":name,"Target":format!("/opt/nautionette-packages/{id}"),"ReadOnly":true}));
        }
        Ok(result)
    }

    pub(crate) async fn reap_packages(&self) {
        let deploy = &self.config.workflows_volume;
        let Ok(rows) = self
            .list_containers(&[format!("{DEPLOYMENT}={deploy}"), PACKAGE.into()], true)
            .await
        else {
            return;
        };
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        for row in rows {
            if row["Labels"][DEPLOYMENT] != *deploy
                || row["Created"]
                    .as_u64()
                    .is_none_or(|created| now.saturating_sub(created) < 330)
            {
                continue;
            }
            let Some(revision) = row["Labels"][PACKAGE]
                .as_str()
                .filter(|rev| valid_revision(rev))
            else {
                continue;
            };
            let Some(id) = row["Id"].as_str().filter(|v| valid_docker_id(v)) else {
                continue;
            };
            if self.force_remove(id).await.is_ok() {
                self.remove_package_network(&volume_name(deploy, revision), revision)
                    .await;
            }
        }
    }

    async fn remove_package_network(&self, name: &str, revision: &str) {
        let target = format!("{API}/networks/{name}");
        let Ok((StatusCode::OK, body)) = self
            .docker_json(Method::GET, &target, Value::Null, self.config.deadline)
            .await
        else {
            return;
        };
        if body["Labels"][DEPLOYMENT] != self.config.workflows_volume
            || body["Labels"][PACKAGE] != revision
        {
            return;
        }
        if let Some(id) = body["Id"].as_str().filter(|id| valid_docker_id(id)) {
            let _ = self
                .docker_request(Method::DELETE, &format!("{API}/networks/{id}"), 1024)
                .await;
        }
    }
}

async fn install_package(
    broker: &Broker,
    id: &str,
    source: Value,
    allow_scripts: bool,
) -> Result<Value, InstallerError> {
    let image = broker.get_pinned_image("default").await.map_err(|_| {
        InstallerError::Invalid("Agent image is not ready; retry after the image build completes")
    })?;
    let _slot = broker.package_slots.try_acquire().map_err(|_| {
        InstallerError::Invalid("Two package installations are already running; retry shortly")
    })?;
    let name = volume_name(&broker.config.workflows_volume, id);
    {
        let mut installing = broker.installing_packages.lock().unwrap();
        if !installing.insert(name.clone()) {
            return Err(InstallerError::Invalid(
                "Installation ID already exists; artifacts are never overwritten",
            ));
        }
    }
    let mut volume_created = false;
    let mut network_id = None;
    let mut container_id = None;
    let result = async {
        if broker.inspect_volume(&name).await?.is_some() { return Err(InstallerError::Invalid("Installation ID already exists; artifacts are never overwritten")); }
        let labels = json!({(DEPLOYMENT):broker.config.workflows_volume,(PACKAGE):id});
        broker.create_volume(&name,&labels).await?;
        volume_created = true;
        let (status,result) = broker.docker_json(Method::POST,&format!("{API}/networks/create"),json!({"Name":name,"Driver":"bridge","Labels":labels,"Options":{"com.docker.network.bridge.enable_icc":"false"}}),broker.config.deadline).await?;
        if status != StatusCode::CREATED { return Err(InstallerError::Daemon); }
        network_id = Some(result["Id"].as_str().filter(|v| valid_docker_id(v)).ok_or(())?.to_owned());
        let body = json!({"Image":image,"Entrypoint":["node","/usr/local/lib/nautionette/package-install.mjs"],"Env":[format!("PACKAGE_SOURCE={source}"),format!("PACKAGE_SCRIPTS={allow_scripts}")],"Labels":labels,
            "HostConfig":{"Mounts":[{"Type":"volume","Source":name,"Target":"/artifact","ReadOnly":false}],"NetworkMode":name,"ReadonlyRootfs":true,"Tmpfs":{"/tmp":"size=256m,exec,mode=1777"},
                "Memory":1073741824,"NanoCpus":1000000000,"PidsLimit":128,"CapDrop":["ALL"],"CapAdd":["CHOWN","SETUID","SETGID"],"SecurityOpt":["no-new-privileges:true"],"LogConfig":{"Type":"json-file","Config":{"max-size":"1m","max-file":"1"}}}});
        let container = container_id.insert(broker.create_helper(body).await?);
        let (status,result) = broker.helper_output(container).await?;
        if status !=0 || !result["error"].is_null() { return Err(InstallerError::Invalid("Package install failed: verify public source, version, manifest and dependencies")); }
        if !result["root"].as_str().is_some_and(valid_path) { return Err(InstallerError::Invalid("Installer returned an invalid artifact path")); }
        if result["resolved"].as_str().is_none_or(|v| v.chars().count() > 500) { return Err(InstallerError::Invalid("Installer returned an invalid resolution")); }
        Ok(json!({"root":result["root"],"resolved":result["resolved"],"integrity":result["integrity"],"resources":result["resources"]}))
    }.await;
    if let Some(id) = container_id {
        let _ = broker.force_remove(&id).await;
    }
    if let Some(id) = network_id {
        let _ = broker
            .docker_request(Method::DELETE, &format!("{API}/networks/{id}"), 1024)
            .await;
    }
    if volume_created && result.is_err() {
        let _ = broker.delete_volume(&name).await;
    }
    broker.installing_packages.lock().unwrap().remove(&name);
    result
}

pub(super) async fn install(
    State(broker): State<Arc<Broker>>,
    Json(body): Json<Value>,
) -> Response {
    let failure = |status, message| (status, Json(json!({"detail":message}))).into_response();
    let Some(id) = body["installation_id"]
        .as_str()
        .filter(|v| valid_revision(v))
        .map(str::to_owned)
    else {
        return failure(
            StatusCode::UNPROCESSABLE_ENTITY,
            "Invalid package revision ID",
        );
    };
    let source = match body["source"]
        .as_str()
        .ok_or(SOURCE_ERROR)
        .and_then(installation_source)
    {
        Ok(src) => src,
        Err(msg) => return failure(StatusCode::UNPROCESSABLE_ENTITY, msg),
    };
    let scripts = match body.get("allow_scripts") {
        None => false,
        Some(Value::Bool(v)) => *v,
        _ => {
            return failure(
                StatusCode::UNPROCESSABLE_ENTITY,
                "allow_scripts must be a boolean",
            );
        }
    };
    match tokio::spawn(async move { install_package(&broker, &id, source, scripts).await }).await {
        Ok(Ok(reply)) => Json(reply).into_response(),
        Ok(Err(InstallerError::Invalid(message))) => {
            failure(StatusCode::UNPROCESSABLE_ENTITY, message)
        }
        _ => failure(
            StatusCode::SERVICE_UNAVAILABLE,
            "Package installer unavailable or timed out; retry",
        ),
    }
}
