use std::{
    pin::Pin,
    sync::{Arc, Mutex},
    time::Duration,
};

use axum::body::Bytes;
use denkzeug_docker_broker_spike::{
    runner::{RunOptions, RunnerEngine, run_turn},
    stream::Exit,
    turns::{TurnKey, TurnRegistry},
};
use futures_util::{Stream, stream};
use serde_json::{Value, json};
use tokio::sync::mpsc;

type Logs = Pin<Box<dyn Stream<Item = Result<Bytes, ()>> + Send>>;

#[derive(Default)]
struct Driver {
    calls: Mutex<Vec<&'static str>>,
    quiet: bool,
    fail_stage: bool,
    fail_kill: bool,
    image_wait: bool,
    exit_code: i64,
    oom_killed: bool,
}

impl Driver {
    fn call(&self, label: &'static str) {
        self.calls.lock().unwrap().push(label);
    }
}

impl RunnerEngine for Driver {
    type Logs = Logs;
    async fn ready(&self, events: &mpsc::Sender<String>) -> Result<(), String> {
        if self.image_wait {
            self.call("image-wait");
            events
                .send("{\"type\":\"status\",\"state\":\"building\"}\n".to_owned())
                .await
                .map_err(|_| "closed".to_owned())?;
            std::future::pending::<()>().await;
        }
        Ok(())
    }
    async fn create(&self) -> Result<String, ()> {
        self.call("create");
        Ok("fixture-container".into())
    }
    async fn stage(&self, id: &str) -> Result<(), ()> {
        assert_eq!(id, "fixture-container");
        self.call("upload");
        if self.fail_stage { Err(()) } else { Ok(()) }
    }
    async fn start(&self, _: &str) -> Result<(), ()> {
        self.call("start");
        Ok(())
    }
    async fn logs(&self, _: &str) -> Result<Self::Logs, ()> {
        self.call("logs");
        if self.quiet {
            return Ok(Box::pin(stream::pending()));
        }
        let payload = br#"{"type":"text","text":"hi"}"#;
        let mut frame = vec![1, 0, 0, 0];
        frame.extend((payload.len() as u32).to_be_bytes());
        frame.extend(payload); // Exercise EOF without a newline.
        Ok(Box::pin(stream::iter([Ok(frame.into())])))
    }
    async fn exit(&self, _: &str) -> Result<Exit, ()> {
        self.call("wait");
        Ok(Exit {
            code: self.exit_code,
            oom_killed: self.oom_killed,
            stderr: String::new(),
        })
    }
    async fn kill(&self, _: &str) -> Result<(), ()> {
        self.call("kill");
        if self.fail_kill { Err(()) } else { Ok(()) }
    }
    async fn remove(&self, _: &str) -> Result<(), ()> {
        self.call("remove");
        Ok(())
    }
    async fn release(&self) {
        self.call("release");
    }
}

fn setup() -> (Arc<TurnRegistry>, TurnKey, RunOptions) {
    (
        Arc::new(TurnRegistry::new(Duration::from_secs(60))),
        TurnKey::new("chat", "turn").unwrap(),
        RunOptions {
            agent_set: "default".into(),
            image: "fixture:latest".into(),
            memory: "4g".into(),
            watchdog: Some(Duration::from_secs(12)),
            operation_budget: Duration::from_secs(2),
            startup_trace: None,
        },
    )
}

async fn drain(mut events: mpsc::Receiver<String>) -> Vec<Value> {
    let mut result = Vec::new();
    while let Some(line) = events.recv().await {
        result.push(serde_json::from_str(&line).unwrap());
    }
    result
}

#[tokio::test(start_paused = true)]
async fn launch_stages_before_start_and_closes_after_removal_and_claim_release() {
    let (registry, key, spec) = setup();
    let driver = Arc::new(Driver::default());
    let events = drain(run_turn(registry.clone(), key, driver.clone(), spec)).await;
    assert_eq!(
        events,
        [
            json!({"type":"started","agent_set":"default","image":"fixture:latest"}),
            json!({"type":"text","text":"hi"}),
            json!({"type":"closed"})
        ]
    );
    assert_eq!(
        *driver.calls.lock().unwrap(),
        [
            "create", "upload", "start", "logs", "wait", "remove", "release"
        ]
    );
    assert!(registry.inventory("").is_empty());
}

