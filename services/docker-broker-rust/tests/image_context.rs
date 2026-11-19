use std::{
    fs,
    path::{Path, PathBuf},
};

use denkzeug_docker_broker_spike::{effective_fingerprints, fingerprints};
use serde_json::{Value, json};

struct Context(PathBuf);
impl Context {
    fn new() -> Self {
        use std::sync::atomic::{AtomicU64, Ordering};
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let root = std::env::temp_dir().join(format!(
            "dz-fingerprint-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        let context = Self(root);
        context.write("pi-base/Dockerfile", "FROM scratch\n");
        context.write("agent-sets/default/Dockerfile", "FROM scratch\n");
        context
    }
    fn write(&self, relative: &str, data: impl AsRef<[u8]>) {
        let path = self.0.join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, data).unwrap();
    }
    fn source(&self) -> Value {
        serde_json::to_value(fingerprints(&self.0).unwrap()).unwrap()
    }
    fn effective(&self, lock: &Path) -> Value {
        serde_json::to_value(effective_fingerprints(&self.0, lock).unwrap()).unwrap()
    }
}
impl Drop for Context {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn finds_only_declared_agent_sets_and_base_change_retags_them() {
    let context = Context::new();
    context.write("agent-sets/no-dockerfile/README.md", "not an agent set");
    context.write("agent-sets/second/Dockerfile", "FROM scratch\n");
    let original = context.source();
    assert_eq!(original["agent_sets"].as_object().unwrap().len(), 2);
    context.write("pi-base/Dockerfile", "FROM other\n");
    let changed = context.source();
    for name in ["default", "second"] {
        assert_ne!(original["agent_sets"][name], changed["agent_sets"][name]);
    }
}

#[test]
fn build_and_import_caches_do_not_change_source_identities() {
    let context = Context::new();
    let original = context.source();
    for name in [
        "__pycache__/generated.pyc",
        "module.pyc",
        "module.pyo",
        "target/generated",
    ] {
        context.write(&format!("pi-base/{name}"), "generated");
    }
    assert_eq!(context.source(), original);
}

#[test]
fn packaged_fingerprints_survive_deployment_dockerfile_rewriting() {
    let context = Context::new();
    let canonical = context.source();
    context.write("lock.json", canonical.to_string());
    context.write(
        "pi-base/Dockerfile",
        "FROM scratch\nARG COOLIFY_RESOURCE_UUID\n",
    );
    context.write(
        "agent-sets/default/Dockerfile",
        "FROM scratch\nARG COOLIFY_RESOURCE_UUID\n",
    );
    assert_ne!(context.source(), canonical);
    assert_eq!(context.effective(&context.0.join("lock.json")), canonical);
}

#[test]
fn missing_packaged_lock_uses_source_but_malformed_present_lock_fails_closed() {
    let context = Context::new();
    let lock = context.0.join("lock.json");
    assert_eq!(context.effective(&lock), context.source());
    for value in [
        json!(null),
        json!([]),
        json!({}),
        json!({"schema":2,"base":"123456789abc","agent_sets":{}}),
        json!({"schema":1,"base":"123456789abc","agent_sets":[]}),
        json!({"schema":1,"base":"INVALID","agent_sets":{}}),
        json!({"schema":1,"base":"123456789abc","agent_sets":{},"extra":true}),
        json!({"schema":1,"base":"123456789abc","agent_sets":{"default":"123456789abG"}}),
    ] {
        context.write("lock.json", value.to_string());
        assert!(
            effective_fingerprints(&context.0, &lock).is_err(),
            "{value}"
        );
    }
    context.write("lock.json", "not json");
    assert!(effective_fingerprints(&context.0, &lock).is_err());
}

#[test]
fn an_agent_set_missing_from_the_lock_uses_its_source_fingerprint() {
    let context = Context::new();
    let mut canonical = context.source();
    canonical["agent_sets"] = json!({"absent":"123456789abc"});
    canonical["base"] = json!("123456789abc");
    context.write("lock.json", canonical.to_string());
    let effective = context.effective(&context.0.join("lock.json"));
    assert_eq!(canonical["base"], effective["base"]);
    assert_eq!(context.source()["agent_sets"], effective["agent_sets"]);
}
