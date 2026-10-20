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
use anki::notetype::{NoteField, Notetype, NotetypeId, NotetypeKind};
use serde::Deserialize;

pub const INBOX_FILE: &str = "inbox.jsonl";
const IMPORTING_FILE: &str = "inbox.importing.jsonl";
const FAILED_FILE: &str = "inbox.failed.jsonl";

pub const DECK_NAME: &str = "KOReader";
pub const TAG: &str = "koreader";
pub const NOTETYPE_NAME: &str = "pbAnki KOReader";
pub const CLOZE_NOTETYPE_NAME: &str = "pbAnki KOReader Cloze";

/// Fields of the KOReader note types, after the two fields of Basic or Cloze.
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
    /// Whether the front has a cloze deletion such as `{{c1::answer}}`.
    pub fn is_cloze(&self) -> bool {
        has_cloze(&self.front)
    }

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

/// Whether `text` has a cloze deletion such as `{{c1::answer}}`.
pub fn has_cloze(text: &str) -> bool {
    text.match_indices("{{c").any(|(i, _)| {
        let rest = &text[i + 3..];
        let digits = rest.chars().take_while(char::is_ascii_digit).count();
        digits > 0 && rest[digits..].starts_with("::")
    })
}

pub(crate) fn text_to_html(text: &str) -> String {
    escape_html(text.trim()).replace('\n', "<br>")
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct ImportSummary {
    pub added: usize,
    pub failed: usize,
}

/// Deck for a card from `book`: a sub-deck of KOReader named after the book.
pub fn deck_name(book: &str) -> String {
    // "::" would nest decks further
    let title = book
        .replace("::", ":")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    if title.is_empty() {
        DECK_NAME.into()
    } else {
        format!("{DECK_NAME}::{title}")
    }
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

    let mut basic = None;
    let mut cloze = None;
    let mut deck_ids = std::collections::HashMap::new();

    let mut summary = ImportSummary::default();
    let mut failed_lines = Vec::new();

    for line in contents.lines().filter(|l| !l.trim().is_empty()) {
        let added = serde_json::from_str::<InboxCard>(line)
            .map_err(|e| e.to_string())
            .and_then(|card| {
                if card.front.trim().is_empty() {
                    return Err("empty front".into());
                }
                let notetype = if card.is_cloze() {
                    cached_notetype(&mut cloze, || koreader_cloze_notetype(col))?
                } else {
                    cached_notetype(&mut basic, || koreader_notetype(col))?
                };
                let mut note = notetype.new_note();
                for (index, value) in card.field_values().into_iter().enumerate() {
                    note.set_field(index, value).map_err(|e| e.to_string())?;
                }
                note.tags.push(TAG.into());
                let deck_name = deck_name(&card.book);
                let deck_id = match deck_ids.get(&deck_name) {
                    Some(&id) => id,
                    None => {
                        let id = col
                            .get_or_create_normal_deck(&deck_name)
                            .map_err(|e| format!("Cannot create deck {deck_name}: {e}"))?
                            .id;
                        deck_ids.insert(deck_name, id);
                        id
                    }
                };
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

fn cached_notetype(
    cache: &mut Option<Arc<Notetype>>,
    load: impl FnOnce() -> Result<Arc<Notetype>, String>,
) -> Result<Arc<Notetype>, String> {
    if cache.is_none() {
        *cache = Some(load()?);
    }
    Ok(cache.clone().unwrap())
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
    add_koreader_notetype(col, &basic, NOTETYPE_NAME)
}

/// The KOReader cloze note type, created from Cloze on first use.
pub fn koreader_cloze_notetype(col: &mut Collection) -> Result<Arc<Notetype>, String> {
    if let Some(notetype) = col
        .get_notetype_by_name(CLOZE_NOTETYPE_NAME)
        .map_err(|e| e.to_string())?
    {
        return Ok(notetype);
    }
    let cloze = match col
        .get_notetype_by_name("Cloze")
        .map_err(|e| e.to_string())?
    {
        Some(cloze) if cloze.config.kind() == NotetypeKind::Cloze => cloze,
        // Renamed: any cloze note type with the stock template
        _ => col
            .get_all_notetypes()
            .map_err(|e| e.to_string())?
            .into_iter()
            .find(|n| {
                n.config.kind() == NotetypeKind::Cloze
                    && n.name != CLOZE_NOTETYPE_NAME
                    && n.fields.len() >= 2
            })
            .ok_or("No Cloze note type to base the KOReader cloze note type on")?,
    };
    add_koreader_notetype(col, &cloze, CLOZE_NOTETYPE_NAME)
}

/// Adds a copy of `base` named `name` with the context fields after its first two.
fn add_koreader_notetype(
    col: &mut Collection,
    base: &Notetype,
    name: &str,
) -> Result<Arc<Notetype>, String> {
    let mut notetype = base.clone();
    notetype.id = NotetypeId(0);
    notetype.name = name.into();
    notetype.fields.truncate(2);
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
        .map_err(|e| format!("Cannot create note type {name}: {e}"))?;

    col.get_notetype_by_name(name)
        .map_err(|e| e.to_string())?
        .ok_or_else(|| format!("Note type {name} missing after creation"))
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
        if notetype.name != NOTETYPE_NAME && notetype.name != CLOZE_NOTETYPE_NAME {
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

    /// Where the highlight is, when the book is still on the device.
    pub fn book_location(&self) -> Option<crate::open_book::BookLocation> {
        crate::open_book::BookLocation::from_fields(&self.file, &self.position)
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
    fn names_a_sub_deck_per_book() {
        assert_eq!(
            deck_name("Pride and Prejudice"),
            "KOReader::Pride and Prejudice"
        );
        assert_eq!(
            deck_name("  Dune:: Part  One\n"),
            "KOReader::Dune: Part One"
        );
        assert_eq!(deck_name("  "), "KOReader");
    }

    #[test]
    fn files_cards_under_their_book() {
        let dir = tempfile::tempdir().unwrap();
        let mut col = collection(dir.path());
        fs::write(
            dir.path().join(INBOX_FILE),
            concat!(
                r#"{"front":"Q1","book":"Hamlet"}"#,
                "\n",
                r#"{"front":"Q2","book":"Hamlet"}"#,
                "\n",
                r#"{"front":"Q3"}"#,
                "\n",
            ),
        )
        .unwrap();
        import_inbox(&mut col, dir.path()).unwrap();

        let count = |col: &mut Collection, search: &str| {
            col.search_cards(search, anki::search::SortMode::NoOrder)
                .unwrap()
                .len()
        };
        assert_eq!(count(&mut col, "\"deck:KOReader::Hamlet\""), 2);
        assert_eq!(count(&mut col, "deck:KOReader -deck:KOReader::*"), 1);
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
    fn imports_cloze_cards_as_cloze_notes() {
        let dir = tempfile::tempdir().unwrap();
        let mut col = collection(dir.path());
        fs::write(
            dir.path().join(INBOX_FILE),
            concat!(
                r#"{"front":"Self-control needs the {{c1::pause-and-plan}} response.","back":"Not fight-or-flight","quote":"pause and plan","book":"Willpower"}"#,
                "\n",
                r#"{"front":"Plain {{question}}?","back":"Answer","book":"Willpower"}"#,
                "\n",
            ),
        )
        .unwrap();
        assert_eq!(import_inbox(&mut col, dir.path()).unwrap().added, 2);

        let cloze = koreader_cloze_notetype(&mut col).unwrap();
        assert_eq!(cloze.config.kind(), NotetypeKind::Cloze);
        let names: Vec<_> = cloze.fields.iter().map(|f| f.name.as_str()).collect();
        assert_eq!(&names[..2], ["Text", "Back Extra"]);
        assert_eq!(&names[2..], CONTEXT_FIELDS);

        let ids = col
            .search_notes_unordered(&format!("\"note:{CLOZE_NOTETYPE_NAME}\""))
            .unwrap();
        assert_eq!(ids.len(), 1);
        let note = col.storage.get_note(ids[0]).unwrap().unwrap();
        assert_eq!(
            note.fields()[0],
            "Self-control needs the {{c1::pause-and-plan}} response."
        );
        assert_eq!(note.fields()[2], "pause and plan");
        let context = CardContext::from_note(&cloze, &note).unwrap();
        assert_eq!(context.book, "Willpower");
        assert_eq!(
            col.search_cards(
                &format!("\"note:{CLOZE_NOTETYPE_NAME}\""),
                anki::search::SortMode::NoOrder
            )
            .unwrap()
            .len(),
            1
        );
    }

    #[test]
    fn detects_cloze_deletions() {
        assert!(has_cloze("The {{c1::Seine}} river"));
        assert!(has_cloze("{{c12::a::hint}}"));
        assert!(!has_cloze("{{c::a}}"));
        assert!(!has_cloze("{{Front}}"));
        assert!(!has_cloze("plain"));
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
