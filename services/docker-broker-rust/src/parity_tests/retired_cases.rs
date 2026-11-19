use super::*;
use std::sync::atomic::Ordering;

fn resources(fixture: &Fixture) {
    let mut d = fixture.daemon.lock().unwrap();
    d.containers.insert("f".repeat(64),json!({"Id":"f".repeat(64),"Name":"/broker","Config":{"Labels":{"com.docker.compose.project":"stack"}},"Mounts":[],"State":{"Status":"running"}}));
    for (name, own) in [("own", true), ("foreign", false)] {
        let namespace = if own {
            "fixture-workflows"
        } else {
            "other-workflows"
        };
        let labels = json!({"nautionette.deployment":namespace,"nautionette.miniapp":"old"});
        let id = next_id(&mut d);
        d.containers.insert(id.clone(),json!({"Id":id,"Name":format!("/{name}"),"Config":{"Labels":labels},"Mounts":[],"State":{"Status":"running"}}));
        let id = next_id(&mut d);
        d.networks.insert(
            id.clone(),
            json!({"Id":id,"Name":name,"Labels":labels,"Containers":{("f".repeat(64)):{}}}),
        );
        let id = format!("sha256:{}", next_id(&mut d));
        d.image_labels.insert(id, labels);
        d.volumes.insert(name.into(),json!({"Name":name,"Labels":{"com.docker.compose.project":if own{"stack"}else{"other"},"com.docker.compose.volume":"miniapps"}}));
    }
    d.volumes.insert("unrelated".into(),json!({"Name":"unrelated","Labels":{"com.docker.compose.project":"stack","com.docker.compose.volume":"backend-data"}}));
}

#[tokio::test]
async fn retired_cleanup_is_idempotent_scoped_and_never_uses_global_prune() {
    let fixture = Fixture::new().await;
    resources(&fixture);
    let state = fixture.state(fixture.worker_settings());
    state.reconcile_retired().await;
    assert!(state.retired_complete.load(Ordering::Acquire));
    let calls = fixture.daemon.lock().unwrap().calls.len();
    state.reconcile_retired().await;
    let d = fixture.daemon.lock().unwrap();
    assert_eq!(d.calls.len(), calls);
    assert!(d.volumes.contains_key("foreign"));
    assert!(d.volumes.contains_key("unrelated"));
    assert!(!d.volumes.contains_key("own"));
    assert_eq!(d.networks.len(), 1);
    assert_eq!(d.image_labels.len(), 1);
    assert_eq!(d.containers.len(), 2);
    assert!(d.calls.iter().any(|(m, p, b)| m == "POST"
        && p.ends_with("/disconnect")
        && b["Container"] == "f".repeat(64)
        && b["Force"] == true));
    assert!(!d.calls.iter().any(|(_, p, _)| p.contains("prune")));
}

#[tokio::test]
async fn retired_partial_failure_is_not_marked_complete_and_retries_safely() {
    let fixture = Fixture::new().await;
    resources(&fixture);
    let state = fixture.state(fixture.worker_settings());
    fixture
        .daemon
        .lock()
        .unwrap()
        .fail
        .push(("DELETE".into(), "/volumes/own".into(), 409, 1));
    state.reconcile_retired().await;
    assert!(!state.retired_complete.load(Ordering::Acquire));
    assert!(fixture.daemon.lock().unwrap().volumes.contains_key("own"));
    state.reconcile_retired().await;
    assert!(state.retired_complete.load(Ordering::Acquire));
    assert!(!fixture.daemon.lock().unwrap().volumes.contains_key("own"));
}

#[tokio::test]
async fn retired_cleanup_without_own_stack_identity_does_not_list_or_remove() {
    let fixture = Fixture::new().await;
    resources(&fixture);
    fixture
        .daemon
        .lock()
        .unwrap()
        .containers
        .get_mut(&"f".repeat(64))
        .unwrap()["Config"]["Labels"] = json!({});
    let state = fixture.state(fixture.worker_settings());
    state.reconcile_retired().await;
    assert!(!state.retired_complete.load(Ordering::Acquire));
    let d = fixture.daemon.lock().unwrap();
    assert_eq!(d.calls.len(), 1);
    assert!(d.calls.iter().all(|(m, _, _)| m == "GET"));
}
