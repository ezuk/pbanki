//! Applies card changes queued by the desktop app.
//!
//! The desktop app writes one JSON object per line to `changes.jsonl`. Applying
//! renames the file first, and appends one line per change to
//! `changes.results.jsonl`, which the desktop app reads at the next plug-in.
//! A change whose id already has a result is skipped, so a retry is harmless.

use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;

use anki::card::CardId;
use anki::cloze::cloze_numbers_in_string;
use anki::collection::Collection;
use anki::notes::{Note, NoteId};
use anki::notetype::NotetypeKind;
use anki::search::SortMode;
use anki::timestamp::TimestampSecs;
use anki_proto::scheduler::bury_or_suspend_cards_request::Mode as BuryMode;
use serde::{Deserialize, Serialize};

use crate::edit::has_formatting;
use crate::inbox::{CLOZE_NOTETYPE_NAME, NOTETYPE_NAME, text_to_html};

pub const CHANGES_FILE: &str = "changes.jsonl";
pub const APPLYING_FILE: &str = "changes.applying.jsonl";
pub const RESULTS_FILE: &str = "changes.results.jsonl";

/// Found in the built app by the desktop app, which never runs it.
pub const FORMAT_MARKER: &str = "pbanki-changes-format-1";

/// Note types whose first two fields are the question and the answer.
const EDITABLE_NOTETYPES: [&str; 4] = ["Basic", "Cloze", NOTETYPE_NAME, CLOZE_NOTETYPE_NAME];

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
pub struct Change {
    pub id: String,
    /// `edit`, `suspend`, `unsuspend` or `delete`
    pub op: String,
    pub note_id: i64,
    /// Fields as the desktop app last saw them
    pub expect_front: Option<String>,
    pub expect_back: Option<String>,
    /// Typed text for the sides being edited
    pub front: Option<String>,
    pub back: Option<String>,
    /// Reset the card to new, keeping its review log
    pub learn: bool,
}

#[derive(Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ChangeResult {
    pub id: String,
    pub note_id: i64,
    /// `applied`, `already` or `refused`
    pub status: String,
    /// Why it was refused: `changed`, `gone`, `formatted`, `cloze`, `empty`,
    /// `note_type`, `several_cards`, `bad` or `failed`
    #[serde(skip_serializing_if = "String::is_empty")]
    pub reason: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    pub detail: String,
    /// Fields found on the reader
    #[serde(skip_serializing_if = "Option::is_none")]
    pub front: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub back: Option<String>,
    pub at: i64,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct ApplySummary {
    pub applied: usize,
    pub refused: usize,
}

impl ApplySummary {
    /// Line for the deck list, or nothing when no change ran.
    pub fn notice(&self) -> Option<String> {
        let plural = |n| if n == 1 { "change" } else { "changes" };
        let applied = (self.applied > 0).then(|| {
            format!(
                "✓ {} {} from your Mac applied.",
                self.applied,
                plural(self.applied)
            )
        });
        let refused = (self.refused > 0).then(|| {
            if self.applied > 0 {
                format!("{} couldn't be applied. Details on your Mac.", self.refused)
            } else {
                format!(
                    "{} {} from your Mac couldn't be applied. Details on your Mac.",
                    self.refused,
                    plural(self.refused)
                )
            }
        });
        let lines: Vec<String> = applied.into_iter().chain(refused).collect();
        (!lines.is_empty()).then(|| lines.join("\n"))
    }
}

fn seen_ids(path: &Path) -> HashSet<String> {
    let Ok(contents) = fs::read_to_string(path) else {
        return HashSet::new();
    };
    contents
        .lines()
        .filter_map(|line| serde_json::from_str::<ChangeResult>(line).ok())
        .map(|result| result.id)
        .collect()
}

/// Appends one line, starting a new line after a partly written one.
fn append_result(path: &Path, result: &ChangeResult) -> Result<(), String> {
    let line = serde_json::to_string(result).map_err(|e| e.to_string())?;
    let mut file = OpenOptions::new()
        .create(true)
        .read(true)
        .append(true)
        .open(path)
        .map_err(|e| format!("Cannot open results: {e}"))?;
    let len = file.metadata().map(|m| m.len()).unwrap_or(0);
    let mut prefix = "";
    if len > 0 {
        let mut last = [0u8];
        let ended = file.seek(SeekFrom::End(-1)).is_ok()
            && file.read_exact(&mut last).is_ok()
            && last[0] == b'\n';
        if !ended {
            prefix = "\n";
        }
    }
    file.write_all(format!("{prefix}{line}\n").as_bytes())
        .and_then(|()| file.sync_all())
        .map_err(|e| format!("Cannot write results: {e}"))
}

fn refused(reason: &str) -> Outcome {
    Outcome {
        status: "refused",
        reason: reason.into(),
        detail: String::new(),
    }
}

