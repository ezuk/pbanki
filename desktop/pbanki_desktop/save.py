"""The save preview and pipeline, the plugin update and eject.

Functions take plain data, with no knowledge of HTTP:

book: {id?, device_path, title, author}
highlight: {hid?, key, text, note?, chapter?, chapter_number?, page or pageno?, position or pos0?,
            reviewed? or status?}

A batch moves backed_up -> written -> verified, and every step can be repeated, so a crash at
any point resumes safely: see finish_batch. Lines are matched whole and each holds its batch
id, so a line from an earlier attempt is never added twice.
"""

import json
import logging
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unicodedata
from collections import Counter
from pathlib import Path

from . import cardgen, device, epub, paths, reader
from . import store as store_module
from .store import BACKED_UP, VERIFIED

WAITING = "waiting"
FAILED = "failed"

IMPORTING_FILE = paths.PBANKI_DIR + "/inbox.importing.jsonl"
FAILED_FILE = paths.PBANKI_DIR + "/inbox.failed.jsonl"
COLLECTION_FILE = paths.PBANKI_DIR + "/collection/collection.anki2"

MAX_PAGE = 2**32 - 1
JUNK_FILES = {".DS_Store", "Thumbs.db"}
JUNK_DIRS = {"__pycache__", ".git"}

_CLOZE_GAP = re.compile(r"\{\{c\d+::(.*?)\}\}")
# Anki's invalid_char_for_field
_FIELD_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_log = logging.getLogger(__name__)

# One write to the reader at a time, across the Save button, Try again and the mount watcher
_write_lock = threading.Lock()


class SaveBlocked(Exception):
    """This save can't start. The message is shown to the user."""


class EjectError(Exception):
    """The reader couldn't be ejected. The message is shown to the user."""


# Field values


def _text(value):
    return "" if value is None else str(value)


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def page_number(item):
    """The page as a whole number, or None. An annotation's `page` is often an xpointer."""
    for field in ("pageno", "page"):
        value = item.get(field)
        if isinstance(value, bool):
            continue
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, str) and value.isascii() and value.isdigit():
            value = int(value)
        if isinstance(value, int) and 0 <= value <= MAX_PAGE:
            return value
    return None


def position_of(item):
    """The xpointer in reflowable books, the page number as text in fixed-layout ones."""
    position = item.get("position")
    if isinstance(position, str):
        return position
    pos0 = item.get("pos0")
    if isinstance(pos0, str):
        return pos0
    if isinstance(pos0, dict) and pos0.get("page") is not None:
        return str(pos0["page"])
    return ""


def chapter_name(item):
    """The chapter as cards show it, or "" when the highlight has none."""
    title = _text(item.get("chapter")).strip()
    return epub.chapter_card(item.get("chapter_number"), title) if title else ""


def deck_name(title):
    """The deck pbAnki files a book's cards under, as deck_name in common/src/inbox.rs."""
    name = " ".join(_text(title).replace("::", ":").split())
    return f"KOReader::{name}" if name else "KOReader"


def _author(book):
    return _text(book.get("author")).replace("\n", ", ")


def _encode(value):
    # One valid UTF-8 line: pbAnki reads the whole file as UTF-8 and splits it on newlines
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8", "replace").decode("utf-8")


# Lines


def _inbox_line(book, batch_id, front, back, quote, chapter, page, position):
    card = {
        "front": front,
        "back": back,
        "quote": quote,
        "book": _text(book.get("title")),
        "author": _author(book),
        "chapter": chapter,
    }
    if page is not None:
        card["page"] = page
    card["file"] = book["device_path"]
    card["position"] = position
    card["batch"] = batch_id
    return _encode(card)


def highlight_card_line(book, highlight, card, batch_id):
    return _inbox_line(
        book,
        batch_id,
        card["front"],
        card["back"],
        _text(highlight.get("text")),
        chapter_name(highlight),
        page_number(highlight),
        position_of(highlight),
    )


