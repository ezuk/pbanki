use std::cell::RefCell;
use std::fs;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::Arc;
use std::time::Instant;

use anki::collection::{Collection, CollectionBuilder};
use anki::error::{OrNotFound, Result as AnkiResult};
use anki::notetype::{Notetype, NotetypeKind};
use anki::ops::Op;
use anki::prelude::I18n;
use anki::scheduler::answering::{CardAnswer, Rating};
use anki::scheduler::states::SchedulingStates;
use anki::timestamp::{TimestampMillis, TimestampSecs};

use crate::{
    CardNode, Config, Constants, DeckNode, DeckTree, LoginState, MainWindow, SyncManager,
    SyncResult, SyncStatus,
};

use slint::{ComponentHandle, ModelRc, SharedString, VecModel};

/// Scale the PocketBook backend renders at; config sizes are in device pixels.
pub const DEVICE_SCALE_FACTOR: f32 = 3.0;

pub struct LearnSession {
    pub collection: Rc<RefCell<Collection>>,
    pub current_card: RefCell<Option<i64>>,
    pub states: RefCell<Option<SchedulingStates>>,
    pub start_time: RefCell<Option<Instant>>,
    pub sync_manager: Rc<SyncManager>,
    /// Folder holding the collection directory and the KOReader inbox.
    pub app_dir: PathBuf,
}

pub fn init_session(config: &Config) -> Result<Rc<LearnSession>, String> {
    let collection_dir = Path::new(&config.general.collection_path);

    fs::create_dir_all(collection_dir).map_err(|e| {
        format!(
            "Cannot create collection directory at {}: {e}",
            collection_dir.display()
        )
    })?;

    let col_file = collection_dir.join("collection.anki2");
    match fs::metadata(&col_file) {
        Ok(meta) => eprintln!("Opening {} ({} bytes)", col_file.display(), meta.len()),
        Err(_) => eprintln!("Creating {}", col_file.display()),
    }
    let mut col = CollectionBuilder::new(&col_file)
        .set_tr(I18n::new(&["en"]))
        .build()
        .map_err(|e| format!("Cannot open collection at {}: {e}", col_file.display()))?;

    match col.search_notes_unordered("") {
        Ok(notes) => eprintln!("Collection has {} notes", notes.len()),
        Err(e) => eprintln!("Cannot count notes: {e}"),
    }

    // Suspend Image Occlusion Enhanced cards
    let search = "note:\"Image Occlusion Enhanced\"";
    if let Ok(card_ids) = col.search_cards(search, anki::search::SortMode::NoOrder) {
        let _ = col.bury_or_suspend_cards(
            &card_ids,
            anki_proto::scheduler::bury_or_suspend_cards_request::Mode::Suspend,
        );
    }

    Ok(Rc::new(LearnSession {
        collection: Rc::new(RefCell::new(col)),
        current_card: RefCell::new(None),
        states: RefCell::new(None),
        start_time: RefCell::new(None),
        sync_manager: Rc::new(SyncManager::new()),
        app_dir: collection_dir
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_default(),
    }))
}

/// Text the settings screen asks the platform keyboard for.
pub struct TextRequest {
    pub title: String,
    pub current: String,
    pub password: bool,
}

/// Opens the platform keyboard; `done` receives the text, or `None` if cancelled.
pub type TextPrompt = Rc<dyn Fn(TextRequest, Box<dyn FnOnce(Option<String>)>)>;

/// Logs in to AnkiWeb with a username and password, returning the session key.
pub type LoginFn = Arc<dyn Fn(&str, &str) -> Result<String, String> + Send + Sync>;

/// Device-specific services the UI needs.
#[derive(Clone)]
pub struct PlatformHooks {
    /// Without one, the settings screen uses its own text fields
    pub text_prompt: Option<TextPrompt>,
    /// Turns on networking before going online
    pub connect_network: Option<Rc<dyn Fn() -> Result<(), String>>>,
    pub login: LoginFn,
}

