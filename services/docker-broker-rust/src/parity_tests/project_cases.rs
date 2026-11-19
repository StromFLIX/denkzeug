use super::*;

fn storage(fixture: &Fixture) {
    let sessions = fixture.root.path().join("sessions");
    let projects = fixture.root.path().join("projects");
    std::fs::create_dir_all(&sessions).unwrap();
    std::fs::create_dir_all(projects.join("a".repeat(32)).join(".git")).unwrap();
    std::fs::create_dir_all(
        projects
            .join(".sessions")
            .join("a".repeat(32))
            .join("c".repeat(12)),
    )
    .unwrap();
    let mut d = fixture.daemon.lock().unwrap();
    d.images.insert("fixture-agent:v1".into());
    d.containers.insert("f".repeat(64),json!({"Name":"/broker","Config":{"Labels":{}},"Mounts":[{"Type":"volume","Name":"fixture-projects","Destination":projects},{"Type":"volume","Name":"fixture-sessions","Destination":sessions}],"State":{"Status":"running"}}));
}
fn job() -> Value {
    json!({"chat_id":"c".repeat(12),"turn_id":"turn","project_ids":["a".repeat(32),"a".repeat(32)],"project_remotes":{("a".repeat(32)):"owner/repo"},"project_credentials":[{"full_name":"owner/repo","token":"fixture-private-project","expires_at":"2099-01-01T00:00:00Z"}]})
}
async fn run(fixture: &Fixture, state: Arc<Broker>, job: Value) -> Vec<Value> {
    let response = router_for_state(state)
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/agent/run")
                .header("content-type", "application/json")
                .header("x-internal-token", "test-token")
                .body(Body::from(job.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
    let _ = fixture;
    std::str::from_utf8(&body)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

#[tokio::test]
async fn selected_project_mounts_are_exact_deduplicated_subpaths_and_claims_release() {
    let fixture = Fixture::new().await;
    storage(&fixture);
    let state = fixture.state(fixture.settings());
    for _ in 0..2 {
        let events = run(&fixture, state.clone(), job()).await;
        assert!(
            !events.iter().any(|event| event["type"] == "error"),
            "{events:?}"
        );
        assert_eq!(events.last().unwrap()["type"], "closed");
        assert!(state.turns.inventory("").is_empty());
    }
    let d = fixture.daemon.lock().unwrap();
    let create = &d
        .calls
        .iter()
        .find(|(_, p, _)| p == "/containers/create")
        .unwrap()
        .2;
    let mounts = create["HostConfig"]["Mounts"].as_array().unwrap();
    assert_eq!(mounts.len(), 4); // two project subpaths, session, workflows
    let git = mounts
        .iter()
        .find(|m| m["Target"] == format!("/project-repositories/{}", "a".repeat(32)))
        .unwrap();
    assert_eq!(git["Source"], "fixture-projects");
    assert_eq!(git["ReadOnly"], false);
    assert_eq!(
        git["VolumeOptions"]["Subpath"],
        format!("{}/.git", "a".repeat(32))
    );
    assert!(!create.to_string().contains("fixture-private-project"));
    let bytes = &d.archives[0];
    let mut archive = tar::Archive::new(bytes.as_slice());
    let entries = archive
        .entries()
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(entries[0].header().mode().unwrap(), 0o711);
}

#[tokio::test]
async fn bad_project_ids_paths_missing_mounts_and_symlinks_fail_before_create() {
    for bad in ["../escape", "/etc", "", "b"] {
        let fixture = Fixture::new().await;
        storage(&fixture);
        let mut payload = job();
        payload["project_ids"] = json!([bad]);
        let events = run(&fixture, fixture.state(fixture.settings()), payload).await;
        assert!(events.iter().any(|e| e["type"] == "error"));
        assert!(
            !fixture
                .daemon
                .lock()
                .unwrap()
                .calls
                .iter()
                .any(|(_, p, _)| p == "/containers/create")
        );
    }
    for failure in [
        "volume",
        "missing-mount",
        "wrong-destination",
        "missing-name",
        "foreign",
        "missing",
        "symlink",
        "git-symlink",
        "sessions-symlink",
    ] {
        let fixture = Fixture::new().await;
        storage(&fixture);
        let projects = fixture.root.path().join("projects");
        let selected = projects.join("a".repeat(32));
        match failure {
            "volume" => {
                fixture
                    .daemon
                    .lock()
                    .unwrap()
                    .containers
                    .get_mut(&"f".repeat(64))
                    .unwrap()["Mounts"][0]["Type"] = json!("bind")
            }
            "missing-mount" => {
                fixture
                    .daemon
                    .lock()
                    .unwrap()
                    .containers
                    .get_mut(&"f".repeat(64))
                    .unwrap()["Mounts"]
                    .as_array_mut()
                    .unwrap()
                    .remove(0);
            }
            "wrong-destination" => {
                fixture
                    .daemon
                    .lock()
                    .unwrap()
                    .containers
                    .get_mut(&"f".repeat(64))
                    .unwrap()["Mounts"][0]["Destination"] = json!("/other");
            }
            "missing-name" => {
                fixture
                    .daemon
                    .lock()
                    .unwrap()
                    .containers
                    .get_mut(&"f".repeat(64))
                    .unwrap()["Mounts"][0]
                    .as_object_mut()
                    .unwrap()
                    .remove("Name");
            }
            "foreign" => {
                fixture.daemon.lock().unwrap().containers.clear();
            }
            "missing" => std::fs::remove_dir_all(&selected).unwrap(),
            "symlink" => {
                std::fs::remove_dir_all(&selected).unwrap();
                std::os::unix::fs::symlink(&projects, &selected).unwrap();
            }
            "git-symlink" => {
                std::fs::remove_dir(selected.join(".git")).unwrap();
                std::os::unix::fs::symlink(&projects, selected.join(".git")).unwrap();
            }
            "sessions-symlink" => {
                std::fs::remove_dir_all(projects.join(".sessions")).unwrap();
                std::os::unix::fs::symlink(&projects, projects.join(".sessions")).unwrap();
            }
            _ => unreachable!(),
        }
        let events = run(&fixture, fixture.state(fixture.settings()), job()).await;
        assert!(
            events.iter().any(|event| event["type"] == "error"),
            "{failure}: {events:?}"
        );
        assert!(
            !fixture
                .daemon
                .lock()
                .unwrap()
                .calls
                .iter()
                .any(|(_, p, _)| p == "/containers/create")
        );
    }
}

#[tokio::test]
async fn stale_project_owners_are_removed_only_when_exited_on_the_same_volume() {
    for (status, other, allowed) in [
        ("exited", false, true),
        ("dead", false, true),
        ("running", false, false),
        ("paused", false, false),
        ("restarting", false, false),
        ("created", false, false),
        ("removing", false, false),
        ("unknown", false, false),
        ("exited", true, true),
    ] {
        let fixture = Fixture::new().await;
        storage(&fixture);
        let old = "e".repeat(64);
        fixture.daemon.lock().unwrap().containers.insert(old.clone(),json!({"Name":"/old","Config":{"Labels":{(format!("nautionette.project.{}","a".repeat(32))):"c".repeat(12)}},"Mounts":[{"Type":"volume","Name":if other{"other-projects"}else{"fixture-projects"},"Destination":"/projects"}],"State":{"Status":status}}));
        let events = run(&fixture, fixture.state(fixture.settings()), job()).await;
        assert_eq!(
            !events.iter().any(|e| e["type"] == "error"),
            allowed,
            "{status} other={other} {events:?}"
        );
        assert_eq!(
            fixture.daemon.lock().unwrap().containers.contains_key(&old),
            !allowed || other
        );
    }
}

#[tokio::test]
async fn a_failed_stale_container_removal_never_releases_project_ownership() {
    let fixture = Fixture::new().await;
    storage(&fixture);
    let id = "e".repeat(64);
    fixture.daemon.lock().unwrap().containers.insert(id.clone(),json!({"Name":"/old","Config":{"Labels":{(format!("nautionette.project.{}","a".repeat(32))):"c".repeat(12)}},"Mounts":[{"Type":"volume","Name":"fixture-projects","Destination":"/projects"}],"State":{"Status":"exited"}}));
    fixture.daemon.lock().unwrap().fail.push((
        "DELETE".into(),
        format!("/containers/{id}"),
        409,
        1,
    ));
    let state = fixture.state(fixture.settings());
    let events = run(&fixture, state.clone(), job()).await;
    assert!(events.iter().any(|e| e["type"] == "error"));
    assert!(fixture.daemon.lock().unwrap().containers.contains_key(&id));
    assert!(
        !fixture
            .daemon
            .lock()
            .unwrap()
            .calls
            .iter()
            .any(|(_, p, _)| p == "/containers/create")
    );
    // The failed caller releases only its own host claim; retry still has to
    // inspect/remove the old Docker container before it can create a new one.
    let events = run(&fixture, state, job()).await;
    assert!(!events.iter().any(|e| e["type"] == "error"));
    assert!(!fixture.daemon.lock().unwrap().containers.contains_key(&id));
}

#[tokio::test]
async fn session_wrong_volume_symlinks_quota_and_orphan_fail_closed() {
    for failure in ["bind", "symlink", "quota", "orphan"] {
        let fixture = Fixture::new().await;
        storage(&fixture);
        let chat = "chat";
        let dir = fixture.root.path().join("sessions").join(chat);
        std::fs::create_dir(&dir).unwrap();
        match failure {
            "bind" => {
                fixture
                    .daemon
                    .lock()
                    .unwrap()
                    .containers
                    .get_mut(&"f".repeat(64))
                    .unwrap()["Mounts"][1]["Type"] = json!("bind")
            }
            "symlink" => std::os::unix::fs::symlink("/tmp", dir.join("unsafe")).unwrap(),
            "quota" => std::fs::write(dir.join("data"), vec![0; 4096]).unwrap(),
            "orphan" => {
                fixture.daemon.lock().unwrap().containers.insert("e".repeat(64),json!({"Name":"/orphan","Config":{"Labels":{"nautionette.chat":chat,"nautionette.turn":"old"}},"Mounts":[{"Type":"volume","Name":"fixture-workflows","Destination":"/workflows"}],"State":{"Status":"created"}}));
            }
            _ => unreachable!(),
        }
        let events = run(
            &fixture,
            fixture.state(fixture.settings()),
            json!({"chat_id":chat,"turn_id":"turn"}),
        )
        .await;
        assert!(
            events.iter().any(|e| e["type"] == "error"),
            "{failure} {events:?}"
        );
        assert!(
            !fixture
                .daemon
                .lock()
                .unwrap()
                .calls
                .iter()
                .any(|(_, p, _)| p == "/containers/create")
        );
    }
}
