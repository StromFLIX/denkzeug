//! Private, repository-scoped Git credential payloads. Docker orchestration
//! must verify exact live ownership before staging or activating a renewal.
use std::{collections::BTreeSet, fmt, io};

use chrono::DateTime;
use serde_json::Value;

const DIR_NAME: &str = "nautionette-git-credentials";
const DIR: &str = "/tmp/nautionette-git-credentials";

#[derive(Debug, PartialEq, Eq)]
pub enum InvalidBatch {
    Size,
    InvalidCredential,
    Expiry,
    Scope,
}

impl fmt::Display for InvalidBatch {
    fn fmt(&self, out: &mut fmt::Formatter<'_>) -> fmt::Result {
        out.write_str(match self {
            Self::Size => "Expected repository-scoped Git credentials",
            Self::InvalidCredential => "Invalid Git credential",
            Self::Expiry => "Invalid Git credential expiry",
            Self::Scope => "Git credential repository scope does not match this turn",
        })
    }
}

impl std::error::Error for InvalidBatch {}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Delivery {
    Initial,
    Renewal,
}

// Deliberately not Debug or Serialize. Only archive() may expose token bytes.
pub struct Batch(Value);

impl Batch {
    pub fn validate(
        credentials: Value,
        expected: Option<&BTreeSet<String>>,
    ) -> Result<Self, InvalidBatch> {
        let values = credentials
            .as_array()
            .filter(|v| (1..=20).contains(&v.len()))
            .ok_or(InvalidBatch::Size)?;
        let mut names = BTreeSet::new();
        for item in values {
            let get = |key| item[key].as_str().ok_or(InvalidBatch::InvalidCredential);
            let name = get("full_name")?;
            let token = get("token")?;
            let expiry = get("expires_at")?;
            if !valid_repository(name)
                || !names.insert(name)
                || token.is_empty()
                || token.chars().take(1025).count() > 1024
                || token.bytes().any(|byte| byte.is_ascii_control())
            {
                return Err(InvalidBatch::InvalidCredential);
            }
            // The producer uses GitHub's timezone-qualified RFC 3339 expiry.
            if DateTime::parse_from_rfc3339(expiry).is_err() {
                return Err(InvalidBatch::Expiry);
            }
        }
        if expected.is_some_and(|expected| {
            !names
                .iter()
                .copied()
                .eq(expected.iter().map(String::as_str))
        }) {
            return Err(InvalidBatch::Scope);
        }
        Ok(Self(credentials))
    }

    pub fn archive(&self, delivery: Delivery) -> io::Result<(&'static str, Vec<u8>)> {
        let mut archive = tar::Builder::new(Vec::new());
        let (target, filename) = match delivery {
            Delivery::Initial => {
                let mut header = tar::Header::new_ustar();
                header.set_entry_type(tar::EntryType::Directory);
                header.set_mode(0o711);
                header.set_uid(0);
                header.set_gid(0);
                header.set_size(0);
                archive.append_data(&mut header, DIR_NAME, io::empty())?;
                ("/tmp", "nautionette-git-credentials/current.json")
            }
            Delivery::Renewal => (DIR, "pending.json"),
        };
        let raw = serde_json::to_vec(&self.0)?;
        let mut header = tar::Header::new_ustar();
        header.set_entry_type(tar::EntryType::Regular);
        header.set_mode(0o440);
        header.set_uid(0);
        header.set_gid(10001);
        header.set_size(raw.len() as u64);
        archive.append_data(&mut header, filename, raw.as_slice())?;
        Ok((target, archive.into_inner()?))
    }
}

pub fn activation() -> (&'static str, [&'static str; 3]) {
    (
        "0:0",
        [
            "node",
            "-e",
            "require('node:fs').renameSync('/tmp/nautionette-git-credentials/pending.json', '/tmp/nautionette-git-credentials/current.json')",
        ],
    )
}

fn valid_repository(value: &str) -> bool {
    let part = |value: &str| {
        !value.is_empty()
            && value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
    };
    value
        .split_once('/')
        .is_some_and(|(owner, repo)| part(owner) && part(repo))
}