impl Default for PlatformHooks {
    fn default() -> Self {
        Self {
            text_prompt: None,
            connect_network: None,
            login: Arc::new(crate::sync::login_blocking),
        }
    }
}

/// Opens the collection and connects the UI callbacks, or shows the startup error.
///
pub fn setup_ui(ui: &MainWindow, config: &Config, hooks: PlatformHooks) {
    let constants = ui.global::<Constants>();
    constants.set_min_font_size(*crate::config::FONT_SIZES.start() as i32);
    constants.set_max_font_size(*crate::config::FONT_SIZES.end() as i32);
    constants.set_font_size_px(config.display.font_size() as i32);

    setup_settings(ui, Rc::new(RefCell::new(config.clone())), hooks);

    let session = match init_session(config) {
        Ok(session) => session,
        Err(message) => {
            eprintln!("{message}");
            ui.set_error_message(message.into());
            return;
        }
    };

    let session_for_deck_tree = session.clone();
    let ui_weak = ui.as_weak();
    ui.on_update_deck_tree(move || {
        if let Some(ui) = ui_weak.upgrade() {
            ui.set_deck_tree(update_deck_tree(&session_for_deck_tree));
        }
    });

    let session_for_toggle = session.clone();
    let ui_weak = ui.as_weak();
    ui.on_toggle_deck(move |deck| {
        toggle_deck(&session_for_toggle, &deck);
        if let Some(ui) = ui_weak.upgrade() {
            ui.set_deck_tree(update_deck_tree(&session_for_toggle));
        }
    });

    let session_for_rate = session.clone();
    let ui_weak = ui.as_weak();
    ui.on_rate(move |rating, deck, columns, rows| {
        let grid = Grid::new(columns, rows);
        let next = rate_card(&session_for_rate, rating, deck, grid);
        if let Some(ui) = ui_weak.upgrade() {
            ui.set_current_card(next);
        }
    });

    let session_for_undo = session.clone();
    let ui_weak = ui.as_weak();
    ui.on_undo(move |deck, columns, rows| {
        let Some(card) = undo_rating(&session_for_undo, deck, Grid::new(columns, rows)) else {
            return false;
        };
        if let Some(ui) = ui_weak.upgrade() {
            ui.set_current_card(card);
        }
        true
    });

    let session_for_deck = session.clone();
    let ui_weak = ui.as_weak();
    ui.on_deck_clicked(move |deck, columns, rows| {
        let next = next_card(&session_for_deck, deck, Grid::new(columns, rows));
        if let Some(ui) = ui_weak.upgrade() {
            ui.set_current_card(next);
        }
    });

    ui.set_deck_tree(update_deck_tree(&session));
}

/// Imports cards queued by the KOReader plugin.
pub fn import_inbox(session: &LearnSession) {
    let mut col = session.collection.borrow_mut();
    match crate::inbox::import_inbox(&mut col, &session.app_dir) {
        Ok(summary) if summary.added + summary.failed > 0 => {
            eprintln!(
                "Inbox: imported {} cards, {} failed",
                summary.added, summary.failed
            );
        }
        Ok(_) => {}
        Err(e) => eprintln!("Inbox import failed: {e}"),
    }
}

/// Login as saved, and as last validated on the settings screen.
#[derive(Default)]
struct LoginDraft {
    saved_username: String,
    saved_password: String,
    /// Username, password and session key of the last successful validation
    validated: Option<(String, String, String)>,
}

/// Whether the login on the settings screen can be saved as it is.
pub fn login_state(saved: (&str, &str), current: (&str, &str)) -> LoginState {
    if current == saved {
        LoginState::Unchanged
    } else if current.0.is_empty() && current.1.is_empty() {
        LoginState::Cleared
    } else {
        LoginState::Edited
    }
}

