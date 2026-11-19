use super::*;

#[tokio::test]
async fn missing_workers_are_recreated_once_with_shared_workflows() {
    let fixture = Fixture::new().await;
    fixture.daemon.lock().unwrap().worker_health = Some("healthy");
    let state = fixture.state(fixture.worker_settings());
    state.reconcile_workers().await;
    state.reconcile_workers().await;
    let health = state.workers_snapshot().await;
    assert_eq!(health["status"], "ready");
    assert_eq!(health["ready"], 1);
    let data = fixture.daemon.lock().unwrap();
    let creates: Vec<_> = data
        .calls
        .iter()
        .filter(|(m, p, _)| m == "POST" && p == "/containers/create")
        .collect();
    assert_eq!(creates.len(), 1);
    let body = &creates[0].2;
    assert_eq!(body["Image"], "fixture-worker:v1");
    assert_eq!(body["Env"], json!(["TEMPORAL_ADDRESS=temporal:7233"]));
    assert_eq!(
        body["HostConfig"]["Binds"],
        json!(["fixture-workflows:/workflows:rw", "artifacts:/artifacts:rw"])
    );
    assert_eq!(body["HostConfig"]["NetworkMode"], "control");
    assert_eq!(
        body["HostConfig"]["RestartPolicy"]["Name"],
        "unless-stopped"
    );
    assert_eq!(
        body["Labels"],
        json!({"com.docker.compose.project":"stack","com.docker.compose.service":"worker","com.docker.compose.container-number":"1"})
    );
}

#[tokio::test]
async fn stopped_unhealthy_paused_and_dead_workers_are_repaired_in_place() {
    for (status, health, action, replaced) in [
        ("created", None, "start", false),
        ("exited", None, "start", false),
        ("paused", None, "unpause", false),
        ("running", Some("unhealthy"), "restart", false),
        ("dead", None, "", true),
    ] {
        let fixture = Fixture::new().await;
        let id = fixture.worker(status, health);
        let state = fixture.state(fixture.worker_settings());
        state.reconcile_workers().await;
        let data = fixture.daemon.lock().unwrap();
        let (method, path) = if replaced {
            ("DELETE", format!("/containers/{id}"))
        } else {
            ("POST", format!("/containers/{id}/{action}"))
        };
        assert!(
            data.calls
                .iter()
                .any(|(m, p, _)| *m == method && *p == path),
            "{status}"
        );
        assert_eq!(
            data.calls
                .iter()
                .filter(|(m, p, _)| m == "POST" && p == "/containers/create")
                .count(),
            usize::from(replaced)
        );
    }
}

#[tokio::test]
async fn unstartable_workers_do_not_create_unbounded_replacements() {
    let fixture = Fixture::new().await;
    let id = fixture.worker("created", None);
    fixture.daemon.lock().unwrap().fail.push((
        "POST".into(),
        format!("/containers/{id}/start"),
        500,
        20,
    ));
    let state = fixture.state(fixture.worker_settings());
    for _ in 0..4 {
        state.reconcile_workers().await;
    }
    assert_eq!(state.workers_snapshot().await["status"], "degraded");
    let d = fixture.daemon.lock().unwrap();
    assert_eq!(d.containers.len(), 1);
    assert!(!d.calls.iter().any(|(_, p, _)| p == "/containers/create"));
}

#[tokio::test]
async fn failed_create_and_daemon_loss_retry_and_recover() {
    for (method, path) in [("POST", "/containers/create"), ("GET", "/containers/json")] {
        let fixture = Fixture::new().await;
        fixture
            .daemon
            .lock()
            .unwrap()
            .fail
            .push((method.into(), path.into(), 500, 1));
        fixture.daemon.lock().unwrap().worker_health = Some("healthy");
        let state = fixture.state(fixture.worker_settings());
        state.reconcile_workers().await;
        assert_eq!(state.workers_snapshot().await["status"], "degraded");
        state.reconcile_workers().await;
        assert_eq!(state.workers_snapshot().await["status"], "ready");
    }
}

