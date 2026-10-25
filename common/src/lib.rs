slint::include_modules!();

pub mod api;
pub mod background;
pub mod collection;
pub mod config;
pub mod edit;
pub mod inbox;
pub mod limits;
pub mod render;
pub mod sync;
pub mod sync_ui;
pub mod utils;

pub use api::{
    LearnSession, PlatformHooks, TextPrompt, TextRequest, init_session, next_card, rate_card,
    setup_ui, undo_rating, update_deck_tree,
};
pub use config::Config;
pub use sync::{SyncFn, SyncLogin, SyncMode, SyncOutcome, SyncReport, SyncStep};
