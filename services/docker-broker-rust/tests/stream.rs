use denkzeug_docker_broker_spike::stream::{
    Exit, LogError, RunnerLogs, Terminal, exit_event, watchdog_budget,
};
use serde_json::json;
use std::time::Duration;

fn docker_frame(stream: u8, data: &[u8]) -> Vec<u8> {
    let mut frame = vec![stream, 0, 0, 0];
    frame.extend((data.len() as u32).to_be_bytes());
    frame.extend(data);
    frame
}

#[test]
fn docker_chunks_and_ndjson_records_can_split_at_any_byte() {
    let raw = [
        docker_frame(1, b"  {\"type\":\"text\",\"text\":\"h"),
        docker_frame(2, b"never copy stderr into stdout or the event log"),
        docker_frame(1, b"i\"}\n \n"),
        docker_frame(
            1,
            b"{\"type\":\"result\",\"ok\":false,\"error\":\"EACCES: session.tmp\"}\nplain ",
        ),
        docker_frame(1, &[0xff]),
    ]
    .concat();
    for chunk_size in [1, 2, 7, 8, 11, 64, raw.len()] {
        let mut reader = RunnerLogs::new(1024);
        let mut events = Vec::new();
        for chunk in raw.chunks(chunk_size) {
            reader.push(chunk, |line| events.push(line)).unwrap();
        }
        reader.finish(|line| events.push(line)).unwrap();
        assert_eq!(
            events,
            [
                "{\"type\":\"text\",\"text\":\"hi\"}\n",
                "{\"type\":\"result\",\"ok\":false,\"error\":\"EACCES: session.tmp\"}\n",
                "plain �\n"
            ]
        );
        assert_eq!(reader.result_error(), "EACCES: session.tmp");
    }
}

#[test]
fn oversized_and_truncated_streams_do_not_get_silently_reported_as_success() {
    let mut malformed = RunnerLogs::new(5);
    assert_eq!(
        malformed.push(&docker_frame(0, b"x"), |_| panic!()),
        Err(LogError::InvalidHeader)
    );
    let mut oversized = RunnerLogs::new(5);
    assert_eq!(
        oversized.push(&docker_frame(1, b"123456"), |_| panic!()),
        Err(LogError::OversizedLine)
    );
    let mut truncated = RunnerLogs::new(5);
    truncated
        .push(&docker_frame(1, b"123")[..10], |_| panic!())
        .unwrap();
    assert_eq!(
        truncated.finish(|_| panic!()),
        Err(LogError::TruncatedFrame)
    );
    let mut many_short_events = RunnerLogs::new(5);
    let mut count = 0;
    many_short_events
        .push(&docker_frame(1, b"1\n2\n3\n4\n5\n"), |_| count += 1)
        .unwrap();
    assert_eq!(count, 5);
}

#[test]
fn cancellation_wins_over_sigkill_and_timeout_wins_over_engine_failures() {
    let crashed = Exit {
        code: 137,
        oom_killed: false,
        stderr: String::new(),
    };
    assert_eq!(exit_event(Terminal::Stopped, Err(()), "", "4g"), None);
    for exit in [Ok(crashed.clone()), Err(())] {
        let event = exit_event(Terminal::TimedOut(12), exit, "", "4g").unwrap();
        assert_eq!(event["reason"], "timeout");
        assert_eq!(event["timeout_seconds"], 12);
    }
    let ordinary = exit_event(Terminal::Exited, Ok(crashed.clone()), "", "4g").unwrap();
    let oom = exit_event(
        Terminal::Exited,
        Ok(Exit {
            oom_killed: true,
            ..crashed
        }),
        "",
        "4g",
    )
    .unwrap();
    assert_eq!(ordinary["reason"], "sigkill");
    assert_eq!(oom["reason"], "oom_killed");
    assert!(oom["message"].as_str().unwrap().contains("4g"));
    assert_eq!(
        exit_event(
            Terminal::Exited,
            Ok(Exit {
                code: 1,
                oom_killed: false,
                stderr: String::new()
            }),
            "EACCES: session.tmp",
            "4g"
        )
        .unwrap(),
        json!({"type":"error", "message":"agent container exited 1: EACCES: session.tmp"})
    );
}

#[test]
fn watchdog_exemption_applies_only_to_explicit_interactive_turns() {
    for (requested, expected) in [
        (None, 3600),
        (Some(900), 900),
        (Some(1800), 1800),
        (Some(7200), 3600),
    ] {
        let job = match requested {
            Some(value) => json!({"timeout_seconds":value}),
            None => json!({}),
        };
        assert_eq!(
            watchdog_budget(&job, 3600).unwrap(),
            Some(Duration::from_secs(expected))
        );
    }
    for (mode, chat, turn, flag, requested, expected) in [
        ("interactive", "chat", "turn", json!(true), Some(900), None),
        (
            "workflow",
            "chat",
            "turn",
            json!(true),
            Some(7200),
            Some(3600),
        ),
        (
            "interactive",
            "chat",
            "",
            json!(true),
            Some(1800),
            Some(1800),
        ),
        ("interactive", "", "turn", json!(true), None, Some(3600)),
        (
            "interactive",
            "chat",
            "turn",
            json!("true"),
            Some(900),
            Some(900),
        ),
        (
            "interactive",
            "chat",
            "turn",
            json!(false),
            Some(0),
            Some(3600),
        ),
    ] {
        let job = json!({"mode":mode,"chat_id":chat,"turn_id":turn,"timeout_exempt":flag,"timeout_seconds":requested});
        assert_eq!(
            watchdog_budget(&job, 3600).unwrap(),
            expected.map(Duration::from_secs)
        );
    }
    assert!(watchdog_budget(&json!({"timeout_seconds":"invalid"}), 3600).is_err());
}
