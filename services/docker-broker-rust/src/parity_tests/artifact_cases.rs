use super::*;

fn artifact(fixture: &Fixture, index: usize) -> (String, Value, String) {
    let (name, spec) = fixture.seed_artcraft()[index].clone();
    let id = fixture.daemon.lock().unwrap().volumes[&name]["Labels"]["nautionette.artcraft"]
        .as_str()
        .unwrap()
        .to_owned();
    (id, spec, name)
}

#[tokio::test]
async fn new_artcraft_installs_have_only_a_fixed_network_none_helper_and_private_staging() {
    let fixture = Fixture::new().await;
    let (id, spec, name) = artifact(&fixture, 0);
    fixture.daemon.lock().unwrap().volumes.remove(&name);
    fixture
        .daemon
        .lock()
        .unwrap()
        .images
        .insert("fixture-agent:v1".into());
    let state = fixture.state(fixture.settings());
    let result = fixture
        .request(
            router_for_state(state.clone()),
            "/artcraft/install",
            json!({"installation_id":id,"release":spec}),
        )
        .await;
    assert_eq!(result, (StatusCode::OK, spec));
    assert!(state.verified_artcraft.lock().unwrap().contains(&name));
    let d = fixture.daemon.lock().unwrap();
    assert_eq!(d.containers.len(), 0);
    assert_eq!(d.networks.len(), 0);
    assert!(d.volumes.contains_key(&name));
    let creates: Vec<_> = d
        .calls
        .iter()
        .filter(|(m, p, _)| m == "POST" && p == "/containers/create")
        .collect();
    assert_eq!(creates.len(), 1);
    let body = &creates[0].2;
    assert_eq!(
        body["Entrypoint"],
        json!(["python3", "/artifact/.install.py"])
    );
    assert_eq!(body["HostConfig"]["NetworkMode"], "none");
    assert_eq!(body["HostConfig"]["ReadonlyRootfs"], true);
    assert_eq!(body["HostConfig"]["PidsLimit"], 128);
    assert_eq!(body["HostConfig"]["Memory"], 2147483648u64);
    assert_eq!(body["HostConfig"]["CapDrop"], json!(["ALL"]));
    assert_eq!(
        body["HostConfig"]["SecurityOpt"],
        json!(["no-new-privileges:true"])
    );
    assert_eq!(
        body["HostConfig"]["Mounts"],
        json!([{"Source":name,"Target":"/artifact","Type":"volume","ReadOnly":false}])
    );
    assert_eq!(body["Env"].as_array().unwrap().len(), 1);
    assert!(
        body["Env"][0]
            .as_str()
            .unwrap()
            .starts_with("ARTCRAFT_RELEASE=")
    );
    assert!(!d.calls.iter().any(|(_, p, _)| p == "/networks/create"));
}

#[tokio::test]
async fn artcraft_failure_reaps_helper_unpublished_volume_and_returns_every_slot() {
    for failure in ["exit", "spec", "upload", "wait", "logs"] {
        let fixture = Fixture::new().await;
        let (id, spec, name) = artifact(&fixture, 0);
        fixture.daemon.lock().unwrap().volumes.remove(&name);
        fixture
            .daemon
            .lock()
            .unwrap()
            .images
            .insert("fixture-agent:v1".into());
        {
            let mut d = fixture.daemon.lock().unwrap();
            match failure {
                "exit" => d.helper_exit = 1,
                "spec" => d.helper_output = Some(json!({"app":"bad"})),
                "upload" => d.fail.push(("PUT".into(), "/containers/".into(), 500, 1)),
                "wait" => d.fail.push(("POST".into(), "/containers/".into(), 500, 1)),
                "logs" => d.fail.push(("GET".into(), "/containers/".into(), 500, 1)),
                _ => unreachable!(),
            }
        }
        let state = fixture.state(fixture.settings());
        let result = fixture
            .request(
                router_for_state(state.clone()),
                "/artcraft/install",
                json!({"installation_id":id,"release":spec}),
            )
            .await;
        assert!(
            result.0.is_client_error() || result.0.is_server_error(),
            "{failure}"
        );
        assert!(!state.verified_artcraft.lock().unwrap().contains(&name));
        assert_eq!(state.artcraft_slots.available_permits(), 2);
        let data = fixture.daemon.lock().unwrap();
        assert!(data.containers.is_empty(), "{failure}");
        assert!(!data.volumes.contains_key(&name), "{failure}");
    }
}