fn done(status: &'static str) -> Outcome {
    Outcome {
        status,
        reason: String::new(),
        detail: String::new(),
    }
}

struct Outcome {
    status: &'static str,
    reason: String,
    detail: String,
}

fn sides(note: &Note) -> (String, String) {
    let field = |i: usize| note.fields().get(i).cloned().unwrap_or_default();
    (field(0), field(1))
}

/// Whether both sides are as the desktop app last saw them.
fn as_expected(note: &Note, change: &Change) -> bool {
    let (front, back) = sides(note);
    change.expect_front.as_deref() == Some(front.as_str())
        && change.expect_back.as_deref() == Some(back.as_str())
}

fn note_card_ids(
    col: &mut Collection,
    note_id: NoteId,
    extra: &str,
) -> anki::error::Result<Vec<CardId>> {
    col.search_cards(&format!("nid:{} {extra}", note_id.0), SortMode::NoOrder)
}

fn reset_to_new(col: &mut Collection, note_id: NoteId) -> anki::error::Result<()> {
    let cards = note_card_ids(col, note_id, "")?;
    col.reschedule_cards_as_new(&cards, true, false, false, None)?;
    Ok(())
}

fn apply_edit(col: &mut Collection, note: Note, change: &Change) -> anki::error::Result<Outcome> {
    let target_front = change.front.as_deref().map(text_to_html);
    let target_back = change.back.as_deref().map(text_to_html);
    let (front, back) = sides(&note);
    let at_target = (target_front.is_some() || target_back.is_some())
        && target_front.as_ref().is_none_or(|t| *t == front)
        && target_back.as_ref().is_none_or(|t| *t == back);
    if at_target {
        if change.learn {
            reset_to_new(col, note.id)?;
        }
        return Ok(done("already"));
    }
    if !as_expected(&note, change) {
        return Ok(refused("changed"));
    }
    let Some(notetype) = col.get_notetype(note.notetype_id)? else {
        return Ok(refused("note_type"));
    };
    if !EDITABLE_NOTETYPES.contains(&notetype.name.as_str()) || note.fields().len() < 2 {
        return Ok(refused("note_type"));
    }
    if note_card_ids(col, note.id, "")?.len() != 1 {
        return Ok(refused("several_cards"));
    }
    if (target_front.is_some() && has_formatting(&front))
        || (target_back.is_some() && has_formatting(&back))
    {
        return Ok(refused("formatted"));
    }
    if target_front.as_ref().is_some_and(|t| t.is_empty()) {
        return Ok(refused("empty"));
    }
    if notetype.config.kind() == NotetypeKind::Cloze
        && let Some(target) = &target_front
        && cloze_numbers_in_string(target) != cloze_numbers_in_string(&front)
    {
        return Ok(refused("cloze"));
    }

    let mut note = note;
    if let Some(target) = target_front {
        note.set_field(0, target)?;
    }
    if let Some(target) = target_back {
        note.set_field(1, target)?;
    }
    col.update_note(&mut note)?;
    if change.learn {
        reset_to_new(col, note.id)?;
    }
    Ok(done("applied"))
}

fn apply_one(
    col: &mut Collection,
    change: &Change,
) -> anki::error::Result<(Outcome, Option<Note>)> {
    let note_id = NoteId(change.note_id);
    let note = col.storage.get_note(note_id)?;
    let Some(note) = note else {
        let outcome = if change.op == "delete" {
            done("already")
        } else if ["edit", "suspend", "unsuspend"].contains(&change.op.as_str()) {
            refused("gone")
        } else {
            refused("bad")
        };
        return Ok((outcome, None));
    };
    let outcome = match change.op.as_str() {
        "edit" => apply_edit(col, note, change)?,
        "suspend" => {
            let active = note_card_ids(col, note_id, "-is:suspended")?;
            if active.is_empty() {
                done("already")
            } else if !as_expected(&note, change) {
                refused("changed")
            } else {
                col.bury_or_suspend_cards(&active, BuryMode::Suspend)?;
                done("applied")
            }
        }
        "unsuspend" => {
            let suspended = note_card_ids(col, note_id, "is:suspended")?;
            if suspended.is_empty() {
                done("already")
            } else {
                col.unbury_or_unsuspend_cards(&suspended)?;
                done("applied")
            }
        }
        "delete" => {
            if !as_expected(&note, change) {
                refused("changed")
            } else {
                col.remove_notes(&[note_id])?;
                done("applied")
            }
        }
        _ => refused("bad"),
    };
    let now = col.storage.get_note(note_id)?;
    Ok((outcome, now))
}