fn login_message(state: LoginState, config: &Config) -> String {
    match state {
        LoginState::Unchanged if config.ankiweb.username.is_empty() => "Sync is off.".into(),
        LoginState::Unchanged if config.ankiweb.token().is_some() => "Logged in.".into(),
        LoginState::Unchanged => "Not validated yet.".into(),
        LoginState::Cleared => "Saving turns sync off.".into(),
        LoginState::Edited => "Validate to save the new login.".into(),
        LoginState::Checking => "Checking with AnkiWeb…".into(),
        LoginState::Passed => "Login works.".into(),
        LoginState::Failed => String::new(),
    }
}

fn setup_settings(ui: &MainWindow, config: Rc<RefCell<Config>>, hooks: PlatformHooks) {
    ui.set_native_keyboard(hooks.text_prompt.is_some());
    let draft = Rc::new(RefCell::new(LoginDraft::default()));

    let refresh_login_state = {
        let config = config.clone();
        let draft = draft.clone();
        move |ui: &MainWindow| {
            let draft = draft.borrow();
            let username = ui.get_settings_username();
            let password = ui.get_settings_password();
            let state = match &draft.validated {
                Some((u, p, _))
                    if u.as_str() == username.as_str() && p.as_str() == password.as_str() =>
                {
                    LoginState::Passed
                }
                _ => login_state(
                    (&draft.saved_username, &draft.saved_password),
                    (&username, &password),
                ),
            };
            ui.set_settings_login_state(state);
            ui.set_settings_login_message(login_message(state, &config.borrow()).into());
        }
    };

    let ui_weak = ui.as_weak();
    let config_for_open = config.clone();
    let draft_for_open = draft.clone();
    let refresh = refresh_login_state.clone();
    ui.on_open_settings(move || {
        let Some(ui) = ui_weak.upgrade() else {
            return;
        };
        let config = config_for_open.borrow();
        *draft_for_open.borrow_mut() = LoginDraft {
            saved_username: config.ankiweb.username.clone(),
            saved_password: config.ankiweb.password.clone(),
            validated: None,
        };
        ui.set_settings_username(config.ankiweb.username.as_str().into());
        ui.set_settings_password(config.ankiweb.password.as_str().into());
        ui.set_settings_font_size(config.display.font_size() as i32);
        drop(config);
        refresh(&ui);
    });

    let ui_weak = ui.as_weak();
    let refresh = refresh_login_state.clone();
    ui.on_credentials_edited(move || {
        if let Some(ui) = ui_weak.upgrade() {
            refresh(&ui);
        }
    });

    let ui_weak = ui.as_weak();
    let refresh = refresh_login_state.clone();
    let text_prompt = hooks.text_prompt.clone();
    ui.on_request_text(move |field| {
        let (Some(ui), Some(prompt)) = (ui_weak.upgrade(), text_prompt.as_ref()) else {
            return;
        };
        let password = field == "password";
        let request = TextRequest {
            title: if password {
                "AnkiWeb password"
            } else {
                "AnkiWeb username"
            }
            .into(),
            current: if password {
                ui.get_settings_password()
            } else {
                ui.get_settings_username()
            }
            .into(),
            password,
        };
        let ui_weak = ui.as_weak();
        let refresh = refresh.clone();
        prompt(
            request,
            Box::new(move |text| {
                let (Some(ui), Some(text)) = (ui_weak.upgrade(), text) else {
                    return;
                };
                if password {
                    ui.set_settings_password(text.into());
                } else {
                    ui.set_settings_username(text.trim().into());
                }
                refresh(&ui);
            }),
        );
    });

    let ui_weak = ui.as_weak();
    let draft_for_validate = draft.clone();
    ui.on_validate_login(move || {
        let Some(ui) = ui_weak.upgrade() else {
            return;
        };
        let username = ui.get_settings_username().trim().to_owned();
        let password = ui.get_settings_password().to_string();

        if let Some(connect) = &hooks.connect_network
            && let Err(e) = connect()
        {
            ui.set_settings_login_state(LoginState::Failed);
            ui.set_settings_login_message(e.into());
            return;
        }

        ui.set_settings_login_state(LoginState::Checking);
        ui.set_settings_login_message("Checking with AnkiWeb…".into());

        let login = hooks.login.clone();
        let ui_weak = ui.as_weak();
        let draft = draft_for_validate.clone();
        crate::background::run_in_background(
            {
                let (username, password) = (username.clone(), password.clone());
                move || login(&username, &password)
            },
            move |result| {
                let Some(ui) = ui_weak.upgrade() else {
                    return;
                };
                // Ignore the result if the fields changed while checking
                if ui.get_settings_username().trim() != username
                    || ui.get_settings_password() != password
                {
                    return;
                }
                match result {
                    Ok(token) => {
                        draft.borrow_mut().validated = Some((username, password, token));
                        ui.set_settings_login_state(LoginState::Passed);
                        ui.set_settings_login_message("Login works.".into());
                    }
                    Err(message) => {
                        ui.set_settings_login_state(LoginState::Failed);
                        ui.set_settings_login_message(message.into());
                    }
                }
            },
        );
    });

    let ui_weak = ui.as_weak();
    ui.on_save_settings(move || {
        let Some(ui) = ui_weak.upgrade() else {
            return;
        };
        let mut config = config.borrow_mut();
        match ui.get_settings_login_state() {
            LoginState::Passed => {
                if let Some((username, password, token)) = draft.borrow_mut().validated.take() {
                    config.set_login(&username, &password, Some(token));
                }
            }
            LoginState::Cleared => config.set_login("", "", None),
            LoginState::Unchanged => {}
            // Save is disabled until the login is validated
            LoginState::Edited | LoginState::Checking | LoginState::Failed => return,
        }
        config.set_font_size(ui.get_settings_font_size().max(0) as u32);
        ui.global::<Constants>()
            .set_font_size_px(config.display.font_size as i32);
        if let Err(e) = config.save() {
            eprintln!("Failed to save settings: {e}");
        }
        ui.set_showing_settings(false);
    });
}