#[tokio::test]
async fn artifact_reuse_is_verified_and_ownership_mismatch_never_removes_or_runs() {
    for field in ["app", "version", "sha256", "arch", "nautionette.deployment"] {
        let fixture = Fixture::new().await;
        let (id, spec, name) = artifact(&fixture, 0);
        fixture
            .daemon
            .lock()
            .unwrap()
            .images
            .insert("fixture-agent:v1".into());
        let label = if field == "nautionette.deployment" {
            field.to_owned()
        } else {
            format!("nautionette.artcraft.{field}")
        };
        fixture
            .daemon
            .lock()
            .unwrap()
            .volumes
            .get_mut(&name)
            .unwrap()["Labels"][&label] = json!("wrong");
        let result = fixture
            .request(
                router_for_state(fixture.state(fixture.settings())),
                "/artcraft/install",
                json!({"installation_id":id,"release":spec}),
            )
            .await;
        assert_eq!(result.0, StatusCode::UNPROCESSABLE_ENTITY);
        let d = fixture.daemon.lock().unwrap();
        assert!(d.volumes.contains_key(&name));
        assert!(
            !d.calls
                .iter()
                .any(|(m, p, _)| m == "DELETE" || p == "/containers/create")
        );
    }
    let fixture = Fixture::new().await;
    let (id, spec, name) = artifact(&fixture, 0);
    fixture
        .daemon
        .lock()
        .unwrap()
        .images
        .insert("fixture-agent:v1".into());
    let state = fixture.state(fixture.settings());
    assert_eq!(
        fixture
            .request(
                router_for_state(state.clone()),
                "/artcraft/install",
                json!({"installation_id":id,"release":spec})
            )
            .await
            .0,
        StatusCode::OK
    );
    let d = fixture.daemon.lock().unwrap();
    assert!(d.volumes.contains_key(&name));
    assert!(
        !d.calls
            .iter()
            .any(|(m, p, _)| m == "DELETE" && p.starts_with("/volumes/"))
    );
    assert!(
        d.calls
            .iter()
            .find(|(_, p, _)| p == "/containers/create")
            .unwrap()
            .2["Entrypoint"]
            .as_array()
            .unwrap()
            .iter()
            .any(|v| v == "--verify")
    );
}

#[tokio::test]
async fn capacity_and_slots_and_missing_images_fail_before_resources_are_created() {
    for reason in ["image", "slots", "count"] {
        let fixture = Fixture::new().await;
        let (id, spec, name) = artifact(&fixture, 0);
        fixture.daemon.lock().unwrap().volumes.remove(&name);
        if reason != "image" {
            fixture
                .daemon
                .lock()
                .unwrap()
                .images
                .insert("fixture-agent:v1".into());
        }
        let state = fixture.state(fixture.settings());
        let _held = if reason == "slots" {
            Some(state.artcraft_slots.acquire_many(2).await.unwrap())
        } else {
            None
        };
        if reason == "count" {
            let mut d = fixture.daemon.lock().unwrap();
            for i in 0..128 {
                d.volumes.insert(format!("full-{i}"),json!({"Labels":{"nautionette.deployment":"fixture-workflows","nautionette.artcraft":"fixture"}}));
            }
        }
        let result = fixture
            .request(
                router_for_state(state.clone()),
                "/artcraft/install",
                json!({"installation_id":id,"release":spec}),
            )
            .await;
        assert!(result.0.is_client_error() || result.0.is_server_error());
        assert!(
            !fixture
                .daemon
                .lock()
                .unwrap()
                .calls
                .iter()
                .any(|(_, p, _)| p == "/containers/create" || p == "/volumes/create"),
            "{reason}"
        );
    }
}

