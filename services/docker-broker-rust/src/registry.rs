//! Broker-local Docker registry authentication. Values are intentionally never Debug.
use std::{
    collections::BTreeMap,
    env, io,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

use axum::http::{HeaderMap, HeaderValue};
use base64::{
    Engine as _,
    engine::general_purpose::{STANDARD, URL_SAFE},
};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
};

const HUB: &str = "https://index.docker.io/v1/";
const CAP: u64 = 1024 * 1024;

struct Store {
    service: Option<String>,
    helpers: BTreeMap<String, String>,
    inline: BTreeMap<String, Value>,
}

impl Store {
    async fn load(explicit: Option<&Path>) -> Result<Self, ()> {
        let mut files = Vec::new();
        if let Some(path) = explicit {
            files.push(path.to_owned());
        } else {
            if let Some(dir) = env::var_os("DOCKER_CONFIG").filter(|dir| !dir.is_empty()) {
                files.push(PathBuf::from(dir).join("config.json"));
            }
            if let Some(home) = env::var_os("HOME").filter(|home| !home.is_empty()) {
                let home = PathBuf::from(home);
                files.push(home.join(".docker/config.json"));
                files.push(home.join(".dockercfg"));
            }
        }
        for path in files {
            let file = match tokio::fs::File::open(&path).await {
                Ok(file) => file,
                Err(err) if err.kind() == io::ErrorKind::NotFound && explicit.is_none() => continue,
                Err(_) => return Err(()),
            };
            let mut data = Vec::new();
            file.take(CAP + 1)
                .read_to_end(&mut data)
                .await
                .map_err(|_| ())?;
            if data.len() as u64 > CAP {
                return Err(());
            }
            let value: Value = serde_json::from_slice(&data).map_err(|_| ())?;
            return Self::parse(value);
        }
        Self::parse(json!({}))
    }

    fn parse(value: Value) -> Result<Self, ()> {
        let mut inline = BTreeMap::new();
        let auths = match value.get("auths") {
            Some(Value::Null) | None => value.as_object().ok_or(())?,
            Some(auths) => auths.as_object().ok_or(())?,
        };
        for (domain, entry) in auths {
            if !entry.is_object() {
                continue;
            }
            let auth = if let Some(token) =
                entry["identitytoken"].as_str().filter(|v| !v.is_empty())
            {
                json!({"IdentityToken":token})
            } else if let Some(encoded) = entry["auth"].as_str().filter(|v| !v.is_empty()) {
                let raw = STANDARD.decode(encoded).map_err(|_| ())?;
                let plain = String::from_utf8(raw).map_err(|_| ())?;
                let (username, password) = plain.split_once(':').ok_or(())?;
                json!({"username":username,"password":password,"email":entry["email"].as_str().unwrap_or(""),"serveraddress":domain})
            } else if entry.as_object().is_some_and(|obj| obj.is_empty()) {
                json!({})
            } else {
                continue;
            };
            inline.insert(domain.clone(), auth);
        }
        let service = value["credsStore"]
            .as_str()
            .filter(|name| !name.is_empty())
            .map(str::to_owned);
        let helpers = match value["credHelpers"].as_object() {
            Some(entries) => entries
                .iter()
                .map(|(domain, name)| {
                    name.as_str()
                        .map(|name| (domain.clone(), name.into()))
                        .ok_or(())
                })
                .collect::<Result<_, _>>()?,
            None => BTreeMap::new(),
        };
        Ok(Self {
            service,
            helpers,
            inline,
        })
    }

    async fn for_pull(&self, image: &str) -> Result<Option<Value>, ()> {
        let registry = registry(image);
        let key = if registry == "docker.io" {
            HUB
        } else {
            registry
        };
        if let Some(helper) = self.helpers.get(key).or(self.service.as_ref())
            && let Some(auth) = get(helper, key).await?
        {
            return Ok(Some(auth));
        }
        Ok(self
            .inline
            .get(key)
            .or_else(|| {
                self.inline
                    .iter()
                    .find(|(domain, _)| normalize(domain) == registry)
                    .map(|(_, value)| value)
            })
            .cloned()
            .filter(|v| v.as_object().is_some_and(|v| !v.is_empty())))
    }

