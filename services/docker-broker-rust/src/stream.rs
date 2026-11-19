//! Docker's non-TTY log multiplexing and the broker's NDJSON contract.
use std::time::Duration;

use serde_json::{Value, json};

#[derive(Debug, PartialEq, Eq)]
pub enum LogError {
    InvalidHeader,
    TruncatedFrame,
    OversizedLine,
}

pub struct RunnerLogs {
    header: [u8; 8],
    header_len: usize,
    remaining: usize,
    line: Vec<u8>,
    max_line: usize,
    result_error: String,
}

impl RunnerLogs {
    pub fn new(max_line: usize) -> Self {
        Self {
            header: [0; 8],
            header_len: 0,
            remaining: 0,
            line: Vec::new(),
            max_line,
            result_error: String::new(),
        }
    }

    pub fn push(&mut self, mut bytes: &[u8], mut emit: impl FnMut(String)) -> Result<(), LogError> {
        while !bytes.is_empty() {
            if self.remaining == 0 {
                let size = (8 - self.header_len).min(bytes.len());
                self.header[self.header_len..self.header_len + size]
                    .copy_from_slice(&bytes[..size]);
                self.header_len += size;
                bytes = &bytes[size..];
                if self.header_len < 8 {
                    break;
                }
                if !matches!(self.header[0], 1 | 2) || self.header[1..4] != [0, 0, 0] {
                    return Err(LogError::InvalidHeader);
                }
                self.remaining =
                    u32::from_be_bytes(self.header[4..8].try_into().expect("fixed length"))
                        as usize;
                self.header_len = 0;
            }
            let size = self.remaining.min(bytes.len());
            if self.header[0] == 1 {
                self.stdout(&bytes[..size], &mut emit)?;
            }
            self.remaining -= size;
            bytes = &bytes[size..];
        }
        Ok(())
    }

    pub fn finish(&mut self, mut emit: impl FnMut(String)) -> Result<(), LogError> {
        if self.remaining != 0 || self.header_len != 0 {
            return Err(LogError::TruncatedFrame);
        }
        self.emit_line(&mut emit);
        Ok(())
    }

    pub fn result_error(&self) -> &str {
        &self.result_error
    }

    fn stdout(&mut self, mut bytes: &[u8], emit: &mut impl FnMut(String)) -> Result<(), LogError> {
        while !bytes.is_empty() {
            let newline = bytes.iter().position(|byte| *byte == b'\n');
            let end = newline.unwrap_or(bytes.len());
            if end > self.max_line.saturating_sub(self.line.len()) {
                return Err(LogError::OversizedLine);
            }
            self.line.extend_from_slice(&bytes[..end]);
            if newline.is_none() {
                break;
            }
            self.emit_line(emit);
            bytes = &bytes[end + 1..];
        }
        Ok(())
    }

    fn emit_line(&mut self, emit: &mut impl FnMut(String)) {
        let decoded = String::from_utf8_lossy(&self.line);
        let text = decoded.trim();
        if !text.is_empty() {
            if let Ok(result) = serde_json::from_str::<Value>(text)
                && result["type"] == "result"
                && result["ok"] == false
            {
                self.result_error = result["error"]
                    .as_str()
                    .unwrap_or_default()
                    .chars()
                    .take(800)
                    .collect();
            }
            emit(format!("{text}\n"));
        }
        self.line.clear();
    }
}

pub fn watchdog_budget(job: &Value, ceiling: u64) -> Result<Option<Duration>, &'static str> {
    let nonempty = |key| job[key].as_str().is_some_and(|s| !s.is_empty());
    if job["timeout_exempt"] == true
        && job["mode"] == "interactive"
        && nonempty("chat_id")
        && nonempty("turn_id")
    {
        return Ok(None);
    }
    let requested = match &job["timeout_seconds"] {
        Value::Null | Value::Bool(false) => ceiling,
        Value::Bool(true) => 1,
        Value::Number(number) => match (number.as_u64(), number.as_f64()) {
            (Some(0), _) => ceiling,
            (Some(n), _) => n,
            (None, Some(0.0)) => ceiling,
            (None, Some(n)) => n as u64,
            _ => return Err("Invalid agent timeout"),
        },
        Value::String(value) if value.is_empty() => ceiling,
        Value::String(value) => value
            .trim()
            .parse::<i64>()
            .map_err(|_| "Invalid agent timeout")?
            .max(0) as u64,
        _ => return Err("Invalid agent timeout"),
    };
    Ok(Some(Duration::from_secs(requested.min(ceiling))))
}

#[derive(Debug, Clone)]
pub struct Exit {
    pub code: i64,
    pub oom_killed: bool,
    pub stderr: String,
}

pub enum Terminal {
    Stopped,
    TimedOut(u64),
    Exited,
}

pub fn exit_event(
    terminal: Terminal,
    exit: Result<Exit, ()>,
    result_error: &str,
    memory: &str,
) -> Option<Value> {
    match terminal {
        Terminal::Stopped => return None,
        Terminal::TimedOut(seconds) => {
            return Some(
                json!({"type":"error", "reason":"timeout", "timeout_seconds":seconds,
            "message":format!("agent call exceeded its {seconds}s time limit and was stopped. Disable agent timeout in Chat options before continuing in a new message, or increase AGENT_RUN_TIMEOUT_SECONDS (workflow calls must also increase their timeout_seconds).") }),
            );
        }
        Terminal::Exited => (),
    }
    let Ok(exit) = exit else {
        return Some(
            json!({"type":"error", "message":"Could not observe the agent container exit"}),
        );
    };
    let code = exit.code;
    match (code, exit.oom_killed) {
        (0, _) => None,
        (_, true) => Some(
            json!({"type":"error", "reason":"oom_killed", "exit_code":code,
            "message":format!("agent container ran out of memory (limit {memory}, exit {code}). Reduce memory use or increase AGENT_MEMORY_LIMIT.")}),
        ),
        (137, false) => Some(json!({"type":"error","reason":"sigkill", "exit_code":code,
            "message":"agent container was killed (SIGKILL, exit 137). The broker timeout did not fire and Docker did not report an OOM kill; check host memory and deployment logs."})),
        (_, false) => {
            let detail = if result_error.is_empty() {
                exit.stderr
                    .char_indices()
                    .rev()
                    .nth(799)
                    .map_or(exit.stderr.as_str(), |(offset, _)| &exit.stderr[offset..])
            } else {
                result_error
            };
            Some(
                json!({"type":"error","message":format!("agent container exited {code}: {detail}")}),
            )
        }
    }
}
