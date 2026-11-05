//! Runs the reader's own code against a throwaway collection, for tests of the whole path from
//! the desktop app to AnkiWeb. It never touches a real device: every path is an argument.
//!
//! Usage: cargo run -p dev --bin reader_sim -- <command>
//!
//!   serve <folder>                        Anki's sync server; prints its address, runs until killed
//!   create <app dir> <notes json>         a new collection; prints the note ids
//!   open <app dir>                        what opening pbAnki does: import the inbox, apply changes
//!   edit <app dir> <note id> <front>      change a note's question, as Anki would
//!   review <app dir> <note id>            answer a note's card once
//!   remove <app dir> <note id>            delete a note, as Anki would
//!   upload|download|sync <app dir> <url>  sync with the server
//!   dump <app dir>                        the notes and cards as JSON

use std::path::{Path, PathBuf};

use anki::card::CardId;
use anki::collection::Collection;
use anki::decks::DeckId;
use anki::notes::NoteId;
use anki::scheduler::answering::{CardAnswer, Rating};
use anki::search::SortMode;
use anki::sync::http_server::{SimpleServer, SyncServerConfig};
use anki::sync::login::{SyncAuth, sync_login};
use anki::timestamp::TimestampMillis;
use serde_json::{Value, json};

fn open(dir: &str) -> Collection {
    let folder = Path::new(dir).join("collection");
    std::fs::create_dir_all(&folder).unwrap();
    let path: PathBuf = folder.join("collection.anki2");
    common::collection::open_collection(&path).unwrap()
}

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap()
}

async fn login(url: &str) -> SyncAuth {
    let mut auth = sync_login(
        "user",
        "pass",
        Some(url.to_string()),
        reqwest::Client::new(),
    )
    .await
    .unwrap();
    auth.endpoint = Some(url.parse().unwrap());
    auth
}

fn serve(folder: &str) {
    // Safety: nothing else runs yet
    unsafe { std::env::set_var("SYNC_USER1", "user:pass") };
    let config: SyncServerConfig = serde_json::from_value(json!({
        "host": "127.0.0.1", "port": 0, "base": folder,
    }))
    .unwrap();
    runtime().block_on(async {
        let (addr, server) = SimpleServer::make_server(config).await.unwrap();
        println!("http://{addr}/");
        server.await.unwrap();
    });
}

fn create(dir: &str, notes: &str) {
    let mut col = open(dir);
    let notes: Vec<Value> = serde_json::from_str(notes).unwrap();
    let mut ids = vec![];
    for item in notes {
        let name = item["notetype"].as_str().unwrap_or("Basic");
        let notetype = col.get_notetype_by_name(name).unwrap().unwrap();
        let mut note = notetype.new_note();
        note.set_field(0, item["front"].as_str().unwrap()).unwrap();
        note.set_field(1, item["back"].as_str().unwrap_or(""))
            .unwrap();
        col.add_note(&mut note, DeckId(1)).unwrap();
        ids.push(note.id.0);
    }
    println!("{}", json!(ids));
}

fn apply(dir: &str) {
    let mut col = open(dir);
    let imported = common::inbox::import_inbox(&mut col, Path::new(dir)).unwrap();
    let summary = common::changes::apply_changes(&mut col, Path::new(dir)).unwrap();
    println!(
        "{}",
        json!({"imported": imported.added, "applied": summary.applied, "refused": summary.refused, "notice": summary.notice()})
    );
}

fn edit(dir: &str, id: &str, front: &str) {
    let mut col = open(dir);
    let mut note = col
        .storage
        .get_note(NoteId(id.parse().unwrap()))
        .unwrap()
        .unwrap();
    note.set_field(0, front).unwrap();
    col.update_note(&mut note).unwrap();
}

fn review(dir: &str, id: &str) {
    let mut col = open(dir);
    let ids: Vec<CardId> = col
        .search_cards(&format!("nid:{id}"), SortMode::NoOrder)
        .unwrap();
    let states = col.get_scheduling_states(ids[0]).unwrap();
    let mut answer = CardAnswer {
        card_id: ids[0],
        current_state: states.current,
        new_state: states.good,
        rating: Rating::Good,
        answered_at: TimestampMillis::now(),
        milliseconds_taken: 1000,
        custom_data: None,
        from_queue: false,
    };
    col.answer_card(&mut answer).unwrap();
}

fn remove(dir: &str, id: &str) {
    open(dir)
        .remove_notes(&[NoteId(id.parse().unwrap())])
        .unwrap();
}

fn cloud(command: &str, dir: &str, url: &str) {
    let col = open(dir);
    let rt = runtime();
    rt.block_on(async {
        let auth = login(url).await;
        let client = reqwest::Client::new();
        match command {
            "upload" => col.full_upload(auth, client).await.unwrap(),
            "download" => col.full_download(auth, client).await.unwrap(),
            _ => {
                let mut col = col;
                let output = col.normal_sync(auth, client).await.unwrap();
                println!("{:?}", output.required);
            }
        }
    });
}

fn dump(dir: &str) {
    let mut col = open(dir);
    let suspended = col.search_cards("is:suspended", SortMode::NoOrder).unwrap();
    let new = col.search_cards("is:new", SortMode::NoOrder).unwrap();
    let mut rows = vec![];
    for id in col.search_cards("", SortMode::NoOrder).unwrap() {
        let card = col.storage.get_card(id).unwrap().unwrap();
        let note = col.storage.get_note(card.note_id()).unwrap().unwrap();
        rows.push(json!({
            "id": note.id.0, "front": note.fields()[0], "back": note.fields()[1], "usn": note.usn.0,
            "suspended": suspended.contains(&id), "new": new.contains(&id),
            "revlog": col.card_stats(id).unwrap().revlog.len(),
        }));
    }
    println!("{}", Value::Array(rows));
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    match args.as_slice() {
        ["serve", folder] => serve(folder),
        ["create", dir, notes] => create(dir, notes),
        ["open", dir] => apply(dir),
        ["edit", dir, id, front] => edit(dir, id, front),
        ["review", dir, id] => review(dir, id),
        ["remove", dir, id] => remove(dir, id),
        [command @ ("upload" | "download" | "sync"), dir, url] => cloud(command, dir, url),
        ["dump", dir] => dump(dir),
        _ => panic!("usage: see the top of reader_sim.rs"),
    }
}
