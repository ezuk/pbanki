use std::cell::RefCell;
use std::fs;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::Arc;
use std::time::Instant;

use anki::card::{Card, CardQueueNumber};
use anki::collection::Collection;
use anki::error::{OrNotFound, Result as AnkiResult};
use anki::notes::Note;
use anki::notetype::{Notetype, NotetypeKind};
use anki::ops::Op;
use anki::scheduler::answering::{CardAnswer, Rating};
use anki::scheduler::states::SchedulingStates;
use anki::timestamp::{TimestampMillis, TimestampSecs};
use anki_proto::decks::DeckTreeNode;

use crate::collection::{SharedCollection, open_collection};
use crate::limits::{DailyLimits, remaining_today};
use crate::{CardNode, Config, Constants, DeckNode, DeckTree, LoginState, MainWindow};

use slint::{ComponentHandle, ModelRc, SharedString, VecModel};

/// Scale the PocketBook backend renders at; config sizes are in device pixels.
pub const DEVICE_SCALE_FACTOR: f32 = 3.0;

pub struct LearnSession {
    pub collection: SharedCollection,
    pub current_card: RefCell<Option<i64>>,
    pub states: RefCell<Option<SchedulingStates>>,
    pub start_time: RefCell<Option<Instant>>,
    /// Suspensions made from the learn screen that undo may reverse
    pub user_suspensions: RefCell<u32>,
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
    let mut col = open_collection(&col_file)
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
        collection: SharedCollection::new(col, col_file),
        current_card: RefCell::new(None),
        states: RefCell::new(None),
        start_time: RefCell::new(None),
        user_suspensions: RefCell::new(0),
        app_dir: collection_dir
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_default(),
    }))
}

/// Text the settings and edit screens ask the platform keyboard for.
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
    pub sync: crate::SyncFn,
    /// Set when the app comes back to the foreground
    pub reopened: Option<Arc<std::sync::atomic::AtomicBool>>,
}

