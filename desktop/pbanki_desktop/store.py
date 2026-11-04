"""SQLite store: the schema and every query the app runs.

One connection per thread. All values are bound as parameters. Functions that change
a missing row raise NotFound, and changing a card that is already saved raises
NotEditable, so a late edit can't quietly alter what went to the reader.
"""

import copy
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from . import paths

BUSY_TIMEOUT_SECONDS = 15

KINDS = ("qa", "cloze", "qonly", "def")
DRAFT = "draft"
REMOVED = "removed"
SAVED = "saved"

BACKED_UP = "backed_up"
WRITTEN = "written"
VERIFIED = "verified"

SOURCE_AI = "AI"
SOURCE_AI_EDITED = "AI, edited"
SOURCE_AI_REWRITTEN = "AI, rewritten"
SOURCE_YOU = "You"
SOURCE_COMPANION = "From the Book Companion"
SOURCE_COMPANION_EDITED = "From the Book Companion, edited"

DEFAULT_SETTINGS = {
    "assistants": {"claude": True, "codex": True},
    "model": "claude-sonnet-5-5",
    "meta_model": "claude-opus-5-5",
    "companion_send": "Overview + chapter",
    "sees": "600 words each side",
    "kind": "qa",
    "count": 3,
    "last_prompt": "Standard",
    "book_prompts": {},
    "suggest_connections": True,
    "suggest_ideas": True,
    "mark_connection_sources": True,
}

# Index i takes the database from version i to version i + 1
MIGRATIONS = [
    (
        """CREATE TABLE books(
            id TEXT PRIMARY KEY,
            device_path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT '',
            stem TEXT NOT NULL DEFAULT '',
            ext TEXT NOT NULL DEFAULT '',
            data_json TEXT NOT NULL DEFAULT '{}',
            last_seen INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE highlight_state(
            book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            hid TEXT NOT NULL,
            key TEXT NOT NULL,
            opened INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            later_research TEXT,
            chapter_notes_seen INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(book_id, hid)
        )""",
        """CREATE TABLE cards(
            id TEXT PRIMARY KEY,
            book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            hid TEXT NOT NULL,
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            front TEXT NOT NULL DEFAULT '',
            back TEXT NOT NULL DEFAULT '',
            suggested_front TEXT,
            suggested_back TEXT,
            position INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'draft',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""",
        "CREATE INDEX cards_by_highlight ON cards(book_id, hid, state, position)",
        """CREATE TABLE connections(
            id TEXT PRIMARY KEY,
            book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT '',
            front TEXT NOT NULL DEFAULT '',
            back TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            sources_json TEXT NOT NULL DEFAULT '[]',
            opened INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'draft',
            created_at REAL NOT NULL
        )""",
        "CREATE INDEX connections_by_book ON connections(book_id, state)",
        """CREATE TABLE ideas(
            id TEXT PRIMARY KEY,
            book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            chapter TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            quote TEXT NOT NULL DEFAULT '',
            front TEXT NOT NULL DEFAULT '',
            back TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            opened INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'draft',
            created_at REAL NOT NULL
        )""",
        "CREATE INDEX ideas_by_book ON ideas(book_id, state)",
        """CREATE TABLE chapter_notes(
            book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            chapter TEXT NOT NULL,
            big TEXT NOT NULL DEFAULT '',
            points_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY(book_id, chapter)
        )""",
        """CREATE TABLE save_batches(
            id TEXT PRIMARY KEY,
            book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            created_at REAL NOT NULL,
            status TEXT NOT NULL,
            inbox_json TEXT NOT NULL,
            reviewed_json TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            intended_inbox BLOB,
            intended_reviewed BLOB,
            error TEXT
        )""",
        "CREATE INDEX save_batches_by_status ON save_batches(status, created_at)",
        """CREATE TABLE saved_cards(
            batch_id TEXT NOT NULL REFERENCES save_batches(id) ON DELETE CASCADE,
            hid TEXT NOT NULL,
            position INTEGER NOT NULL,
            kind TEXT NOT NULL,
            front TEXT NOT NULL,
            back TEXT NOT NULL,
            source TEXT NOT NULL
        )""",
        "CREATE INDEX saved_cards_by_highlight ON saved_cards(hid, batch_id)",
        """CREATE TABLE runs(
            book_id TEXT PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            options_json TEXT NOT NULL DEFAULT '{}',
            progress_json TEXT NOT NULL DEFAULT '{}',
            updated_at REAL NOT NULL
        )""",
        """CREATE TABLE settings(
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        )""",
        """CREATE TABLE reader_reviewed(
            book_id TEXT NOT NULL,
            key TEXT NOT NULL,
            PRIMARY KEY (book_id, key)
        )""",
    ),
]

