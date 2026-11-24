use super::*;
use base64::{Engine as _, engine::general_purpose::STANDARD};
use std::io::Write;

fn packed(value: &Value) -> String {
    let mut gzip = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
    gzip.write_all(value.to_string().as_bytes()).unwrap();
    STANDARD.encode(gzip.finish().unwrap())
}
fn payload(mode: &str) -> Value {
    json!({"app":"app","target":"staging","commit":"b".repeat(40),"mode":mode,"shards":"4","files":{
        "context/Dockerfile.controller":"FROM scratch", "temporal_deploy_job.py":"pass",
        "context/upgrade-plan.json":"{\"target\":\"1.32.2\",\"releases\":[{\"version\":\"1.32.2\"}]}"
    }})
}
fn receipt(value: &Value) -> Vec<u8> {
    let data = value.to_string();
    let mut archive = tar::Builder::new(Vec::new());
    let mut header = tar::Header::new_ustar();
    header.set_size(data.len() as u64);
    header.set_mode(0o644);
    header.set_cksum();
    archive
        .append_data(&mut header, "completed.json", data.as_bytes())
        .unwrap();
    archive.into_inner().unwrap()
}
async fn fixture(status: &str, revision: &str, safe: bool, with_receipt: bool) -> Fixture {
    let fixture = Fixture::new().await;
    {
        let mut d = fixture.daemon.lock().unwrap();
        let own = "1".repeat(64);
        let id = "2".repeat(64);
        d.containers.insert(own.clone(),json!({"Id":own,"Name":"/broker","Config":{"Labels":{"com.docker.compose.project":"app","com.docker.compose.service":"docker-broker"}}}));
        d.containers.insert(id.clone(),json!({"Id":id,"Name":"/nautionette-temporal-app","Config":{"Labels":{"nautionette.temporal.project":"app","nautionette.temporal.commit":revision}},"State":{"Status":"running"},"HostConfig":{"RestartPolicy":{"Name":"unless-stopped"}}}));
        let state = json!({"commit":revision,"status":status,"safe_to_retry":safe});
        d.exec_reply = Some(state.clone());
        if with_receipt {
            d.receipts.insert(id, receipt(&state));
        }
        d.next_id = 10;
    }
    fixture
}

#[tokio::test]
async fn maintenance_finish_requires_receipt_and_retires_without_removing_controller() {
    let f = fixture("completed", &"b".repeat(40), false, true).await;
    crate::maintenance::launch(f.settings(), "broker", &packed(&payload("finish")))
        .await
        .unwrap();
    crate::maintenance::launch(f.settings(), "broker", &packed(&payload("finish")))
        .await
        .unwrap();
    let d = f.daemon.lock().unwrap();
    let id = "2".repeat(64);
    assert_eq!(d.containers[&id]["State"]["Status"], "exited");
    assert_eq!(
        d.containers[&id]["HostConfig"]["RestartPolicy"]["Name"],
        "no"
    );
    assert!(!d.calls.iter().any(|(m, _, _)| m == "DELETE"));
    assert_eq!(
        d.execs.len(),
        1,
        "a repeated finish must not execute in a stopped controller"
    );
}

#[tokio::test]
async fn maintenance_prepare_reuses_same_revision_and_refuses_ambiguous_previous_work() {
    for (revision, status, accepted) in [
        ("b".repeat(40), "prepared", true),
        ("a".repeat(40), "needs_recovery", false),
    ] {
        let f = fixture(status, &revision, false, false).await;
        let result =
            crate::maintenance::launch(f.settings(), "broker", &packed(&payload("prepare"))).await;
        assert_eq!(result.is_ok(), accepted);
        assert!(
            !f.daemon
                .lock()
                .unwrap()
                .calls
                .iter()
                .any(|(m, p, _)| m == "DELETE" || p == "/build")
        );
    }
}