    async fn for_build(&self) -> Result<BTreeMap<String, Value>, ()> {
        let mut result = self.inline.clone();
        let mut helper_domains = BTreeMap::new();
        if let Some(store) = &self.service {
            let listing = run_helper(store, "list", None).await?.ok_or(())?;
            let listing = listing.as_object().ok_or(())?;
            if listing.len() > 100 {
                return Err(());
            }
            for key in listing.keys() {
                helper_domains.insert(key.clone(), store.to_owned());
            }
        }
        helper_domains.extend(self.helpers.clone());
        if helper_domains.len() > 100 {
            return Err(());
        }
        for (key, helper) in helper_domains {
            if let Some(auth) = get(&helper, &key).await? {
                result.insert(key.clone(), auth.clone());
                result.insert(normalize(&key).into(), auth);
            }
        }
        if let Some(auth) = result
            .get("docker.io")
            .cloned()
            .or_else(|| result.get("index.docker.io").cloned())
        {
            result.entry(HUB.into()).or_insert(auth);
        }
        Ok(result)
    }
}

fn normalize(value: &str) -> &str {
    let host = value
        .strip_prefix("https://")
        .or_else(|| value.strip_prefix("http://"))
        .unwrap_or(value)
        .split('/')
        .next()
        .unwrap_or(value);
    if matches!(host, "index.docker.io" | "registry-1.docker.io") {
        "docker.io"
    } else {
        host
    }
}
fn registry(image: &str) -> &str {
    match image.split_once('/') {
        Some((host, _)) if host.contains(['.', ':']) || host == "localhost" => normalize(host),
        _ => "docker.io",
    }
}

async fn get(helper: &str, key: &str) -> Result<Option<Value>, ()> {
    let Some(value) = run_helper(helper, "get", Some(key)).await? else {
        return Ok(None);
    };
    let username = value["Username"].as_str().ok_or(())?;
    let secret = value["Secret"].as_str().ok_or(())?;
    if username.is_empty() && secret.is_empty() {
        return Ok(None);
    }
    if username == "<token>" {
        Ok(Some(json!({"IdentityToken":secret,"ServerAddress":key})))
    } else {
        Ok(Some(
            json!({"Username":username,"Password":secret,"ServerAddress":key}),
        ))
    }
}

async fn run_helper(helper: &str, action: &str, key: Option<&str>) -> Result<Option<Value>, ()> {
    if helper.is_empty()
        || helper.len() > 64
        || !helper
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'.' | b'-' | b'_'))
    {
        return Err(());
    }
    if key.is_some_and(|key| key.len() > 1024 || key.bytes().any(|b| b.is_ascii_control())) {
        return Err(());
    }
    execute_helper(
        std::ffi::OsStr::new(&format!("docker-credential-{helper}")),
        action,
        key,
    )
    .await
}

async fn execute_helper(
    program: &std::ffi::OsStr,
    action: &str,
    key: Option<&str>,
) -> Result<Option<Value>, ()> {
    tokio::time::timeout(Duration::from_secs(3), async {
        let mut child = Command::new(program)
            .arg(action)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| ())?;
        let mut stdin = child.stdin.take().ok_or(())?;
        if let Some(key) = key {
            stdin.write_all(key.as_bytes()).await.map_err(|_| ())?;
        }
        drop(stdin);
        let mut data = Vec::new();
        child
            .stdout
            .take()
            .ok_or(())?
            .take(CAP + 1)
            .read_to_end(&mut data)
            .await
            .map_err(|_| ())?;
        if data.len() as u64 > CAP {
            return Err(());
        }
        let status = child.wait().await.map_err(|_| ())?;
        if status.success() {
            return serde_json::from_slice(&data).map(Some).map_err(|_| ());
        }
        let reply = std::str::from_utf8(&data).map_err(|_| ())?.trim();
        if action == "get" && reply == "credentials not found in native keychain" {
            Ok(None)
        } else {
            Err(())
        }
    })
    .await
    .map_err(|_| ())?
}