def _source_line(highlight):
    page = page_number(highlight)
    place = " · ".join(p for p in (chapter_name(highlight), "" if page is None else f"p. {page}") if p)
    text = " ".join(_text(highlight.get("text")).split())
    return f'{place}: "{text}"' if place else f'"{text}"'


def connection_chapters(connection, by_hid):
    """Chapter names of a connection's sources, each once, joined with ` · `."""
    names = []
    for hid in connection["sources"]:
        name = chapter_name(by_hid[hid]) if hid in by_hid else ""
        if name and name not in names:
            names.append(name)
    return " · ".join(names)


def connection_line(book, connection, by_hid, batch_id):
    sources = [by_hid[hid] for hid in connection["sources"] if hid in by_hid]
    return _inbox_line(
        book,
        batch_id,
        connection["front"],
        connection["back"],
        "\n".join(_source_line(h) for h in sources),
        connection_chapters(connection, by_hid),
        None,
        "",
    )


def idea_line(book, idea, batch_id):
    return _inbox_line(book, batch_id, idea["front"], idea["back"], "", _text(idea.get("chapter")), None, "")


def reviewed_line(book, highlight, cards, batch_id):
    """The reviewed.jsonl line marking one highlight. Definition cards stay out of the history."""
    quote = _text(highlight.get("text"))
    note = _text(highlight.get("note"))
    entries = []
    for card in cards:
        if card["kind"] == "def":
            continue
        entry = {"front": card["front"], "back": card["back"]}
        if card.get("suggested") is not None:
            entry["suggested"] = {"front": card["suggested"]["front"], "back": card["suggested"]["back"]}
        entry["quote"] = quote
        if note:
            entry["note"] = note
        entries.append(entry)
    return _encode(
        {"v": 1, "batch": batch_id, "file": book["device_path"], "key": highlight["key"], "cards": entries}
    )


# What gets saved


def _book_id(book):
    return book.get("id") or store_module.book_id_for(book["device_path"])


def _hid(highlight):
    return highlight.get("hid") or store_module.hid_for(highlight["key"])


def _filled(item):
    return item["front"].strip() != ""


def _cloze_sound(front):
    """Whether a cloze hides some words and has no stray braces around its hidden parts."""
    gaps = _CLOZE_GAP.findall(front)
    rest = _CLOZE_GAP.sub("", front)
    return any(gap.strip() for gap in gaps) and "{{" not in rest and "}}" not in rest


def need_of(item):
    """What a written card lacks before it can be answered on the reader: "cloze", "answer" or None."""
    front = item["front"]
    if item.get("kind") == "cloze" or cardgen.is_cloze(front) or "{{c" in front:
        return None if _cloze_sound(front) else "cloze"
    return None if item["back"].strip() else "answer"


