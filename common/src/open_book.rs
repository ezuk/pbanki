//! Opens a KOReader card's book at its highlight.
//!
//! pbAnki leaves a jump request next to the KOReader inbox, then asks the
//! platform to open the book in KOReader. The pbAnki KOReader plugin moves to
//! the position when that book opens or KOReader comes to the front.

use std::fs;
use std::path::Path;

use anki::timestamp::TimestampSecs;

pub const REQUEST_FILE: &str = "open-in-book.json";

/// Where a card's highlight is: the book on the device and KOReader's position.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BookLocation {
    pub file: String,
    pub position: String,
}

impl BookLocation {
    /// Location from a card's File and Position fields, when the book still exists.
    pub fn from_fields(file: &str, position: &str) -> Option<Self> {
        let file = anki::text::decode_entities(file.trim()).into_owned();
        let position = anki::text::decode_entities(position.trim()).into_owned();
        (!file.is_empty() && !position.is_empty() && Path::new(&file).is_file())
            .then_some(Self { file, position })
    }
}

/// Saves the jump request for the KOReader plugin.
pub fn write_request(app_dir: &Path, location: &BookLocation) -> Result<(), String> {
    let request = serde_json::json!({
        "file": location.file,
        "position": location.position,
        "time": TimestampSecs::now().0,
    });
    fs::write(app_dir.join(REQUEST_FILE), request.to_string())
        .map_err(|e| format!("Cannot save the jump request: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn needs_an_existing_book_and_a_position() {
        let dir = tempfile::tempdir().unwrap();
        let book = dir.path().join("Tom & Jerry.epub");
        fs::write(&book, "").unwrap();
        let escaped = book.to_string_lossy().replace('&', "&amp;");

        let location = BookLocation::from_fields(&escaped, "/body/p[3]/text().0").unwrap();
        assert_eq!(location.file, book.to_string_lossy());
        assert!(BookLocation::from_fields(&escaped, " ").is_none());
        assert!(BookLocation::from_fields("/missing.epub", "/body/p[3]").is_none());
    }

    #[test]
    fn writes_the_request() {
        let dir = tempfile::tempdir().unwrap();
        let location = BookLocation {
            file: "/mnt/ext1/Book.epub".into(),
            position: "12".into(),
        };
        write_request(dir.path(), &location).unwrap();
        let saved: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(dir.path().join(REQUEST_FILE)).unwrap())
                .unwrap();
        assert_eq!(saved["file"], "/mnt/ext1/Book.epub");
        assert_eq!(saved["position"], "12");
        assert!(saved["time"].as_i64().unwrap() > 0);
    }
}
