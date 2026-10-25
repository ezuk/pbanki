//! Daily new card and review limits: the ones pbAnki sets on Anki's deck
//! presets, and the overall cap the Everything row applies across decks.

use anki::collection::Collection;
use anki::config::BoolKey;
use anki::deckconfig::{DeckConfig, DeckConfigId, UpdateDeckConfigsRequest};
use anki::decks::DeckId;
use anki::error::Result as AnkiResult;
use anki_proto::deck_config::UpdateDeckConfigsMode;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DailyLimits {
    pub new: u32,
    pub reviews: u32,
}

/// Limits of Anki's Default preset.
pub fn daily_limits(col: &mut Collection) -> AnkiResult<DailyLimits> {
    let config = col
        .get_deck_config(DeckConfigId(1), true)?
        .unwrap_or_default();
    Ok(DailyLimits {
        new: config.inner.new_per_day,
        reviews: config.inner.reviews_per_day,
    })
}

/// Sets the limits on every preset, saved the way Anki's deck options screen
/// saves them.
pub fn set_daily_limits(col: &mut Collection, limits: DailyLimits) -> AnkiResult<()> {
    // The Default deck always exists; it keeps its preset and limits
    let target = DeckId(1);
    let current = col.get_deck_configs_for_update(target)?;
    let current_deck = current.current_deck.unwrap_or_default();
    let mut configs: Vec<DeckConfig> = current
        .all_config
        .into_iter()
        .filter_map(|c| c.config)
        .map(DeckConfig::from)
        .collect();
    for config in &mut configs {
        config.inner.new_per_day = limits.new;
        config.inner.reviews_per_day = limits.reviews;
    }
    // The target deck is set to the last preset
    configs.sort_by_key(|c| c.id.0 == current_deck.config_id);

    col.update_deck_configs(UpdateDeckConfigsRequest {
        target_deck_id: target,
        configs,
        removed_config_ids: vec![],
        mode: UpdateDeckConfigsMode::Normal,
        card_state_customizer: current.card_state_customizer,
        limits: current_deck.limits.unwrap_or_default(),
        new_cards_ignore_review_limit: current.new_cards_ignore_review_limit,
        apply_all_parent_limits: current.apply_all_parent_limits,
        fsrs: current.fsrs,
        fsrs_reschedule: false,
        fsrs_health_check: current.fsrs_health_check,
    })?;
    Ok(())
}

/// What is left today of the Default preset's limits, counting the cards
/// studied today in all of `decks` together, as Anki counts a parent deck.
pub fn remaining_today(col: &mut Collection, decks: &[DeckId]) -> AnkiResult<DailyLimits> {
    let limits = daily_limits(col)?;
    let today = col.timing_today()?.days_elapsed;
    let (mut new_done, mut reviews_done) = (0, 0);
    for id in decks {
        if let Some(deck) = col.get_deck(*id)?
            && deck.common.last_day_studied == today
        {
            new_done += deck.common.new_studied;
            reviews_done += deck.common.review_studied;
        }
    }

    let mut reviews = limits.reviews as i32 - reviews_done;
    let mut new = limits.new as i32 - new_done;
    if !col.get_config_bool(BoolKey::NewCardsIgnoreReviewLimit) {
        reviews -= new_done;
        new = new.min(reviews);
    }
    Ok(DailyLimits {
        new: new.max(0) as u32,
        reviews: reviews.max(0) as u32,
    })
}