fn apply_file(
    col: &mut Collection,
    dir: &Path,
    path: &Path,
    seen: &mut HashSet<String>,
    summary: &mut ApplySummary,
) -> Result<(), String> {
    let results = dir.join(RESULTS_FILE);
    let contents = fs::read_to_string(path).map_err(|e| format!("Cannot read changes: {e}"))?;
    for line in contents.lines().filter(|l| !l.trim().is_empty()) {
        let parsed = serde_json::from_str::<Change>(line);
        let change = parsed.as_ref().ok();
        let id = change.map(|c| c.id.clone()).unwrap_or_default();
        if !id.is_empty() && seen.contains(&id) {
            continue;
        }
        let (outcome, note) = match change {
            Some(change) if !change.id.is_empty() => match apply_one(col, change) {
                Ok(done) => done,
                Err(e) => {
                    let mut outcome = refused("failed");
                    outcome.detail = e.to_string();
                    (
                        outcome,
                        col.storage.get_note(NoteId(change.note_id)).ok().flatten(),
                    )
                }
            },
            _ => {
                let mut outcome = refused("bad");
                outcome.detail = line.chars().take(500).collect();
                (outcome, None)
            }
        };
        let current = note.as_ref().map(sides);
        let result = ChangeResult {
            id: id.clone(),
            note_id: change.map(|c| c.note_id).unwrap_or_default(),
            status: outcome.status.into(),
            reason: outcome.reason,
            detail: outcome.detail,
            front: current.as_ref().map(|s| s.0.clone()),
            back: current.as_ref().map(|s| s.1.clone()),
            at: TimestampSecs::now().0,
        };
        append_result(&results, &result)?;
        if result.status == "refused" {
            summary.refused += 1;
        } else {
            summary.applied += 1;
        }
        seen.insert(id);
    }
    Ok(())
}

