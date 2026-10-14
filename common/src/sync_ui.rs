//! The Sync button and the status bar on the deck list.

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use slint::ComponentHandle;

use crate::api::{LearnSession, PlatformHooks, import_inbox, update_deck_tree};
use crate::{Config, MainWindow, SyncLogin, SyncMode, SyncOutcome, SyncReport, SyncStep};

/// Lets e-ink draw the status before the UI thread blocks on Wi-Fi.
const PAINT_DELAY: Duration = Duration::from_millis(300);
const STEP_POLL: Duration = Duration::from_millis(250);

pub fn step_message(step: SyncStep) -> &'static str {
    match step {
        SyncStep::LoggingIn => "Logging in to AnkiWeb…",
        SyncStep::Syncing => "Syncing with AnkiWeb…",
        SyncStep::BackingUp => "Backing up this reader's collection…",
        SyncStep::Downloading => "Downloading from AnkiWeb…",
        SyncStep::Uploading => "Uploading to AnkiWeb…",
    }
}

pub fn outcome_message(report: &SyncReport) -> String {
    let message = match &report.outcome {
        SyncOutcome::Synced => "✓ Synced with AnkiWeb.".to_string(),
        SyncOutcome::Downloaded => "✓ Downloaded AnkiWeb's collection.".to_string(),
        SyncOutcome::Uploaded => "✓ Uploaded this collection to AnkiWeb.".to_string(),
        SyncOutcome::FullSyncNeeded { .. } => {
            "AnkiWeb and this reader have changes that can't be merged. Choose which collection to keep.".to_string()
        }
        SyncOutcome::Failed(message) => format!("✗ {message}"),
    };
    if report.server_message.is_empty() {
        message
    } else {
        format!("{message}\nAnkiWeb: {}", report.server_message)
    }
}

/// Connects the Sync button and the full sync choice, and syncs on startup
/// when that is turned on.
pub fn setup_sync(
    ui: &MainWindow,
    session: &Rc<LearnSession>,
    config: &Rc<RefCell<Config>>,
    hooks: &PlatformHooks,
) {
    let start = {
        let ui_weak = ui.as_weak();
        let session = session.clone();
        let config = config.clone();
        let hooks = hooks.clone();
        Rc::new(move |mode: SyncMode| {
            if let Some(ui) = ui_weak.upgrade() {
                start_sync(&ui, &session, &config, &hooks, mode);
            }
        })
    };

    let start_normal = start.clone();
    ui.on_sync(move || start_normal(SyncMode::Normal));
    ui.on_full_sync(move |upload| {
        start(if upload {
            SyncMode::Upload
        } else {
            SyncMode::Download
        })
    });

    let ui_weak = ui.as_weak();
    ui.on_dismiss_sync(move || {
        if let Some(ui) = ui_weak.upgrade()
            && !ui.get_syncing()
        {
            show(&ui, "", false);
        }
    });

    if config.borrow().ankiweb.auto_sync && config.borrow().ankiweb.sync_login().is_some() {
        let ui_weak = ui.as_weak();
        slint::Timer::single_shot(PAINT_DELAY, move || {
            if let Some(ui) = ui_weak.upgrade() {
                ui.invoke_sync();
            }
        });
    }
}

fn show(ui: &MainWindow, message: &str, failed: bool) {
    ui.set_sync_message(message.into());
    ui.set_sync_failed(failed);
    ui.set_sync_choice(false);
    ui.set_sync_confirm("".into());
}

fn start_sync(
    ui: &MainWindow,
    session: &Rc<LearnSession>,
    config: &Rc<RefCell<Config>>,
    hooks: &PlatformHooks,
    mode: SyncMode,
) {
    if ui.get_syncing() {
        return;
    }
    let Some(login) = config.borrow().ankiweb.sync_login() else {
        show(ui, "✗ Log in to AnkiWeb in Settings to sync.", true);
        return;
    };
    // KOReader cards go up with this sync
    import_inbox(session);

    ui.set_syncing(true);
    let run = {
        let ui_weak = ui.as_weak();
        let session = session.clone();
        let config = config.clone();
        let hooks = hooks.clone();
        move || {
            if let Some(ui) = ui_weak.upgrade() {
                run_sync(&ui, &session, &config, &hooks, login, mode);
            }
        }
    };

    let Some(connect) = hooks.connect_network.clone() else {
        show(ui, step_message(SyncStep::Syncing), false);
        run();
        return;
    };
    show(ui, "Turning on Wi-Fi…", false);
    let ui_weak = ui.as_weak();
    slint::Timer::single_shot(PAINT_DELAY, move || match connect() {
        Ok(()) => run(),
        Err(message) => {
            if let Some(ui) = ui_weak.upgrade() {
                ui.set_syncing(false);
                show(&ui, &format!("✗ {message}"), true);
            }
        }
    });
}