pub fn update_deck_tree(session: &LearnSession) -> DeckTree {
    import_inbox(session);
    match session
        .collection
        .borrow_mut()
        .deck_tree(Some(TimestampSecs::now()))
    {
        Ok(deck_tree) => DeckTree {
            new_total: deck_tree.new_count as i32,
            learn_total: deck_tree.learn_count as i32,
            due_total: deck_tree.review_count as i32,
            deck_nodes: crate::utils::flatten_tree(&deck_tree).into(),
        },
        Err(e) => {
            eprintln!("Failed to load deck tree: {e}");
            DeckTree::default()
        }
    }
}

/// Collapses or expands a deck in the study screen's deck list.
pub fn toggle_deck(session: &LearnSession, deck: &DeckNode) {
    if let Err(e) = session.collection.borrow_mut().set_deck_collapsed(
        anki::decks::DeckId(deck.id),
        !deck.collapsed,
        anki_proto::decks::set_deck_collapsed_request::Scope::Reviewer,
    ) {
        eprintln!("Failed to toggle deck {}: {e}", deck.id);
    }
}

/// Text grid a card page is laid out on.
#[derive(Debug, Clone, Copy)]
pub struct Grid {
    pub columns: usize,
    pub rows: usize,
}

impl Grid {
    pub fn new(columns: i32, rows: i32) -> Self {
        Self {
            columns: columns.max(1) as usize,
            rows: rows.max(1) as usize,
        }
    }
}

/// A card with no id, used when there is nothing to study.
fn message_card(deck: DeckNode, message: impl Into<SharedString>) -> CardNode {
    CardNode {
        id: -1,
        deck,
        message: message.into(),
        ..Default::default()
    }
}

const FINISHED_MESSAGE: &str = "Congratulations!\n\nYou have finished this deck for now.";

pub fn next_card(session: &LearnSession, deck: DeckNode, grid: Grid) -> CardNode {
    match load_next_card(session, deck.clone(), grid) {
        Ok(Some(card)) => card,
        Ok(None) => message_card(
            DeckNode {
                new: 0,
                learn: 0,
                due: 0,
                ..deck
            },
            FINISHED_MESSAGE,
        ),
        Err(e) => {
            eprintln!("Failed to load next card: {e}");
            message_card(deck, format!("Error loading card: {e}"))
        }
    }
}

