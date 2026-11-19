//! Exercise the real executable's signal handler, not just a router drop.
use rustix::process::{Pid, Signal, kill_process};
use std::{
    io::{BufRead, BufReader},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
fn sigterm_drains_the_http_server_and_exits_successfully() {
    let root = tempfile::tempdir().unwrap();
    let mut child = Process(
        Command::new(env!("CARGO_BIN_EXE_denkzeug-docker-broker-spike"))
            .env_clear()
            .env("INTERNAL_TOKEN", "synthetic-local-token")
            .env("BROKER_DOCKER_SOCKET", root.path().join("none.sock"))
            .env("BROKER_PORT", "0")
            .env("OTEL_SDK_DISABLED", "true")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let mut line = String::new();
    BufReader::new(child.0.stderr.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    let address = line.split_whitespace().last().unwrap();
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    assert!(
        client
            .get(format!("http://{address}/healthz"))
            .send()
            .unwrap()
            .status()
            .is_success()
    );
    kill_process(
        Pid::from_raw(child.0.id().try_into().unwrap()).unwrap(),
        Signal::TERM,
    )
    .unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(status) = child.0.try_wait().unwrap() {
            assert!(
                status.success(),
                "signal did not invoke graceful shutdown: {status}"
            );
            break;
        }
        assert!(
            Instant::now() < deadline,
            "broker did not finish its shutdown"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}