fn run_sync(
    ui: &MainWindow,
    session: &Rc<LearnSession>,
    config: &Rc<RefCell<Config>>,
    hooks: &PlatformHooks,
    login: SyncLogin,
    mode: SyncMode,
) {
    let first_step = match mode {
        SyncMode::Normal if login.token.is_none() => SyncStep::LoggingIn,
        SyncMode::Normal => SyncStep::Syncing,
        SyncMode::Download => SyncStep::BackingUp,
        SyncMode::Upload => SyncStep::Uploading,
    };
    show(ui, step_message(first_step), false);

    // Shows each step as the sync thread reaches it
    let step = Arc::new(Mutex::new(None::<SyncStep>));
    let ticker = Rc::new(slint::Timer::default());
    {
        let step = step.clone();
        let ui_weak = ui.as_weak();
        ticker.start(slint::TimerMode::Repeated, STEP_POLL, move || {
            if let (Some(ui), Some(step)) = (ui_weak.upgrade(), step.lock().unwrap().take()) {
                ui.set_sync_message(step_message(step).into());
            }
        });
    }

    let sync = hooks.sync.clone();
    let collection = session.collection.clone();
    let ui_weak = ui.as_weak();
    let session = session.clone();
    let config = config.clone();
    crate::background::run_in_background(
        move || {
            sync(&collection, &login, mode, &|s| {
                *step.lock().unwrap() = Some(s);
            })
        },
        move |report| {
            ticker.stop();
            save_login_changes(&config, &report);
            let Some(ui) = ui_weak.upgrade() else {
                return;
            };
            ui.set_syncing(false);
            let failed = matches!(report.outcome, SyncOutcome::Failed(_));
            show(&ui, &outcome_message(&report), failed);
            if let SyncOutcome::FullSyncNeeded {
                upload_ok,
                download_ok,
            } = report.outcome
            {
                ui.set_sync_choice(true);
                ui.set_sync_upload_ok(upload_ok);
                ui.set_sync_download_ok(download_ok);
            }
            ui.set_deck_tree(update_deck_tree(&session));
        },
    );
}

fn save_login_changes(config: &Rc<RefCell<Config>>, report: &SyncReport) {
    if report.new_token.is_none() && report.new_endpoint.is_none() && !report.token_rejected {
        return;
    }
    let mut config = config.borrow_mut();
    if report.token_rejected {
        config.ankiweb.token = None;
    }
    if let Some(token) = &report.new_token {
        config.ankiweb.token = Some(token.clone());
    }
    if let Some(endpoint) = &report.new_endpoint {
        config.ankiweb.endpoint = Some(endpoint.clone());
    }
    if let Err(e) = config.save() {
        eprintln!("Failed to save the AnkiWeb login: {e}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn messages_explain_the_outcome() {
        let mut report = SyncReport::new(SyncOutcome::Synced);
        assert_eq!(outcome_message(&report), "✓ Synced with AnkiWeb.");

        report.server_message = "Maintenance tonight".into();
        assert!(outcome_message(&report).ends_with("\nAnkiWeb: Maintenance tonight"));

        let failed = SyncReport::new(SyncOutcome::Failed("No network.".into()));
        assert_eq!(outcome_message(&failed), "✗ No network.");
    }

    #[test]
    fn rejected_token_is_dropped_and_new_one_saved() {
        let dir = tempfile::tempdir().unwrap();
        let config = Rc::new(RefCell::new(Config {
            path: Some(dir.path().join("config.toml")),
            ..Default::default()
        }));
        config.borrow_mut().ankiweb.token = Some("old".into());

        let mut report = SyncReport::new(SyncOutcome::Failed("rejected".into()));
        report.token_rejected = true;
        save_login_changes(&config, &report);
        assert_eq!(config.borrow().ankiweb.token, None);

        let mut report = SyncReport::new(SyncOutcome::Synced);
        report.new_token = Some("fresh".into());
        report.new_endpoint = Some("https://sync9.ankiweb.net/".into());
        save_login_changes(&config, &report);
        assert_eq!(config.borrow().ankiweb.token.as_deref(), Some("fresh"));
        assert_eq!(
            config.borrow().ankiweb.endpoint.as_deref(),
            Some("https://sync9.ankiweb.net/")
        );
    }
}