/// Whether the card asks for a typed answer; those are skipped.
fn is_type_in(notetype: &Notetype, template_idx: u16) -> bool {
    let template = if notetype.config.kind() == NotetypeKind::Cloze {
        notetype.templates.first()
    } else {
        notetype.templates.get(template_idx as usize)
    };
    template.is_some_and(|t| t.config.q_format.contains("{{type:"))
}

fn load_next_card(
    session: &LearnSession,
    deck: DeckNode,
    grid: Grid,
) -> AnkiResult<Option<CardNode>> {
    let mut col = session.collection.borrow_mut();
    col.set_current_deck(anki::decks::DeckId(deck.id))?;
    // Bury type-in cards until one pbAnki can show is at the front
    let (queued_cards, card, note, notetype) = loop {
        let queued_cards = col.get_queued_cards(1, false)?;
        let Some(card) = queued_cards.cards.first().cloned() else {
            return Ok(None);
        };
        let note = col
            .storage
            .get_note(card.card.note_id())?
            .or_not_found(card.card.note_id())?;
        let notetype = col
            .get_notetype(note.notetype_id)?
            .or_not_found(note.notetype_id)?;
        if !is_type_in(&notetype, card.card.template_idx()) {
            break (queued_cards, card, note, notetype);
        }
        col.bury_or_suspend_cards(
            &[card.card.id()],
            anki_proto::scheduler::bury_or_suspend_cards_request::Mode::BuryUser,
        )?;
    };

    *session.start_time.borrow_mut() = Some(Instant::now());
    *session.states.borrow_mut() = Some(card.states.clone());

    let durations = col
        .describe_next_states(&card.states)
        .unwrap_or_else(|_| vec![String::new(); 4]);

    let rendered = col.render_existing_card(card.card.id(), false, false)?;
    let context = crate::inbox::CardContext::from_note(&notetype, &note);
    drop(col);

    let context_pages = context
        .as_ref()
        .map(|c| crate::render::layout(&c.to_html(), grid.columns, grid.rows))
        .unwrap_or_default();

    let answer_pages = crate::render::layout(&rendered.answer(), grid.columns, grid.rows);
    let answer_start = crate::render::first_rule_page(&answer_pages).unwrap_or(0);

    let card_node = CardNode {
        id: card.card.id().0,
        deck: DeckNode {
            new: queued_cards.new_count as i32,
            learn: queued_cards.learning_count as i32,
            due: queued_cards.review_count as i32,
            ..deck
        },
        question: crate::utils::pages_model(crate::render::layout(
            &rendered.question(),
            grid.columns,
            grid.rows,
        )),
        answer: crate::utils::pages_model(answer_pages),
        answer_start: answer_start as i32,
        durations: ModelRc::new(VecModel::from(
            durations
                .into_iter()
                .map(SharedString::from)
                .collect::<Vec<_>>(),
        )),
        context: crate::utils::pages_model(context_pages),
        // Opening the book needs a device test first
        can_open_book: false,
        message: SharedString::new(),
    };

    *session.current_card.borrow_mut() = Some(card_node.id);
    Ok(Some(card_node))
}

/// Undoes the last rating and returns that card again, or `None` if there is
/// no rating to undo.
pub fn undo_rating(session: &LearnSession, deck: DeckNode, grid: Grid) -> Option<CardNode> {
    let undone = {
        let mut col = session.collection.borrow_mut();
        // Type-in cards buried after the rating come off the undo stack first
        while col.undo_status().undo == Some(Op::Bury) {
            if col.undo().is_err() {
                break;
            }
        }
        if col.undo_status().undo == Some(Op::AnswerCard) {
            col.undo()
                .map_err(|e| eprintln!("Failed to undo rating: {e}"))
                .is_ok()
        } else {
            false
        }
    };

    // Reload either way, which also re-buries any type-in card at the front
    let card = next_card(session, deck, grid);
    (undone && card.id != -1).then_some(card)
}

