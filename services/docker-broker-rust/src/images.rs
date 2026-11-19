use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{self, Read},
    path::{Path, PathBuf},
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Fingerprints {
    schema: u8,
    pub(crate) base: String,
    pub(crate) agent_sets: BTreeMap<String, String>,
}

/// Match the CI source fingerprint generator. This does not substitute for the
/// packaged lock: deployment build systems may rewrite the copied Dockerfiles.
pub fn fingerprints(root: &Path) -> io::Result<Fingerprints> {
    resolve(root, None)
}

/// Resolve the packaged lock first; only absent entries are hashed locally.
pub fn effective_fingerprints(root: &Path, lock: &Path) -> io::Result<Fingerprints> {
    let bytes = match fs::read(lock) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return fingerprints(root),
        Err(error) => return Err(error),
    };
    let invalid = || {
        io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid packaged fingerprint lock",
        )
    };
    if bytes.iter().find(|byte| !byte.is_ascii_whitespace()) != Some(&b'{') {
        return Err(invalid());
    }
    let lock: Fingerprints = serde_json::from_slice(&bytes).map_err(|_| invalid())?;
    fn valid(hash: &str) -> bool {
        hash.len() == 12 && hash.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    }
    if lock.schema != 1 || !valid(&lock.base) || !lock.agent_sets.values().all(|hash| valid(hash)) {
        return Err(invalid());
    }
    resolve(root, Some(&lock))
}

fn resolve(root: &Path, lock: Option<&Fingerprints>) -> io::Result<Fingerprints> {
    let mut source_base = None;
    let mut agent_sets = BTreeMap::new();
    for entry in fs::read_dir(root.join("agent-sets"))? {
        let entry = entry?;
        let path = entry.path();
        if path.is_dir() && path.join("Dockerfile").is_file() {
            let name = entry
                .file_name()
                .into_string()
                .map_err(|_| invalid_name())?;
            let hash = if let Some(hash) = lock.and_then(|lock| lock.agent_sets.get(&name)) {
                hash.clone()
            } else {
                let base = source_base_hash(&mut source_base, root)?;
                let hash = context_hash(&path)?;
                short_hex(&Sha256::digest(format!("{hash}{base}")))
            };
            agent_sets.insert(name, hash);
        }
    }
    Ok(Fingerprints {
        schema: 1,
        base: match lock {
            Some(lock) => lock.base.clone(),
            None => source_base_hash(&mut source_base, root)?.to_owned(),
        },
        agent_sets,
    })
}

fn source_base_hash<'a>(cached: &'a mut Option<String>, root: &Path) -> io::Result<&'a str> {
    if cached.is_none() {
        *cached = Some(context_hash(&root.join("pi-base"))?);
    }
    Ok(cached.as_deref().expect("cached base hash"))
}

fn short_hex(bytes: &[u8]) -> String {
    bytes[..6]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn invalid_name() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "non-UTF-8 image context path")
}

fn visit(root: &Path, dir: &Path, result: &mut Vec<(Vec<String>, PathBuf)>) -> io::Result<()> {
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        if matches!(entry.file_name().to_str(), Some("target" | "__pycache__")) {
            continue;
        }
        let path = entry.path();
        if entry.file_type()?.is_dir() {
            visit(root, &path, result)?;
        } else if path.is_file()
            && !matches!(
                path.extension().and_then(|v| v.to_str()),
                Some("pyc" | "pyo")
            )
        {
            let relative = path.strip_prefix(root).expect("walked descendants only");
            let parts = relative
                .iter()
                .map(|part| part.to_str().map(str::to_owned).ok_or_else(invalid_name))
                .collect::<io::Result<_>>()?;
            result.push((parts, path));
        }
    }
    Ok(())
}

fn context_hash(root: &Path) -> io::Result<String> {
    let mut files = Vec::new();
    visit(root, root, &mut files)?;
    // Python pathlib sorts component-by-component, not by the joined string.
    files.sort_unstable_by(|a, b| a.0.cmp(&b.0));
    let mut digest = Sha256::new();
    let mut buffer = [0; 32 * 1024];
    for (parts, path) in files {
        digest.update(parts.join("/").as_bytes());
        let mut file = File::open(path)?;
        loop {
            let n = file.read(&mut buffer)?;
            if n == 0 {
                break;
            }
            digest.update(&buffer[..n]);
        }
    }
    Ok(short_hex(&digest.finalize()))
}
