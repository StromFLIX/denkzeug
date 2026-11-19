//! Plan runner payload transport without invoking Docker. Secret-bearing types
//! intentionally omit `Debug`, so callers cannot accidentally trace them.
use std::{collections::BTreeMap, io};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use serde_json::{Value, json};

pub struct JobContext {
    pub agent_env: BTreeMap<String, String>,
    pub token: String,
    pub worker_network: String,
    pub chat_network: String,
    pub workflows_volume: String,
    pub workspace_size: String,
    pub traced_parent: Option<String>,
}

pub struct AgentPlan {
    pub environment: BTreeMap<String, String>,
    pub archives: Vec<PrivateArchive>,
    pub user: Option<&'static str>,
    pub network: String,
    pub workflows_volume: Option<String>,
    pub tmpfs: BTreeMap<&'static str, String>,
    pub package_runtime: Value,
    pub project_credentials: Value,
}

pub struct PrivateArchive {
    bytes: Vec<u8>,
}

impl PrivateArchive {
    pub fn destination(&self) -> &'static str {
        "/tmp"
    }
    pub fn contents(&self) -> &[u8] {
        &self.bytes
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(values) => !values.is_empty(),
        Value::Object(values) => !values.is_empty(),
    }
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

pub fn plan_job(job: Value, context: &JobContext) -> io::Result<AgentPlan> {
    let mut job = match job {
        Value::Object(fields) => fields,
        _ => return Err(invalid("agent job must be an object")),
    };
    let member = job.get("member_mode").is_some_and(truthy);
    if member {
        if job
            .get("agent_set")
            .is_some_and(|value| truthy(value) && value != "default")
        {
            return Err(invalid("This container environment is administrator-only"));
        }
        for field in [
            "packages",
            "package_runtime",
            "project_credentials",
            "artcraft",
        ] {
            job.insert(field.into(), json!([]));
        }
    }
    let chat = job.get("chat_id").is_some_and(truthy);
    let unprivileged = chat || member || job.get("project_ids").is_some_and(truthy);
    let package_runtime = job.remove("package_runtime").unwrap_or_else(|| json!([]));
    let project_credentials = job
        .remove("project_credentials")
        .unwrap_or_else(|| json!([]));
    let outputs = job.remove("output_publication");
    job.remove("_trace_context");
    let mut environment = context.agent_env.clone();
    environment.remove("AGENT_JOB");
    environment.remove("AGENT_JOB_FILE");
    if let Some(trace) = &context.traced_parent {
        job.insert("_trace_context".into(), json!({"traceparent":trace}));
        environment.insert("TRACEPARENT".into(), trace.clone());
    }
    if chat || member {
        environment.remove("BACKEND_URL");
        environment.remove("INTERNAL_TOKEN");
    } else if !context.token.is_empty() {
        environment.insert("INTERNAL_TOKEN".into(), context.token.clone());
    }
    if member {
        environment.remove("OTEL_EXPORTER_OTLP_HEADERS");
        environment.insert("OTEL_SDK_DISABLED".into(), "true".into());
    }
    let mut tmpfs = BTreeMap::new();
    let user = if unprivileged {
        environment.insert("HOME".into(), "/workspace".into());
        environment.insert("PI_CODING_AGENT_DIR".into(), "/workspace/.pi-agent".into());
        tmpfs.insert("/projects", "size=1m,uid=10001,gid=10001".to_owned());
        tmpfs.insert(
            "/workspace",
            format!("size={},exec,uid=10001,gid=10001", context.workspace_size),
        );
        Some("10001:10001")
    } else {
        tmpfs.insert(
            "/workspace",
            format!("size={},exec", context.workspace_size),
        );
        None
    };

    let history_has_files = match job.get("history") {
        None => false,
        Some(Value::Array(history)) => history
            .iter()
            .any(|message| message.get("files").is_some_and(truthy)),
        Some(_) => return Err(invalid("agent history must be an array")),
    };
    let has_files = history_has_files || job.get("files").is_some_and(truthy);
    let raw = serde_json::to_vec(&job)?;
    let mut entries = Vec::new();
    if has_files || raw.len() > 32_000 {
        environment.insert("AGENT_JOB_FILE".into(), "/tmp/nautionette-job.json".into());
        entries.push(("nautionette-job.json", raw));
    } else {
        environment.insert("AGENT_JOB".into(), STANDARD.encode(raw));
    }
    if truthy(&package_runtime) {
        entries.push((
            "nautionette-packages.json",
            serde_json::to_vec(&package_runtime)?,
        ));
    }
    if let Some(outputs) = outputs.filter(truthy) {
        entries.push(("nautionette-outputs.json", serde_json::to_vec(&outputs)?));
    }
    let archives = if entries.is_empty() {
        Vec::new()
    } else {
        // These files have identical target directory and ownership, so Docker
        // only needs one upload. Git credentials have different permissions and
        // are always prepared separately by the scoped credential handler.
        let mut archive = tar::Builder::new(Vec::new());
        let owner = if unprivileged { 10001 } else { 0 };
        for (name, data) in entries {
            let mut header = tar::Header::new_ustar();
            header.set_entry_type(tar::EntryType::Regular);
            header.set_mode(0o600);
            header.set_uid(owner);
            header.set_gid(owner);
            header.set_size(data.len() as u64);
            archive.append_data(&mut header, name, data.as_slice())?;
        }
        vec![PrivateArchive {
            bytes: archive.into_inner()?,
        }]
    };
    Ok(AgentPlan {
        environment,
        archives,
        user,
        network: if chat || member {
            context.chat_network.clone()
        } else {
            context.worker_network.clone()
        },
        workflows_volume: (!member).then(|| context.workflows_volume.clone()),
        tmpfs,
        package_runtime,
        project_credentials,
    })
}