pub fn rate_card(session: &LearnSession, rating: i32, deck: DeckNode, grid: Grid) -> CardNode {
    let card_id = *session.current_card.borrow();
    let states = session.states.borrow().clone();

    let (Some(card_id), Some(states)) = (card_id, states) else {
        return next_card(session, deck, grid);
    };

    let (new_state, rating) = match rating {
        0 => (states.again, Rating::Again),
        1 => (states.hard, Rating::Hard),
        2 => (states.good, Rating::Good),
        3 => (states.easy, Rating::Easy),
        _ => return next_card(session, deck, grid),
    };

    let elapsed = session
        .start_time
        .borrow()
        .map(|t| t.elapsed().as_millis() as u32)
        .unwrap_or(0);

    let mut answer = CardAnswer {
        card_id: anki::card::CardId(card_id),
        current_state: states.current,
        new_state,
        rating,
        answered_at: TimestampMillis::now(),
        milliseconds_taken: elapsed,
        custom_data: None,
        from_queue: true,
    };

    if let Err(e) = session.collection.borrow_mut().answer_card(&mut answer) {
        eprintln!("Failed to answer card {card_id}: {e}");
    }
    *session.current_card.borrow_mut() = None;
    *session.states.borrow_mut() = None;

    next_card(session, deck, grid)
}

#[expect(
    clippy::await_holding_refcell_ref,
    reason = "block_on drives the future on this thread"
)]
pub fn sync_ankiweb(session: &LearnSession, config: &Config) -> SyncResult {
    let failure = |message: String| SyncResult {
        success: false,
        message,
        server_message: None,
    };

    let rt = match tokio::runtime::Runtime::new() {
        Ok(rt) => rt,
        Err(e) => return failure(format!("Cannot start sync runtime: {e}")),
    };

    let ankiweb_config = &config.ankiweb;

    let hkey = match ankiweb_config.token() {
        Some(token) => token.to_owned(),
        None => {
            let login = session
                .sync_manager
                .login(&ankiweb_config.username, &ankiweb_config.password);
            match rt.block_on(login) {
                Ok(token) => {
                    let mut cfg = config.clone();
                    cfg.ankiweb.token = Some(token.clone());
                    if let Err(e) = cfg.save() {
                        eprintln!("Failed to save sync token: {e}");
                    }
                    token
                }
                Err(e) => return failure(format!("Login failed: {e}")),
            }
        }
    };

    let result = rt.block_on(async {
        let mut col = session.collection.borrow_mut();
        session.sync_manager.sync_collection(&mut col, &hkey).await
    });

    result.unwrap_or_else(|e| failure(format!("Sync failed: {e}")))
}

pub fn get_sync_status(session: &LearnSession) -> SyncStatus {
    session.sync_manager.get_status()
}

#[cfg(test)]
mod tests {
    use super::*;
    use anki::decks::DeckId;
    use slint::Model;

    fn session_in(dir: &Path) -> Rc<LearnSession> {
        let mut config = Config::default();
        config.general.collection_path = dir.join("collection").to_string_lossy().into();
        init_session(&config).unwrap()
    }

    fn add_basic_note(session: &LearnSession, front: &str, back: &str) {
        let mut col = session.collection.borrow_mut();
        let notetype = col.get_notetype_by_name("Basic").unwrap().unwrap();
        let mut note = notetype.new_note();
        note.set_field(0, front).unwrap();
        note.set_field(1, back).unwrap();
        col.add_note(&mut note, DeckId(1)).unwrap();
    }

    fn default_deck() -> DeckNode {
        DeckNode {
            id: 1,
            ..Default::default()
        }
    }

    const GRID: Grid = Grid {
        columns: 40,
        rows: 16,
    };

