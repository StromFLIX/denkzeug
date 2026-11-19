use std::{collections::BTreeMap, io::Read};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use denkzeug_docker_broker_spike::job::{JobContext, plan_job};
use serde_json::{Value, json};

fn context() -> JobContext {
    JobContext {
        agent_env: BTreeMap::from([
            ("BACKEND_URL".into(), "http://backend:8080".into()),
            (
                "OTEL_EXPORTER_OTLP_HEADERS".into(),
                "authorization=synthetic".into(),
            ),
        ]),
        token: "synthetic-internal-token".into(),
        worker_network: "control".into(),
        chat_network: "chat-only".into(),
        workflows_volume: "local-workflows".into(),
        workspace_size: "768m".into(),
        traced_parent: Some("trusted-trace-parent".into()),
    }
}

#[test]
fn large_jobs_current_files_and_history_files_use_owner_only_tar_entries() {
    for (job, uid) in [
        (json!({"images":[{"data":"x".repeat(40_000)}]}), 0),
        (json!({"files":[{"name":"report.pdf","data":"YWJj"}]}), 0),
        (
            json!({"chat_id":"chat","history":[{"files":[{"data":"YWJj"}]}]}),
            10001,
        ),
    ] {
        let plan = plan_job(job.clone(), &context()).unwrap();
        assert_eq!(
            plan.environment["AGENT_JOB_FILE"],
            "/tmp/nautionette-job.json"
        );
        assert!(!plan.environment.contains_key("AGENT_JOB"));
        assert_eq!(plan.archives.len(), 1);
        assert_eq!(plan.archives[0].destination(), "/tmp");
        let mut archive = tar::Archive::new(plan.archives[0].contents());
        let mut entries = archive.entries().unwrap();
        let mut entry = entries.next().unwrap().unwrap();
        assert_eq!(entry.path().unwrap().to_str(), Some("nautionette-job.json"));
        assert_eq!(entry.header().mode().unwrap(), 0o600);
        assert_eq!(entry.header().uid().unwrap(), uid);
        assert_eq!(entry.header().gid().unwrap(), uid);
        let mut raw = Vec::new();
        entry.read_to_end(&mut raw).unwrap();
        let mut actual: Value = serde_json::from_slice(&raw).unwrap();
        assert_eq!(
            actual.as_object_mut().unwrap().remove("_trace_context"),
            Some(json!({"traceparent":"trusted-trace-parent"}))
        );
        assert_eq!(actual, job);
        assert!(entries.next().is_none());
    }
}

#[test]
fn output_and_package_files_are_private_and_credentials_do_not_mix_with_job_data() {
    for prompt in ["small".to_owned(), "x".repeat(40_000)] {
        let job = json!({
          "chat_id":"chat","turn_id":"turn","prompt":prompt,
          "_trace_context":{"traceparent":"forged"},
          "project_credentials":[{"token":"synthetic-private-turn-token"}],
          "package_runtime":[{"fixture":"runtime"}],
          "output_publication":{"grant":"synthetic-output-token"},
        });
        let plan = plan_job(job, &context()).unwrap();
        assert_eq!(
            plan.project_credentials[0]["token"],
            "synthetic-private-turn-token"
        );
        assert_eq!(plan.package_runtime, json!([{"fixture":"runtime"}]));
        let mut files = BTreeMap::<String, String>::new();
        for archive in &plan.archives {
            let mut reader = tar::Archive::new(archive.contents());
            for entry in reader.entries().unwrap() {
                let mut entry = entry.unwrap();
                assert_eq!(entry.header().mode().unwrap(), 0o600);
                assert_eq!(entry.header().uid().unwrap(), 10001);
                let name = entry.path().unwrap().to_str().unwrap().to_owned();
                let mut contents = String::new();
                entry.read_to_string(&mut contents).unwrap();
                assert!(!contents.contains("synthetic-private-turn-token"));
                files.insert(name, contents);
            }
        }
        assert!(files["nautionette-outputs.json"].contains("synthetic-output-token"));
        assert!(files.contains_key("nautionette-packages.json"));
        let inline;
        let public = if let Some(value) = plan.environment.get("AGENT_JOB") {
            inline = String::from_utf8(STANDARD.decode(value).unwrap()).unwrap();
            inline.as_str()
        } else {
            &files["nautionette-job.json"]
        };
        for secret in [
            "synthetic-private-turn-token",
            "synthetic-output-token",
            "project_credentials",
            "output_publication",
            "package_runtime",
            "forged",
        ] {
            assert!(!public.contains(secret));
        }
        assert_eq!(plan.environment["TRACEPARENT"], "trusted-trace-parent");
    }
}

#[test]
fn role_policies_keep_chats_and_members_off_the_control_network() {
    for (job, user, network, workflows, control, member) in [
        (json!({}), None, "control", true, true, false),
        (
            json!({"chat_id":"chat","turn_id":"turn"}),
            Some("10001:10001"),
            "chat-only",
            true,
            false,
            false,
        ),
        (
            json!({"member_mode":true,"packages":["privileged"],"artcraft":["privileged"],"package_runtime":[1],"project_credentials":["secret"]}),
            Some("10001:10001"),
            "chat-only",
            false,
            false,
            true,
        ),
    ] {
        let plan = plan_job(job, &context()).unwrap();
        assert_eq!(plan.user, user);
        assert_eq!(plan.network, network);
        assert_eq!(plan.workflows_volume.is_some(), workflows);
        for key in ["BACKEND_URL", "INTERNAL_TOKEN"] {
            assert_eq!(plan.environment.contains_key(key), control);
        }
        assert_eq!(
            plan.tmpfs["/workspace"],
            if user.is_some() {
                "size=768m,exec,uid=10001,gid=10001"
            } else {
                "size=768m,exec"
            }
        );
        assert_eq!(
            plan.environment.contains_key("OTEL_EXPORTER_OTLP_HEADERS"),
            !member
        );
        if member {
            let data: Value =
                serde_json::from_slice(&STANDARD.decode(&plan.environment["AGENT_JOB"]).unwrap())
                    .unwrap();
            assert_eq!(data["packages"], json!([]));
            assert_eq!(data["artcraft"], json!([]));
            assert_eq!(plan.project_credentials, json!([]));
            assert!(plan.archives.is_empty());
        }
    }
    assert!(
        plan_job(
            json!({"member_mode":true,"agent_set":"private"}),
            &context()
        )
        .is_err()
    );
}