#[tokio::test]
async fn artifacts_are_pinned_readonly_and_defaults_reverify_after_restart() {
    let fixture = Fixture::new().await;
    let (id, spec, name) = artifact(&fixture, 0);
    fixture
        .daemon
        .lock()
        .unwrap()
        .images
        .insert("fixture-agent:v1".into());
    let state = fixture.state(fixture.settings());
    let selected =
        artcraft::selection(json!([{"app":spec["app"],"release":spec,"installation_id":id}]))
            .unwrap();
    let mounts = state.artcraft_mounts(&selected).await.unwrap();
    assert_eq!(mounts[0]["Source"], name);
    assert_eq!(mounts[0]["ReadOnly"], true);
    assert_eq!(
        mounts[0]["Target"],
        format!("/opt/artcraft/{}", spec["app"].as_str().unwrap())
    );
    let calls = fixture
        .daemon
        .lock()
        .unwrap()
        .calls
        .iter()
        .filter(|(_, p, _)| p == "/containers/create")
        .count();
    state.artcraft_mounts(&selected).await.unwrap();
    assert_eq!(
        fixture
            .daemon
            .lock()
            .unwrap()
            .calls
            .iter()
            .filter(|(_, p, _)| p == "/containers/create")
            .count(),
        calls
    );
    state.verified_artcraft.lock().unwrap().clear();
    state.artcraft_mounts(&selected).await.unwrap();
    assert_eq!(
        fixture
            .daemon
            .lock()
            .unwrap()
            .calls
            .iter()
            .filter(|(_, p, _)| p == "/containers/create")
            .count(),
        calls + 1
    );
    fixture.daemon.lock().unwrap().volumes.remove(&name);
    assert!(state.artcraft_mounts(&selected).await.is_err());
}

#[tokio::test]
async fn package_failure_reaps_all_unpublished_resources_and_rejects_overwrite() {
    for failure in ["exit", "metadata", "wait", "success"] {
        let fixture = Fixture::new().await;
        fixture
            .daemon
            .lock()
            .unwrap()
            .images
            .insert("fixture-agent:v1".into());
        {
            let mut d = fixture.daemon.lock().unwrap();
            if failure == "exit" {
                d.helper_exit = 1;
            }
            if failure == "metadata" {
                d.helper_output = Some(json!({"root":"../escape","resolved":"1"}));
            }
            if failure == "wait" {
                d.fail.push(("POST".into(), "/containers/".into(), 500, 1));
            }
        }
        let state = fixture.state(fixture.settings());
        let app = router_for_state(state.clone());
        let body = json!({"installation_id":"a".repeat(32),"source":"npm:example"});
        let result = fixture
            .request(app.clone(), "/packages/install", body.clone())
            .await;
        assert_eq!(
            result.0 == StatusCode::OK,
            failure == "success",
            "{failure}"
        );
        assert_eq!(state.package_slots.available_permits(), 2);
        {
            let d = fixture.daemon.lock().unwrap();
            assert!(d.containers.is_empty());
            assert!(d.networks.is_empty());
            assert_eq!(d.volumes.len(), usize::from(failure == "success"));
        }
        if failure == "success" {
            assert_eq!(
                fixture.request(app, "/packages/install", body).await.0,
                StatusCode::UNPROCESSABLE_ENTITY
            );
        }
    }
}

