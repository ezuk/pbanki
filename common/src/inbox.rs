//! Imports cards queued by the KOReader plugin.
//!
//! The plugin appends one JSON object per line to `inbox.jsonl`. Import renames the
//! file before reading it, so cards appended during an import land in a fresh inbox.

use std::fs;
use std::io::Write;
use std::path::Path;
use std::sync::Arc;

use anki::collection::Collection;
use anki::notes::Note;
use anki::notetype::{NoteField, Notetype, NotetypeId};
use serde::Deserialize;

pub const INBOX_FILE: &str = "inbox.jsonl";
const IMPORTING_FILE: &str = "inbox.importing.jsonl";
const FAILED_FILE: &str = "inbox.failed.jsonl";

pub const DECK_NAME: &str = "KOReader";
pub const TAG: &str = "koreader";
pub const NOTETYPE_NAME: &str = "pbAnki KOReader";

/// Fields of the KOReader note type, after Basic's Front and Back.
pub const CONTEXT_FIELDS: [&str; 7] = [
    "Quote", "Book", "Author", "Chapter", "Page", "File", "Position",
];

#[derive(Debug, Default, Deserialize)]
#[serde(default)]
pub struct InboxCard {
    pub front: String,
    pub back: String,
    /// Full highlighted text
    pub quote: String,
    pub book: String,
    pub author: String,
    pub chapter: String,
    pub page: Option<u32>,
    /// Book path on the device
    pub file: String,
    /// KOReader position (xpointer) of the highlight
    pub position: String,
}

impl InboxCard {
    fn field_values(&self) -> [String; 9] {
        [
            text_to_html(&self.front),
            text_to_html(&self.back),
            text_to_html(&self.quote),
            escape_html(&self.book),
            escape_html(&self.author),
            escape_html(&self.chapter),
            self.page.map(|p| p.to_string()).unwrap_or_default(),
            escape_html(&self.file),
            escape_html(&self.position),
        ]
    }
}

