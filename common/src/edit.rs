//! Editing the current card's front and back from the study screen.

use std::cell::RefCell;
use std::rc::Rc;

use anki::card::CardId;
use anki::error::{OrNotFound, Result as AnkiResult};
use anki::notes::NoteId;
use slint::{ComponentHandle, Model, ModelRc, VecModel};

use crate::api::{Grid, LearnSession, next_card};
use crate::{EditSide, MainWindow};

/// Sides that can be edited: the note's first two fields.
const SIDES: usize = 2;

/// Tags that only break lines; anything else is formatting plain text loses.
const LINE_TAGS: [&str; 3] = ["br", "div", "p"];

/// The note being edited and its sides as they were loaded.
struct Draft {
    note_id: NoteId,
    original: Vec<String>,
}

fn tag_name(tag: &str) -> String {
    tag.trim_start_matches('/')
        .split(|c: char| c.is_whitespace() || c == '/')
        .next()
        .unwrap_or("")
        .to_ascii_lowercase()
}

/// Plain text of a field: line breaks kept, tags dropped, entities decoded.
pub fn field_to_text(html: &str) -> String {
    let mut text = String::new();
    let mut rest = html;
    while let Some(start) = rest.find('<') {
        text.push_str(&rest[..start]);
        let Some(end) = rest[start..].find('>') else {
            text.push_str(&rest[start..]);
            rest = "";
            break;
        };
        let tag = &rest[start + 1..start + end];
        let name = tag_name(tag);
        let opens_block = !tag.starts_with('/') && name != "br";
        let at_line_start = text.is_empty() || text.ends_with('\n');
        if LINE_TAGS.contains(&name.as_str()) && !(opens_block && at_line_start) {
            text.push('\n');
        }
        rest = &rest[start + end + 1..];
    }
    text.push_str(rest);
    anki::text::decode_entities(&text).trim().to_string()
}

/// Whether a field holds more than text and line breaks, e.g. bold or images.
pub fn has_formatting(html: &str) -> bool {
    let mut rest = html;
    while let Some(start) = rest.find('<') {
        let Some(end) = rest[start..].find('>') else {
            return false;
        };
        if !LINE_TAGS.contains(&tag_name(&rest[start + 1..start + end]).as_str()) {
            return true;
        }
        rest = &rest[start + end + 1..];
    }
    html.contains("[sound:")
}

/// The current card's note and its editable sides.
fn load_sides(session: &LearnSession) -> AnkiResult<Option<(NoteId, Vec<EditSide>)>> {
    let Some(card_id) = *session.current_card.borrow() else {
        return Ok(None);
    };
    let mut col = session.collection.lock();
    let card = col
        .storage
        .get_card(CardId(card_id))?
        .or_not_found(card_id)?;
    let note = col
        .storage
        .get_note(card.note_id())?
        .or_not_found(card.note_id())?;
    let notetype = col
        .get_notetype(note.notetype_id)?
        .or_not_found(note.notetype_id)?;
    let sides = note
        .fields()
        .iter()
        .zip(&notetype.fields)
        .take(SIDES)
        .map(|(html, field)| EditSide {
            title: field.name.clone().into(),
            text: field_to_text(html).into(),
            formatted: has_formatting(html),
        })
        .collect();
    Ok(Some((note.id, sides)))
}

/// Writes the sides that changed; returns whether the note was updated.
fn save_sides(
    session: &LearnSession,
    note_id: NoteId,
    original: &[String],
    texts: &[String],
) -> AnkiResult<bool> {
    let mut col = session.collection.lock();
    let mut note = col.storage.get_note(note_id)?.or_not_found(note_id)?;
    let mut changed = false;
    for (idx, (before, after)) in original.iter().zip(texts).enumerate() {
        if before.trim() != after.trim() {
            note.set_field(idx, crate::inbox::text_to_html(after))?;
            changed = true;
        }
    }
    if changed {
        col.update_note(&mut note)?;
    }
    Ok(changed)
}

