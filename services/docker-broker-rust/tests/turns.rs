use std::{collections::BTreeMap, sync::Mutex, time::Duration};

use denkzeug_docker_broker_spike::turns::{
    BeginError, CleanupError, RecoveryEngine, RemoveResult, TurnKey, TurnRegistry,
};
use tokio::sync::oneshot;

fn key(chat: &str, turn: &str) -> TurnKey {
    TurnKey::new(chat, turn).unwrap()
}

#[derive(Default)]
struct Engine {
    containers: Mutex<BTreeMap<TurnKey, Vec<String>>>,
    // Simulate transient Docker failure, concurrent removal, and an unconfirmed removal.
    mode: Mutex<&'static str>,
}

impl Engine {
    fn add(&self, key: &TurnKey, id: &str) {
        self.containers
            .lock()
            .unwrap()
            .entry(key.clone())
            .or_default()
            .push(id.into());
    }
}

impl RecoveryEngine for Engine {
    async fn exact_turn_containers(&self, turn: &TurnKey) -> Result<Vec<String>, ()> {
        Ok(self
            .containers
            .lock()
            .unwrap()
            .get(turn)
            .cloned()
            .unwrap_or_default())
    }

    async fn force_remove(&self, id: &str) -> Result<RemoveResult, ()> {
        let mode = *self.mode.lock().unwrap();
        match mode {
            "unavailable" => return Err(()),
            "lingering" => return Ok(RemoveResult::Removed),
            _ => (),
        }
        let mut containers = self.containers.lock().unwrap();
        for ids in containers.values_mut() {
            ids.retain(|candidate| candidate != id);
        }
        Ok(if mode == "not_found" {
            RemoveResult::AlreadyAbsent
        } else {
            RemoveResult::Removed
        })
    }
}

#[tokio::test(start_paused = true)]
async fn delayed_and_duplicate_run_requests_are_fenced() {
    let turns = TurnRegistry::new(Duration::from_secs(15));
    let key = key("chat", "turn");
    let engine = Engine::default();
    assert!(TurnKey::new("chat", "").is_none());
    turns
        .cleanup(&engine, &key, Duration::from_secs(1))
        .await
        .unwrap();
    assert!(matches!(turns.begin(key.clone()), Err(BeginError::Retired)));
    tokio::time::advance(Duration::from_secs(15)).await;
    let owner = turns.begin(key.clone()).unwrap();
    assert!(matches!(
        turns.begin(key.clone()),
        Err(BeginError::Duplicate)
    ));
    assert_eq!(turns.inventory("chat"), vec![key.clone()]);
    assert!(turns.inventory("other").is_empty());
    turns.finish(&owner);
    assert!(turns.inventory("").is_empty());
    let next = turns.begin(key.clone()).unwrap();
    turns.finish(&owner);
    assert_eq!(turns.inventory(""), [key]);
    turns.finish(&next);
}

#[tokio::test(start_paused = true)]
async fn cleanup_during_create_waits_for_release_and_prevents_late_start() {
    let turns = TurnRegistry::new(Duration::from_secs(15));
    let target = key("chat", "turn");
    let engine = Engine::default();
    let owner = turns.begin(target.clone()).unwrap();
    let (permit_send, permit_recv) = oneshot::channel();
    let cleanup = async {
        let result = turns
            .cleanup(&engine, &target, Duration::from_secs(5))
            .await;
        assert!(
            permit_send.send(()).is_ok(),
            "claim was not released before cleanup completed"
        );
        result
    };
    let creator = async {
        owner.wait_for_stop().await;
        // Docker create completed only after cleanup requested Stop.
        engine.add(&target, "late-container");
        assert!(owner.start_permit().await.is_none());
        engine.force_remove("late-container").await.unwrap();
        turns.finish(&owner);
        permit_recv.await.unwrap();
    };
    let (result, _) = tokio::join!(cleanup, creator);
    assert_eq!(result, Ok(()));
    assert!(turns.inventory("").is_empty());
    assert!(matches!(turns.begin(target), Err(BeginError::Retired)));
}

#[tokio::test(start_paused = true)]
async fn cleanup_timeout_does_not_release_someone_elses_claim() {
    let turns = TurnRegistry::new(Duration::from_secs(15));
    let engine = Engine::default();
    let target = key("chat", "turn");
    let owner = turns.begin(target.clone()).unwrap();
    assert_eq!(
        turns
            .cleanup(&engine, &target, Duration::from_secs(2))
            .await,
        Err(CleanupError::Deadline)
    );
    assert_eq!(turns.inventory(""), vec![target.clone()]);
    assert!(owner.start_permit().await.is_none());
    turns.finish(&owner);
    assert_eq!(
        turns
            .cleanup(&engine, &target, Duration::from_secs(2))
            .await,
        Ok(())
    );
}

#[tokio::test]
async fn slow_start_only_serializes_control_for_that_turn() {
    let turns = TurnRegistry::new(Duration::from_secs(10));
    let a = key("chat-a", "turn");
    let b = key("chat-b", "turn");
    let a_owner = turns.begin(a.clone()).unwrap();
    let b_owner = turns.begin(b.clone()).unwrap();
    let a_permit = a_owner.start_permit().await.unwrap();
    let engine = Engine::default();
    let controlling_a = turns.cleanup(&engine, &a, Duration::from_secs(2));
    let controlling_b = async {
        a_owner.wait_for_stop().await;
        assert!(b_owner.start_permit().await.is_some());
        turns.finish(&b_owner);
        turns
            .cleanup(&engine, &b, Duration::from_secs(1))
            .await
            .unwrap();
        drop(a_permit);
        turns.finish(&a_owner);
    };
    let (answer, _) = tokio::join!(controlling_a, controlling_b);
    assert_eq!(answer, Ok(()));
}

#[tokio::test]
async fn exact_orphan_cleanup_is_idempotent_and_requires_docker_confirmation() {
    let turns = TurnRegistry::new(Duration::from_secs(15));
    let engine = Engine::default();
    let target = key("chat", "turn");
    let other_turn = key("chat", "other");
    let other_chat = key("other", "turn");
    engine.add(&target, "orphan");
    engine.add(&other_turn, "active");
    engine.add(&other_chat, "protected");
    for (mode, expected) in [
        ("unavailable", Err(CleanupError::Engine)),
        ("lingering", Err(CleanupError::StillPresent)),
        ("not_found", Ok(())),
        ("", Ok(())),
    ] {
        *engine.mode.lock().unwrap() = mode;
        assert_eq!(
            turns
                .cleanup(&engine, &target, Duration::from_secs(1))
                .await,
            expected
        );
    }
    assert_eq!(
        engine.exact_turn_containers(&other_turn).await.unwrap(),
        ["active"]
    );
    assert_eq!(
        engine.exact_turn_containers(&other_chat).await.unwrap(),
        ["protected"]
    );
}