def _select(store, book, highlights, mark_connection_sources):
    """What a save covers, by the mock's rules: only what the user opened."""
    book_id = _book_id(book)
    states = store.list_highlights(book_id)
    in_flight = store.unverified_reviewed_keys(book_id)
    drafts = {}
    for card in store.list_cards(book_id):
        drafts.setdefault(card["hid"], []).append(card)
    by_hid = {_hid(h): h for h in highlights}

    pending, skipped = [], []
    for hid, h in by_hid.items():
        if h.get("reviewed") or h.get("status") == "reviewed" or h["key"] in in_flight:
            continue
        (skipped if states.get(hid, {}).get("skipped") else pending).append(hid)
    pending_set = set(pending)
    skipped_cards = sum(1 for hid in skipped for card in drafts.get(hid, []) if _filled(card))

    with_cards = []
    for hid in pending:
        if states.get(hid, {}).get("opened") and any(_filled(c) for c in drafts.get(hid, [])):
            with_cards.append(hid)
    unopened = [
        hid for hid in pending if not states.get(hid, {}).get("opened") and drafts.get(hid)
    ]

    all_connections = store.list_connections(book_id)
    all_ideas = store.list_ideas(book_id)
    connections = [c for c in all_connections if c["opened"] and _filled(c)]
    ideas = [i for i in all_ideas if i["opened"] and _filled(i)]

    connection_only = []
    for connection in connections:
        for hid in connection["sources"]:
            if (
                hid in pending_set
                and hid not in connection_only
                and not any(_filled(c) for c in drafts.get(hid, []))
            ):
                connection_only.append(hid)
    marked_sources = connection_only if mark_connection_sources else []

    needs, need_kinds = [], []
    for hid in with_cards:
        for card in drafts[hid]:
            need = need_of(card) if _filled(card) else None
            if need:
                needs.append(hid)
                need_kinds.append(need)
    need_connections = [c["id"] for c in connections if need_of(c)]
    need_ideas = [i["id"] for i in ideas if need_of(i)]
    need_kinds += ["answer"] * (len(need_connections) + len(need_ideas))
    count = sum(sum(1 for c in drafts[hid] if _filled(c)) for hid in with_cards) + len(connections) + len(ideas)

    return {
        "by_hid": by_hid,
        "drafts": drafts,
        "with_cards": with_cards,
        "connection_only": connection_only,
        "marked_sources": marked_sources,
        "skipped": skipped,
        "skipped_cards": skipped_cards,
        "pending": pending,
        "unopened": unopened,
        "connections": connections,
        "ideas": ideas,
        "connections_waiting": sum(1 for c in all_connections if not c["opened"]),
        "ideas_waiting": sum(1 for i in all_ideas if not i["opened"]),
        "needs": needs,
        "need_connections": need_connections,
        "need_ideas": need_ideas,
        "need_kinds": need_kinds,
        "count": count,
        "mark_count": len(with_cards) + len(skipped) + len(marked_sources),
        "left": len(pending) - len(with_cards) - len(marked_sources),
    }


def _need_text(kinds):
    answers, clozes = kinds.count("answer"), kinds.count("cloze")
    parts = []
    if answers:
        verb = "needs an answer" if answers == 1 else "need answers"
        parts.append(f"{_plural(answers, 'question')} still {verb}.")
    if clozes:
        verb = "needs" if clozes == 1 else "need"
        parts.append(f"{_plural(clozes, 'cloze card')} still {verb} words hidden correctly.")
    fix = "Answer or remove before saving." if not clozes else "Fix or remove before saving."
    return " ".join([*parts, fix])


def _left_text(selection):
    parts = []
    left = selection["left"]
    parts.append(f"{_plural(left, 'highlight')} will stay to do." if left > 0 else "Every pending highlight is covered.")
    unopened = len(selection["unopened"])
    if unopened == 1:
        parts.append("1 highlight with suggestions you haven't opened is not included. Its suggestions stay here for later.")
    elif unopened:
        parts.append(
            f"{unopened} highlights with suggestions you haven't opened are not included. "
            "Their suggestions stay here for later."
        )
    for n, word in ((selection["connections_waiting"], "connection"), (selection["ideas_waiting"], "key idea")):
        if n:
            verb = "stays" if n == 1 else "stay"
            parts.append(f"{_plural(n, word)} you haven't opened also {verb} here.")
    n = selection["skipped_cards"]
    if n == 1:
        parts.append("1 card on a skipped highlight will not be saved. Undo the skip to keep it.")
    elif n:
        parts.append(f"{n} cards on skipped highlights will not be saved. Undo the skip to keep them.")
    return " ".join(parts)


def _where(highlight):
    page = page_number(highlight)
    number = highlight.get("chapter_number")
    title = _text(highlight.get("chapter")).strip()
    chapter = f"Ch. {number}" if number and not epub.is_numbered_title(title) else title
    return " · ".join(p for p in (chapter, "" if page is None else f"p. {page}") if p)


_NEED_LABELS = {"answer": "No answer yet", "cloze": "No words hidden"}


def _preview_row(card):
    need = need_of(card)
    label = {"need": bool(need), **({"needLabel": _NEED_LABELS[need]} if need else {})}
    if card.get("kind") == "cloze":
        return {"front": _CLOZE_GAP.sub("[...]", card["front"]), "back": "Cloze", **label}
    return {"front": card["front"], "back": card["back"], **label}