_CLOZE = re.compile(r"\{\{c\d+::")


def is_pending_card(card):
    """Whether `card` is an AI suggestion exactly as the AI wrote it."""
    suggested = card.get("suggested")
    return (card["source"] == SOURCE_AI and suggested is not None
            and card["front"] == suggested.get("front") and card["back"] == (suggested.get("back") or ""))


class StoreError(Exception):
    """The database can't be used. The message is shown to the user."""


class NotFound(KeyError):
    """No row with that id."""


class NotEditable(Exception):
    """The row is in a state that doesn't allow this change."""


def _digest(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def book_id_for(device_path):
    return _digest(device_path)


def hid_for(key):
    return _digest(key)


def new_id():
    return secrets.token_hex(8)


def was_edited(card):
    """Same as cardgen.wasEdited: answering a question the AI left blank is not an edit."""
    s = card.get("suggested")
    if s is None:
        return False
    if s["front"] != card["front"]:
        return True
    return s["back"] != card["back"] and (s["back"] != "" or bool(_CLOZE.search(s["front"])))


def _dumps(value):
    return json.dumps(value, ensure_ascii=False)


def _card(row):
    card = dict(row)
    front, back = card.pop("suggested_front"), card.pop("suggested_back")
    card["suggested"] = None if front is None else {"front": front, "back": back or ""}
    return card


def _connection(row):
    item = dict(row)
    item["sources"] = json.loads(item.pop("sources_json"))
    item["opened"] = bool(item["opened"])
    return item


def _idea(row):
    item = dict(row)
    item["opened"] = bool(item["opened"])
    return item


def _highlight(row):
    item = dict(row)
    for field in ("opened", "skipped", "chapter_notes_seen"):
        item[field] = bool(item[field])
    return item


def _book(row):
    book = dict(row)
    book["data"] = json.loads(book.pop("data_json"))
    return book


def _batch(row):
    batch = dict(row)
    batch["inbox_lines"] = json.loads(batch.pop("inbox_json"))
    batch["reviewed_lines"] = json.loads(batch.pop("reviewed_json"))
    batch["summary"] = json.loads(batch.pop("summary_json"))
    return batch


def _run(row):
    run = dict(row)
    run["options"] = json.loads(run.pop("options_json"))
    run["progress"] = json.loads(run.pop("progress_json"))
    return run


def _marks(values):
    return ",".join("?" for _ in values)


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else paths.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        conn = self._conn()
        try:
            self._prepare_file(conn)
        except BaseException:
            self.close()
            raise

    # Connections and transactions

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            self._local.depth = 0
        return conn

    def close(self):
        """Closes this thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def transaction(self):
        """All-or-nothing block holding the write lock. Inner blocks join the outer one."""
        conn = self._conn()
        if self._local.depth:
            self._local.depth += 1
            try:
                yield conn
            finally:
                self._local.depth -= 1
            return
        conn.execute("BEGIN IMMEDIATE")
        self._local.depth = 1
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        finally:
            self._local.depth = 0

    def _prepare_file(self, conn):
        # Switching to WAL can report busy at once, without honouring the busy timeout
        deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
        while True:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                self._migrate(conn)
                return
            except sqlite3.OperationalError as e:
                if e.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY or time.monotonic() > deadline:
                    raise
                time.sleep(0.05)

    def _migrate(self, conn):
        conn.execute("BEGIN EXCLUSIVE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise StoreError(
                    "This database was made by a newer version of pbAnki Desktop. Update the app to open it."
                )
            for number in range(version, len(MIGRATIONS)):
                for statement in MIGRATIONS[number]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version={number + 1:d}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def schema_version(self):
        return self._conn().execute("PRAGMA user_version").fetchone()[0]

    def _one(self, sql, args=()):
        return self._conn().execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        return self._conn().execute(sql, args).fetchall()

    def _write(self, sql, args=()):
        # SQLite can't store half an emoji, which an AI reply can hold, so it becomes "?"
        args = [a.encode("utf-8", "replace").decode("utf-8") if isinstance(a, str) else a for a in args]
        return self._conn().execute(sql, args).rowcount

    # Books

    def upsert_book(self, book):
        """Adds or updates a book from {device_path, title?, author?, stem?, ext?, data?}.
        Fields left out keep their stored values. Returns its id."""
        device_path = book["device_path"]
        book_id = book_id_for(device_path)
        pure = PurePosixPath(device_path)
        with self.transaction():
            row = self._one("SELECT * FROM books WHERE id = ?", (book_id,))
            old = dict(row) if row else {"title": pure.stem, "author": "", "stem": pure.stem,
                                         "ext": pure.suffix.lstrip("."), "data_json": "{}"}
            data = book.get("data")
            # An upsert, because replacing the row would delete its cards through the foreign keys
            self._write(
                "INSERT INTO books(id, device_path, title, author, stem, ext, data_json, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET title = excluded.title, "
                "author = excluded.author, stem = excluded.stem, ext = excluded.ext, "
                "data_json = excluded.data_json, last_seen = excluded.last_seen",
                (
                    book_id,
                    device_path,
                    book.get("title", old["title"]),
                    book.get("author", old["author"]),
                    book.get("stem", old["stem"]),
                    book.get("ext", old["ext"]),
                    _dumps(data) if data is not None else old["data_json"],
                    int(time.time()),
                ),
            )
        return book_id

    def get_book(self, book_id):
        row = self._one("SELECT * FROM books WHERE id = ?", (book_id,))
        return _book(row) if row else None

    def list_books(self):
        return [_book(r) for r in self._all("SELECT * FROM books ORDER BY last_seen DESC, title")]

    # Highlights

    def ensure_highlights(self, book_id, keys):
        """Makes sure these highlight keys have state rows. Returns {key: hid}."""
        hids = {key: hid_for(key) for key in keys}
        with self.transaction():
            for key, hid in hids.items():
                self._write(
                    "INSERT OR IGNORE INTO highlight_state(book_id, hid, key) VALUES (?, ?, ?)",
                    (book_id, hid, key),
                )
        return hids

    def get_highlight(self, book_id, hid):
        row = self._one("SELECT * FROM highlight_state WHERE book_id = ? AND hid = ?", (book_id, hid))
        return _highlight(row) if row else None

    def list_highlights(self, book_id):
        rows = self._all("SELECT * FROM highlight_state WHERE book_id = ?", (book_id,))
        return {row["hid"]: _highlight(row) for row in rows}

    def _set_highlight(self, sql, args):
        if not self._write(sql, args):
            raise NotFound(args[-1])

    def set_opened(self, book_id, hid, opened=True):
        self._set_highlight(
            "UPDATE highlight_state SET opened = ? WHERE book_id = ? AND hid = ?", (int(opened), book_id, hid)
        )

    def set_skipped(self, book_id, hid, skipped=True):
        self._set_highlight(
            "UPDATE highlight_state SET skipped = ? WHERE book_id = ? AND hid = ?", (int(skipped), book_id, hid)
        )

    def set_highlight_error(self, book_id, hid, error):
        self._set_highlight(
            "UPDATE highlight_state SET error = ? WHERE book_id = ? AND hid = ?", (error, book_id, hid)
        )

    def set_later_research(self, book_id, hid, text):
        self._set_highlight(
            "UPDATE highlight_state SET later_research = ? WHERE book_id = ? AND hid = ?", (text, book_id, hid)
        )

    def set_chapter_notes_seen(self, book_id, hid, seen=True):
        self._set_highlight(
            "UPDATE highlight_state SET chapter_notes_seen = ? WHERE book_id = ? AND hid = ?",
            (int(seen), book_id, hid),
        )

    # Cards

    def add_card(self, book_id, hid, kind="qa", front="", back="", source=None, suggested=None):
        """Adds a draft. `suggested` ({front, back}) is the AI's own text and makes the source AI."""
        if kind not in KINDS:
            raise ValueError(f"unknown card kind {kind}")
        if source is None:
            source = SOURCE_AI if suggested is not None else SOURCE_YOU
        now = time.time()
        card_id = new_id()
        with self.transaction():
            top = self._one(
                "SELECT COALESCE(MAX(position), -1) FROM cards WHERE book_id = ? AND hid = ?", (book_id, hid)
            )[0]
            self._write(
                "INSERT INTO cards(id, book_id, hid, kind, source, front, back, suggested_front, suggested_back, "
                "position, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    card_id,
                    book_id,
                    hid,
                    kind,
                    source,
                    front,
                    back,
                    suggested["front"] if suggested else None,
                    suggested["back"] if suggested else None,
                    top + 1,
                    DRAFT,
                    now,
                    now,
                ),
            )
        return self.get_card(card_id)

    def clear_suggested_cards(self, book_id, hids):
        """Deletes the AI cards nobody has changed from these highlights. Returns how many."""
        cleared = 0
        with self.transaction():
            for hid in hids:
                for card in self.list_cards(book_id, hid):
                    if is_pending_card(card):
                        self._write("DELETE FROM cards WHERE id = ?", (card["id"],))
                        cleared += 1
        return cleared

    def clear_suggested_connections(self, book_id):
        """Deletes the AI connections nobody has opened or changed."""
        self._write("DELETE FROM connections WHERE book_id = ? AND state = ? AND source = ? AND opened = 0",
                    (book_id, DRAFT, SOURCE_AI))

    def clear_suggested_ideas(self, book_id):
        """Deletes the key ideas nobody has opened or changed."""
        self._write("DELETE FROM ideas WHERE book_id = ? AND state = ? AND source = ? AND opened = 0",
                    (book_id, DRAFT, SOURCE_COMPANION))

    def get_card(self, card_id):
        row = self._one("SELECT * FROM cards WHERE id = ?", (card_id,))
        return _card(row) if row else None

    def _require_card(self, card_id):
        card = self.get_card(card_id)
        if card is None:
            raise NotFound(card_id)
        return card

    def update_card(self, card_id, front=None, back=None):
        """Edits a draft and keeps its source in step with cardgen.wasEdited."""
        with self.transaction():
            card = self._require_card(card_id)
            if card["state"] != DRAFT:
                raise NotEditable("This card can't be edited any more.")
            if front is not None:
                card["front"] = front
            if back is not None:
                card["back"] = back
            source = card["source"]
            if card["suggested"] is not None:
                if was_edited(card):
                    source = SOURCE_AI_EDITED
                elif source == SOURCE_AI_EDITED:
                    source = SOURCE_AI
            self._write(
                "UPDATE cards SET front = ?, back = ?, source = ?, updated_at = ? WHERE id = ?",
                (card["front"], card["back"], source, time.time(), card_id),
            )
        return self.get_card(card_id)

    def rewrite_card(self, card_id, front, back):
        """Replaces a draft with the AI's rewrite, which becomes the text later edits compare against."""
        with self.transaction():
            card = self._require_card(card_id)
            if card["state"] != DRAFT:
                raise NotEditable("This card can't be edited any more.")
            self._write(
                "UPDATE cards SET front = ?, back = ?, suggested_front = ?, suggested_back = ?, source = ?, "
                "updated_at = ? WHERE id = ?",
                (front, back, front, back, SOURCE_AI_REWRITTEN, time.time(), card_id),
            )
        return self.get_card(card_id)

    def remove_card(self, card_id):
        with self.transaction():
            card = self._require_card(card_id)
            if card["state"] == SAVED:
                raise NotEditable("This card is already saved.")
            self._write(
                "UPDATE cards SET state = ?, updated_at = ? WHERE id = ?", (REMOVED, time.time(), card_id)
            )
        return self.get_card(card_id)

    def restore_card(self, card_id):
        with self.transaction():
            card = self._require_card(card_id)
            if card["state"] == SAVED:
                raise NotEditable("This card is already saved.")
            self._write("UPDATE cards SET state = ?, updated_at = ? WHERE id = ?", (DRAFT, time.time(), card_id))
        return self.get_card(card_id)

    def list_cards(self, book_id, hid=None, states=(DRAFT,)):
        """Cards in position order, of one highlight or the whole book."""
        args = [book_id, *states]
        sql = f"SELECT * FROM cards WHERE book_id = ? AND state IN ({_marks(states)})"
        if hid is not None:
            sql += " AND hid = ?"
            args.append(hid)
        return [_card(r) for r in self._all(sql + " ORDER BY hid, position", args)]

    def existing_cards(self, book_id, hid):
        """The highlight's cards for cardgen's `existing`: {front, back, suggested}."""
        cards = []
        for card in self.list_cards(book_id, hid, (DRAFT, SAVED)):
            item = {"front": card["front"], "back": card["back"]}
            if card["suggested"] is not None:
                item["suggested"] = card["suggested"]
            cards.append(item)
        return cards

    def rejected_cards(self, book_id, hid):
        """AI suggestions removed from this highlight, for cardgen's `rejected`: {front, back}."""
        rows = self._all(
            "SELECT suggested_front, suggested_back FROM cards WHERE book_id = ? AND hid = ? AND state = ? "
            "AND suggested_front IS NOT NULL ORDER BY updated_at, position",
            (book_id, hid, REMOVED),
        )
        return [{"front": r["suggested_front"], "back": r["suggested_back"] or ""} for r in rows]

    def draft_counts(self):
        """Unsaved cards per book, {book_id: count}, leaving out blank ones."""
        rows = self._all(
            "SELECT book_id, COUNT(*) AS n FROM cards WHERE state = ? AND TRIM(front) != '' GROUP BY book_id",
            (DRAFT,),
        )
        return {r["book_id"]: r["n"] for r in rows}

    # Connections

    def add_connection(self, book_id, title, front, back, sources, source=SOURCE_AI):
        item_id = new_id()
        self._write(
            "INSERT INTO connections(id, book_id, title, front, back, source, sources_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, book_id, title, front, back, source, _dumps(list(sources)), time.time()),
        )
        return self.get_connection(item_id)

    def get_connection(self, item_id):
        row = self._one("SELECT * FROM connections WHERE id = ?", (item_id,))
        return _connection(row) if row else None

    def _require_connection(self, item_id):
        item = self.get_connection(item_id)
        if item is None:
            raise NotFound(item_id)
        return item

    def list_connections(self, book_id, states=(DRAFT,)):
        rows = self._all(
            f"SELECT * FROM connections WHERE book_id = ? AND state IN ({_marks(states)}) ORDER BY created_at, rowid",
            (book_id, *states),
        )
        return [_connection(r) for r in rows]

    def update_connection(self, item_id, front=None, back=None, title=None):
        with self.transaction():
            item = self._require_connection(item_id)
            if item["state"] != DRAFT:
                raise NotEditable("This connection can't be edited any more.")
            new = {
                "front": item["front"] if front is None else front,
                "back": item["back"] if back is None else back,
                "title": item["title"] if title is None else title,
            }
            changed = any(new[k] != item[k] for k in new)
            source = SOURCE_AI_EDITED if changed and item["source"] == SOURCE_AI else item["source"]
            self._write(
                "UPDATE connections SET front = ?, back = ?, title = ?, source = ? WHERE id = ?",
                (new["front"], new["back"], new["title"], source, item_id),
            )
        return self.get_connection(item_id)

    def open_connection(self, item_id, opened=True):
        if not self._write("UPDATE connections SET opened = ? WHERE id = ?", (int(opened), item_id)):
            raise NotFound(item_id)

    def remove_connection(self, item_id):
        self._change_state("connections", item_id, REMOVED)

    def restore_connection(self, item_id):
        self._change_state("connections", item_id, DRAFT)

    def _change_state(self, table, item_id, state):
        # `table` is one of two fixed names, never input
        if table not in ("connections", "ideas"):
            raise ValueError(table)
        with self.transaction():
            row = self._one(f"SELECT state FROM {table} WHERE id = ?", (item_id,))
            if row is None:
                raise NotFound(item_id)
            if row["state"] == SAVED:
                raise NotEditable("This is already saved.")
            self._write(f"UPDATE {table} SET state = ? WHERE id = ?", (state, item_id))

    # Key ideas

    def add_idea(self, book_id, chapter, title, note, quote, front, back, source=SOURCE_COMPANION):
        item_id = new_id()
        self._write(
            "INSERT INTO ideas(id, book_id, chapter, title, note, quote, front, back, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, book_id, chapter, title, note, quote, front, back, source, time.time()),
        )
        return self.get_idea(item_id)

    def get_idea(self, item_id):
        row = self._one("SELECT * FROM ideas WHERE id = ?", (item_id,))
        return _idea(row) if row else None

    def _require_idea(self, item_id):
        item = self.get_idea(item_id)
        if item is None:
            raise NotFound(item_id)
        return item

    def list_ideas(self, book_id, states=(DRAFT,)):
        rows = self._all(
            f"SELECT * FROM ideas WHERE book_id = ? AND state IN ({_marks(states)}) ORDER BY created_at, rowid",
            (book_id, *states),
        )
        return [_idea(r) for r in rows]

    def update_idea(self, item_id, front=None, back=None):
        with self.transaction():
            item = self._require_idea(item_id)
            if item["state"] != DRAFT:
                raise NotEditable("This key idea can't be edited any more.")
            new_front = item["front"] if front is None else front
            new_back = item["back"] if back is None else back
            changed = new_front != item["front"] or new_back != item["back"]
            source = SOURCE_COMPANION_EDITED if changed and item["source"] == SOURCE_COMPANION else item["source"]
            self._write(
                "UPDATE ideas SET front = ?, back = ?, source = ? WHERE id = ?",
                (new_front, new_back, source, item_id),
            )
        return self.get_idea(item_id)

    def open_idea(self, item_id, opened=True):
        if not self._write("UPDATE ideas SET opened = ? WHERE id = ?", (int(opened), item_id)):
            raise NotFound(item_id)

    def remove_idea(self, item_id):
        self._change_state("ideas", item_id, REMOVED)

    def restore_idea(self, item_id):
        self._change_state("ideas", item_id, DRAFT)

    # Chapter notes

    def set_chapter_notes(self, book_id, chapter, big, points):
        self._write(
            "INSERT OR REPLACE INTO chapter_notes(book_id, chapter, big, points_json) VALUES (?, ?, ?, ?)",
            (book_id, chapter, big, _dumps(points)),
        )

    def get_chapter_notes(self, book_id):
        """{chapter: {big, points}}"""
        rows = self._all("SELECT * FROM chapter_notes WHERE book_id = ?", (book_id,))
        return {r["chapter"]: {"big": r["big"], "points": json.loads(r["points_json"])} for r in rows}

    def clear_chapter_notes(self, book_id):
        self._write("DELETE FROM chapter_notes WHERE book_id = ?", (book_id,))

    # Settings

    def get_setting(self, key, default=None):
        """The stored value, else `default`, else the built-in default."""
        row = self._one("SELECT value_json FROM settings WHERE key = ?", (key,))
        if row:
            return json.loads(row["value_json"])
        if default is not None:
            return default
        return self._defaults().get(key)

    def set_setting(self, key, value):
        self._write("INSERT OR REPLACE INTO settings(key, value_json) VALUES (?, ?)", (key, _dumps(value)))

    def get_settings(self):
        """Every setting, stored values over the built-in defaults."""
        settings = self._defaults()
        for row in self._all("SELECT key, value_json FROM settings"):
            settings[row["key"]] = json.loads(row["value_json"])
        return settings

    def set_settings(self, values):
        with self.transaction():
            for key, value in values.items():
                self.set_setting(key, value)

    @staticmethod
    def _defaults():
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["mount"] = paths.default_mount()
        return settings

    # Save batches

    def record_batch(
        self,
        batch_id,
        book_id,
        inbox_lines,
        reviewed_lines,
        summary,
        saved_cards=(),
        card_ids=(),
        blank_card_ids=(),
        connection_ids=(),
        idea_ids=(),
        marked_hids=(),
    ):
        """Step 1 of a save, all at once: the batch as `backed_up`, its drafts `saved`, blank
        drafts of saved highlights `removed`, and opened and skipped cleared on marked highlights."""
        now = time.time()
        with self.transaction():
            self._write(
                "INSERT INTO save_batches(id, book_id, created_at, status, inbox_json, reviewed_json, summary_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, book_id, now, BACKED_UP, _dumps(inbox_lines), _dumps(reviewed_lines), _dumps(summary)),
            )
            for position, card in enumerate(saved_cards):
                self._write(
                    "INSERT INTO saved_cards(batch_id, hid, position, kind, front, back, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (batch_id, card["hid"], position, card["kind"], card["front"], card["back"], card["source"]),
                )
            for card_id in card_ids:
                self._write("UPDATE cards SET state = ?, updated_at = ? WHERE id = ?", (SAVED, now, card_id))
            for card_id in blank_card_ids:
                self._write("UPDATE cards SET state = ?, updated_at = ? WHERE id = ?", (REMOVED, now, card_id))
            for item_id in connection_ids:
                self._write("UPDATE connections SET state = ? WHERE id = ?", (SAVED, item_id))
            for item_id in idea_ids:
                self._write("UPDATE ideas SET state = ? WHERE id = ?", (SAVED, item_id))
            for hid in marked_hids:
                self._write(
                    "UPDATE highlight_state SET opened = 0, skipped = 0 WHERE book_id = ? AND hid = ?",
                    (book_id, hid),
                )
        return batch_id

    def get_batch(self, batch_id):
        row = self._one("SELECT * FROM save_batches WHERE id = ?", (batch_id,))
        return _batch(row) if row else None

    def list_batches(self, book_id=None, statuses=None):
        """Batches, oldest first."""
        sql = "SELECT * FROM save_batches WHERE 1 = 1"
        args = []
        if book_id is not None:
            sql += " AND book_id = ?"
            args.append(book_id)
        if statuses is not None:
            sql += f" AND status IN ({_marks(statuses)})"
            args.extend(statuses)
        return [_batch(r) for r in self._all(sql + " ORDER BY created_at, rowid", args)]

    def unverified_batches(self, book_id=None):
        return self.list_batches(book_id, (BACKED_UP, WRITTEN))

    def mark_batch_written(self, batch_id, intended_inbox, intended_reviewed):
        """Records what the two files should hold, just before they are handed to the disk."""
        self._write(
            "UPDATE save_batches SET status = ?, intended_inbox = ?, intended_reviewed = ? "
            "WHERE id = ? AND status != ?",
            (WRITTEN, intended_inbox, intended_reviewed, batch_id, VERIFIED),
        )

    def mark_batch_verified(self, batch_id):
        with self.transaction():
            self._write(
                "UPDATE save_batches SET status = ?, error = NULL, intended_inbox = NULL, intended_reviewed = NULL "
                "WHERE id = ?",
                (VERIFIED, batch_id),
            )
            batch = self.get_batch(batch_id)
            for line in batch["reviewed_lines"] if batch else ():
                self._write(
                    "INSERT OR IGNORE INTO reader_reviewed(book_id, key) VALUES (?, ?)",
                    (batch["book_id"], json.loads(line)["key"]),
                )

    def set_batch_error(self, batch_id, error):
        self._write("UPDATE save_batches SET error = ? WHERE id = ?", (error, batch_id))

    def list_saved_cards(self, book_id, hid=None):
        """Cards saved from highlights of this book, in the order they were saved."""
        sql = (
            "SELECT s.* FROM saved_cards s JOIN save_batches b ON b.id = s.batch_id WHERE b.book_id = ?"
        )
        args = [book_id]
        if hid is not None:
            sql += " AND s.hid = ?"
            args.append(hid)
        return [dict(r) for r in self._all(sql + " ORDER BY b.created_at, b.rowid, s.position", args)]

    def unverified_reviewed_keys(self, book_id):
        """Highlight keys in this book's batches not yet proven on the reader."""
        keys = set()
        for batch in self.unverified_batches(book_id):
            for line in batch["reviewed_lines"]:
                keys.add(json.loads(line)["key"])
        return keys

    def reader_reviewed(self, book_id):
        """This book's keys in the reader's reviewed.jsonl when last seen, plus those proven since."""
        return {r["key"] for r in self._all("SELECT key FROM reader_reviewed WHERE book_id = ?", (book_id,))}

    def set_reader_reviewed(self, book_id, keys):
        """Records this book's keys just read from the reader's reviewed.jsonl."""
        with self.transaction():
            self._write("DELETE FROM reader_reviewed WHERE book_id = ?", (book_id,))
            for key in keys:
                self._write("INSERT INTO reader_reviewed(book_id, key) VALUES (?, ?)", (book_id, key))

    # Runs

    def get_run(self, book_id):
        row = self._one("SELECT * FROM runs WHERE book_id = ?", (book_id,))
        return _run(row) if row else None

    def set_run(self, book_id, status, options=None, progress=None):
        """Stores a book's run. Options or progress left out keep their stored values."""
        with self.transaction():
            old = self.get_run(book_id)
            self._write(
                "INSERT OR REPLACE INTO runs(book_id, status, options_json, progress_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    book_id,
                    status,
                    _dumps(options if options is not None else (old["options"] if old else {})),
                    _dumps(progress if progress is not None else (old["progress"] if old else {})),
                    time.time(),
                ),
            )
        return self.get_run(book_id)

    def list_runs(self):
        return [_run(r) for r in self._all("SELECT * FROM runs ORDER BY updated_at")]