/// Applies every queued change and clears the queue.
pub fn apply_changes(col: &mut Collection, dir: &Path) -> Result<ApplySummary, String> {
    let queued = dir.join(CHANGES_FILE);
    let applying = dir.join(APPLYING_FILE);
    let mut summary = ApplySummary::default();
    if !queued.exists() && !applying.exists() {
        return Ok(summary);
    }
    eprintln!("Applying changes ({FORMAT_MARKER})");
    let mut seen = seen_ids(&dir.join(RESULTS_FILE));
    // Finish a run that was interrupted before starting on the new file
    if applying.exists() {
        apply_file(col, dir, &applying, &mut seen, &mut summary)?;
        fs::remove_file(&applying).map_err(|e| format!("Cannot clear changes: {e}"))?;
    }
    if queued.exists() {
        fs::rename(&queued, &applying).map_err(|e| format!("Cannot claim changes: {e}"))?;
        apply_file(col, dir, &applying, &mut seen, &mut summary)?;
        fs::remove_file(&applying).map_err(|e| format!("Cannot clear changes: {e}"))?;
    }
    Ok(summary)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collection::open_collection;
    use crate::edit::field_to_text;
    use crate::inbox::{clear_unsynced, import_inbox, koreader_cloze_notetype, restore_unsynced};
    use anki::decks::DeckId;
    use anki::sync::http_server::{SimpleServer, SyncServerConfig};
    use anki::sync::login::{SyncAuth, sync_login};
    use serde_json::json;
    use tempfile::{TempDir, tempdir};

    fn collection(dir: &Path) -> Collection {
        open_collection(&dir.join("collection.anki2")).unwrap()
    }

    fn add_note(col: &mut Collection, notetype: &str, fields: &[&str]) -> NoteId {
        let notetype = col.get_notetype_by_name(notetype).unwrap().unwrap();
        let mut note = notetype.new_note();
        for (i, field) in fields.iter().enumerate() {
            note.set_field(i, *field).unwrap();
        }
        col.add_note(&mut note, DeckId(1)).unwrap();
        note.id
    }

    fn fields(col: &Collection, id: NoteId) -> (String, String) {
        sides(&col.storage.get_note(id).unwrap().unwrap())
    }

    fn cards(col: &Collection, id: NoteId) -> Vec<anki::card::Card> {
        col.storage.all_cards_of_note(id).unwrap()
    }

    fn suspended(col: &mut Collection, id: NoteId) -> bool {
        note_card_ids(col, id, "is:suspended").unwrap().len() == cards(col, id).len()
    }

    fn queue(dir: &Path, changes: &[serde_json::Value]) {
        let lines: Vec<String> = changes.iter().map(|c| c.to_string()).collect();
        fs::write(dir.join(CHANGES_FILE), lines.join("\n") + "\n").unwrap();
    }

    fn results(dir: &Path) -> Vec<ChangeResult> {
        fs::read_to_string(dir.join(RESULTS_FILE))
            .unwrap_or_default()
            .lines()
            .filter_map(|l| serde_json::from_str(l).ok())
            .collect()
    }

    fn edit(id: &str, note: NoteId, front: &str, back: &str, new_front: &str) -> serde_json::Value {
        json!({"id": id, "op": "edit", "note_id": note.0, "expect_front": front, "expect_back": back, "front": new_front})
    }

    #[test]
    fn no_queue_is_a_no_op() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        let summary = apply_changes(&mut col, dir.path()).unwrap();
        assert_eq!(summary, ApplySummary::default());
        assert_eq!(summary.notice(), None);
        assert!(!dir.path().join(RESULTS_FILE).exists());
    }

    #[test]
    fn edits_only_the_sides_sent_and_clears_the_queue() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        let id = add_note(&mut col, "Basic", &["Old?", "<b>Bold</b> answer"]);
        queue(
            dir.path(),
            &[edit(
                "a",
                id,
                "Old?",
                "<b>Bold</b> answer",
                "New & <better>?\nTwo lines",
            )],
        );

        let summary = apply_changes(&mut col, dir.path()).unwrap();

        assert_eq!(summary.applied, 1);
        let (front, back) = fields(&col, id);
        assert_eq!(front, "New &amp; &lt;better&gt;?<br>Two lines");
        assert_eq!(field_to_text(&front), "New & <better>?\nTwo lines");
        assert_eq!(back, "<b>Bold</b> answer");
        assert!(!dir.path().join(CHANGES_FILE).exists());
        assert!(!dir.path().join(APPLYING_FILE).exists());
        assert_eq!(results(dir.path())[0].status, "applied");
        assert_eq!(
            results(dir.path())[0].front.as_deref(),
            Some(front.as_str())
        );
    }

    #[test]
    fn refuses_a_note_that_changed_since_the_mac_saw_it() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        let id = add_note(&mut col, "Basic", &["Edited on the reader", "A"]);
        queue(dir.path(), &[edit("a", id, "Old?", "A", "From the Mac")]);

        let summary = apply_changes(&mut col, dir.path()).unwrap();

        assert_eq!(
            summary,
            ApplySummary {
                applied: 0,
                refused: 1
            }
        );
        assert_eq!(fields(&col, id).0, "Edited on the reader");
        let result = &results(dir.path())[0];
        assert_eq!(
            (result.status.as_str(), result.reason.as_str()),
            ("refused", "changed")
        );
        assert_eq!(result.front.as_deref(), Some("Edited on the reader"));
    }

    #[test]
    fn checks_either_side_for_staleness() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        let id = add_note(&mut col, "Basic", &["Q", "Answer changed elsewhere"]);
        queue(dir.path(), &[edit("a", id, "Q", "Old answer", "New Q")]);
        apply_changes(&mut col, dir.path()).unwrap();
        assert_eq!(results(dir.path())[0].reason, "changed");
        assert_eq!(fields(&col, id).0, "Q");
    }

    #[test]
    fn a_change_already_in_place_counts_as_done() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        let id = add_note(&mut col, "Basic", &["Same new text", "A"]);
        queue(dir.path(), &[edit("a", id, "Old?", "A", "Same new text")]);
        let summary = apply_changes(&mut col, dir.path()).unwrap();
        assert_eq!(summary.applied, 1);
        assert_eq!(results(dir.path())[0].status, "already");
    }

    #[test]
    fn refuses_a_deleted_note() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        queue(dir.path(), &[edit("a", NoteId(123), "Q", "A", "New")]);
        apply_changes(&mut col, dir.path()).unwrap();
        assert_eq!(results(dir.path())[0].reason, "gone");
    }

    #[test]
    fn repeats_the_desktop_apps_checks() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        let formatted = add_note(&mut col, "Basic", &["<b>Q</b>", "A"]);
        let image = add_note(&mut col, "Basic", &["Q2", "<img src=\"a.png\">"]);
        let sound = add_note(&mut col, "Basic", &["[sound:a.mp3]", "A"]);
        let empty = add_note(&mut col, "Basic", &["Q3", "A"]);
        let reversed = add_note(&mut col, "Basic (and reversed card)", &["Q4", "A"]);
        let two_clozes = add_note(&mut col, "Cloze", &["{{c1::a}} {{c2::b}}", ""]);
        let one_cloze = add_note(&mut col, "Cloze", &["{{c1::a}} b", ""]);
        queue(
            dir.path(),
            &[
                edit("formatted", formatted, "<b>Q</b>", "A", "Plain"),
                json!({"id": "image", "op": "edit", "note_id": image.0, "expect_front": "Q2", "expect_back": "<img src=\"a.png\">", "back": "Plain"}),
                edit("sound", sound, "[sound:a.mp3]", "A", "Plain"),
                edit("empty", empty, "Q3", "A", "   "),
                edit("reversed", reversed, "Q4", "A", "New"),
                edit(
                    "two",
                    two_clozes,
                    "{{c1::a}} {{c2::b}}",
                    "",
                    "{{c1::a}} {{c2::c}}",
                ),
                edit("number", one_cloze, "{{c1::a}} b", "", "{{c2::a}} b"),
                edit("noneleft", one_cloze, "{{c1::a}} b", "", "no cloze"),
                json!({"id": "typo", "op": "nonsense", "note_id": empty.0}),
            ],
        );
        apply_changes(&mut col, dir.path()).unwrap();
        let reasons: Vec<(String, String)> = results(dir.path())
            .into_iter()
            .map(|r| (r.id, r.reason))
            .collect();
        let want = [
            ("formatted", "formatted"),
            ("image", "formatted"),
            ("sound", "formatted"),
            ("empty", "empty"),
            ("reversed", "note_type"),
            ("two", "several_cards"),
            ("number", "cloze"),
            ("noneleft", "cloze"),
            ("typo", "bad"),
        ];
        assert_eq!(
            reasons,
            want.map(|(a, b)| (a.to_string(), b.to_string())).to_vec()
        );
        assert_eq!(fields(&col, formatted).0, "<b>Q</b>");
        assert_eq!(fields(&col, one_cloze).0, "{{c1::a}} b");
    }

    #[test]
    fn edits_a_koreader_cloze_keeping_its_numbers_and_extra_fields() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        koreader_cloze_notetype(&mut col).unwrap();
        let id = add_note(
            &mut col,
            CLOZE_NOTETYPE_NAME,
            &["{{c1::a}} b", "", "The quote"],
        );
        queue(
            dir.path(),
            &[edit("a", id, "{{c1::a}} b", "", "{{c1::a}} better")],
        );
        apply_changes(&mut col, dir.path()).unwrap();
        assert_eq!(results(dir.path())[0].status, "applied");
        let note = col.storage.get_note(id).unwrap().unwrap();
        assert_eq!(note.fields()[0], "{{c1::a}} better");
        assert_eq!(note.fields()[2], "The quote");
        assert_eq!(cards(&col, id).len(), 1);
    }

    fn review_card(col: &mut Collection, id: NoteId) {
        let cards = note_card_ids(col, id, "").unwrap();
        col.set_due_date(&cards, "30", None).unwrap();
    }

    #[test]
    fn learn_again_resets_the_card_and_keeps_its_review_log() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        let keep = add_note(&mut col, "Basic", &["Keep", "A"]);
        let reset = add_note(&mut col, "Basic", &["Reset", "A"]);
        review_card(&mut col, keep);
        review_card(&mut col, reset);
        let card = cards(&col, reset)[0].id();
        let logged = col.card_stats(card).unwrap().revlog.len();
        assert!(logged > 0);

        queue(
            dir.path(),
            &[
                edit("keep", keep, "Keep", "A", "Keep typo fixed"),
                json!({"id": "reset", "op": "edit", "note_id": reset.0, "expect_front": "Reset", "expect_back": "A", "back": "New answer", "learn": true}),
            ],
        );
        apply_changes(&mut col, dir.path()).unwrap();

        let is_new =
            |col: &mut Collection, id: NoteId| note_card_ids(col, id, "is:new").unwrap().len() == 1;
        assert!(!is_new(&mut col, keep));
        assert!(is_new(&mut col, reset));
        assert!(col.card_stats(card).unwrap().revlog.len() >= logged);
    }

    #[test]
    fn suspends_and_unsuspends_every_card_of_a_note() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        let id = add_note(&mut col, "Basic (and reversed card)", &["Q", "A"]);
        queue(
            dir.path(),
            &[
                json!({"id": "s", "op": "suspend", "note_id": id.0, "expect_front": "Q", "expect_back": "A"}),
            ],
        );
        apply_changes(&mut col, dir.path()).unwrap();
        assert!(suspended(&mut col, id));

        queue(
            dir.path(),
            &[
                json!({"id": "s2", "op": "suspend", "note_id": id.0, "expect_front": "Changed", "expect_back": "A"}),
            ],
        );
        apply_changes(&mut col, dir.path()).unwrap();
        assert_eq!(results(dir.path())[1].status, "already");

        queue(
            dir.path(),
            &[json!({"id": "u", "op": "unsuspend", "note_id": id.0})],
        );
        apply_changes(&mut col, dir.path()).unwrap();
        assert!(!suspended(&mut col, id));
        assert_eq!(results(dir.path())[2].status, "applied");
    }

    #[test]
    fn suspend_is_refused_when_the_text_changed() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        let id = add_note(&mut col, "Basic", &["Q", "A"]);
        queue(
            dir.path(),
            &[
                json!({"id": "s", "op": "suspend", "note_id": id.0, "expect_front": "Old", "expect_back": "A"}),
            ],
        );
        apply_changes(&mut col, dir.path()).unwrap();
        assert_eq!(results(dir.path())[0].reason, "changed");
        assert!(!suspended(&mut col, id));
    }

    #[test]
    fn delete_keeps_the_review_log_and_refuses_changed_text() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        let id = add_note(&mut col, "Basic", &["Q", "A"]);
        let other = add_note(&mut col, "Basic", &["Other", "A"]);
        review_card(&mut col, id);
        let card = cards(&col, id)[0].id();
        queue(
            dir.path(),
            &[
                json!({"id": "stale", "op": "delete", "note_id": other.0, "expect_front": "Old", "expect_back": "A"}),
                json!({"id": "d", "op": "delete", "note_id": id.0, "expect_front": "Q", "expect_back": "A"}),
                json!({"id": "again", "op": "delete", "note_id": id.0, "expect_front": "Q", "expect_back": "A"}),
            ],
        );
        apply_changes(&mut col, dir.path()).unwrap();
        let got = results(dir.path());
        assert_eq!(got[0].reason, "changed");
        assert_eq!(got[1].status, "applied");
        assert_eq!(got[2].status, "already");
        assert!(col.storage.get_note(id).unwrap().is_none());
        assert!(col.storage.get_note(other).unwrap().is_some());
        let left: u32 = col
            .search_cards(&format!("cid:{}", card.0), SortMode::NoOrder)
            .unwrap()
            .len() as u32;
        assert_eq!(left, 0);
    }

    #[test]
    fn resumes_an_interrupted_run_without_repeating_finished_changes() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        let first = add_note(&mut col, "Basic", &["One", "A"]);
        let second = add_note(&mut col, "Basic", &["Two", "A"]);
        // The first change ran and reported, the second never started
        let lines = [
            edit("a", first, "One", "A", "One edited").to_string(),
            edit("b", second, "Two", "A", "Two edited").to_string(),
        ];
        fs::write(dir.path().join(APPLYING_FILE), lines.join("\n")).unwrap();
        fs::write(
            dir.path().join(RESULTS_FILE),
            "{\"id\":\"a\",\"note_id\":1,\"status\":\"applied\",\"at\":1}\n{\"id\":\"half",
        )
        .unwrap();
        // Someone edited the first note afterwards, which must survive
        let mut note = col.storage.get_note(first).unwrap().unwrap();
        note.set_field(0, "One edited later").unwrap();
        col.update_note(&mut note).unwrap();
        // A new file arrived while the old one was stuck, and repeats change b
        queue(
            dir.path(),
            &[
                edit("b", second, "Two", "A", "Two edited"),
                edit("c", first, "One edited later", "A", "One newest"),
            ],
        );

        let summary = apply_changes(&mut col, dir.path()).unwrap();

        assert_eq!(summary.applied, 2);
        assert_eq!(fields(&col, second).0, "Two edited");
        assert_eq!(fields(&col, first).0, "One newest");
        assert!(!dir.path().join(APPLYING_FILE).exists());
        let ids: Vec<String> = results(dir.path()).into_iter().map(|r| r.id).collect();
        assert_eq!(ids, ["a", "b", "c"]);
    }

    #[test]
    fn a_change_that_ran_but_never_reported_is_not_applied_twice() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        let id = add_note(&mut col, "Basic", &["New", "A"]);
        review_card(&mut col, id);
        // Crashed between the edit and the reset: the retry still resets
        fs::write(
            dir.path().join(APPLYING_FILE),
            json!({"id": "a", "op": "edit", "note_id": id.0, "expect_front": "Old", "expect_back": "A", "front": "New", "learn": true}).to_string(),
        )
        .unwrap();
        apply_changes(&mut col, dir.path()).unwrap();
        assert_eq!(results(dir.path())[0].status, "already");
        assert_eq!(note_card_ids(&mut col, id, "is:new").unwrap().len(), 1);
    }

    #[test]
    fn a_broken_line_is_reported_and_the_rest_still_run() {
        let dir = tempdir().unwrap();
        let mut col = collection(dir.path());
        let id = add_note(&mut col, "Basic", &["Q", "A"]);
        fs::write(
            dir.path().join(CHANGES_FILE),
            format!("{{broken\n{}\n", edit("a", id, "Q", "A", "New")),
        )
        .unwrap();
        let summary = apply_changes(&mut col, dir.path()).unwrap();
        assert_eq!(
            summary,
            ApplySummary {
                applied: 1,
                refused: 1
            }
        );
        assert_eq!(results(dir.path())[0].reason, "bad");
        assert_eq!(fields(&col, id).0, "New");
    }

    #[test]
    fn notice_says_what_happened() {
        let notice = |applied, refused| ApplySummary { applied, refused }.notice();
        assert_eq!(notice(0, 0), None);
        assert_eq!(notice(1, 0).unwrap(), "✓ 1 change from your Mac applied.");
        assert_eq!(
            notice(3, 1).unwrap(),
            "✓ 3 changes from your Mac applied.\n1 couldn't be applied. Details on your Mac."
        );
        assert_eq!(
            notice(0, 2).unwrap(),
            "2 changes from your Mac couldn't be applied. Details on your Mac."
        );
    }

    #[test]
    fn imported_lines_wait_until_a_sync_has_them() {
        let dir = tempdir().unwrap();
        let line = "{\"front\":\"Q\",\"back\":\"A\",\"book\":\"B\"}\n";
        let mut col = collection(dir.path());
        fs::write(dir.path().join("inbox.jsonl"), line).unwrap();
        import_inbox(&mut col, dir.path()).unwrap();

        restore_unsynced(dir.path()).unwrap();
        assert_eq!(
            fs::read_to_string(dir.path().join("inbox.jsonl")).unwrap(),
            line
        );
        // Nothing left to restore a second time
        restore_unsynced(dir.path()).unwrap();
        assert_eq!(
            fs::read_to_string(dir.path().join("inbox.jsonl")).unwrap(),
            line
        );

        import_inbox(&mut col, dir.path()).unwrap();
        clear_unsynced(dir.path()).unwrap();
        restore_unsynced(dir.path()).unwrap();
        assert!(!dir.path().join("inbox.jsonl").exists());
    }

    // What the engine does when both sides change, against its own sync server.

    struct Cloud {
        _folder: TempDir,
        rt: tokio::runtime::Runtime,
        auth: SyncAuth,
    }

    impl Cloud {
        fn start() -> Self {
            let folder = tempdir().unwrap();
            // Safety: every test that reads it sets the same value
            unsafe { std::env::set_var("SYNC_USER1", "user:pass") };
            let rt = tokio::runtime::Builder::new_multi_thread()
                .worker_threads(1)
                .enable_all()
                .build()
                .unwrap();
            let config: SyncServerConfig = serde_json::from_value(json!({
                "host": "127.0.0.1", "port": 0, "base": folder.path(),
            }))
            .unwrap();
            let addr = rt.block_on(async {
                let (addr, server) = SimpleServer::make_server(config).await.unwrap();
                tokio::spawn(server);
                addr
            });
            let endpoint = format!("http://{addr}/");
            let mut auth = rt
                .block_on(sync_login(
                    "user",
                    "pass",
                    Some(endpoint.clone()),
                    reqwest::Client::new(),
                ))
                .unwrap();
            auth.endpoint = Some(endpoint.parse().unwrap());
            Self {
                _folder: folder,
                rt,
                auth,
            }
        }

        /// Merges both ways; fails if the engine asks for a full sync.
        fn sync(&self, col: &mut Collection) {
            let output = self
                .rt
                .block_on(col.normal_sync(self.auth.clone(), reqwest::Client::new()))
                .unwrap();
            use anki::sync::collection::normal::SyncActionRequired as Required;
            assert!(
                matches!(
                    output.required,
                    Required::NoChanges | Required::NormalSyncRequired
                ),
                "needed {:?}",
                output.required
            );
        }

        fn upload(&self, col: Collection) {
            self.rt
                .block_on(col.full_upload(self.auth.clone(), reqwest::Client::new()))
                .unwrap();
        }

        fn download(&self, col: Collection) {
            self.rt
                .block_on(col.full_download(self.auth.clone(), reqwest::Client::new()))
                .unwrap();
        }
    }

    /// A reader and another device, both holding the cloud's collection.
    fn two_devices(cloud: &Cloud, notes: &[(&str, &str)]) -> (TempDir, TempDir, Vec<NoteId>) {
        let (reader, other) = (tempdir().unwrap(), tempdir().unwrap());
        let mut col = collection(reader.path());
        let ids = notes
            .iter()
            .map(|(front, back)| add_note(&mut col, "Basic", &[front, back]))
            .collect();
        cloud.upload(col);
        cloud.download(collection(other.path()));
        (reader, other, ids)
    }

    // Sync compares whole-second modification times
    fn next_second() {
        std::thread::sleep(std::time::Duration::from_millis(1100));
    }

    #[test]
    fn queued_changes_reach_another_device_without_a_full_sync() {
        let cloud = Cloud::start();
        let (reader, other, ids) = two_devices(&cloud, &[("Q1", "A"), ("Q2", "A"), ("Q3", "A")]);
        let (mut reader_col, mut other_col) = (collection(reader.path()), collection(other.path()));
        queue(
            reader.path(),
            &[
                edit("e", ids[0], "Q1", "A", "Q1 edited"),
                json!({"id": "s", "op": "suspend", "note_id": ids[1].0, "expect_front": "Q2", "expect_back": "A"}),
                json!({"id": "d", "op": "delete", "note_id": ids[2].0, "expect_front": "Q3", "expect_back": "A"}),
            ],
        );
        apply_changes(&mut reader_col, reader.path()).unwrap();
        // Waiting for sync: the desktop app reads this marker as "on the reader"
        assert_eq!(
            reader_col.storage.get_note(ids[0]).unwrap().unwrap().usn.0,
            -1
        );

        cloud.sync(&mut reader_col);
        cloud.sync(&mut other_col);

        assert_ne!(
            reader_col.storage.get_note(ids[0]).unwrap().unwrap().usn.0,
            -1
        );
        assert_eq!(fields(&other_col, ids[0]).0, "Q1 edited");
        assert!(suspended(&mut other_col, ids[1]));
        assert!(other_col.storage.get_note(ids[2]).unwrap().is_none());
    }

    #[test]
    fn the_newest_edit_of_a_note_wins_the_whole_note() {
        let cloud = Cloud::start();
        let (reader, other, ids) = two_devices(&cloud, &[("Q", "A")]);
        let (mut reader_col, mut other_col) = (collection(reader.path()), collection(other.path()));
        queue(
            reader.path(),
            &[edit("e", ids[0], "Q", "A", "Reader question")],
        );
        apply_changes(&mut reader_col, reader.path()).unwrap();
        next_second();
        let mut note = other_col.storage.get_note(ids[0]).unwrap().unwrap();
        note.set_field(1, "Other device answer").unwrap();
        other_col.update_note(&mut note).unwrap();

        cloud.sync(&mut reader_col);
        cloud.sync(&mut other_col);
        cloud.sync(&mut reader_col);

        // Not merged by side: the reader's question is gone
        let want = ("Q".to_string(), "Other device answer".to_string());
        assert_eq!(fields(&reader_col, ids[0]), want);
        assert_eq!(fields(&other_col, ids[0]), want);
    }

    #[test]
    fn a_delete_beats_a_later_edit_elsewhere() {
        let cloud = Cloud::start();
        let (reader, other, ids) = two_devices(&cloud, &[("Q", "A")]);
        let (mut reader_col, mut other_col) = (collection(reader.path()), collection(other.path()));
        queue(
            reader.path(),
            &[
                json!({"id": "d", "op": "delete", "note_id": ids[0].0, "expect_front": "Q", "expect_back": "A"}),
            ],
        );
        apply_changes(&mut reader_col, reader.path()).unwrap();
        next_second();
        let mut note = other_col.storage.get_note(ids[0]).unwrap().unwrap();
        note.set_field(0, "Edited elsewhere").unwrap();
        other_col.update_note(&mut note).unwrap();

        cloud.sync(&mut reader_col);
        cloud.sync(&mut other_col);
        cloud.sync(&mut reader_col);

        assert!(reader_col.storage.get_note(ids[0]).unwrap().is_none());
        assert!(other_col.storage.get_note(ids[0]).unwrap().is_none());
    }

    #[test]
    fn a_later_card_change_elsewhere_undoes_a_suspend() {
        let cloud = Cloud::start();
        let (reader, other, ids) = two_devices(&cloud, &[("Q", "A")]);
        let (mut reader_col, mut other_col) = (collection(reader.path()), collection(other.path()));
        queue(
            reader.path(),
            &[
                json!({"id": "s", "op": "suspend", "note_id": ids[0].0, "expect_front": "Q", "expect_back": "A"}),
            ],
        );
        apply_changes(&mut reader_col, reader.path()).unwrap();
        next_second();
        review_card(&mut other_col, ids[0]);

        cloud.sync(&mut reader_col);
        cloud.sync(&mut other_col);
        cloud.sync(&mut reader_col);

        assert!(!suspended(&mut reader_col, ids[0]));
        assert!(!suspended(&mut other_col, ids[0]));
    }

    #[test]
    fn a_full_download_replaces_applied_changes_and_later_changes_still_run() {
        let cloud = Cloud::start();
        let (reader, _other, ids) = two_devices(&cloud, &[("Q", "A")]);
        let mut reader_col = collection(reader.path());
        queue(reader.path(), &[edit("e", ids[0], "Q", "A", "Edited")]);
        apply_changes(&mut reader_col, reader.path()).unwrap();
        fs::write(
            reader.path().join("inbox.jsonl"),
            "{\"front\":\"New\",\"back\":\"Card\",\"book\":\"B\"}\n",
        )
        .unwrap();
        import_inbox(&mut reader_col, reader.path()).unwrap();
        let waiting = edit("later", ids[0], "Q", "A", "Edited after download");

        cloud.download(reader_col);
        let mut reader_col = collection(reader.path());
        assert_eq!(fields(&reader_col, ids[0]).0, "Q");
        assert!(reader_col.search_notes_unordered("New").unwrap().is_empty());

        restore_unsynced(reader.path()).unwrap();
        import_inbox(&mut reader_col, reader.path()).unwrap();
        queue(reader.path(), &[waiting]);
        apply_changes(&mut reader_col, reader.path()).unwrap();

        assert_eq!(reader_col.search_notes_unordered("New").unwrap().len(), 1);
        assert_eq!(fields(&reader_col, ids[0]).0, "Edited after download");
    }
}
