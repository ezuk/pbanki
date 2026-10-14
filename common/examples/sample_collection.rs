//! Creates a small Anki collection for manual testing.
//!
//! Usage: cargo run -p common --example sample_collection -- <output dir>

use std::path::PathBuf;

use anki::collection::CollectionBuilder;
use anki::prelude::I18n;

const LONG_TEXT: &str = "The Rhine is a European river that begins in the Swiss canton of \
Graubünden in the southeastern Swiss Alps. It forms part of the Swiss-Liechtenstein, \
Swiss-Austrian and Swiss-German borders, then flows through the German Rhineland and the \
Netherlands, and eventually empties into the North Sea. It drains an area of about 185,000 \
square kilometres. The river has been a vital navigable waterway since Roman times, carrying \
trade and goods deep inland, and it has served as a defensive frontier as well. Castles and \
fortifications along its banks, especially in the Middle Rhine valley, are a reminder of its \
importance. Today it is one of the busiest waterways in the world, linking the industrial \
centres of Switzerland, Germany, France and the Netherlands with the port of Rotterdam.";

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let dir: PathBuf = std::env::args()
        .nth(1)
        .ok_or("usage: sample_collection <output dir>")?
        .into();
    std::fs::create_dir_all(&dir)?;
    let path = dir.join("collection.anki2");
    if path.exists() {
        return Err(format!("{} already exists", path.display()).into());
    }

    let mut col = CollectionBuilder::new(&path)
        .set_tr(I18n::new(&["en"]))
        .build()?;

    // (notetype, deck, fields)
    let notes: &[(&str, &str, &[&str])] = &[
        (
            "Basic",
            "pbAnki Test::Basics",
            &["What is the capital of France?", "Paris"],
        ),
        (
            "Basic",
            "pbAnki Test::Basics",
            &[
                "<b>Bold</b>, <i>italic</i> and<br>a line break",
                "HTML is stripped",
            ],
        ),
        (
            "Basic",
            "pbAnki Test::Basics",
            &["Describe the Rhine", LONG_TEXT],
        ),
        (
            "Basic",
            "pbAnki Test::Languages",
            &[
                "Grüß Gott — où est la gare? Ça va?",
                "German and French accents",
            ],
        ),
        (
            "Basic",
            "pbAnki Test::Languages",
            &["Здравствуйте", "Hello (Russian, formal)"],
        ),
        (
            "Basic (type in the answer)",
            "pbAnki Test::Card types",
            &["2 + 2 = ?", "4"],
        ),
        (
            "Cloze",
            "pbAnki Test::Card types",
            &["The {{c1::Seine}} flows through {{c2::Paris}}.", ""],
        ),
    ];

    for (notetype_name, deck_name, fields) in notes {
        let notetype = col
            .get_notetype_by_name(notetype_name)?
            .ok_or_else(|| format!("missing notetype {notetype_name}"))?;
        let deck = col.get_or_create_normal_deck(deck_name)?;
        let mut note = notetype.new_note();
        for (index, field) in fields.iter().enumerate() {
            note.set_field(index, *field)?;
        }
        col.add_note(&mut note, deck.id)?;
    }

    col.close(None)?;
    println!("Wrote {}", path.display());
    Ok(())
}