impl Default for PlatformHooks {
    fn default() -> Self {
        Self {
            text_prompt: None,
            connect_network: None,
            login: Arc::new(crate::sync::login_blocking),
            sync: Arc::new(crate::sync::sync_blocking),
            reopened: None,
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

    let shared_config = Rc::new(RefCell::new(config.clone()));
    let session = init_session(config);
    setup_settings(
        ui,
        shared_config.clone(),
        hooks.clone(),
        session.as_ref().ok().cloned(),
    );

    let session = match session {
        Ok(session) => session,
        Err(message) => {
            eprintln!("{message}");
            ui.set_error_message(message.into());
            return;
        }
    };

    crate::sync_ui::setup_sync(ui, &session, &shared_config, &hooks);
    crate::edit::setup_edit(ui, &session, &hooks);

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

    let session_for_suspend = session.clone();
    let ui_weak = ui.as_weak();
    ui.on_suspend(move |deck, columns, rows| {
        let next = suspend_card(&session_for_suspend, deck, Grid::new(columns, rows));
        if let Some(ui) = ui_weak.upgrade() {
            ui.set_current_card(next);
        }
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
    let mut col = session.collection.lock();
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

fn setup_settings(
    ui: &MainWindow,
    config: Rc<RefCell<Config>>,
    hooks: PlatformHooks,
    session: Option<Rc<LearnSession>>,
) {
    ui.set_native_keyboard(hooks.text_prompt.is_some());
    let draft = Rc::new(RefCell::new(LoginDraft::default()));
    // Limits as the settings screen opened with them
    let opened_limits = Rc::new(RefCell::new(None::<DailyLimits>));

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
    let session_for_open = session.clone();
    let limits_for_open = opened_limits.clone();
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
        ui.set_settings_sync_on_startup(config.ankiweb.auto_sync);
        drop(config);
        let limits = session_for_open.as_ref().and_then(|session| {
            crate::limits::daily_limits(&mut session.collection.lock())
                .map_err(|e| eprintln!("Failed to read daily limits: {e}"))
                .ok()
        });
        ui.set_settings_limits_available(limits.is_some());
        if let Some(limits) = limits {
            ui.set_settings_new_per_day(limits.new as i32);
            ui.set_settings_reviews_per_day(limits.reviews as i32);
        }
        *limits_for_open.borrow_mut() = limits;
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
        config.ankiweb.auto_sync = ui.get_settings_sync_on_startup();
        ui.global::<Constants>()
            .set_font_size_px(config.display.font_size as i32);
        if let Err(e) = config.save() {
            eprintln!("Failed to save settings: {e}");
        }
        let limits = DailyLimits {
            new: ui.get_settings_new_per_day().max(0) as u32,
            reviews: ui.get_settings_reviews_per_day().max(0) as u32,
        };
        if let Some(session) = &session
            && opened_limits
                .borrow()
                .is_some_and(|opened| opened != limits)
        {
            let saved = crate::limits::set_daily_limits(&mut session.collection.lock(), limits);
            match saved {
                Ok(()) => ui.set_deck_tree(update_deck_tree(session)),
                Err(e) => eprintln!("Failed to save daily limits: {e}"),
            }
        }
        ui.set_showing_settings(false);
    });
}

pub fn update_deck_tree(session: &LearnSession) -> DeckTree {
    import_inbox(session);
    let mut col = session.collection.lock();
    let tree = col.deck_tree(Some(TimestampSecs::now())).and_then(|tree| {
        let remaining = remaining_today(&mut col, &top_level_decks(&tree))?;
        Ok((tree, remaining))
    });
    drop(col);
    match tree {
        Ok((deck_tree, remaining)) => {
            let deck_nodes = crate::utils::flatten_tree(&deck_tree);
            deck_nodes.insert(0, everything_deck(&deck_tree, remaining));
            DeckTree {
                new_total: deck_tree.new_count as i32,
                learn_total: deck_tree.learn_count as i32,
                due_total: deck_tree.review_count as i32,
                deck_nodes: deck_nodes.into(),
            }
        }
        Err(e) => {
            eprintln!("Failed to load deck tree: {e}");
            DeckTree::default()
        }
    }
}

/// Id of the "Everything" row, which studies every top-level deck in turn.
pub const EVERYTHING_ID: i64 = 0;

fn everything_deck(deck_tree: &DeckTreeNode, remaining: DailyLimits) -> DeckNode {
    DeckNode {
        id: EVERYTHING_ID,
        name: "Everything".into(),
        level: 1,
        new: deck_tree.new_count.min(remaining.new) as i32,
        learn: deck_tree.learn_count as i32,
        due: deck_tree.review_count.min(remaining.reviews) as i32,
        ..Default::default()
    }
}

fn top_level_decks(deck_tree: &DeckTreeNode) -> Vec<anki::decks::DeckId> {
    deck_tree
        .children
        .iter()
        .map(|deck| anki::decks::DeckId(deck.deck_id))
        .collect()
}

/// Collapses or expands a deck in the study screen's deck list.
pub fn toggle_deck(session: &LearnSession, deck: &DeckNode) {
    if let Err(e) = session.collection.lock().set_deck_collapsed(
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

/// The card at the front of the study queue.
struct NextCard {
    card: Card,
    states: SchedulingStates,
    note: Note,
    notetype: Arc<Notetype>,
    new: usize,
    learn: usize,
    due: usize,
}

/// The next card of a deck and its children. With `cap`, new cards and
/// reviews past it are buried until tomorrow.
fn queued_card(
    col: &mut Collection,
    deck_id: i64,
    cap: Option<DailyLimits>,
) -> AnkiResult<Option<NextCard>> {
    col.set_current_deck(anki::decks::DeckId(deck_id))?;
    // Bury type-in cards until one pbAnki can show is at the front
    loop {
        let queued_cards = col.get_queued_cards(1, false)?;
        let Some(card) = queued_cards.cards.first().cloned() else {
            return Ok(None);
        };
        let capped = |kind: CardQueueNumber| {
            cap.is_some_and(|cap| match kind {
                CardQueueNumber::New => cap.new == 0,
                CardQueueNumber::Review => cap.reviews == 0,
                _ => false,
            })
        };
        if capped(card.card.queue_number()) {
            let total =
                queued_cards.new_count + queued_cards.learning_count + queued_cards.review_count;
            let over: Vec<_> = col
                .get_queued_cards(total, false)?
                .cards
                .iter()
                .filter(|c| capped(c.card.queue_number()))
                .map(|c| c.card.id())
                .collect();
            col.bury_or_suspend_cards(
                &over,
                anki_proto::scheduler::bury_or_suspend_cards_request::Mode::BurySched,
            )?;
            continue;
        }
        let note = col
            .storage
            .get_note(card.card.note_id())?
            .or_not_found(card.card.note_id())?;
        let notetype = col
            .get_notetype(note.notetype_id)?
            .or_not_found(note.notetype_id)?;
        if !is_type_in(&notetype, card.card.template_idx()) {
            return Ok(Some(NextCard {
                card: card.card,
                states: card.states,
                note,
                notetype,
                new: queued_cards.new_count,
                learn: queued_cards.learning_count,
                due: queued_cards.review_count,
            }));
        }
        col.bury_or_suspend_cards(
            &[card.card.id()],
            anki_proto::scheduler::bury_or_suspend_cards_request::Mode::BuryUser,
        )?;
    }
}

/// The next card of the first top-level deck with cards to study, in list
/// order, with the counts of all decks. The Default preset's limits cap all
/// decks together.
fn everything_card(col: &mut Collection) -> AnkiResult<Option<NextCard>> {
    let tree = col.deck_tree(Some(TimestampSecs::now()))?;
    let remaining = remaining_today(col, &top_level_decks(&tree))?;
    for deck in &tree.children {
        if deck.new_count + deck.learn_count + deck.review_count == 0 {
            continue;
        }
        if let Some(next) = queued_card(col, deck.deck_id, Some(remaining))? {
            return Ok(Some(NextCard {
                new: tree.new_count.min(remaining.new) as usize,
                learn: tree.learn_count as usize,
                due: tree.review_count.min(remaining.reviews) as usize,
                ..next
            }));
        }
    }
    Ok(None)
}

fn load_next_card(
    session: &LearnSession,
    deck: DeckNode,
    grid: Grid,
) -> AnkiResult<Option<CardNode>> {
    let mut col = session.collection.lock();
    let next = if deck.id == EVERYTHING_ID {
        everything_card(&mut col)?
    } else {
        queued_card(&mut col, deck.id, None)?
    };
    let Some(NextCard {
        card,
        states,
        note,
        notetype,
        new,
        learn,
        due,
    }) = next
    else {
        return Ok(None);
    };

    *session.start_time.borrow_mut() = Some(Instant::now());
    *session.states.borrow_mut() = Some(states.clone());

    let durations = col
        .describe_next_states(&states)
        .unwrap_or_else(|_| vec![String::new(); 4]);

    let rendered = col.render_existing_card(card.id(), false, false)?;
    let context = crate::inbox::CardContext::from_note(&notetype, &note);
    drop(col);

    let context_pages = context
        .as_ref()
        .map(|c| crate::render::layout(&c.to_html(), grid.columns, grid.rows))
        .unwrap_or_default();

    let answer_pages = crate::render::layout(&rendered.answer(), grid.columns, grid.rows);
    let answer_start = crate::render::first_rule_page(&answer_pages).unwrap_or(0);

    let card_node = CardNode {
        id: card.id().0,
        deck: DeckNode {
            new: new as i32,
            learn: learn as i32,
            due: due as i32,
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
        message: SharedString::new(),
    };

    *session.current_card.borrow_mut() = Some(card_node.id);
    Ok(Some(card_node))
}

/// Undoes the last rating or suspension and returns that card again, or `None`
/// if there is nothing to undo.
pub fn undo_rating(session: &LearnSession, deck: DeckNode, grid: Grid) -> Option<CardNode> {
    let undone = {
        let mut col = session.collection.lock();
        // Type-in cards buried and deck switches after the rating come off
        // the undo stack first
        while matches!(col.undo_status().undo, Some(Op::Bury | Op::SetCurrentDeck)) {
            if col.undo().is_err() {
                break;
            }
        }
        let mut user_suspensions = session.user_suspensions.borrow_mut();
        let undoable = match col.undo_status().undo {
            Some(Op::AnswerCard) => true,
            // Suspensions made at startup stay in place
            Some(Op::Suspend) => *user_suspensions > 0,
            _ => false,
        };
        if undoable && col.undo_status().undo == Some(Op::Suspend) {
            *user_suspensions -= 1;
        }
        undoable
            && col
                .undo()
                .map_err(|e| eprintln!("Failed to undo: {e}"))
                .is_ok()
    };

    // Reload either way, which also re-buries any type-in card at the front
    let card = next_card(session, deck, grid);
    (undone && card.id != -1).then_some(card)
}

/// Suspends the current card and returns the next one.
pub fn suspend_card(session: &LearnSession, deck: DeckNode, grid: Grid) -> CardNode {
    if let Some(card_id) = session.current_card.borrow_mut().take() {
        let result = session.collection.lock().bury_or_suspend_cards(
            &[anki::card::CardId(card_id)],
            anki_proto::scheduler::bury_or_suspend_cards_request::Mode::Suspend,
        );
        match result {
            Ok(_) => *session.user_suspensions.borrow_mut() += 1,
            Err(e) => eprintln!("Failed to suspend card {card_id}: {e}"),
        }
    }
    *session.states.borrow_mut() = None;
    next_card(session, deck, grid)
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

    if let Err(e) = session.collection.lock().answer_card(&mut answer) {
        eprintln!("Failed to answer card {card_id}: {e}");
    }
    *session.current_card.borrow_mut() = None;
    *session.states.borrow_mut() = None;

    next_card(session, deck, grid)
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
        let mut col = session.collection.lock();
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
        let mut col = session.collection.lock();
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
        assert_eq!(tree.deck_nodes.row_count(), 2);
        assert_eq!(tree.deck_nodes.row_data(1).unwrap().new, 1);

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
                .row_data(1)
                .unwrap()
                .new,
            0
        );
    }

    #[test]
    fn everything_studies_each_top_level_deck_in_turn() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        add_basic_note(&session, "First", "1");
        {
            let mut col = session.collection.lock();
            let deck = col.get_or_create_normal_deck("Second").unwrap();
            let notetype = col.get_notetype_by_name("Basic").unwrap().unwrap();
            let mut note = notetype.new_note();
            note.set_field(0, "Second").unwrap();
            note.set_field(1, "2").unwrap();
            col.add_note(&mut note, deck.id).unwrap();
        }

        let everything = update_deck_tree(&session).deck_nodes.row_data(0).unwrap();
        assert_eq!(everything.id, EVERYTHING_ID);
        assert_eq!(everything.name, "Everything");
        assert_eq!(everything.new, 2);

        let card = next_card(&session, everything.clone(), GRID);
        assert_eq!(texts(&card.question), vec!["First"]);
        assert_eq!(card.deck.new, 2);
        assert_eq!(card.deck.name, "Everything");

        let card = rate_card(&session, 3, everything.clone(), GRID);
        assert_eq!(texts(&card.question), vec!["Second"]);
        assert_eq!(card.deck.new, 1);

        let card = undo_rating(&session, everything.clone(), GRID).unwrap();
        assert_eq!(texts(&card.question), vec!["First"]);

        rate_card(&session, 3, everything.clone(), GRID);
        assert_eq!(rate_card(&session, 3, everything, GRID).id, -1);
    }

    #[test]
    fn new_cards_stop_at_the_daily_limit() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        {
            let mut col = session.collection.lock();
            let notetype = col.get_notetype_by_name("Basic").unwrap().unwrap();
            for book in ["KOReader::Dune", "KOReader::Emma"] {
                let deck = col.get_or_create_normal_deck(book).unwrap();
                for i in 0..50 {
                    let mut note = notetype.new_note();
                    note.set_field(0, format!("{book} {i}")).unwrap();
                    col.add_note(&mut note, deck.id).unwrap();
                }
            }
        }

        let everything = update_deck_tree(&session).deck_nodes.row_data(0).unwrap();
        assert_eq!(everything.new, 20);

        let mut shown = 0;
        let mut card = next_card(&session, everything.clone(), GRID);
        while card.id != -1 {
            shown += 1;
            card = rate_card(&session, 3, everything.clone(), GRID);
        }
        assert_eq!(shown, 20);
    }

    fn add_notes_to(session: &LearnSession, deck: &str, count: usize) {
        let mut col = session.collection.lock();
        let notetype = col.get_notetype_by_name("Basic").unwrap().unwrap();
        let deck = col.get_or_create_normal_deck(deck).unwrap();
        for i in 0..count {
            let mut note = notetype.new_note();
            note.set_field(0, format!("{} {i}", deck.human_name()))
                .unwrap();
            col.add_note(&mut note, deck.id).unwrap();
        }
    }

    /// Ratings until the deck is finished.
    fn study_all(session: &LearnSession, deck: DeckNode) -> usize {
        let mut shown = 0;
        let mut card = next_card(session, deck.clone(), GRID);
        while card.id != -1 {
            shown += 1;
            card = rate_card(session, 3, deck.clone(), GRID);
        }
        shown
    }

    #[test]
    fn everything_caps_all_decks_together() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        add_notes_to(&session, "First", 15);
        add_notes_to(&session, "Second", 15);

        let everything = update_deck_tree(&session).deck_nodes.row_data(0).unwrap();
        assert_eq!(everything.new, 20);
        assert_eq!(study_all(&session, everything.clone()), 20);
        assert!(undo_rating(&session, everything, GRID).is_some());
        rate_card(
            &session,
            3,
            update_deck_tree(&session).deck_nodes.row_data(0).unwrap(),
            GRID,
        );

        // The rest wait until tomorrow
        let tree = update_deck_tree(&session);
        assert_eq!(tree.deck_nodes.row_data(0).unwrap().new, 0);
        assert_eq!(tree.new_total, 0);
    }

    #[test]
    fn daily_limits_apply_to_every_preset() {
        use crate::limits::{DailyLimits, daily_limits, set_daily_limits};
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        add_notes_to(&session, "First", 30);
        add_notes_to(&session, "Second", 30);
        {
            let mut col = session.collection.lock();
            let second = col.get_deck_id("Second").unwrap().unwrap();
            let current = col.get_deck_configs_for_update(second).unwrap();
            let mut other = anki::deckconfig::DeckConfig {
                name: "Other".into(),
                ..Default::default()
            };
            other.inner.new_per_day = 30;
            col.update_deck_configs(anki::deckconfig::UpdateDeckConfigsRequest {
                target_deck_id: second,
                configs: vec![other],
                removed_config_ids: vec![],
                mode: anki_proto::deck_config::UpdateDeckConfigsMode::Normal,
                card_state_customizer: String::new(),
                limits: current.current_deck.unwrap().limits.unwrap(),
                new_cards_ignore_review_limit: false,
                apply_all_parent_limits: false,
                fsrs: false,
                fsrs_reschedule: false,
                fsrs_health_check: false,
            })
            .unwrap();

            let limits = DailyLimits {
                new: 5,
                reviews: 100,
            };
            set_daily_limits(&mut col, limits).unwrap();
            assert_eq!(daily_limits(&mut col).unwrap(), limits);
        }

        let tree = update_deck_tree(&session);
        let deck = |name: &str| tree.deck_nodes.iter().find(|d| d.name == name).unwrap();
        assert_eq!(deck("First").new, 5);
        assert_eq!(deck("Second").new, 5);
        assert_eq!(study_all(&session, deck("Second")), 5);
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
    fn suspend_skips_the_card_until_undone() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        add_basic_note(&session, "First", "1");
        add_basic_note(&session, "Second", "2");

        let first = next_card(&session, default_deck(), GRID);
        let second = suspend_card(&session, default_deck(), GRID);
        assert_ne!(second.id, first.id);
        assert_eq!(second.deck.new, 1);

        let undone = undo_rating(&session, default_deck(), GRID).unwrap();
        assert_eq!(undone.id, first.id);
        assert_eq!(undone.deck.new, 2);
        assert!(undo_rating(&session, default_deck(), GRID).is_none());
    }

    #[test]
    fn undo_leaves_startup_suspensions_alone() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_in(dir.path());
        add_basic_note(&session, "Only", "1");
        let card = next_card(&session, default_deck(), GRID);
        session
            .collection
            .lock()
            .bury_or_suspend_cards(
                &[anki::card::CardId(card.id)],
                anki_proto::scheduler::bury_or_suspend_cards_request::Mode::Suspend,
            )
            .unwrap();

        assert!(undo_rating(&session, default_deck(), GRID).is_none());
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
            .lock()
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
            let mut col = session.collection.lock();
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