    /// Text of every line on every page; rules show as "---".
    fn texts(pages: &ModelRc<crate::Page>) -> Vec<String> {
        pages
            .iter()
            .flat_map(|page| page.lines.iter().collect::<Vec<_>>())
            .map(|line| {
                if line.rule {
                    "---".to_string()
                } else {
                    line.spans.iter().map(|s| s.text.to_string()).collect()
                }
            })
            .collect()
    }

    fn add_note(session: &LearnSession, notetype: &str, fields: &[&str]) {
        let mut col = session.collection.borrow_mut();
        let notetype = col.get_notetype_by_name(notetype).unwrap().unwrap();
        let mut note = notetype.new_note();
        for (i, field) in fields.iter().enumerate() {
            note.set_field(i, *field).unwrap();
        }
        col.add_note(&mut note, DeckId(1)).unwrap();
    }

    #[test]
    fn login_state_decides_whether_save_is_allowed() {
        let saved = ("me@example.com", "secret");
        assert_eq!(login_state(saved, saved), LoginState::Unchanged);
        assert_eq!(login_state(saved, ("", "")), LoginState::Cleared);
        assert_eq!(
            login_state(saved, ("me@example.com", "other")),
            LoginState::Edited
        );
        assert_eq!(login_state(("", ""), ("", "")), LoginState::Unchanged);
    }

    #[test]
    fn init_fails_when_collection_path_is_a_file() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("not_a_dir");
        fs::write(&file, "").unwrap();

        let mut config = Config::default();
        config.general.collection_path = file.to_string_lossy().into();

