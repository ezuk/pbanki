//! AnkiWeb login and collection sync.

use std::sync::Arc;

use anki::error::{AnkiError, SyncErrorKind};
use anki::sync::collection::normal::SyncActionRequired;
use anki::sync::login::{SyncAuth, sync_login};

use crate::collection::SharedCollection;

/// Saved AnkiWeb login.
#[derive(Debug, Clone, Default)]
pub struct SyncLogin {
    pub username: String,
    pub password: String,
    pub token: Option<String>,
    /// Sync server AnkiWeb assigned to this account
    pub endpoint: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SyncMode {
    /// Merge changes both ways
    Normal,
    /// Replace this collection with AnkiWeb's
    Download,
    /// Replace AnkiWeb's collection with this one
    Upload,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SyncStep {
    LoggingIn,
    Syncing,
    BackingUp,
    Downloading,
    Uploading,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SyncOutcome {
    Synced,
    Downloaded,
    Uploaded,
    /// The two collections can't be merged; one has to replace the other
    FullSyncNeeded {
        upload_ok: bool,
        download_ok: bool,
    },
    Failed(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SyncReport {
    pub outcome: SyncOutcome,
    /// Token from a fresh login, to save
    pub new_token: Option<String>,
    /// Sync server to use from now on
    pub new_endpoint: Option<String>,
    /// The saved token was rejected and should be dropped
    pub token_rejected: bool,
    /// Announcement from AnkiWeb
    pub server_message: String,
}

impl SyncReport {
    pub fn new(outcome: SyncOutcome) -> Self {
        Self {
            outcome,
            new_token: None,
            new_endpoint: None,
            token_rejected: false,
            server_message: String::new(),
        }
    }
}

/// Syncs the collection, reporting each step. Runs on a background thread.
pub type SyncFn = Arc<
    dyn Fn(&SharedCollection, &SyncLogin, SyncMode, &(dyn Fn(SyncStep) + Sync)) -> SyncReport
        + Send
        + Sync,
>;

fn http_client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .connect_timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| format!("Cannot start the network: {e}"))
}

fn runtime() -> Result<tokio::runtime::Runtime, String> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| format!("Cannot start the network: {e}"))
}

/// Syncs with AnkiWeb. Blocks the calling thread.
pub fn sync_blocking(
    col: &SharedCollection,
    login: &SyncLogin,
    mode: SyncMode,
    progress: &(dyn Fn(SyncStep) + Sync),
) -> SyncReport {
    let failed = |message: String| SyncReport::new(SyncOutcome::Failed(message));
    let (rt, client) = match (runtime(), http_client()) {
        (Ok(rt), Ok(client)) => (rt, client),
        (Err(e), _) | (_, Err(e)) => return failed(e),
    };

    let mut report = SyncReport::new(SyncOutcome::Synced);
    let hkey = match &login.token {
        Some(token) => token.clone(),
        None => {
            progress(SyncStep::LoggingIn);
            let login_future = sync_login(
                &login.username,
                &login.password,
                login.endpoint.clone(),
                client.clone(),
            );
            match rt.block_on(login_future) {
                Ok(auth) => {
                    report.new_token = Some(auth.hkey.clone());
                    auth.hkey
                }
                Err(e) => return failed(login_error_message(&e)),
            }
        }
    };
    let auth = SyncAuth {
        hkey,
        endpoint: login
            .endpoint
            .as_deref()
            .and_then(|e| reqwest::Url::parse(e).ok()),
        io_timeout_secs: Some(60),
    };

    let result = match mode {
        SyncMode::Normal => {
            progress(SyncStep::Syncing);
            let output = rt.block_on(col.lock().normal_sync(auth, client));
            output.map(|output| {
                report.new_endpoint = output.new_endpoint;
                report.server_message = output.server_message;
                match output.required {
                    SyncActionRequired::FullSyncRequired {
                        upload_ok,
                        download_ok,
                    } => SyncOutcome::FullSyncNeeded {
                        upload_ok,
                        download_ok,
                    },
                    _ => SyncOutcome::Synced,
                }
            })
        }
        SyncMode::Download => {
            progress(SyncStep::BackingUp);
            if let Err(message) = back_up(col) {
                return failed(message);
            }
            progress(SyncStep::Downloading);
            replace(col, |c| rt.block_on(c.full_download(auth, client)))
                .map(|()| SyncOutcome::Downloaded)
        }
        SyncMode::Upload => {
            progress(SyncStep::Uploading);
            replace(col, |c| rt.block_on(c.full_upload(auth, client)))
                .map(|()| SyncOutcome::Uploaded)
        }
    };

    match result {
        Ok(outcome) => report.outcome = outcome,
        Err(e) => {
            report.token_rejected = is_auth_failure(&e) && login.token.is_some();
            report.outcome = SyncOutcome::Failed(sync_error_message(&e));
        }
    }
    report
}

/// Runs a full sync on the closed collection, then opens it again.
fn replace(
    col: &SharedCollection,
    full_sync: impl FnOnce(anki::collection::Collection) -> Result<(), AnkiError>,
) -> Result<(), AnkiError> {
    col.replace(full_sync).and_then(|result| result)
}

/// Saves the collection to `backups/before-download-<time>.colpkg` next to it,
/// a file desktop Anki can import.
fn back_up(col: &SharedCollection) -> Result<(), String> {
    let folder = col
        .path()
        .parent()
        .map(|dir| dir.join("backups"))
        .ok_or("Cannot find the collection folder.")?;
    std::fs::create_dir_all(&folder)
        .map_err(|e| format!("Cannot create the backup folder: {e}"))?;
    let file = folder.join(format!(
        "before-download-{}.colpkg",
        anki::timestamp::TimestampSecs::now().0
    ));
    col.replace(|c| c.export_colpkg(&file, false, false))
        .and_then(|result| result)
        .map_err(|e| format!("Backup failed, nothing was downloaded: {e}"))
}

/// Logs in to AnkiWeb and returns the session key. Blocks the calling thread.
pub fn login_blocking(username: &str, password: &str) -> Result<String, String> {
    let (runtime, client) = (runtime()?, http_client()?);
    runtime
        .block_on(sync_login(username, password, None, client))
        .map(|auth| auth.hkey)
        .map_err(|e| login_error_message(&e))
}

fn is_auth_failure(error: &AnkiError) -> bool {
    matches!(error, AnkiError::SyncError { source } if source.kind == SyncErrorKind::AuthFailed)
}

/// Explains a failed login in plain words.
pub fn login_error_message(error: &AnkiError) -> String {
    match error {
        _ if is_auth_failure(error) => "AnkiWeb rejected the username or password.".into(),
        AnkiError::NetworkError { .. } => {
            "Couldn't reach AnkiWeb. Check the Wi-Fi connection.".into()
        }
        other => format!("Login failed: {other}"),
    }
}

/// Explains a failed sync in plain words.
pub fn sync_error_message(error: &AnkiError) -> String {
    match error {
        _ if is_auth_failure(error) => {
            "AnkiWeb rejected the saved login. Sync again to log in with the saved password, or check it in Settings.".into()
        }
        AnkiError::NetworkError { .. } => {
            "Couldn't reach AnkiWeb. Check the Wi-Fi connection.".into()
        }
        AnkiError::SyncError { source } => match source.kind {
            SyncErrorKind::ClockIncorrect => {
                "The reader's clock is wrong. Set the date and time, then sync again.".into()
            }
            SyncErrorKind::UploadTooLarge => "The collection is too big for AnkiWeb.".into(),
            SyncErrorKind::ServerMessage => source.info.clone(),
            _ => format!("Sync failed: {error}"),
        },
        other => format!("Sync failed: {other}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use anki::error::{NetworkError, NetworkErrorKind, SyncError};

    fn sync_error(kind: SyncErrorKind) -> AnkiError {
        AnkiError::SyncError {
            source: SyncError {
                info: "from server".into(),
                kind,
            },
        }
    }

    #[test]
    fn explains_login_errors() {
        assert_eq!(
            login_error_message(&sync_error(SyncErrorKind::AuthFailed)),
            "AnkiWeb rejected the username or password."
        );

        let offline = AnkiError::NetworkError {
            source: NetworkError {
                info: String::new(),
                kind: NetworkErrorKind::Offline,
            },
        };
        assert!(login_error_message(&offline).starts_with("Couldn't reach AnkiWeb"));
        assert!(sync_error_message(&offline).starts_with("Couldn't reach AnkiWeb"));
    }

    #[test]
    fn explains_sync_errors() {
        assert!(
            sync_error_message(&sync_error(SyncErrorKind::AuthFailed))
                .starts_with("AnkiWeb rejected the saved login")
        );
        assert!(sync_error_message(&sync_error(SyncErrorKind::ClockIncorrect)).contains("clock"));
        assert_eq!(
            sync_error_message(&sync_error(SyncErrorKind::ServerMessage)),
            "from server"
        );
    }

    #[test]
    fn download_backs_up_before_replacing() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("collection.anki2");
        let col = SharedCollection::new(crate::collection::open_collection(&path).unwrap(), path);
        back_up(&col).unwrap();
        let backups: Vec<_> = std::fs::read_dir(dir.path().join("backups"))
            .unwrap()
            .collect();
        assert_eq!(backups.len(), 1);
    }
}
