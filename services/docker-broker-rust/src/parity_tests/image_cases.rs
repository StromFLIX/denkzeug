use super::*;

#[tokio::test]
async fn builds_once_reuses_images_and_repairs_pruned_alias_without_a_rebuild() {
    let fixture = Fixture::new().await;
    fixture.seed_artcraft();
    let config = fixture.image_settings(false);
    let state = fixture.state(config);
    assert_eq!(fixture.build(&state, false).await["status"], "ready");
    assert_eq!(fixture.daemon.lock().unwrap().builds, 2);
    assert_eq!(fixture.build(&state, false).await["status"], "ready");
    assert_eq!(fixture.daemon.lock().unwrap().builds, 2);
    fixture
        .daemon
        .lock()
        .unwrap()
        .images
        .remove("fixture/base:dev");
    let missing = state.images_snapshot().await;
    assert_eq!(missing["status"], "missing");
    assert_eq!(missing["missing"], json!(["fixture/base:dev"]));
    assert_eq!(fixture.build(&state, false).await["status"], "ready");
    assert_eq!(fixture.daemon.lock().unwrap().builds, 2);
    let tag = state.config.runtime.as_ref().unwrap().agent_sets["default"].clone();
    fixture.daemon.lock().unwrap().images.remove(&tag);
    assert_eq!(state.images_snapshot().await["status"], "missing");
    assert_eq!(fixture.build(&state, false).await["status"], "ready");
    assert_eq!(fixture.daemon.lock().unwrap().builds, 3);
}

#[tokio::test]
async fn registry_images_are_source_addressed_and_retagged_into_the_stack_namespace() {
    let fixture = Fixture::new().await;
    fixture.seed_artcraft();
    let state = fixture.state(fixture.image_settings(true));
    assert_eq!(fixture.build(&state, false).await["status"], "ready");
    let tag = state.config.runtime.as_ref().unwrap().agent_sets["default"].clone();
    {
        let d = fixture.daemon.lock().unwrap();
        assert_eq!((d.pulls, d.builds), (2, 0));
        assert!(d.images.contains(&tag));
        assert!(
            d.images
                .iter()
                .any(|tag| tag.starts_with("registry.invalid/cache/agent-default:"))
        );
        assert!(d.images.contains("fixture/base:dev"));
    }
    fixture.daemon.lock().unwrap().images.remove(&tag);
    assert_eq!(fixture.build(&state, false).await["status"], "ready");
    assert_eq!(fixture.daemon.lock().unwrap().pulls, 3);
    // Force bypasses both registry and local image cache.
    assert_eq!(fixture.build(&state, true).await["status"], "ready");
    let d = fixture.daemon.lock().unwrap();
    assert_eq!((d.pulls, d.builds), (3, 2));
}

#[tokio::test]
async fn a_changed_context_pulls_only_the_new_source_tag_and_staging_aliases_stay_local() {
    let fixture = Fixture::new().await;
    fixture.seed_artcraft();
    let original = fixture.state(fixture.image_settings(true));
    fixture.build(&original, false).await;
    let old_tag = original.config.runtime.as_ref().unwrap().agent_sets["default"].clone();
    std::fs::write(
        fixture
            .root
            .path()
            .join("images/agent-sets/default/Dockerfile"),
        b"FROM scratch\nLABEL changed=yes\n",
    )
    .unwrap();
    let changed = fixture.state(fixture.image_settings(true));
    let new_tag = changed.config.runtime.as_ref().unwrap().agent_sets["default"].clone();
    assert_ne!(old_tag, new_tag);
    assert_eq!(fixture.build(&changed, false).await["status"], "ready");
    {
        let d = fixture.daemon.lock().unwrap();
        assert_eq!(d.pulls, 3);
        assert!(d.images.contains(&old_tag) && d.images.contains(&new_tag));
        assert_eq!(d.builds, 0);
    }
    let mut config = fixture.image_settings(true);
    let mut images = config.image_build.take().unwrap();
    images.local_prefix = "staging/pi-".into();
    images.base_alias = "staging/pi-base:dev".into();
    let stage = fixture.state(config.with_local_image_build(images));
    let result = fixture.build(&stage, false).await;
    assert_eq!(result["status"], "ready");
    let d = fixture.daemon.lock().unwrap();
    assert!(d.images.contains("staging/pi-base:dev"));
    assert!(
        result["images"]
            .as_object()
            .unwrap()
            .values()
            .all(|tag| tag.as_str().unwrap().starts_with("staging/pi-"))
    );
}

