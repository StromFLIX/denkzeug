//! Async runner lifecycle. The driver owns
//! mount/image policy; the supervisor owns order, deadlines and cleanup.
use std::{future::pending, sync::Arc, time::Duration};

use axum::body::Bytes;
use futures_util::{Stream, StreamExt};
use serde_json::{Value, json};
use tokio::{
    sync::mpsc,
    time::{Instant, sleep_until, timeout},
};

use crate::{
    stream::{Exit, RunnerLogs, Terminal, exit_event},
    turns::{BeginError, Turn, TurnKey, TurnRegistry},
};

pub trait RunnerEngine: Send + Sync + 'static {
    type Logs: Stream<Item = Result<Bytes, ()>> + Unpin + Send;
    fn ready(
        &self,
        _output: &mpsc::Sender<String>,
    ) -> impl Future<Output = Result<(), String>> + Send {
        async { Ok(()) }
    }
    fn prepare(&self) -> impl Future<Output = Result<(), &'static str>> + Send {
        async { Ok(()) }
    }
    fn create(&self) -> impl Future<Output = Result<String, ()>> + Send;
    fn stage(&self, id: &str) -> impl Future<Output = Result<(), ()>> + Send;
    fn start(&self, id: &str) -> impl Future<Output = Result<(), ()>> + Send;
    fn logs(&self, id: &str) -> impl Future<Output = Result<Self::Logs, ()>> + Send;
    fn exit(&self, id: &str) -> impl Future<Output = Result<Exit, ()>> + Send;
    fn kill(&self, id: &str) -> impl Future<Output = Result<(), ()>> + Send;
    /// Force removal of exactly this container, preserving volumes, treating a 404 as success.
    fn remove(&self, id: &str) -> impl Future<Output = Result<(), ()>> + Send;
    /// Drop only this runner's claims, even on failed launch or removal. Future
    /// admission still has to verify no old container holds the mounts.
    fn release(&self) -> impl Future<Output = ()> + Send;
}

pub struct RunOptions {
    pub agent_set: String,
    pub image: String,
    pub memory: String,
    pub watchdog: Option<Duration>,
    pub operation_budget: Duration,
}

const MAX_EVENT_BYTES: usize = 32 * 1024 * 1024;
// One outstanding record bounds memory when the consumer slows down. No control
// request needs to use this channel to stop the runner.
const EVENTS_IN_FLIGHT: usize = 1;

pub fn run_turn(
    driver_registry: Arc<TurnRegistry>,
    key: TurnKey,
    driver: Arc<impl RunnerEngine>,
    options: RunOptions,
) -> mpsc::Receiver<String> {
    spawn(Some((driver_registry, key)), driver, options)
}

pub(crate) fn run_workflow(
    driver: Arc<impl RunnerEngine>,
    options: RunOptions,
) -> mpsc::Receiver<String> {
    spawn(None, driver, options)
}

fn spawn(
    registration: Option<(Arc<TurnRegistry>, TurnKey)>,
    driver: Arc<impl RunnerEngine>,
    options: RunOptions,
) -> mpsc::Receiver<String> {
    let (send, receive) = mpsc::channel(EVENTS_IN_FLIGHT);
    let owner = registration.as_ref().map_or_else(
        || Ok(Turn::detached()),
        |(registry, key)| registry.begin(key.clone()),
    );
    tokio::spawn(async move {
        let owner = match owner {
            Ok(owner) => owner,
            Err(reason) => {
                let message = match reason {
                    BeginError::Retired => "This turn was already cleaned up",
                    BeginError::Duplicate => "This turn is already running",
                };
                let _ = send
                    .send(ndjson(json!({"type":"error","message":message})))
                    .await;
                return;
            }
        };
        let image = tokio::select! { biased; _ = owner.wait_for_stop() => None, _ = send.closed() => None, result = driver.ready(&send) => Some(result) };
        if !matches!(image, Some(Ok(()))) {
            driver.release().await;
            if let Some((registry, _)) = registration {
                registry.finish(&owner);
            }
            if let Some(Err(message)) = image {
                let _ = send
                    .send(ndjson(json!({"type":"error","message":message})))
                    .await;
            }
            return;
        }
        let _ = send
            .send(ndjson(
                json!({"type":"started","agent_set":options.agent_set,"image":options.image}),
            ))
            .await;
        let mut container = None;
        let event = drive(driver.as_ref(), &owner, &options, &send, &mut container).await;
        let removal = match container {
            Some(id) => within(options.operation_budget, driver.remove(&id)).await,
            None => Ok(()),
        };
        driver.release().await;
        if let Some((registry, _)) = registration {
            registry.finish(&owner);
        }
        if let Some(event) = event {
            let _ = send.send(ndjson(event)).await;
        }
        if removal.is_err() {
            let _ = send
                .send(ndjson(
                    json!({"type":"error","message":"Could not confirm agent container removal"}),
                ))
                .await;
        }
        let _ = send.send(ndjson(json!({"type":"closed"}))).await;
    });
    receive
}

