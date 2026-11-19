//! Agent ownership, exact-turn fencing and recovery, independent of HTTP I/O.
use std::{
    collections::{BTreeMap, HashMap},
    sync::{Arc, Mutex},
    time::Duration,
};

use tokio::{
    sync::{Mutex as AsyncMutex, MutexGuard, watch},
    time::{Instant, timeout},
};

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, serde::Serialize)]
pub struct TurnKey {
    chat_id: String,
    turn_id: String,
}

impl TurnKey {
    pub fn new(chat: &str, turn: &str) -> Option<Self> {
        (!chat.is_empty() && !turn.is_empty()).then(|| Self {
            chat_id: chat.into(),
            turn_id: turn.into(),
        })
    }

    pub fn chat_id(&self) -> &str {
        &self.chat_id
    }
    pub fn turn_id(&self) -> &str {
        &self.turn_id
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum BeginError {
    Retired,
    Duplicate,
}

#[derive(Debug, PartialEq, Eq)]
pub enum CleanupError {
    Deadline,
    Engine,
    StillPresent,
}

#[derive(Debug, PartialEq, Eq)]
pub enum RemoveResult {
    Removed,
    AlreadyAbsent,
}

/// Implementations must verify deployment, chat ID, and turn ID before
/// returning container IDs. Recovery never removes volumes or releases filesystem claims.
pub trait RecoveryEngine {
    fn exact_turn_containers(
        &self,
        turn: &TurnKey,
    ) -> impl Future<Output = Result<Vec<String>, ()>> + Send;
    fn force_remove(&self, id: &str) -> impl Future<Output = Result<RemoveResult, ()>> + Send;
}

pub struct Turn {
    key: TurnKey,
    stopped: watch::Sender<bool>,
    released: watch::Sender<bool>,
    start_gate: AsyncMutex<()>,
}

impl Turn {
    pub(crate) fn detached() -> Arc<Self> {
        Arc::new(Self::new(TurnKey {
            chat_id: String::new(),
            turn_id: String::new(),
        }))
    }

    fn new(key: TurnKey) -> Self {
        Self {
            key,
            stopped: watch::channel(false).0,
            released: watch::channel(false).0,
            start_gate: AsyncMutex::new(()),
        }
    }

    pub fn is_stopped(&self) -> bool {
        *self.stopped.borrow()
    }

    pub async fn wait_for_stop(&self) {
        let _ = self.stopped.subscribe().wait_for(|flag| *flag).await;
    }

    /// Keep the permit until the bounded Docker start request has returned.
    /// A concurrent cleanup waits for that request rather than racing a late start.
    pub async fn start_permit(&self) -> Option<MutexGuard<'_, ()>> {
        let guard = self.start_gate.lock().await;
        (!self.is_stopped()).then_some(guard)
    }
}

#[derive(Default)]
struct State {
    active: BTreeMap<TurnKey, Arc<Turn>>,
    retired: HashMap<TurnKey, Instant>,
}

pub struct TurnRegistry {
    state: Mutex<State>,
    fence_for: Duration,
}

impl TurnRegistry {
    pub fn new(fence_for: Duration) -> Self {
        Self {
            state: Mutex::new(State::default()),
            fence_for,
        }
    }

    pub fn begin(&self, key: TurnKey) -> Result<Arc<Turn>, BeginError> {
        let now = Instant::now();
        let mut state = self.state.lock().expect("turn registry poisoned");
        state.retired.retain(|_, deadline| *deadline > now);
        if state.retired.contains_key(&key) {
            return Err(BeginError::Retired);
        }
        if state.active.contains_key(&key) {
            return Err(BeginError::Duplicate);
        }
        let turn = Arc::new(Turn::new(key.clone()));
        state.active.insert(key, turn.clone());
        Ok(turn)
    }

    /// Only the owner calls this after its container and claims are released.
    /// Dropping an HTTP request is not an acknowledgement of cleanup.
    pub fn finish(&self, turn: &Arc<Turn>) {
        let mut state = self.state.lock().expect("turn registry poisoned");
        if state
            .active
            .get(&turn.key)
            .is_some_and(|active| Arc::ptr_eq(active, turn))
        {
            state.active.remove(&turn.key);
            turn.released.send_replace(true);
        }
    }

    pub fn inventory(&self, chat: &str) -> Vec<TurnKey> {
        self.state
            .lock()
            .expect("turn registry poisoned")
            .active
            .keys()
            .filter(|key| chat.is_empty() || key.chat_id == chat)
            .cloned()
            .collect()
    }

    pub fn running(&self, key: &TurnKey) -> bool {
        self.state
            .lock()
            .expect("turn registry poisoned")
            .active
            .get(key)
            .is_some_and(|turn| !turn.is_stopped())
    }

    pub fn request_stop(&self, key: &TurnKey) -> bool {
        let state = self.state.lock().expect("turn registry poisoned");
        if let Some(turn) = state.active.get(key) {
            turn.stopped.send_replace(true);
            true
        } else {
            false
        }
    }

    fn retire(&self, key: &TurnKey) -> Option<Arc<Turn>> {
        let now = Instant::now();
        let mut state = self.state.lock().expect("turn registry poisoned");
        state.retired.retain(|_, deadline| *deadline > now);
        state.retired.insert(key.clone(), now + self.fence_for);
        let turn = state.active.get(key).cloned();
        if let Some(turn) = &turn {
            turn.stopped.send_replace(true);
        }
        turn
    }

    pub async fn cleanup(
        &self,
        engine: &impl RecoveryEngine,
        key: &TurnKey,
        budget: Duration,
    ) -> Result<(), CleanupError> {
        let active = self.retire(key);
        let recover = async {
            // Serialize with an in-flight start for this turn only. Never hold
            // the registry lock across Docker requests or claim release.
            let start_gate = match &active {
                Some(turn) => Some(turn.start_gate.lock().await),
                None => None,
            };
            for id in engine
                .exact_turn_containers(key)
                .await
                .map_err(|_| CleanupError::Engine)?
            {
                engine
                    .force_remove(&id)
                    .await
                    .map_err(|_| CleanupError::Engine)?;
            }
            drop(start_gate);
            if let Some(turn) = &active {
                let _ = turn.released.subscribe().wait_for(|flag| *flag).await;
            }
            if !engine
                .exact_turn_containers(key)
                .await
                .map_err(|_| CleanupError::Engine)?
                .is_empty()
            {
                return Err(CleanupError::StillPresent);
            }
            Ok(())
        };
        timeout(budget, recover)
            .await
            .map_err(|_| CleanupError::Deadline)?
    }
}