#[tokio::test]
async fn registry_outage_falls_back_to_pinned_base_build_and_combined_failure_stays_degraded() {
    for failure in [400, 401, 404, 429, 500] {
        let fixture = Fixture::new().await;
        fixture.seed_artcraft();
        fixture.daemon.lock().unwrap().fail.push((
            "POST".into(),
            "/images/create".into(),
            failure,
            1,
        ));
        let state = fixture.state(fixture.image_settings(true));
        assert_eq!(fixture.build(&state, false).await["status"], "ready");
        let d = fixture.daemon.lock().unwrap();
        assert_eq!(d.builds, 1);
        assert_eq!(d.pulls, 1);
        let agent = d
            .images
            .iter()
            .find(|tag| tag.starts_with("fixture/agent-default:"))
            .unwrap();
        assert!(!agent.is_empty());
    }
    let retag = Fixture::new().await;
    retag.seed_artcraft();
    retag.daemon.lock().unwrap().fail.push((
        "POST".into(),
        "/images/registry.invalid/".into(),
        500,
        1000,
    ));
    let state = retag.state(retag.image_settings(true));
    assert_eq!(retag.build(&state, false).await["status"], "ready");
    assert_eq!(retag.daemon.lock().unwrap().builds, 2);
    let fixture = Fixture::new().await;
    fixture.seed_artcraft();
    fixture.daemon.lock().unwrap().fail.extend([
        ("POST".into(), "/images/create".into(), 500, 2),
        ("POST".into(), "/build".into(), 500, 2),
    ]);
    let state = fixture.state(fixture.image_settings(true));
    let result = fixture.build(&state, false).await;
    assert_eq!(result["status"], "failed");
    assert!(!result.to_string().contains("synthetic-private"));
}

#[tokio::test]
async fn pulled_base_is_used_by_hash_for_the_agent_fallback_build() {
    let fixture = Fixture::new().await;
    fixture.seed_artcraft();
    let state = fixture.state(fixture.image_settings(true));
    // Fail only the agent's pull, not the base pull.
    let tag = state.config.runtime.as_ref().unwrap().agent_sets["default"].clone();
    // Seed base source tag as if pulled on an earlier pass; leave the agent missing.
    let base = format!(
        "fixture/base:{}",
        state.config.image_build.as_ref().unwrap().fingerprints.base
    );
    fixture.daemon.lock().unwrap().images.insert(base.clone());
    fixture
        .daemon
        .lock()
        .unwrap()
        .fail
        .push(("POST".into(), "/images/create".into(), 500, 1));
    assert_eq!(fixture.build(&state, false).await["status"], "ready");
    let d = fixture.daemon.lock().unwrap();
    assert_eq!(d.builds, 1);
    assert!(d.images.contains(&tag));
    let args = &d
        .calls
        .iter()
        .find(|(_, path, _)| path == "/build")
        .unwrap()
        .2["query"];
    assert_eq!(
        serde_json::from_str::<Value>(args["buildargs"].as_str().unwrap()).unwrap(),
        json!({"BASE_IMAGE":base})
    );
    assert_eq!(args["pull"], "false");
    assert_eq!(args["rm"], "true");
}

#[tokio::test]
async fn image_wait_reports_build_status_then_a_terminal_failure_or_deadline_instead_of_hanging() {
    for failed in [false, true] {
        let fixture = Fixture::new().await;
        let state = fixture.state(fixture.image_settings(false));
        state.build_state.lock().unwrap().status = "building";
        let (send, mut receive) = tokio::sync::mpsc::channel(10);
        let waiting = state.ensure_image_for_turn("fixture/missing:v1", &send);
        let mutate = async {
            let progress: Value = serde_json::from_str(&receive.recv().await.unwrap()).unwrap();
            assert_eq!(progress["type"], "status");
            assert_eq!(progress["state"], "building");
            assert_eq!(progress["message"], "Setting things up");
            if failed {
                let mut build = state.build_state.lock().unwrap();
                build.status = "failed";
                build.error = Some("no space left on device");
            }
        };
        let (result, _) = tokio::join!(waiting, mutate);
        let error = result.unwrap_err();
        if failed {
            assert_eq!(
                error,
                "the agent image failed to build: no space left on device"
            );
        } else {
            assert!(error.contains("was still building after"));
        }
    }
}

#[tokio::test]
async fn artifact_failure_blocks_readiness_and_can_be_repaired_without_rebuilding_images() {
    let fixture = Fixture::new().await;
    fixture.seed_artcraft();
    let state = fixture.state(fixture.image_settings(false));
    fixture.daemon.lock().unwrap().helper_exit = 1;
    // Existing invalid defaults cannot be replaced while their native verifier
    // returns failure. Staging is a test dependency, never a request option.
    let result = fixture.build(&state, false).await;
    assert_eq!(result["status"], "failed");
    fixture.daemon.lock().unwrap().helper_exit = 0;
    fixture.seed_artcraft();
    assert_eq!(fixture.build(&state, false).await["status"], "ready");
    assert_eq!(fixture.daemon.lock().unwrap().builds, 2);
    let name = fixture
        .daemon
        .lock()
        .unwrap()
        .volumes
        .keys()
        .next()
        .unwrap()
        .clone();
    fixture.daemon.lock().unwrap().volumes.remove(&name);
    assert_eq!(state.images_snapshot().await["status"], "missing");
}
