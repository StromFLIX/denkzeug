use super::*;

fn credentials() -> Value {
    json!([{"full_name":"owner/repo","token":"fixture-private-renewal","expires_at":"2099-01-01T00:00:00Z"}])
}
async fn target(
    fixture: &Fixture,
    state: &Arc<Broker>,
    chat: &str,
    turn: &str,
) -> (TurnKey, Arc<turns::Turn>, String) {
    let key = TurnKey::new(chat, turn).unwrap();
    let owner = state.turns.begin(key.clone()).unwrap();
    let id = {
        let mut d = fixture.daemon.lock().unwrap();
        let id = next_id(&mut d);
        d.containers.insert(id.clone(),json!({"Name":"/agent","Config":{"Labels":{"nautionette.chat":chat,"nautionette.turn":turn}},"Mounts":[{"Type":"volume","Name":"fixture-workflows","Destination":"/workflows"}],"State":{"Running":true,"Status":"running"}}));
        id
    };
    state.live_credentials.lock().await.insert(
        key.clone(),
        Arc::new(runtime::LiveCredential::new(
            id.clone(),
            BTreeSet::from(["owner/repo".into()]),
        )),
    );
    (key, owner, id)
}

#[tokio::test]
async fn renewals_upload_private_data_then_activate_atomically_without_argv_secrets() {
    let fixture = Fixture::new().await;
    let state = fixture.state(fixture.settings());
    let (_key, _owner, _id) = target(&fixture, &state, "chat", "turn").await;
    let result = fixture
        .request(
            router_for_state(state),
            "/agent/project-credentials",
            json!({"chat_id":"chat","turn_id":"turn","credentials":credentials()}),
        )
        .await;
    assert_eq!(result, (StatusCode::OK, json!({"ok":true})));
    let d = fixture.daemon.lock().unwrap();
    let mut tar = tar::Archive::new(d.archives[0].as_slice());
    let entry = tar.entries().unwrap().next().unwrap().unwrap();
    assert_eq!(entry.path_bytes().as_ref(), b"pending.json");
    assert_eq!(
        (
            entry.header().mode().unwrap(),
            entry.header().uid().unwrap(),
            entry.header().gid().unwrap()
        ),
        (0o440, 0, 10001)
    );
    let command = d.execs.values().next().unwrap();
    assert_eq!(command["User"], "0:0");
    assert!(!command.to_string().contains("fixture-private"));
    assert!(command["Cmd"][2].as_str().unwrap().contains("renameSync"));
    let upload = d.calls.iter().position(|(m, _, _)| m == "PUT").unwrap();
    let activate = d
        .calls
        .iter()
        .position(|(_, p, _)| p.ends_with("/exec"))
        .unwrap();
    assert!(upload < activate);
}

#[tokio::test]
async fn renewal_never_targets_wrong_stopped_retired_or_foreign_turn() {
    for case in [
        "wrong-chat",
        "wrong-turn",
        "stopped",
        "retired",
        "orphan",
        "foreign",
        "exited",
        "duplicate",
    ] {
        let fixture = Fixture::new().await;
        let state = fixture.state(fixture.settings());
        let (key, owner, id) = target(&fixture, &state, "chat", "turn").await;
        let (mut chat, mut turn) = ("chat", "turn");
        match case {
            "wrong-chat" => chat = "other",
            "wrong-turn" => turn = "other",
            "stopped" => {
                state.turns.request_stop(&key);
            }
            "orphan" => state.turns.finish(&owner),
            "retired" => {
                let _ = state
                    .turns
                    .cleanup(state.as_ref(), &key, Duration::ZERO)
                    .await;
            }
            "foreign" => {
                fixture
                    .daemon
                    .lock()
                    .unwrap()
                    .containers
                    .get_mut(&id)
                    .unwrap()["Mounts"][0]["Name"] = json!("other-workflows")
            }
            "exited" => {
                fixture
                    .daemon
                    .lock()
                    .unwrap()
                    .containers
                    .get_mut(&id)
                    .unwrap()["State"]["Running"] = json!(false)
            }
            "duplicate" => {
                let mut d = fixture.daemon.lock().unwrap();
                let row = d.containers[&id].clone();
                d.containers.insert("e".repeat(64), row);
            }
            _ => unreachable!(),
        }
        let result = fixture
            .request(
                router_for_state(state),
                "/agent/project-credentials",
                json!({"chat_id":chat,"turn_id":turn,"credentials":credentials()}),
            )
            .await;
        assert_eq!(result, (StatusCode::OK, json!({"ok":false})), "{case}");
        assert!(fixture.daemon.lock().unwrap().archives.is_empty());
    }
}

