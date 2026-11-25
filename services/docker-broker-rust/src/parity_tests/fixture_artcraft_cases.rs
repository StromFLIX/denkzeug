use super::*;

#[tokio::test]
async fn artifact_fixture_preparation_uses_immutable_image_and_fresh_offline_verification() {
    let fixture = Fixture::new().await;
    {
        let mut d = fixture.daemon.lock().unwrap();
        d.images.insert("pi:prepared".into());
        d.image_labels.insert("pi:prepared".into(), json!({}));
    }
    let first = fixture.state(fixture.settings());
    let mounts = crate::artcraft::prepare_fixture_artifacts(first, "pi:prepared", false)
        .await
        .unwrap();
    assert!(!mounts.is_empty());
    assert!(mounts.iter().all(
        |m| m.starts_with("--mount=type=volume,source=nautionette-artcraft-")
            && m.ends_with(",readonly")
    ));
    let count = mounts.len();
    let before = fixture.daemon.lock().unwrap().calls.len();
    // New state simulates another executable invocation: no verification cache.
    let fresh = fixture.state(fixture.settings());
    assert_eq!(
        crate::artcraft::prepare_fixture_artifacts(fresh, "pi:prepared", true)
            .await
            .unwrap(),
        mounts
    );
    let d = fixture.daemon.lock().unwrap();
    let verifies: Vec<_> = d.calls[before..]
        .iter()
        .filter(|(_, p, _)| p == "/containers/create")
        .collect();
    assert_eq!(verifies.len(), count);
    for (_, _, body) in verifies {
        assert_eq!(body["Image"], format!("sha256:{}", "a".repeat(64)));
        assert_eq!(body["HostConfig"]["NetworkMode"], "none");
        assert_eq!(body["HostConfig"]["Mounts"][0]["ReadOnly"], true);
        assert_eq!(body["Entrypoint"][2], "--verify");
    }
    assert!(!d.calls[before..].iter().any(|(m, p, _)| m == "PUT"
        || p == "/volumes/create"
        || p == "/images/create"
        || p == "/build"
        || m == "DELETE" && p.starts_with("/volumes/")));
}

#[tokio::test]
async fn artifact_fixture_verify_only_never_downloads_or_repairs_missing_artifacts() {
    let fixture = Fixture::new().await;
    {
        let mut d = fixture.daemon.lock().unwrap();
        d.images.insert("pi:prepared".into());
        d.image_labels.insert("pi:prepared".into(), json!({}));
    }
    assert!(
        crate::artcraft::prepare_fixture_artifacts(
            fixture.state(fixture.settings()),
            "pi:prepared",
            true
        )
        .await
        .is_err()
    );
    assert!(
        fixture
            .daemon
            .lock()
            .unwrap()
            .calls
            .iter()
            .all(|(m, _, _)| m == "GET")
    );
}