#[tokio::test(start_paused = true)]
async fn failed_archive_never_starts_and_still_removes_and_releases() {
    let (registry, key, spec) = setup();
    let driver = Arc::new(Driver {
        fail_stage: true,
        ..Default::default()
    });
    let events = drain(run_turn(registry.clone(), key, driver.clone(), spec)).await;
    assert_eq!(
        events[1],
        json!({"type":"error","message":"Could not deliver the agent job"})
    );
    assert_eq!(events[2], json!({"type":"closed"}));
    assert_eq!(
        *driver.calls.lock().unwrap(),
        ["create", "upload", "remove", "release"]
    );
    assert!(registry.inventory("").is_empty());
}

#[tokio::test(start_paused = true)]
async fn silent_timeout_wins_over_failed_kill_and_still_removes_and_releases() {
    let (registry, key, spec) = setup();
    let driver = Arc::new(Driver {
        quiet: true,
        fail_kill: true,
        ..Default::default()
    });
    let events = drain(run_turn(registry.clone(), key, driver.clone(), spec)).await;
    assert_eq!(events[1]["reason"], "timeout");
    assert_eq!(events[1]["timeout_seconds"], 12);
    assert_eq!(events[2], json!({"type":"closed"}));
    assert_eq!(
        *driver.calls.lock().unwrap(),
        [
            "create", "upload", "start", "logs", "kill", "remove", "release"
        ]
    );
    assert!(registry.inventory("").is_empty());
}

#[tokio::test(start_paused = true)]
async fn image_waiters_are_visible_and_stop_cancels_before_container_creation() {
    let (registry, key, spec) = setup();
    let driver = Arc::new(Driver {
        image_wait: true,
        ..Default::default()
    });
    let mut frames = run_turn(registry.clone(), key.clone(), driver.clone(), spec);
    assert_eq!(registry.inventory("chat"), std::slice::from_ref(&key));
    assert_eq!(
        serde_json::from_str::<Value>(&frames.recv().await.unwrap()).unwrap()["state"],
        "building"
    );
    assert!(registry.request_stop(&key));
    assert!(drain(frames).await.is_empty());
    assert!(registry.inventory("").is_empty());
    assert_eq!(*driver.calls.lock().unwrap(), ["image-wait", "release"]);
}

#[tokio::test(start_paused = true)]
async fn timeout_exemption_keeps_oom_exit_classification_and_resource_cleanup() {
    for exempt in [false, true] {
        for code in [0, 137] {
            let (registry, key, mut spec) = setup();
            if exempt {
                spec.watchdog = None;
            }
            let driver = Arc::new(Driver {
                exit_code: code,
                oom_killed: code == 137,
                ..Default::default()
            });
            let frames = drain(run_turn(registry.clone(), key, driver.clone(), spec)).await;
            let errors: Vec<_> = frames
                .iter()
                .filter(|frame| frame["type"] == "error")
                .collect();
            assert_eq!(errors.len(), usize::from(code != 0));
            if code != 0 {
                assert_eq!(errors[0]["reason"], "oom_killed");
            }
            assert_eq!(frames.last().unwrap()["type"], "closed");
            assert!(!driver.calls.lock().unwrap().contains(&"kill"));
            assert!(driver.calls.lock().unwrap().contains(&"remove"));
            assert!(registry.inventory("").is_empty());
        }
    }
}

#[tokio::test(start_paused = true)]
async fn explicit_stop_and_stream_disconnect_wake_exempt_silent_turns() {
    for disconnect in [true, false] {
        let (registry, key, mut spec) = setup();
        spec.watchdog = None;
        let driver = Arc::new(Driver {
            quiet: true,
            ..Default::default()
        });
        let mut events = run_turn(registry.clone(), key.clone(), driver.clone(), spec);
        assert_eq!(
            serde_json::from_str::<Value>(&events.recv().await.unwrap()).unwrap()["type"],
            "started"
        );
        while !driver.calls.lock().unwrap().contains(&"logs") {
            tokio::task::yield_now().await;
        }
        if disconnect {
            drop(events);
        } else {
            assert!(registry.request_stop(&key));
            assert_eq!(drain(events).await, [json!({"type":"closed"})]);
        }
        for _ in 0..5 {
            tokio::task::yield_now().await;
        }
        assert!(registry.inventory("").is_empty());
        assert_eq!(
            *driver.calls.lock().unwrap(),
            [
                "create", "upload", "start", "logs", "kill", "remove", "release"
            ]
        );
    }
}