fn escape_html(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

fn text_to_html(text: &str) -> String {
    escape_html(text.trim()).replace('\n', "<br>")
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct ImportSummary {
    pub added: usize,
    pub failed: usize,
}

/// Adds every queued card to the collection and clears the inbox.
pub fn import_inbox(col: &mut Collection, dir: &Path) -> Result<ImportSummary, String> {
    let inbox = dir.join(INBOX_FILE);
    let importing = dir.join(IMPORTING_FILE);

    // Retry a previous import that was interrupted before it finished
    if !importing.exists() {
        if !inbox.exists() {
            return Ok(ImportSummary::default());
        }
        fs::rename(&inbox, &importing).map_err(|e| format!("Cannot claim inbox: {e}"))?;
    }

    let contents = fs::read_to_string(&importing).map_err(|e| format!("Cannot read inbox: {e}"))?;

    let notetype = koreader_notetype(col)?;
    let deck_id = col
        .get_or_create_normal_deck(DECK_NAME)
        .map_err(|e| format!("Cannot create deck {DECK_NAME}: {e}"))?
        .id;

    let mut summary = ImportSummary::default();
    let mut failed_lines = Vec::new();

    for line in contents.lines().filter(|l| !l.trim().is_empty()) {
        let added = serde_json::from_str::<InboxCard>(line)
            .map_err(|e| e.to_string())
            .and_then(|card| {
                if card.front.trim().is_empty() {
                    return Err("empty front".into());
                }
                let mut note = notetype.new_note();
                for (index, value) in card.field_values().into_iter().enumerate() {
                    note.set_field(index, value).map_err(|e| e.to_string())?;
                }
                note.tags.push(TAG.into());
                col.add_note(&mut note, deck_id).map_err(|e| e.to_string())
            });

        match added {
            Ok(_) => summary.added += 1,
            Err(e) => {
                eprintln!("Skipping inbox line ({e}): {line}");
                failed_lines.push(line);
                summary.failed += 1;
            }
        }
    }

    if !failed_lines.is_empty() {
        append_lines(&dir.join(FAILED_FILE), &failed_lines)
            .map_err(|e| format!("Cannot save failed inbox lines: {e}"))?;
    }
    fs::remove_file(&importing).map_err(|e| format!("Cannot clear inbox: {e}"))?;

    Ok(summary)
}

/// The KOReader note type, created from Basic on first use.
pub fn koreader_notetype(col: &mut Collection) -> Result<Arc<Notetype>, String> {
    if let Some(notetype) = col
        .get_notetype_by_name(NOTETYPE_NAME)
        .map_err(|e| e.to_string())?
    {
        return Ok(notetype);
    }

    let basic = col
        .get_notetype_by_name("Basic")
        .map_err(|e| e.to_string())?
        .ok_or("No Basic note type to base the KOReader note type on")?;

    let mut notetype = (*basic).clone();
    notetype.id = NotetypeId(0);
    notetype.name = NOTETYPE_NAME.into();
    notetype
        .fields
        .extend(CONTEXT_FIELDS.iter().map(|name| NoteField::new(*name)));
    for field in &mut notetype.fields {
        field.ord = None;
    }
    for template in &mut notetype.templates {
        template.ord = None;
    }
    col.add_notetype(&mut notetype, false)
        .map_err(|e| format!("Cannot create note type {NOTETYPE_NAME}: {e}"))?;

    col.get_notetype_by_name(NOTETYPE_NAME)
        .map_err(|e| e.to_string())?
        .ok_or_else(|| format!("Note type {NOTETYPE_NAME} missing after creation"))
}

/// Context of a KOReader card, if the note has one.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct CardContext {
    pub quote: String,
    pub book: String,
    pub author: String,
    pub chapter: String,
    pub page: String,
    pub file: String,
    pub position: String,
}

impl CardContext {
    pub fn from_note(notetype: &Notetype, note: &Note) -> Option<Self> {
        if notetype.name != NOTETYPE_NAME {
            return None;
        }
        let field = |name: &str| {
            notetype
                .fields
                .iter()
                .position(|f| f.name == name)
                .and_then(|i| note.fields().get(i).cloned())
                .unwrap_or_default()
        };
        let context = Self {
            quote: field("Quote"),
            book: field("Book"),
            author: field("Author"),
            chapter: field("Chapter"),
            page: field("Page"),
            file: field("File"),
            position: field("Position"),
        };
        (context.quote.trim() != "" || context.book.trim() != "").then_some(context)
    }

    /// Quote, rule, then book details, as HTML for the page renderer.
    pub fn to_html(&self) -> String {
        let mut html = String::new();
        if !self.quote.is_empty() {
            html.push_str(&format!("<i>“{}”</i><hr>", self.quote));
        }
        if !self.book.is_empty() {
            html.push_str(&format!("<b>{}</b><br>", self.book));
        }
        if !self.author.is_empty() {
            html.push_str(&format!("{}<br>", self.author));
        }
        let page = if self.page.is_empty() {
            String::new()
        } else {
            format!("p. {}", self.page)
        };
        let place: Vec<&str> = [self.chapter.as_str(), page.as_str()]
            .into_iter()
            .filter(|s| !s.is_empty())
            .collect();
        if !place.is_empty() {
            html.push_str(&place.join(" · "));
        }
        html
    }

    pub fn can_open(&self) -> bool {
        !self.file.is_empty()
    }
}

fn append_lines(path: &Path, lines: &[&str]) -> std::io::Result<()> {
    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    for line in lines {
        writeln!(file, "{line}")?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use anki::collection::CollectionBuilder;

    fn collection(dir: &Path) -> Collection {
        CollectionBuilder::new(dir.join("collection.anki2"))
            .build()
            .unwrap()
    }

    fn koreader_notes(col: &mut Collection) -> Vec<Vec<String>> {
        let ids = col
            .search_notes_unordered(&format!("deck:{DECK_NAME} tag:{TAG}"))
            .unwrap();
        let mut notes: Vec<_> = ids
            .into_iter()
            .map(|id| col.storage.get_note(id).unwrap().unwrap().fields().clone())
            .collect();
        notes.sort();
        notes
    }

    #[test]
    fn no_inbox_is_a_no_op() {
        let dir = tempfile::tempdir().unwrap();
        let mut col = collection(dir.path());
        assert_eq!(
            import_inbox(&mut col, dir.path()).unwrap(),
            ImportSummary::default()
        );
        assert!(col.get_deck_id(DECK_NAME).unwrap().is_none());
    }

    #[test]
    fn imports_cards_with_context_and_clears_inbox() {
        let dir = tempfile::tempdir().unwrap();
        let mut col = collection(dir.path());
        fs::write(
            dir.path().join(INBOX_FILE),
            concat!(
                r#"{"front":"Who says this?","back":"Hamlet","quote":"To be, or not to be","book":"Hamlet","author":"Shakespeare","chapter":"Act III","page":57,"file":"/mnt/ext1/Hamlet.epub","position":"/body/p[3]"}"#,
                "\n",
                r#"{"front":"a < b & c"}"#,
                "\n\n",
            ),
        )
        .unwrap();

        let summary = import_inbox(&mut col, dir.path()).unwrap();
        assert_eq!(
            summary,
            ImportSummary {
                added: 2,
                failed: 0
            }
        );
        assert!(!dir.path().join(INBOX_FILE).exists());
        assert!(!dir.path().join(IMPORTING_FILE).exists());

        assert_eq!(
            koreader_notes(&mut col),
            vec![
                vec![
                    "Who says this?",
                    "Hamlet",
                    "To be, or not to be",
                    "Hamlet",
                    "Shakespeare",
                    "Act III",
                    "57",
                    "/mnt/ext1/Hamlet.epub",
                    "/body/p[3]",
                ],
                vec!["a &lt; b &amp; c", "", "", "", "", "", "", "", ""],
            ]
        );
    }

    #[test]
    fn reuses_the_note_type() {
        let dir = tempfile::tempdir().unwrap();
        let mut col = collection(dir.path());
        let first = koreader_notetype(&mut col).unwrap();
        let second = koreader_notetype(&mut col).unwrap();
        assert_eq!(first.id, second.id);
        let names: Vec<_> = first.fields.iter().map(|f| f.name.as_str()).collect();
        assert_eq!(&names[..2], ["Front", "Back"]);
        assert_eq!(&names[2..], CONTEXT_FIELDS);
    }

    #[test]
    fn keeps_bad_lines_in_failed_file() {
        let dir = tempfile::tempdir().unwrap();
        let mut col = collection(dir.path());
        fs::write(
            dir.path().join(INBOX_FILE),
            "not json\n{\"front\":\"  \"}\n{\"front\":\"ok\"}\n",
        )
        .unwrap();

        let summary = import_inbox(&mut col, dir.path()).unwrap();
        assert_eq!(
            summary,
            ImportSummary {
                added: 1,
                failed: 2
            }
        );
        assert_eq!(
            fs::read_to_string(dir.path().join(FAILED_FILE)).unwrap(),
            "not json\n{\"front\":\"  \"}\n"
        );
    }

    #[test]
    fn resumes_interrupted_import() {
        let dir = tempfile::tempdir().unwrap();
        let mut col = collection(dir.path());
        fs::write(dir.path().join(IMPORTING_FILE), "{\"front\":\"old\"}\n").unwrap();
        fs::write(dir.path().join(INBOX_FILE), "{\"front\":\"new\"}\n").unwrap();

        assert_eq!(import_inbox(&mut col, dir.path()).unwrap().added, 1);
        assert!(dir.path().join(INBOX_FILE).exists());
        assert_eq!(import_inbox(&mut col, dir.path()).unwrap().added, 1);
        assert_eq!(koreader_notes(&mut col).len(), 2);
    }

    #[test]
    fn multiline_text_becomes_line_breaks() {
        let card: InboxCard =
            serde_json::from_str(r#"{"front":"x","back":"line 1\nline 2"}"#).unwrap();
        assert_eq!(card.field_values()[1], "line 1<br>line 2");
    }

    #[test]
    fn context_formats_quote_and_source() {
        let context = CardContext {
            quote: "To be".into(),
            book: "Hamlet".into(),
            author: "Shakespeare".into(),
            chapter: "Act III".into(),
            page: "57".into(),
            ..Default::default()
        };
        assert_eq!(
            context.to_html(),
            "<i>“To be”</i><hr><b>Hamlet</b><br>Shakespeare<br>Act III · p. 57"
        );
        assert!(!context.can_open());
    }
}