def _groups(selection):
    by_hid = selection["by_hid"]
    groups = []
    for hid in selection["with_cards"]:
        h = by_hid[hid]
        rows = [_preview_row(c) for c in selection["drafts"][hid] if _filled(c)]
        groups.append({"type": "highlight", "id": hid, "head": _text(h.get("text")), "where": _where(h), "rows": rows})
    for c in selection["connections"]:
        groups.append(
            {
                "type": "connection",
                "id": c["id"],
                "head": f"Connection: {c['title']}",
                "where": connection_chapters(c, by_hid),
                "rows": [_preview_row(c)],
            }
        )
    for i in selection["ideas"]:
        groups.append(
            {
                "type": "idea",
                "id": i["id"],
                "head": f"Key idea: {i['title']}",
                "where": _text(i.get("chapter")),
                "rows": [_preview_row(i)],
            }
        )
    return groups


def _noun(count):
    return "card ready" if count == 1 else "cards ready"


def _mark_text(count):
    return f"Mark {_plural(count, 'highlight')} as reviewed"


def preview(store, book, highlights, mark_connection_sources=True):
    """The Save cards screen: what goes to the reader, what gets marked and what stays."""
    selection = _select(store, book, highlights, mark_connection_sources)
    count = selection["count"]
    needs = selection["needs"]
    kinds = selection["need_kinds"]
    return {
        "groups": _groups(selection),
        "count": count,
        "noun": _noun(count),
        "needs": needs,
        "need_connections": selection["need_connections"],
        "need_ideas": selection["need_ideas"],
        "need_text": _need_text(kinds) if kinds else "",
        "blocked": bool(kinds) or count == 0,
        "mark_count": selection["mark_count"],
        "mark_text": _mark_text(selection["mark_count"]),
        "marked": {
            "with_cards": selection["with_cards"],
            "connection_only": selection["marked_sources"],
            "skipped": selection["skipped"],
        },
        "connection_only": selection["connection_only"],
        "left": selection["left"],
        "left_text": _left_text(selection),
    }


def with_held(preview_view, held):
    """`preview_view` plus the cards of `held` batch results, which waited or failed and so are
    no longer drafts. `blocked`, `needs` and `marked` still describe only what can be saved now."""
    if not held:
        return preview_view
    count = preview_view["count"] + sum(result["summary"].get("cards", 0) for result in held)
    mark_count = preview_view["mark_count"] + sum(result["summary"].get("marked", 0) for result in held)
    groups = [group for result in held for group in result["summary"].get("groups", [])]
    return {
        **preview_view,
        "groups": groups + preview_view["groups"],
        "count": count,
        "noun": _noun(count),
        "mark_count": mark_count,
        "mark_text": _mark_text(mark_count),
    }


def back_up(store, book, highlights, mark_connection_sources=True):
    """Step 1: writes the batch to SQLite and turns its drafts `saved`, so they can't be saved twice."""
    book_id = _book_id(book)
    with store.transaction():
        selection = _select(store, book, highlights, mark_connection_sources)
        if selection["need_kinds"]:
            raise SaveBlocked(_need_text(selection["need_kinds"]))
        if selection["count"] == 0:
            batches = store.list_batches(book_id)
            if any(batch["status"] != VERIFIED for batch in batches):
                raise SaveBlocked("These cards are already being saved.")
            if batches:
                raise SaveBlocked("These cards are already saved. There's nothing new to save.")
            raise SaveBlocked("There are no cards to save yet.")
        by_hid, drafts = selection["by_hid"], selection["drafts"]
        batch_id = store_module.new_id()

        inbox_lines, reviewed_lines, saved_cards, card_ids, blank_ids = [], [], [], [], []
        for hid in selection["with_cards"]:
            h = by_hid[hid]
            cards = [c for c in drafts[hid] if _filled(c)]
            blank_ids.extend(c["id"] for c in drafts[hid] if not _filled(c))
            for card in cards:
                inbox_lines.append(highlight_card_line(book, h, card, batch_id))
                saved_cards.append({**card, "hid": hid})
                card_ids.append(card["id"])
            reviewed_lines.append(reviewed_line(book, h, cards, batch_id))
        for hid in selection["skipped"] + selection["marked_sources"]:
            reviewed_lines.append(reviewed_line(book, by_hid[hid], [], batch_id))
        for hid in selection["skipped"]:
            blank_ids.extend(c["id"] for c in drafts.get(hid, []))
        for connection in selection["connections"]:
            inbox_lines.append(connection_line(book, connection, by_hid, batch_id))
        for idea in selection["ideas"]:
            inbox_lines.append(idea_line(book, idea, batch_id))

        summary = {
            "cards": selection["count"],
            "highlight_cards": len(card_ids),
            "connections": len(selection["connections"]),
            "ideas": len(selection["ideas"]),
            "marked": selection["mark_count"],
            "skipped": len(selection["skipped"]),
            "title": _text(book.get("title")),
            "deck": deck_name(book.get("title")),
            "file": book["device_path"],
            "groups": _groups(selection),
        }
        store.record_batch(
            batch_id,
            book_id,
            inbox_lines,
            reviewed_lines,
            summary,
            saved_cards=saved_cards,
            card_ids=card_ids,
            blank_card_ids=blank_ids,
            connection_ids=[c["id"] for c in selection["connections"]],
            idea_ids=[i["id"] for i in selection["ideas"]],
            marked_hids=selection["with_cards"] + selection["skipped"] + selection["marked_sources"],
        )
    return batch_id


