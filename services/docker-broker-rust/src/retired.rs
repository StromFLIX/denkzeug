//! One-time, stack-scoped removal of legacy miniapp resources; never global prune.
use std::sync::atomic::Ordering;

use axum::http::{Method, StatusCode};
use serde_json::{Value, json};

use crate::{Broker, turns::RecoveryEngine, valid_docker_id};

const API: &str = "/v1.45";
const LABEL: &str = "nautionette.miniapp";
const DEPLOY: &str = "nautionette.deployment";

pub(crate) fn component(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 255
        && !matches!(s, "." | "..")
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_'))
}

impl Broker {
    pub(crate) async fn list_docker_resources(
        &self,
        endpoint: &str,
        labels: &[String],
    ) -> Result<Value, ()> {
        let filters = json!({"label":labels}).to_string();
        let query = form_urlencoded::Serializer::new(String::new())
            .append_pair("filters", &filters)
            .finish();
        let (status, result) = self
            .docker_json(
                Method::GET,
                &format!("{API}/{endpoint}?{query}"),
                Value::Null,
                self.config.deadline,
            )
            .await?;
        (status == StatusCode::OK).then_some(result).ok_or(())
    }

    pub(crate) async fn reconcile_retired(&self) {
        if self.retired_complete.load(Ordering::Acquire) || self.config.workers.is_none() {
            return;
        }
        if self.try_reconcile_retired().await.is_ok() {
            self.retired_complete.store(true, Ordering::Release);
        }
    }

    async fn try_reconcile_retired(&self) -> Result<(), ()> {
        let own = &self.config.workers.as_ref().ok_or(())?.docker_self;
        if !component(own) {
            return Err(());
        }
        let (status, broker) = self
            .docker_json(
                Method::GET,
                &format!("{API}/containers/{own}/json"),
                Value::Null,
                self.config.deadline,
            )
            .await?;
        if status != StatusCode::OK {
            return Err(());
        }
        let broker_id = broker["Id"]
            .as_str()
            .filter(|id| valid_docker_id(id))
            .ok_or(())?;
        let project = broker["Config"]["Labels"]["com.docker.compose.project"]
            .as_str()
            .filter(|s| component(s))
            .ok_or(())?;
        let namespace = &self.config.workflows_volume;
        let filter = [LABEL.to_owned(), format!("{DEPLOY}={namespace}")];
        let owned = |value: &Value| {
            value["Labels"][DEPLOY] == *namespace
                && value["Labels"][LABEL]
                    .as_str()
                    .is_some_and(|s| !s.is_empty())
        };
        for row in self
            .list_containers(&filter, true)
            .await?
            .iter()
            .filter(|row| owned(row))
        {
            let id = row["Id"]
                .as_str()
                .filter(|id| valid_docker_id(id))
                .ok_or(())?;
            self.force_remove(id).await?;
        }
        let networks = self.list_docker_resources("networks", &filter).await?;
        for row in networks
            .as_array()
            .ok_or(())?
            .iter()
            .filter(|row| owned(row))
        {
            let id = row["Id"]
                .as_str()
                .filter(|id| valid_docker_id(id))
                .ok_or(())?;
            let (status, network) = self
                .docker_json(
                    Method::GET,
                    &format!("{API}/networks/{id}"),
                    Value::Null,
                    self.config.deadline,
                )
                .await?;
            if status == StatusCode::NOT_FOUND {
                continue;
            }
            if status != StatusCode::OK || !owned(&network) {
                return Err(());
            }
            if network["Containers"].get(broker_id).is_some() {
                let (status, _) = self
                    .docker_json(
                        Method::POST,
                        &format!("{API}/networks/{id}/disconnect"),
                        json!({"Container":broker_id,"Force":true}),
                        self.config.deadline,
                    )
                    .await?;
                if status != StatusCode::OK {
                    return Err(());
                }
            }
            let (status, _) = self
                .docker_request(Method::DELETE, &format!("{API}/networks/{id}"), 1024)
                .await?;
            if !matches!(status, StatusCode::NO_CONTENT | StatusCode::NOT_FOUND) {
                return Err(());
            }
        }
        let images = self.list_docker_resources("images/json", &filter).await?;
        for row in images.as_array().ok_or(())?.iter().filter(|row| owned(row)) {
            let id = row["Id"]
                .as_str()
                .filter(|id| id.strip_prefix("sha256:").is_some_and(valid_docker_id))
                .ok_or(())?;
            let (status, _) = self
                .docker_request(
                    Method::DELETE,
                    &format!("{API}/images/{id}?force=false"),
                    128 * 1024,
                )
                .await?;
            if !matches!(status, StatusCode::OK | StatusCode::NOT_FOUND) {
                return Err(());
            }
        }
        let labels = [
            format!("com.docker.compose.project={project}"),
            "com.docker.compose.volume=miniapps".to_owned(),
        ];
        let volumes = self.list_docker_resources("volumes", &labels).await?;
        if let Some(volumes) = volumes["Volumes"].as_array() {
            for row in volumes.iter().filter(|row| {
                row["Labels"]["com.docker.compose.project"] == project
                    && row["Labels"]["com.docker.compose.volume"] == "miniapps"
            }) {
                let name = row["Name"]
                    .as_str()
                    .filter(|name| component(name))
                    .ok_or(())?;
                self.delete_volume(name).await?;
            }
        }
        Ok(())
    }
}