fn set_header<T: serde::Serialize>(
    headers: &mut HeaderMap,
    name: &'static str,
    data: &T,
) -> Result<(), ()> {
    let mut value =
        HeaderValue::from_str(&URL_SAFE.encode(serde_json::to_vec(data).map_err(|_| ())?))
            .map_err(|_| ())?;
    value.set_sensitive(true);
    headers.insert(name, value);
    Ok(())
}

pub(crate) async fn pull_headers(
    path: Option<&Path>,
    image: &str,
    mut headers: HeaderMap,
) -> Result<HeaderMap, ()> {
    if let Some(auth) = Store::load(path).await?.for_pull(image).await? {
        set_header(&mut headers, "x-registry-auth", &auth)?;
    }
    Ok(headers)
}

pub(crate) async fn build_headers(
    path: Option<&Path>,
    mut headers: HeaderMap,
) -> Result<HeaderMap, ()> {
    let auth = Store::load(path).await?.for_build().await?;
    if !auth.is_empty() {
        set_header(&mut headers, "x-registry-config", &auth)?;
    }
    Ok(headers)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn pull_auth_contains_only_the_selected_registry_and_never_uses_anonymous_on_helper_failure()
     {
        let file = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(file.path(),json!({"auths":{"https://registry.fixture.invalid/v1/":{"auth":STANDARD.encode("fixture:synthetic:secret")},"other.invalid":{"identitytoken":"unrelated"}}}).to_string()).unwrap();
        let headers = pull_headers(
            Some(file.path()),
            "registry.fixture.invalid/project/runner:dev",
            HeaderMap::new(),
        )
        .await
        .unwrap();
        let value = &headers["x-registry-auth"];
        assert!(value.is_sensitive());
        let auth: Value =
            serde_json::from_slice(&URL_SAFE.decode(value.as_bytes()).unwrap()).unwrap();
        assert_eq!(auth["username"], "fixture");
        assert_eq!(auth["password"], "synthetic:secret");
        assert!(!auth.to_string().contains("unrelated"));
        assert!(
            pull_headers(Some(file.path()), "unqualified:dev", HeaderMap::new())
                .await
                .unwrap()
                .is_empty()
        );
        let store=Store::parse(json!({"credHelpers":{"registry.fixture.invalid":"nonexistent-denkzeug-fixture-helper"},"auths":{"registry.fixture.invalid":{"identitytoken":"inline"}}})).unwrap();
        assert!(
            store
                .for_pull("registry.fixture.invalid/runner:v1")
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn helper_protocol_uses_stdin_and_distinguishes_a_missing_key_from_failure() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempfile::tempdir().unwrap();
        let helper = root.path().join("helper");
        std::fs::write(
            &helper,
            r#"#!/bin/sh
case "$1:$(cat)" in
 get:registry.fixture.invalid) printf '%s' '{"Username":"<token>","Secret":"synthetic-value"}' ;;
 get:missing.fixture.invalid) printf '%s' 'credentials not found in native keychain'; exit 1 ;;
 list:) printf '%s' '{"registry.fixture.invalid":"<token>"}' ;;
 *) printf '%s' 'private internal helper failure'; exit 1 ;;
esac
"#,
        )
        .unwrap();
        std::fs::set_permissions(&helper, std::fs::Permissions::from_mode(0o700)).unwrap();
        assert_eq!(
            execute_helper(helper.as_os_str(), "get", Some("registry.fixture.invalid"))
                .await
                .unwrap()
                .unwrap()["Username"],
            "<token>"
        );
        assert!(
            execute_helper(helper.as_os_str(), "get", Some("missing.fixture.invalid"))
                .await
                .unwrap()
                .is_none()
        );
        assert!(
            execute_helper(helper.as_os_str(), "get", Some("bad.fixture.invalid"))
                .await
                .is_err()
        );
        assert_eq!(
            execute_helper(helper.as_os_str(), "list", None)
                .await
                .unwrap()
                .unwrap()["registry.fixture.invalid"],
            "<token>"
        );
    }
}