#[tokio::test]
async fn invalid_unreferenced_artifact_is_replaced_but_held_artifact_is_preserved() {
    for held in [false, true] {
        let fixture = Fixture::new().await;
        let (id, spec, name) = artifact(&fixture, 0);
        fixture
            .daemon
            .lock()
            .unwrap()
            .images
            .insert("fixture-agent:v1".into());
        fixture.daemon.lock().unwrap().helper_exits.extend([1, 0]);
        if held {
            fixture.daemon.lock().unwrap().containers.insert("e".repeat(64),json!({"Name":"/held","Config":{"Labels":{}},"Mounts":[{"Type":"volume","Name":name,"Destination":"/artifact"}],"State":{"Status":"created"}}));
        }
        let state = fixture.state(fixture.settings());
        let result = fixture
            .request(
                router_for_state(state.clone()),
                "/artcraft/install",
                json!({"installation_id":id,"release":spec}),
            )
            .await;
        assert_eq!(result.0 == StatusCode::OK, !held);
        assert_eq!(
            state.verified_artcraft.lock().unwrap().contains(&name),
            !held
        );
        let d = fixture.daemon.lock().unwrap();
        assert!(d.volumes.contains_key(&name));
        assert_eq!(
            d.calls
                .iter()
                .filter(|(_, p, _)| p == "/volumes/create")
                .count(),
            usize::from(!held)
        );
        assert_eq!(
            d.calls
                .iter()
                .filter(|(_, p, _)| p == "/containers/create")
                .count(),
            if held { 1 } else { 2 }
        );
    }
}

#[tokio::test]
async fn parallel_artcraft_installs_make_no_network_even_with_no_address_pool() {
    let fixture = Fixture::new().await;
    let (_, spec, _) = artifact(&fixture, 0);
    fixture
        .daemon
        .lock()
        .unwrap()
        .images
        .insert("fixture-agent:v1".into());
    fixture.daemon.lock().unwrap().wait_gate = Some(Arc::new(tokio::sync::Barrier::new(2)));
    fixture
        .daemon
        .lock()
        .unwrap()
        .fail
        .push(("POST".into(), "/networks/create".into(), 500, 100));
    let state = fixture.state(fixture.settings());
    let a = fixture.request(
        router_for_state(state.clone()),
        "/artcraft/install",
        json!({"installation_id":"a".repeat(32),"release":spec}),
    );
    let b = fixture.request(
        router_for_state(state.clone()),
        "/artcraft/install",
        json!({"installation_id":"b".repeat(32),"release":spec}),
    );
    let (a, b) = tokio::time::timeout(Duration::from_secs(3), async { tokio::join!(a, b) })
        .await
        .unwrap();
    assert_eq!((a.0, b.0), (StatusCode::OK, StatusCode::OK));
    assert_eq!(state.artcraft_slots.available_permits(), 2);
    assert!(fixture.daemon.lock().unwrap().networks.is_empty());
}

#[tokio::test]
async fn stale_helpers_and_unused_legacy_networks_are_reaped_without_artifact_deletion() {
    let fixture = Fixture::new().await;
    let (revision, _, artifact) = artifact(&fixture, 0);
    let old = "a".repeat(64);
    let fresh = "b".repeat(64);
    let labels = json!({"nautionette.deployment":"fixture-workflows","nautionette.artcraft":revision,"nautionette.artcraft.installer":"true"});
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    {
        let mut d = fixture.daemon.lock().unwrap();
        for (id, created) in [(&old, 1), (&fresh, now)] {
            d.containers.insert(id.clone(),json!({"Name":"/installer","Created":created,"Config":{"Labels":labels},"State":{"Status":"running"},"Mounts":[]}));
        }
        for (index, own, active, installer, recent) in [
            (1, true, false, true, false),
            (2, true, true, true, false),
            (3, false, false, true, false),
            (4, true, false, false, false),
            (5, true, false, true, true),
        ] {
            let id = format!("{index:064x}");
            d.networks.insert(id.clone(),json!({"Id":id,"Labels":{"nautionette.deployment":if own{"fixture-workflows"}else{"other-workflows"},"nautionette.artcraft.installer_network":if installer{"true"}else{"false"}},
                "Containers":if active{json!({"helper":{}})}else{json!({})},"Created":if recent{"2999-01-01T00:00:00Z"}else{"2000-01-01T00:00:00Z"}}));
        }
    }
    let state = fixture.state(fixture.settings());
    state.reap_artcraft().await;
    let d = fixture.daemon.lock().unwrap();
    assert!(!d.containers.contains_key(&old));
    assert!(d.containers.contains_key(&fresh));
    assert_eq!(d.networks.len(), 4);
    assert!(!d.networks.contains_key(&format!("{:064x}", 1)));
    assert!(d.volumes.contains_key(&artifact));
    assert!(
        !d.calls
            .iter()
            .any(|(m, p, _)| m == "DELETE" && p.starts_with("/volumes/"))
    );
}