#[tokio::test]
async fn renewal_scope_is_exact_and_every_failure_is_retryable_and_redacted() {
    for error in ["scope", "upload", "exec", "exit", "inspect"] {
        let fixture = Fixture::new().await;
        let state = fixture.state(fixture.settings());
        let (_, _, id) = target(&fixture, &state, "chat", "turn").await;
        let mut batch = credentials();
        match error {
            "scope" => batch[0]["full_name"] = json!("owner/elsewhere"),
            "upload" => fixture.daemon.lock().unwrap().fail.push((
                "PUT".into(),
                format!("/containers/{id}/archive"),
                500,
                1,
            )),
            "exec" => fixture.daemon.lock().unwrap().fail.push((
                "POST".into(),
                format!("/containers/{id}/exec"),
                500,
                1,
            )),
            "exit" => fixture.daemon.lock().unwrap().exec_exit = 1,
            "inspect" => fixture.daemon.lock().unwrap().fail.push((
                "GET".into(),
                format!("/containers/{id}/json"),
                500,
                1,
            )),
            _ => unreachable!(),
        }
        let data = json!({"chat_id":"chat","turn_id":"turn","credentials":batch});
        let result = fixture
            .request(
                router_for_state(state.clone()),
                "/agent/project-credentials",
                data,
            )
            .await;
        assert_eq!(
            result.0,
            if error == "scope" {
                StatusCode::UNPROCESSABLE_ENTITY
            } else {
                StatusCode::SERVICE_UNAVAILABLE
            }
        );
        assert!(!result.1.to_string().contains("fixture-private"));
        fixture.daemon.lock().unwrap().exec_exit = 0;
        let result = fixture
            .request(
                router_for_state(state),
                "/agent/project-credentials",
                json!({"chat_id":"chat","turn_id":"turn","credentials":credentials()}),
            )
            .await;
        assert_eq!(result.1, json!({"ok":true}));
    }
}

#[tokio::test]
async fn stalled_turn_renewal_does_not_block_another_chat() {
    let fixture = Fixture::new().await;
    let state = fixture.state(fixture.settings());
    let (a, _, _) = target(&fixture, &state, "chat-a", "turn").await;
    let (_b, _, _) = target(&fixture, &state, "chat-b", "turn").await;
    let live = state.live_credentials.lock().await[&a].clone();
    let held = live.lock.lock().await;
    let a_data = json!({"chat_id":"chat-a","turn_id":"turn","credentials":credentials()});
    let b_data = json!({"chat_id":"chat-b","turn_id":"turn","credentials":credentials()});
    let a_future = fixture.request(
        router_for_state(state.clone()),
        "/agent/project-credentials",
        a_data,
    );
    tokio::pin!(a_future);
    assert!(
        tokio::time::timeout(Duration::from_millis(20), &mut a_future)
            .await
            .is_err()
    );
    assert_eq!(
        fixture
            .request(
                router_for_state(state),
                "/agent/project-credentials",
                b_data
            )
            .await
            .1,
        json!({"ok":true})
    );
    drop(held);
    assert_eq!(a_future.await.1, json!({"ok":true}));
}
