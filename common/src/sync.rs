use anki::collection::Collection;
use anki::error::{AnkiError, SyncErrorKind};
use anki::sync::collection::normal::SyncOutput;
use anki::sync::login::{SyncAuth, sync_login};
use anki_proto::sync::sync_status_response;
use std::sync::{Arc, Mutex};

#[derive(Debug, Clone)]
pub enum SyncStatus {
    Idle,
    Authenticating,
    CheckingStatus,
    SyncingCollection {
        progress: String,
    },
    SyncingMedia {
        checked: String,
        added: String,
        removed: String,
    },
    Complete {
        message: String,
    },
    Error {
        message: String,
    },
}

#[derive(Debug, Clone)]
pub struct SyncResult {
    pub success: bool,
    pub message: String,
    pub server_message: Option<String>,
}

pub struct SyncManager {
    http_client: reqwest::Client,
    sync_status: Arc<Mutex<SyncStatus>>,
}

impl SyncManager {
    pub fn new() -> Self {
        Self {
            http_client: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(60))
                .build()
                .unwrap(),
            sync_status: Arc::new(Mutex::new(SyncStatus::Idle)),
        }
    }

    pub fn get_status(&self) -> SyncStatus {
        self.sync_status.lock().unwrap().clone()
    }

    fn set_status(&self, status: SyncStatus) {
        *self.sync_status.lock().unwrap() = status;
    }

    fn fail<T>(&self, error: Box<dyn std::error::Error>) -> Result<T, Box<dyn std::error::Error>> {
        self.set_status(SyncStatus::Error {
            message: error.to_string(),
        });
        Err(error)
    }

    pub async fn login(
        &self,
        username: &str,
        password: &str,
    ) -> Result<String, Box<dyn std::error::Error>> {
        self.set_status(SyncStatus::Authenticating);

        match sync_login(username, password, None, self.http_client.clone()).await {
            Ok(auth) => {
                self.set_status(SyncStatus::Idle);
                Ok(auth.hkey)
            }
            Err(e) => self.fail(e.into()),
        }
    }

    pub async fn sync_collection(
        &self,
        collection: &mut Collection,
        hkey: &str,
    ) -> Result<SyncResult, Box<dyn std::error::Error>> {
        match self.try_sync_collection(collection, hkey).await {
            Ok(result) => Ok(result),
            Err(e) => self.fail(e),
        }
    }

    async fn try_sync_collection(
        &self,
        collection: &mut Collection,
        hkey: &str,
    ) -> Result<SyncResult, Box<dyn std::error::Error>> {
        let auth = SyncAuth {
            hkey: hkey.to_string(),
            endpoint: None,
            io_timeout_secs: Some(60),
        };

        self.set_status(SyncStatus::CheckingStatus);

        let sync_required = collection.sync_status_offline()?;

        let mut result = SyncResult {
            success: true,
            message: String::new(),
            server_message: None,
        };

        match sync_required {
            sync_status_response::Required::NoChanges => {
                result.message = "No changes to sync".into();
                self.set_status(SyncStatus::Complete {
                    message: result.message.clone(),
                });
            }
            sync_status_response::Required::NormalSync => {
                self.set_status(SyncStatus::SyncingCollection {
                    progress: "Syncing collection...".into(),
                });

                let sync_output: SyncOutput = collection
                    .normal_sync(auth.clone(), self.http_client.clone())
                    .await?;

                result.message = "Collection synced successfully".into();
                result.server_message = Some(sync_output.server_message);

                self.set_status(SyncStatus::Complete {
                    message: result.message.clone(),
                });
            }
            sync_status_response::Required::FullSync => {
                result.success = false;
                result.message = "Full sync required. Please sync via desktop Anki first.".into();
                self.set_status(SyncStatus::Error {
                    message: result.message.clone(),
                });
            }
        }

        Ok(result)
    }

    pub async fn sync_media(
        &self,
        _collection: &mut Collection,
        _hkey: &str,
    ) -> Result<(), Box<dyn std::error::Error>> {
        Err("Media sync not yet implemented - requires progress handler refactoring".into())
    }

    pub async fn full_sync(
        &self,
        _collection: &mut Collection,
        _hkey: &str,
        _upload: bool,
    ) -> Result<SyncResult, Box<dyn std::error::Error>> {
        Err("Full sync not yet implemented - requires Collection ownership transfer".into())
    }
}

impl Default for SyncManager {
    fn default() -> Self {
        Self::new()
    }
}

/// Logs in to AnkiWeb and returns the session key. Blocks the calling thread.
pub fn login_blocking(username: &str, password: &str) -> Result<String, String> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| format!("Cannot start the network: {e}"))?;
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| format!("Cannot start the network: {e}"))?;
    runtime
        .block_on(sync_login(username, password, None, client))
        .map(|auth| auth.hkey)
        .map_err(|e| login_error_message(&e))
}

/// Explains a failed login in plain words.
pub fn login_error_message(error: &AnkiError) -> String {
    match error {
        AnkiError::SyncError { source } if source.kind == SyncErrorKind::AuthFailed => {
            "AnkiWeb rejected the username or password.".into()
        }
        AnkiError::NetworkError { .. } => {
            "Couldn't reach AnkiWeb. Check the Wi-Fi connection.".into()
        }
        other => format!("Login failed: {other}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use anki::error::{NetworkError, NetworkErrorKind, SyncError};

    #[test]
    fn explains_login_errors() {
        let wrong_password = AnkiError::SyncError {
            source: SyncError {
                info: String::new(),
                kind: SyncErrorKind::AuthFailed,
            },
        };
        assert_eq!(
            login_error_message(&wrong_password),
            "AnkiWeb rejected the username or password."
        );

        let offline = AnkiError::NetworkError {
            source: NetworkError {
                info: String::new(),
                kind: NetworkErrorKind::Offline,
            },
        };
        assert!(login_error_message(&offline).starts_with("Couldn't reach AnkiWeb"));
    }
}