#[tokio::test]
async fn package_runtime_mounts_are_read_only_scoped_and_never_implicitly_created() {
    let fixture = Fixture::new().await;
    let state = fixture.state(fixture.settings());
    let ids = BTreeSet::from(["a".repeat(32)]);
    assert!(state.package_mounts(&ids).await.is_err());
    let name = format!(
        "nautionette-package-{}-{}",
        packages::short_hash(b"fixture-workflows", 12),
        "a".repeat(32)
    );
    fixture.daemon.lock().unwrap().volumes.insert(name.clone(),json!({"Name":name,"Labels":{"nautionette.deployment":"fixture-workflows","nautionette.package":"a".repeat(32)}}));
    let mounts = state.package_mounts(&ids).await.unwrap();
    assert_eq!(mounts[0]["ReadOnly"], true);
    assert_eq!(mounts[0]["Source"], name);
    assert_eq!(
        mounts[0]["Target"],
        format!("/opt/nautionette-packages/{}", "a".repeat(32))
    );
    fixture
        .daemon
        .lock()
        .unwrap()
        .volumes
        .get_mut(&name)
        .unwrap()["Labels"]["nautionette.package"] = json!("other");
    assert!(state.package_mounts(&ids).await.is_err());
    assert!(
        !fixture
            .daemon
            .lock()
            .unwrap()
            .calls
            .iter()
            .any(|(_, p, _)| p == "/volumes/create")
    );
}

#[test]
fn package_sources_filters_and_configuration_match_reference_vectors() {
    for (source, parsed) in [
        (
            "npm:example@1.2.3",
            json!({"kind":"npm","name":"example","spec":"example@1.2.3"}),
        ),
        (
            "npm:@scope/name@next",
            json!({"kind":"npm","name":"@scope/name","spec":"@scope/name@next"}),
        ),
        (
            "git:github.com/org/repo@v1",
            json!({"kind":"git","url":"https://github.com/org/repo.git","ref":"v1"}),
        ),
        (
            "https://github.com/org/repo.git",
            json!({"kind":"git","url":"https://github.com/org/repo.git","ref":"HEAD"}),
        ),
    ] {
        assert_eq!(packages::installation_source(source).unwrap(), parsed);
    }
    for source in [
        "npm:--help",
        "npm:@scope",
        "npm:owner/name",
        "npm:example;echo bad",
        "https://github.com/org/repo@--help",
        "https://github.com/org/repo@../bad",
        "npm:example@file:/tmp/x",
        "http://169.254.169.254/pkg",
        "https://user:password@github.com/org/repo",
        "/local/pkg",
    ] {
        assert!(packages::installation_source(source).is_err(), "{source}");
    }
    let base = json!([{"installation_id":"a".repeat(32),"root":"npm/node_modules/example","configuration":{},"filters":{"extensions":[],"skills":["skills/*","!skills/old"]}}]);
    assert!(packages::runtime_installations(&base).is_ok());
    for key in [
        "GIT_CONFIG",
        "NODE_OPTIONS",
        "NAUTIONETTE_TOOLS",
        "PATH",
        "INTERNAL_TOKEN",
        "HOME",
    ] {
        let mut body = base.clone();
        body[0]["configuration"] = json!({"env":{(key):"secret"}});
        assert!(packages::runtime_installations(&body).is_err(), "{key}");
    }
    for file in [
        "agent/../settings.json",
        "agent/settings.json",
        "agent/extensions/a.js",
        "/etc/profile",
    ] {
        let mut body = base.clone();
        body[0]["configuration"] = json!({"files":{(file):"secret"}});
        assert!(packages::runtime_installations(&body).is_err(), "{file}");
    }
}