# Writing to the reader


def reader_connected(mount):
    """Whether the disk is there. Waits out a remount already in progress."""
    if not mount:
        return False
    with device.remount_lock:
        return reader.is_connected(mount)


def _read_file(path):
    """A file's bytes, or None when it doesn't exist. Any other failure raises, because
    treating an unreadable inbox as empty would overwrite the cards in it."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _raw_lines(data):
    return [line.rstrip(b"\r") for line in (data or b"").split(b"\n") if line.strip()]


def _note_fields(line):
    """A card line's fields as pbAnki stores them: InboxCard::field_values in common/src/inbox.rs,
    then Anki's normalize_field, joined by 0x1f as in the notes table."""
    card = json.loads(line)

    def escaped(name):
        return _text(card.get(name)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def html(name):
        return escaped(name).strip().replace("\n", "<br>")

    values = [html("front"), html("back"), html("quote"), escaped("book"), escaped("author"), escaped("chapter"),
              _text(card.get("page")), escaped("file"), escaped("position")]
    return "\x1f".join(unicodedata.normalize("NFC", _FIELD_CONTROL.sub("", value)) for value in values)


def _in_collection(root, lines):
    """Which of `lines` pbAnki has imported into its collection, as raw lines. Read from a copy on
    this Mac, because opening the file where it is could write beside it on the reader. A
    collection that can't be read gives none: a card twice is better than a card lost."""
    source = root / COLLECTION_FILE
    if not lines or not source.is_file():
        return []
    wanted = {}
    for line in lines:
        wanted.setdefault(_note_fields(line), []).append(line.encode("utf-8"))
    found = []
    try:
        with tempfile.TemporaryDirectory(prefix="pbanki-collection-") as folder:
            copy = Path(folder) / source.name
            shutil.copyfile(source, copy)
            wal = source.with_name(source.name + "-wal")
            if wal.is_file():
                shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
            connection = sqlite3.connect(copy)
            try:
                for (fields,) in connection.execute("SELECT flds FROM notes"):
                    if wanted.get(fields):
                        found.append(wanted[fields].pop())
            finally:
                connection.close()
    except (OSError, sqlite3.Error, ValueError) as e:
        _log.warning("could not read pbAnki's collection: %s", e)
        return []
    return found


def _not_in(lines, present):
    seen = Counter(present)
    missing = []
    for line in lines:
        raw = line.encode("utf-8")
        if seen[raw] > 0:
            seen[raw] -= 1
        else:
            missing.append(line)
    return missing


def _without_cut_off_line(data):
    """`data` without a last line a failed write left unfinished, which pbAnki couldn't read."""
    if not data or data.endswith(b"\n"):
        return data
    head, newline, tail = data.rpartition(b"\n")
    try:
        json.loads(tail)
    except ValueError:
        return head + newline
    return data


def _merged(current, lines, intended, elsewhere=()):
    """What a JSON lines file should hold once `lines` are in it, each exactly once.

    `intended` is what an earlier attempt set out to write. A file that is a shorter start
    of it was cut off mid-write, so `intended` restores whatever else the file held.
    A missing file is not cut off: pbAnki or the plugin may have consumed it.
    """
    if current is not None and intended is not None and current != intended and intended.startswith(current):
        return intended
    base = _without_cut_off_line(current or b"")
    if base and not base.endswith(b"\n"):
        base += b"\n"
    seen = Counter(_raw_lines(base))
    seen.update(elsewhere)
    new = b""
    for line in lines:
        raw = line.encode("utf-8")
        if seen[raw] > 0:
            seen[raw] -= 1
        else:
            new += raw + b"\n"
    return base + new


def _new_files(batch, mount):
    root = Path(mount)
    if not (root / paths.PBANKI_DIR).is_dir():
        raise device.DeviceWriteError(
            "This reader doesn't have pbAnki on it, so your cards stayed on this computer. "
            "Install pbAnki on the reader and try again."
        )
    files = {}
    if batch["inbox_lines"]:
        # pbAnki renames the inbox while importing and keeps lines it couldn't import
        elsewhere = _raw_lines(_read_file(root / IMPORTING_FILE)) + _raw_lines(_read_file(root / FAILED_FILE))
        inbox = root / paths.INBOX_FILE
        current = _read_file(inbox)
        if batch["status"] != BACKED_UP:
            # An earlier attempt may have landed and been imported since, which leaves no trace on the
            # inbox: a failed check doesn't stop the user unplugging and opening pbAnki
            elsewhere += _in_collection(root, _not_in(batch["inbox_lines"], _raw_lines(current) + elsewhere))
        merged = _merged(current, batch["inbox_lines"], batch["intended_inbox"], elsewhere)
        # Every line taken already: nothing to write, and no empty file to make
        if merged or current is not None:
            files[inbox] = merged
    if batch["reviewed_lines"]:
        reviewed = root / paths.REVIEWED_FILE
        files[reviewed] = _merged(_read_file(reviewed), batch["reviewed_lines"], batch["intended_reviewed"])
    return files


def batch_result(batch):
    """A batch as the screens see it: `verified`, `waiting` for the reader, or `failed` with a sentence."""
    if batch["status"] == VERIFIED:
        status = VERIFIED
    elif batch["error"]:
        status = FAILED
    else:
        status = WAITING
    return {
        "batch": batch["id"],
        "book_id": batch["book_id"],
        "status": status,
        "error": batch["error"],
        "summary": batch["summary"],
    }


def finish_batch(store, batch_id, mount):
    """Steps 2 to 4 for one batch. Safe to call at any time and any number of times.

    Even when every line is already on the reader the files go through device.write_files,
    since only its remount and read-back prove they are on the disk and not just in this
    Mac's cache.
    """
    with _write_lock:
        batch = store.get_batch(batch_id)
        if batch is None:
            raise store_module.NotFound(batch_id)
        if batch["status"] == VERIFIED:
            return batch_result(batch)
        if not reader_connected(mount):
            store.set_batch_error(batch_id, None)
            return batch_result(store.get_batch(batch_id))
        try:
            files = _new_files(batch, mount)
            root = Path(mount)
            store.mark_batch_written(
                batch_id, files.get(root / paths.INBOX_FILE), files.get(root / paths.REVIEWED_FILE)
            )
            device.write_files(mount, files)
        except (device.DeviceWriteError, device.ForbiddenPath) as e:
            store.set_batch_error(batch_id, str(e))
        except OSError as e:
            store.set_batch_error(batch_id, f"Could not read the reader: {e.strerror or e}")
        except Exception:
            _log.exception("saving batch %s failed", batch_id)
            store.set_batch_error(batch_id, "Something went wrong while saving. Your cards are safe on this computer.")
        else:
            store.mark_batch_verified(batch_id)
        return batch_result(store.get_batch(batch_id))


def finish_pending(store, mount, book_id=None):
    """Finishes every unverified batch, oldest first, for Try again, a replugged reader and app start."""
    return [finish_batch(store, b["id"], mount) for b in store.unverified_batches(book_id)]


def pending_saves(store, book_id=None):
    """Unverified batches as the screens see them."""
    return [batch_result(b) for b in store.unverified_batches(book_id)]


def save(store, book, highlights, mount, mark_connection_sources=True):
    """The whole pipeline. Returns batch_result for the new batch; SaveBlocked if it can't start."""
    batch_id = back_up(store, book, highlights, mark_connection_sources)
    # Every book's, oldest first: another book's failed write may have left these files cut off
    finish_pending(store, mount)
    return batch_result(store.get_batch(batch_id))


# Reviewed status


def reviewed_keys(store, book, sidecar_reviewed, mount):
    """Reviewed status for display: the sidecar, the reader's reviewed.jsonl and unverified batches.
    With the reader unplugged, reviewed.jsonl is as last seen plus the batches proven since."""
    book_id = _book_id(book)
    on_reader = None
    if mount and reader.is_connected(mount):
        try:
            found = reader.read_reviewed_keys(mount).get(book["device_path"], set())
        except OSError:
            found = None
        # A reader pulled out during that read looks like one with no file
        if found is not None and reader.is_connected(mount):
            store.set_reader_reviewed(book_id, found)
            on_reader = found
    if on_reader is None:
        on_reader = store.reader_reviewed(book_id)
    return set(sidecar_reviewed or ()) | on_reader | store.unverified_reviewed_keys(book_id)


# Plugin update and eject


def _plugin_update_mark():
    return paths.home() / "plugin-update-under-way"


def plugin_update_cut_off():
    """Whether a plugin update started and was never proven, so the plugin on the reader may be
    part old and part new, or cut off in the middle of a file."""
    return _plugin_update_mark().exists()


def plugin_update(mount, source=None):
    """Copies the plugin folder to the reader, skipping files that already match, all in one
    proven write. Only ever writes the plugin's own folder, so the prompts the plugin keeps in
    applications/pbanki/prompts stay its own business. A mark on this Mac lasts from the first
    write until the update is proven. Returns {copied, unchanged}."""
    source = Path(source) if source is not None else paths.repo_root() / "koreader" / "pbanki.koplugin"
    with _write_lock:
        if not reader_connected(mount):
            raise device.DeviceWriteError("Plug in your PocketBook to update the plugin.")
        root = Path(mount)
        if not (root / paths.KOREADER_DIR).is_dir():
            raise device.DeviceWriteError("KOReader isn't on this reader, so the plugin wasn't updated.")
        target_dir = root / paths.PLUGIN_DIR
        files, copied, unchanged = {}, [], 0
        for path in sorted(source.rglob("*")):
            rel = path.relative_to(source)
            if not path.is_file() or path.name in JUNK_FILES or path.name.startswith("._"):
                continue
            if JUNK_DIRS.intersection(rel.parts):
                continue
            data = path.read_bytes()
            target = target_dir / rel
            try:
                if _read_file(target) == data:
                    unchanged += 1
                    continue
            except OSError:
                pass
            files[target] = data
            copied.append(rel.as_posix())
        if files:
            _plugin_update_mark().parent.mkdir(parents=True, exist_ok=True)
            _plugin_update_mark().touch()
            device.write_files(mount, files)
        _plugin_update_mark().unlink(missing_ok=True)
    return {"copied": sorted(copied), "unchanged": unchanged}


def eject(mount):
    """Ejects the reader once no write is under way. A folder that isn't a disk has nothing to eject."""
    if not device.is_real_disk(mount):
        return
    with _write_lock:
        try:
            done = device._run_diskutil("eject", str(mount)).returncode == 0
        except (OSError, subprocess.SubprocessError):
            done = False
    if not done:
        raise EjectError("Something on this Mac is still using the reader. Close it and try again.")