#[tokio::test]
async fn readiness_requires_an_explicit_health_probe() {
    let fixture = Fixture::new().await;
    fixture.worker("running", None);
    let state = fixture.state(fixture.worker_settings());
    assert_eq!(state.workers_snapshot().await["ready"], 0);
    assert_eq!(state.workers_snapshot().await["status"], "degraded");
}

#[tokio::test]
async fn restart_fails_closed_without_stack_identity_and_empty_is_success() {
    let fixture = Fixture::new().await;
    let mut config = fixture.worker_settings();
    config.workers.as_mut().unwrap().project_override = None;
    let state = fixture.state(config);
    state.reconcile_workers().await;
    assert!(fixture.daemon.lock().unwrap().containers.is_empty());
    let answer = fixture
        .request(router_for_state(state), "/worker/restart", json!({}))
        .await;
    assert_eq!(answer.0, StatusCode::SERVICE_UNAVAILABLE);
    let empty = fixture
        .request(
            broker(fixture.worker_settings()),
            "/worker/restart",
            json!({}),
        )
        .await;
    assert_eq!(empty.0, StatusCode::OK);
    assert_eq!(empty.1["restarted"], json!([]));
}

#[tokio::test]
async fn health_requires_all_resources_and_pruning_one_does_not_hide_healthy_workers() {
    let fixture = Fixture::new().await;
    fixture.seed_artcraft();
    let worker = fixture.worker_settings().workers.take().unwrap();
    let state = fixture.state(
        fixture
            .image_settings(false)
            .with_local_workers(worker, None),
    );
    fixture.build(&state, false).await;
    let ready_sets = image_build::agent_sets(State(state.clone())).await.0;
    assert_eq!(ready_sets["agent_sets"][0]["ready"], true);
    assert_eq!(
        ready_sets["agent_sets"][0]["image"],
        state.config.runtime.as_ref().unwrap().agent_sets["default"]
    );
    let response = router_for_state(state.clone())
        .oneshot(
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let health: Value =
        serde_json::from_slice(&to_bytes(response.into_body(), 1024 * 1024).await.unwrap())
            .unwrap();
    assert_eq!(health["status"], "degraded");
    assert_eq!(health["image_status"], "ready");
    fixture.worker("running", Some("healthy"));
    let response = router_for_state(state.clone())
        .oneshot(
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let health: Value =
        serde_json::from_slice(&to_bytes(response.into_body(), 1024 * 1024).await.unwrap())
            .unwrap();
    assert_eq!(health["status"], "ok");
    fixture
        .daemon
        .lock()
        .unwrap()
        .images
        .remove(&state.config.runtime.as_ref().unwrap().agent_sets["default"]);
    let missing_sets = image_build::agent_sets(State(state.clone())).await.0;
    assert_eq!(missing_sets["agent_sets"][0]["ready"], false);
    let response = router_for_state(state)
        .oneshot(
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let health: Value =
        serde_json::from_slice(&to_bytes(response.into_body(), 1024 * 1024).await.unwrap())
            .unwrap();
    assert_eq!(health["status"], "degraded");
    assert_eq!(health["image_status"], "missing");
    assert_eq!(health["workers"]["ready"], 1);
}

#[tokio::test]
async fn monitor_starts_immediately_repairs_after_pruning_and_stops_when_its_router_is_dropped() {
    let fixture = Fixture::new().await;
    fixture.seed_artcraft();
    fixture.daemon.lock().unwrap().worker_health = Some("healthy");
    let worker = fixture.worker_settings().workers.take().unwrap();
    let mut config = fixture
        .image_settings(false)
        .with_local_workers(worker, Some(Duration::from_millis(20)));
    // Legacy resource cleanup needs a broker identity, and fails independently
    // without one. Image/worker reconciliation must still execute this pass.
    config.workers.as_mut().unwrap().docker_self = "not-present".into();
    let state = fixture.state(config);
    let weak = Arc::downgrade(&state);
    let app = router_for_state(state.clone());
    tokio::time::timeout(Duration::from_secs(8), async {
        loop {
            if state.workers_snapshot().await["ready"] == 1
                && state.images_snapshot().await["status"] == "ready"
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    fixture.daemon.lock().unwrap().images.clear();
    fixture.daemon.lock().unwrap().containers.clear();
    tokio::time::timeout(Duration::from_secs(8), async {
        loop {
            if state.workers_snapshot().await["ready"] == 1
                && state.images_snapshot().await["status"] == "ready"
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(fixture.daemon.lock().unwrap().builds, 4);
    drop(app);
    drop(state);
    tokio::time::timeout(Duration::from_secs(2), async {
        while weak.strong_count() > 0 {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let before = fixture.daemon.lock().unwrap().calls.len();
    tokio::time::sleep(Duration::from_millis(60)).await;
    assert_eq!(fixture.daemon.lock().unwrap().calls.len(), before);
}

#[tokio::test]
async fn monitor_does_not_skip_worker_repair_after_image_inspection_failure() {
    let fixture = Fixture::new().await;
    fixture.daemon.lock().unwrap().worker_health = Some("healthy");
    fixture
        .daemon
        .lock()
        .unwrap()
        .fail
        .push(("GET".into(), "/images/".into(), 500, 1000));
    let worker = fixture.worker_settings().workers.take().unwrap();
    let state = fixture.state(
        fixture
            .image_settings(false)
            .with_local_workers(worker, Some(Duration::from_millis(20))),
    );
    let app = router_for_state(state.clone());
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if state.workers_snapshot().await["ready"] == 1 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert_ne!(state.images_snapshot().await["status"], "ready");
    drop(app);
}

#[tokio::test]
async fn managed_lifespan_joins_the_monitor_without_waiting_for_its_poll_interval() {
    let fixture = Fixture::new().await;
    fixture.daemon.lock().unwrap().worker_health = Some("healthy");
    let mut config = fixture.worker_settings();
    config.reconcile_interval = Some(Duration::from_secs(60));
    let (router, shutdown) = managed_broker(config);
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if fixture
                .daemon
                .lock()
                .unwrap()
                .calls
                .iter()
                .any(|(method, path, _)| method == "POST" && path.ends_with("/start"))
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    drop(router);
    tokio::time::timeout(Duration::from_secs(2), shutdown.finish())
        .await
        .unwrap();
    let calls = fixture.daemon.lock().unwrap().calls.len();
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert_eq!(fixture.daemon.lock().unwrap().calls.len(), calls);
}

#[tokio::test]
async fn restart_and_reconcile_are_serialized_and_stack_scoped() {
    let fixture = Fixture::new().await;
    let owned = fixture.worker("running", Some("healthy"));
    let foreign = fixture.worker("running", Some("healthy"));
    fixture
        .daemon
        .lock()
        .unwrap()
        .containers
        .get_mut(&foreign)
        .unwrap()["Config"]["Labels"]["com.docker.compose.project"] = json!("other-stack");
    let state = fixture.state(fixture.worker_settings());
    let (a, ()) = tokio::join!(
        fixture.request(
            router_for_state(state.clone()),
            "/worker/restart",
            json!({})
        ),
        state.reconcile_workers()
    );
    assert_eq!(a.1["grace_seconds"], 50);
    assert_eq!(a.1["restarted"], json!(["stack-worker-1"]));
    let data = fixture.daemon.lock().unwrap();
    assert!(
        data.calls
            .iter()
            .any(|(m, p, _)| m == "POST" && p == &format!("/containers/{owned}/restart"))
    );
    assert!(
        !data
            .calls
            .iter()
            .any(|(m, p, _)| m == "POST" && p.contains(&foreign))
    );
}