        let err = init_session(&config).err().unwrap();
        assert!(err.contains("Cannot create collection directory"), "{err}");
    }

    #[test]
    fn empty_collection_has_no_cards_due() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());

        let card = next_card(&session, default_deck(), GRID);
        assert_eq!(card.id, -1);
        assert_eq!(card.message, FINISHED_MESSAGE);
    }

    #[test]
    fn studies_and_rates_a_new_card() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        add_basic_note(&session, "<b>Front</b> side", "Back side");

        let tree = update_deck_tree(&session);
        assert_eq!(tree.deck_nodes.row_count(), 1);
        assert_eq!(tree.deck_nodes.row_data(0).unwrap().new, 1);

        let card = next_card(&session, default_deck(), GRID);
        assert_ne!(card.id, -1);
        assert_eq!(texts(&card.question), vec!["Front side"]);
        assert_eq!(texts(&card.answer), vec!["Front side", "---", "Back side"]);
        assert_eq!(card.answer_start, 0);
        assert_eq!(card.durations.row_count(), 4);
        assert_eq!(card.deck.new, 1);

        // "Easy" graduates the card, so nothing else is due
        let next = rate_card(&session, 3, default_deck(), GRID);
        assert_eq!(next.id, -1);
        assert_eq!(
            update_deck_tree(&session)
                .deck_nodes
                .row_data(0)
                .unwrap()
                .new,
            0
        );
    }

    #[test]
    fn rating_without_a_current_card_is_harmless() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());

        let card = rate_card(&session, 2, default_deck(), GRID);
        assert_eq!(card.id, -1);
    }

    #[test]
    fn invalid_rating_keeps_the_card() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        add_basic_note(&session, "Q", "A");
        let card = next_card(&session, default_deck(), GRID);

        assert_eq!(rate_card(&session, 7, default_deck(), GRID).id, card.id);
    }

    #[test]
    fn undo_brings_back_the_rated_card() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        add_basic_note(&session, "First", "1");
        add_basic_note(&session, "Second", "2");

        let first = next_card(&session, default_deck(), GRID);
        let second = rate_card(&session, 2, default_deck(), GRID);
        assert_ne!(second.id, first.id);

        let undone = undo_rating(&session, default_deck(), GRID).unwrap();
        assert_eq!(undone.id, first.id);
        assert_eq!(undone.deck.new, 2);

        // The restored card can be rated again
        let next = rate_card(&session, 3, default_deck(), GRID);
        assert_eq!(next.id, second.id);
    }

    #[test]
    fn undo_steps_over_buried_type_in_cards() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        add_note(&session, "Basic", &["First", "1"]);
        add_note(&session, "Basic (type in the answer)", &["2 + 2 = ?", "4"]);
        add_note(&session, "Basic", &["Third", "3"]);

        let first = next_card(&session, default_deck(), GRID);
        let third = rate_card(&session, 3, default_deck(), GRID);
        assert_eq!(texts(&third.question), vec!["Third"]);

        let undone = undo_rating(&session, default_deck(), GRID).unwrap();
        assert_eq!(undone.id, first.id);
        assert_eq!(rate_card(&session, 3, default_deck(), GRID).id, third.id);
    }

    #[test]
    fn undo_without_a_rating_does_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        add_basic_note(&session, "Only", "1");
        let card = next_card(&session, default_deck(), GRID);

        assert!(undo_rating(&session, default_deck(), GRID).is_none());
        assert_eq!(rate_card(&session, 3, default_deck(), GRID).id, -1);
        assert!(card.id != -1);
    }

    #[test]
    fn skips_type_in_cards() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        add_note(&session, "Basic (type in the answer)", &["2 + 2 = ?", "4"]);
        add_note(&session, "Basic", &["Q", "A"]);

        let card = next_card(&session, default_deck(), GRID);
        assert_eq!(texts(&card.question), vec!["Q"]);
        assert_eq!(card.deck.new, 1, "buried type-in card is not counted");
        assert_eq!(rate_card(&session, 3, default_deck(), GRID).id, -1);
    }

    #[test]
    fn cloze_hides_then_reveals_in_bold() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        add_note(&session, "Cloze", &["The {{c1::Seine}} flows", ""]);

        let card = next_card(&session, default_deck(), GRID);
        assert_eq!(texts(&card.question), vec!["The [...] flows"]);
        let answer = card.answer.row_data(0).unwrap();
        let line = answer.lines.row_data(0).unwrap();
        assert!(line.spans.iter().any(|s| s.text == "Seine" && s.bold));
    }

    #[test]
    fn answer_starts_on_page_with_the_back() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        let long_front = vec!["line"; 20].join("<br>");
        add_basic_note(&session, &long_front, "Back");

        let card = next_card(&session, default_deck(), GRID);
        assert_eq!(card.question.row_count(), 2);
        assert_eq!(card.answer_start, 1);
    }

    #[test]
    fn collapsing_a_deck_hides_its_children() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        session
            .collection
            .borrow_mut()
            .get_or_create_normal_deck("Parent::Child")
            .unwrap();

        let rows = |session: &LearnSession| update_deck_tree(session).deck_nodes.row_count();
        let parent = |session: &LearnSession| {
            update_deck_tree(session)
                .deck_nodes
                .iter()
                .find(|d| d.name == "Parent")
                .unwrap()
        };

        // New decks start collapsed
        assert!(parent(&session).collapsed);
        let collapsed_rows = rows(&session);

        toggle_deck(&session, &parent(&session));
        assert!(!parent(&session).collapsed);
        assert_eq!(rows(&session), collapsed_rows + 1);

        toggle_deck(&session, &parent(&session));
        assert_eq!(rows(&session), collapsed_rows);
    }

    #[test]
    fn suspends_image_occlusion_enhanced_cards_on_open() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        {
            let mut col = session.collection.borrow_mut();
            let mut notetype = (*col.get_notetype_by_name("Basic").unwrap().unwrap()).clone();
            notetype.id = Default::default();
            notetype.name = "Image Occlusion Enhanced".into();
            col.add_notetype(&mut notetype, false).unwrap();
            let mut note = notetype.new_note();
            note.set_field(0, "IO").unwrap();
            col.add_note(&mut note, DeckId(1)).unwrap();
        }
        drop(session);

        let session = session_in(dir.path());
        assert_eq!(next_card(&session, default_deck(), GRID).id, -1);
    }
}