/// Connects the Edit button on the study screen and the edit screen.
pub fn setup_edit(ui: &MainWindow, session: &Rc<LearnSession>) {
    let draft: Rc<RefCell<Option<Draft>>> = Rc::default();
    let sides = Rc::new(VecModel::<EditSide>::default());
    ui.set_edit_sides(ModelRc::from(sides.clone()));

    let ui_weak = ui.as_weak();
    let session_for_open = session.clone();
    let draft_for_open = draft.clone();
    let sides_for_open = sides.clone();
    ui.on_edit_card(move || {
        let Some(ui) = ui_weak.upgrade() else {
            return;
        };
        match load_sides(&session_for_open) {
            Ok(Some((note_id, loaded))) => {
                let original = loaded.iter().map(|s| s.text.to_string()).collect();
                sides_for_open.set_vec(loaded);
                *draft_for_open.borrow_mut() = Some(Draft { note_id, original });
                ui.set_edit_error("".into());
                ui.set_edit_side(0);
                ui.set_editing(true);
            }
            Ok(None) => {}
            Err(e) => eprintln!("Failed to open card for editing: {e}"),
        }
    });

    let sides_for_typed = sides.clone();
    ui.on_edit_typed(move |side, text| {
        set_text(&sides_for_typed, side, text.into());
    });

    let ui_weak = ui.as_weak();
    let session_for_save = session.clone();
    ui.on_save_edit(move |deck, columns, rows| {
        let Some(ui) = ui_weak.upgrade() else {
            return;
        };
        let Some(Draft { note_id, original }) = draft.borrow_mut().take() else {
            ui.set_editing(false);
            return;
        };
        let texts: Vec<String> = sides.iter().map(|s| s.text.to_string()).collect();
        match save_sides(&session_for_save, note_id, &original, &texts) {
            Ok(changed) => {
                if changed {
                    let card = next_card(&session_for_save, deck, Grid::new(columns, rows));
                    ui.set_current_card(card);
                }
                ui.set_editing(false);
            }
            Err(e) => {
                eprintln!("Failed to save card: {e}");
                ui.set_edit_error(format!("Couldn't save the card: {e}").into());
                *draft.borrow_mut() = Some(Draft { note_id, original });
            }
        }
    });
}

fn set_text(sides: &VecModel<EditSide>, side: i32, text: String) {
    let idx = side as usize;
    if let Some(mut row) = sides.row_data(idx) {
        row.text = text.into();
        sides.set_row_data(idx, row);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Config, DeckNode, init_session};
    use anki::decks::DeckId;

    fn session_with_note(dir: &std::path::Path, front: &str, back: &str) -> Rc<LearnSession> {
        let mut config = Config::default();
        config.general.collection_path = dir.join("collection").to_string_lossy().into();
        let session = init_session(&config).unwrap();
        {
            let mut col = session.collection.lock();
            let notetype = col.get_notetype_by_name("Basic").unwrap().unwrap();
            let mut note = notetype.new_note();
            note.set_field(0, front).unwrap();
            note.set_field(1, back).unwrap();
            col.add_note(&mut note, DeckId(1)).unwrap();
        }
        let deck = DeckNode {
            id: 1,
            ..Default::default()
        };
        next_card(&session, deck, Grid::new(40, 16));
        session
    }

    #[test]
    fn saves_only_the_changed_side() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_with_note(dir.path(), "Capital of <b>France</b>?", "Paris");
        let (note_id, sides) = load_sides(&session).unwrap().unwrap();
        let titles: Vec<_> = sides.iter().map(|s| s.title.to_string()).collect();
        assert_eq!(titles, ["Front", "Back"]);
        assert_eq!(sides[0].text, "Capital of France?");
        assert!(sides[0].formatted);

        let original: Vec<String> = sides.iter().map(|s| s.text.to_string()).collect();
        let texts = vec![original[0].clone(), "Paris\non the Seine".to_string()];
        assert!(save_sides(&session, note_id, &original, &texts).unwrap());

        let note = session
            .collection
            .lock()
            .storage
            .get_note(note_id)
            .unwrap()
            .unwrap();
        assert_eq!(note.fields()[0], "Capital of <b>France</b>?");
        assert_eq!(note.fields()[1], "Paris<br>on the Seine");
    }

    #[test]
    fn unchanged_sides_leave_the_note_alone() {
        let dir = tempfile::tempdir().unwrap();
        let session = session_with_note(dir.path(), "Q", "A");
        let (note_id, sides) = load_sides(&session).unwrap().unwrap();
        let original: Vec<String> = sides.iter().map(|s| s.text.to_string()).collect();
        assert!(!save_sides(&session, note_id, &original, &original).unwrap());
    }

    #[test]
    fn plain_text_keeps_line_breaks() {
        assert_eq!(field_to_text("line 1<br>line 2"), "line 1\nline 2");
        assert_eq!(
            field_to_text("one<div>two</div><div>three</div>"),
            "one\ntwo\nthree"
        );
        assert_eq!(
            field_to_text("<div>first</div><div>second</div>"),
            "first\nsecond"
        );
        assert_eq!(
            field_to_text("Tom &amp; Jerry&nbsp;&lt;3"),
            "Tom & Jerry <3"
        );
        assert_eq!(field_to_text("<b>bold</b> text"), "bold text");
    }

    #[test]
    fn formatting_is_anything_beyond_line_breaks() {
        assert!(!has_formatting("plain"));
        assert!(!has_formatting("a<br>b<div>c</div><br/>"));
        assert!(has_formatting("<b>bold</b>"));
        assert!(has_formatting("<img src=\"cat.jpg\">"));
        assert!(has_formatting("[sound:word.mp3]"));
    }

    #[test]
    fn koreader_text_round_trips() {
        let text = "Who said \"<this>\" & why?\nOn two lines";
        let html = crate::inbox::text_to_html(text);
        assert!(!has_formatting(&html));
        assert_eq!(field_to_text(&html), text);
    }
}