#[tokio::test]
async fn maintenance_ownership_and_malformed_payloads_fail_before_mutation() {
    for fault in ["broker", "controller", "revision", "path", "gzip", "limit"] {
        let f = fixture("completed", &"b".repeat(40), false, true).await;
        let mut input = payload("finish");
        {
            let mut d = f.daemon.lock().unwrap();
            match fault {
                "broker" => {
                    d.containers.get_mut(&"1".repeat(64)).unwrap()["Config"]["Labels"]["com.docker.compose.project"] =
                        json!("foreign")
                }
                "controller" => {
                    d.containers.get_mut(&"2".repeat(64)).unwrap()["Config"]["Labels"]["nautionette.temporal.project"] =
                        json!("foreign")
                }
                "revision" => input["commit"] = json!("bad"),
                "path" => input["files"]["../escape"] = json!("bad"),
                _ => (),
            }
        }
        let encoded = match fault {
            "gzip" => STANDARD.encode(b"not gzip"),
            "limit" => "a".repeat(100001),
            _ => packed(&input),
        };
        assert!(
            crate::maintenance::launch(f.settings(), "broker", &encoded)
                .await
                .is_err()
        );
        assert!(
            f.daemon
                .lock()
                .unwrap()
                .calls
                .iter()
                .all(|(m, _, _)| m == "GET")
        );
    }
}

#[tokio::test]
async fn maintenance_missing_or_mismatched_receipt_never_retires() {
    for bad in [
        None,
        Some(json!({"status":"completed","commit":"a".repeat(40)})),
        Some(json!({"status":"prepared","commit":"b".repeat(40)})),
    ] {
        let f = fixture("completed", &"b".repeat(40), false, false).await;
        if let Some(v) = bad {
            f.daemon
                .lock()
                .unwrap()
                .receipts
                .insert("2".repeat(64), receipt(&v));
        }
        assert!(
            crate::maintenance::launch(f.settings(), "broker", &packed(&payload("finish")))
                .await
                .is_err()
        );
        assert!(
            !f.daemon
                .lock()
                .unwrap()
                .calls
                .iter()
                .any(|(_, p, _)| p.ends_with("/update") || p.ends_with("/stop"))
        );
    }
}

#[tokio::test]
async fn maintenance_build_failure_keeps_existing_completed_evidence_until_explicit_next_candidate()
{
    let f = fixture("completed", &"a".repeat(40), false, true).await;
    f.daemon
        .lock()
        .unwrap()
        .fail
        .push(("POST".into(), "/build".into(), 500, 1));
    let result =
        crate::maintenance::launch(f.settings(), "broker", &packed(&payload("prepare"))).await;
    assert!(result.is_err());
    assert!(
        f.daemon
            .lock()
            .unwrap()
            .containers
            .contains_key(&"2".repeat(64)),
        "failed replacement build must retain the previous completed controller"
    );
    assert!(
        f.daemon
            .lock()
            .unwrap()
            .receipts
            .contains_key(&"2".repeat(64))
    );
    assert!(
        !f.daemon
            .lock()
            .unwrap()
            .calls
            .iter()
            .any(|(_, p, _)| p == "/containers/create")
    );
}

#[tokio::test]
async fn maintenance_safe_retry_builds_pinned_controller_with_only_existing_mounts() {
    let f = fixture("failed", &"b".repeat(40), true, false).await;
    crate::maintenance::launch(f.settings(), "broker", &packed(&payload("prepare")))
        .await
        .unwrap_err();
    // The synthetic controller reports safe-retry failure after create, so the
    // launcher must return failure (not retry or remove possibly applied work).
    let d = f.daemon.lock().unwrap();
    let creates: Vec<_> = d
        .calls
        .iter()
        .filter(|(_, p, _)| p == "/containers/create")
        .collect();
    assert_eq!(creates.len(), 1);
    let body = &creates[0].2;
    assert_eq!(body["HostConfig"]["Binds"].as_array().unwrap().len(), 3);
    assert_eq!(
        body["HostConfig"]["SecurityOpt"],
        json!(["no-new-privileges:true"])
    );
    assert_eq!(
        body["HostConfig"]["RestartPolicy"]["Name"],
        "unless-stopped"
    );
    assert_eq!(
        body["Cmd"],
        json!(["python", "/opt/maintenance/temporal_deploy_job.py", "serve"])
    );
    assert_eq!(
        body["Labels"]["nautionette.temporal.commit"],
        "b".repeat(40)
    );
}
