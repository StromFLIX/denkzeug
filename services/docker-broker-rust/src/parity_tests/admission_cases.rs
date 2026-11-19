use super::*;

#[tokio::test]
async fn retired_execution_and_network_routes_are_absent_in_the_full_runtime() {
    let fixture = Fixture::new().await;
    for path in [
        "/agent/internet",
        "/miniapps/capability",
        "/miniapps/old/deploy",
        "/miniapps/old/request",
        "/containers/create",
    ] {
        let app = broker(fixture.settings());
        let unauth = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(path)
                    .body(Body::from("{"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(unauth.status(), StatusCode::UNAUTHORIZED);
        let rejected = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(path)
                    .header("x-internal-token", "test-token")
                    .header("content-type", "application/json")
                    .body(Body::from("{}"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(rejected.status(), StatusCode::NOT_FOUND);
    }
    assert!(fixture.daemon.lock().unwrap().calls.is_empty());
}

#[tokio::test]
async fn unknown_agent_set_is_a_single_explicit_ndjson_error_without_any_docker_request() {
    let fixture = Fixture::new().await;
    let response = broker(fixture.settings())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/agent/run")
                .header("x-internal-token", "test-token")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"agent_set":"made-up"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = to_bytes(response.into_body(), 1024).await.unwrap();
    assert_eq!(
        std::str::from_utf8(&bytes).unwrap(),
        "{\"message\":\"unknown agent set 'made-up'\",\"type\":\"error\"}\n"
    );
    assert!(fixture.daemon.lock().unwrap().calls.is_empty());
}

#[tokio::test]
async fn administrative_json_rejections_are_422_and_never_echo_private_input() {
    let fixture = Fixture::new().await;
    for path in [
        "/agent/run",
        "/agent/control",
        "/agent/project-credentials",
        "/agent/cleanup",
        "/packages/install",
        "/artcraft/install",
    ] {
        let response = broker(fixture.settings())
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(path)
                    .header("content-type", "application/json")
                    .header("x-internal-token", "test-token")
                    .body(Body::from("{\"synthetic-private-input\":"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            response.status(),
            StatusCode::UNPROCESSABLE_ENTITY,
            "{path}"
        );
        let bytes = to_bytes(response.into_body(), 1024).await.unwrap();
        assert!(!String::from_utf8_lossy(&bytes).contains("synthetic-private-input"));
    }
    assert!(fixture.daemon.lock().unwrap().calls.is_empty());
}

#[tokio::test]
async fn control_requires_an_exact_chat_turn_and_mount_even_with_forged_stack_labels() {
    let fixture = Fixture::new().await;
    let own = "a".repeat(64);
    let foreign = "b".repeat(64);
    let other = "c".repeat(64);
    let row = |chat, turn, volume| json!({"Name":"/agent","Config":{"Labels":{"nautionette.chat":chat,"nautionette.turn":turn,"nautionette.deployment":"fixture-workflows"}},"Mounts":[{"Type":"volume","Name":volume,"Destination":"/workflows"}],"State":{"Status":"running","Running":true}});
    {
        let mut d = fixture.daemon.lock().unwrap();
        d.containers
            .insert(own.clone(), row("chat", "turn", "fixture-workflows"));
        d.containers
            .insert(foreign.clone(), row("chat", "turn", "other-workflows"));
        d.containers
            .insert(other.clone(), row("chat", "different", "fixture-workflows"));
    }
    let app = broker(fixture.settings());
    for (chat, turn, expected) in [
        ("wrong", "turn", false),
        ("chat", "missing", false),
        ("chat", "turn", true),
    ] {
        let result = fixture
            .request(
                app.clone(),
                "/agent/control",
                json!({"chat_id":chat,"turn_id":turn,"command":{"type":"stop","id":"cmd"}}),
            )
            .await;
        assert_eq!(result, (StatusCode::OK, json!({"ok":expected})));
    }
    let data = fixture.daemon.lock().unwrap();
    assert!(
        data.calls
            .iter()
            .any(|(m, p, _)| m == "POST" && p == &format!("/containers/{own}/kill"))
    );
    for id in [foreign, other] {
        assert!(
            !data
                .calls
                .iter()
                .any(|(m, p, _)| m == "POST" && p.contains(&id))
        );
    }
}

#[tokio::test]
async fn package_runtime_validation_rejects_resource_escape_before_mount_or_container_creation() {
    let fixture = Fixture::new().await;
    for runtime in [
        json!([{"installation_id":"../../host"}]),
        json!([{"installation_id":"a".repeat(32),"root":"../escape","filters":{},"configuration":{}}]),
    ] {
        let response = broker(fixture.settings())
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/agent/run")
                    .header("x-internal-token", "test-token")
                    .header("content-type", "application/json")
                    .body(Body::from(json!({"package_runtime":runtime}).to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let bytes = to_bytes(response.into_body(), 1024).await.unwrap();
        assert!(std::str::from_utf8(&bytes).unwrap().contains("error"));
    }
    assert!(fixture.daemon.lock().unwrap().calls.is_empty());
}
