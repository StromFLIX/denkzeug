use super::*;

fn receipt(status: &str, commit: &str) -> Vec<u8> {
    let raw = json!({"status":status,"commit":commit}).to_string();
    let mut tar = tar::Builder::new(Vec::new());
    let mut h = tar::Header::new_ustar();
    h.set_mode(0o600);
    h.set_size(raw.len() as u64);
    tar.append_data(&mut h, "completed.json", raw.as_bytes())
        .unwrap();
    tar.into_inner().unwrap()
}

#[tokio::test]
async fn controller_receipts_gate_the_exact_commit_and_cleanup_policy_without_leaking_state() {
    for (status, policy, commit, archive, required, expected) in [
        ("exited", "no", "current", "valid", true, "completed"),
        (
            "exited",
            "no",
            "wrong",
            "valid",
            true,
            "verification_pending",
        ),
        ("running", "no", "current", "valid", true, "cleanup_pending"),
        (
            "exited",
            "always",
            "current",
            "valid",
            true,
            "cleanup_pending",
        ),
        (
            "exited",
            "no",
            "current",
            "missing",
            true,
            "inspection_failed",
        ),
        (
            "exited",
            "no",
            "current",
            "malformed",
            true,
            "inspection_failed",
        ),
        ("running", "no", "current", "legacy", false, "completed"),
    ] {
        let fixture = Fixture::new().await;
        let mut cfg = fixture.worker_settings();
        cfg.workers.as_mut().unwrap().temporal_receipt_required = required;
        let current = "a".repeat(40);
        let found = if commit == "current" {
            current.clone()
        } else {
            "b".repeat(40)
        };
        let id = "c".repeat(64);
        {
            let mut d = fixture.daemon.lock().unwrap();
            d.containers.insert(id.clone(),json!({"Name":"/controller","Config":{"Labels":{"nautionette.temporal.project":"stack","nautionette.temporal.commit":found}},"HostConfig":{"RestartPolicy":{"Name":policy}},"State":{"Status":status},"Mounts":[]}));
            match archive {
                "valid" => {
                    d.receipts.insert(id.clone(), receipt("completed", &found));
                }
                "malformed" => {
                    d.receipts
                        .insert(id.clone(), b"private-damaged-receipt".to_vec());
                }
                "legacy" => {
                    d.exec_reply = Some(
                        json!({"status":"completed","commit":found,"private":"must not appear in health"}),
                    );
                }
                _ => (),
            }
        }
        let result = fixture.state(cfg).temporal_snapshot().await;
        assert_eq!(
            result["phase"], expected,
            "{status} {policy} {commit} {archive}"
        );
        assert!(!result.to_string().contains("private"));
        assert_eq!(result["status"] == "ready", expected == "completed");
    }
}

#[tokio::test]
async fn missing_or_duplicate_controller_fails_only_the_required_receipt_fence() {
    let fixture = Fixture::new().await;
    assert_eq!(
        fixture
            .state(fixture.worker_settings())
            .temporal_snapshot()
            .await,
        json!({"status":"ready","phase":"not_required"})
    );
    let mut cfg = fixture.worker_settings();
    cfg.workers.as_mut().unwrap().temporal_receipt_required = true;
    let state = fixture.state(cfg);
    assert_eq!(state.temporal_snapshot().await["phase"], "receipt_missing");
    {
        let mut d = fixture.daemon.lock().unwrap();
        for i in 1..=2 {
            d.containers.insert(format!("{i:064x}"),json!({"Name":"/controller","Config":{"Labels":{"nautionette.temporal.project":"stack","nautionette.temporal.commit":"a".repeat(40)}},"State":{"Status":"exited"},"Mounts":[]}));
        }
    }
    assert_eq!(
        state.temporal_snapshot().await["phase"],
        "controller_unavailable"
    );
}