fn ndjson(event: Value) -> String {
    format!("{event}\n")
}

async fn within<T>(
    budget: Duration,
    operation: impl Future<Output = Result<T, ()>>,
) -> Result<T, ()> {
    timeout(budget, operation).await.map_err(|_| ())?
}

async fn drive(
    driver: &impl RunnerEngine,
    owner: &Turn,
    options: &RunOptions,
    send: &mpsc::Sender<String>,
    container: &mut Option<String>,
) -> Option<Value> {
    let active = || !owner.is_stopped() && !send.is_closed();
    let error = |message| active().then(|| json!({"type":"error","message":message}));
    if !active() {
        return None;
    }
    let budget = options.operation_budget;
    match timeout(budget, driver.prepare()).await {
        Ok(Ok(())) => (),
        Ok(Err(message)) => return error(message),
        Err(_) => return error("Agent storage preparation timed out"),
    }
    if !active() {
        return None;
    }
    let Ok(id) = within(budget, driver.create()).await else {
        return error("Could not create the agent container");
    };
    let id = container.insert(id);
    if !active() {
        return None;
    }
    if within(budget, driver.stage(id)).await.is_err() {
        return error("Could not deliver the agent job");
    }
    {
        let _permit = owner.start_permit().await?;
        if !active() {
            return None;
        }
        if within(budget, driver.start(id)).await.is_err() {
            return error("Could not start the agent container");
        }
    }
    let mut decoder = RunnerLogs::new(MAX_EVENT_BYTES);
    let (terminal, exit) = {
        let completion = observe(driver, id, budget, send, &mut decoder);
        let deadline = async {
            if let Some(watchdog) = options.watchdog {
                sleep_until(Instant::now() + watchdog).await;
                watchdog.as_secs()
            } else {
                pending().await
            }
        };
        tokio::select! {
            biased;
            _ = owner.wait_for_stop() => (Terminal::Stopped, Err(())),
            _ = send.closed() => (Terminal::Stopped, Err(())),
            seconds = deadline => (Terminal::TimedOut(seconds), Err(())),
            exit = completion => (Terminal::Exited, exit),
        }
    };
    let terminal = if owner.is_stopped() || send.is_closed() {
        Terminal::Stopped
    } else {
        terminal
    };
    if matches!(terminal, Terminal::Stopped | Terminal::TimedOut(_)) {
        // Record the terminal reason before kill. Closing a silent stream or
        // racing a naturally exited container must not overwrite the reason.
        let _ = within(budget, driver.kill(id)).await;
    }
    exit_event(terminal, exit, decoder.result_error(), &options.memory)
}

async fn observe(
    driver: &impl RunnerEngine,
    id: &str,
    budget: Duration,
    send: &mpsc::Sender<String>,
    decoder: &mut RunnerLogs,
) -> Result<Exit, ()> {
    let mut logs = within(budget, driver.logs(id)).await?;
    let mut events = Vec::new();
    while let Some(chunk) = logs.next().await {
        let chunk = chunk?;
        // Drain lines incrementally even if the Docker client produces one large chunk.
        for slice in chunk.chunks(8192) {
            decoder
                .push(slice, |line| events.push(line))
                .map_err(|_| ())?;
            for event in events.drain(..) {
                send.send(event).await.map_err(|_| ())?;
            }
        }
    }
    decoder.finish(|line| events.push(line)).map_err(|_| ())?;
    for event in events {
        send.send(event).await.map_err(|_| ())?;
    }
    within(budget, driver.exit(id)).await
}
