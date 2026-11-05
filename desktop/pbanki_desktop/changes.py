"""Changes to cards in the reader's collection, made on this Mac.

Nothing here touches the collection. A change waits in SQLite and goes to the reader in
changes.jsonl, written whole with device.write_files. pbAnki applies the file when it opens and
appends one line per change to changes.results.jsonl, which is read back at the next plug-in.
What the collection itself says then settles where each change stands.
"""

import html
import json
import logging
import re
import time
import unicodedata
from pathlib import Path

from . import device, paths, save
from . import store as store_module

_log = logging.getLogger(__name__)

WAITING, WRITTEN, APPLIED, SYNCED = "waiting", "written", "applied", "synced"
STAGES = (WAITING, WRITTEN, APPLIED, SYNCED)
EDIT, SUSPEND, UNSUSPEND, DELETE = "edit", "suspend", "unsuspend", "delete"
UNDONE = "undone"

ERROR_SETTING = "changes_error"
OLD_BUILD_SETTING = "changes_old_build"
APP_CHECK_SETTING = "changes_app_check"
FORMAT_MARKER = b"pbanki-changes-format-1"

EDITABLE_NOTETYPES = ("Basic", "Cloze", "pbAnki KOReader", "pbAnki KOReader Cloze")
CLOZE_NOTETYPES = ("Cloze", "pbAnki KOReader Cloze")
LINE_TAGS = ("br", "div", "p")

CLOZE_ERROR = ("Keep the same cloze numbers. Changing them would add a new card or leave an empty one behind. "
               "To hide a different part, add a new card instead.")

_BREAK = re.compile(r"<br\s*/?>|</div>|</p>", re.I)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_CLOZE = re.compile(r"\{\{c\d+::(.*?)(::.*?)?\}\}")
_CLOZE_NUMBER = re.compile(r"\{\{c(\d+)::")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_STEP_NAMES = ("On this computer", "On the reader", "Applied in pbAnki", "On AnkiWeb")
_STEP_NOW = ("Waiting for your reader.", "On your reader. It applies when you open pbAnki there.",
             "Applied on your reader. It goes to AnkiWeb next time pbAnki syncs.", "On AnkiWeb.")
_NOT_APPLIED = {
    "formatted": "This card has formatting on your reader now, so your edit did not overwrite it. Edit it in Anki.",
    "cloze": "The cloze numbers on your reader no longer match, so your edit was not applied.",
    "empty": "A card needs a question, so this edit was not applied.",
    "note_type": "This kind of card can only be edited in Anki.",
    "several_cards": "This note makes several cards, so it can only be edited in Anki.",
    "bad": "pbAnki on your reader could not read this change.",
    "failed": "pbAnki on your reader hit an error applying this change.",
}


class ChangeRefused(Exception):
    """This change can't be made. The message is shown to the user."""


def plain(text):
    """A field as plain text. Cloze deletions become [brackets]."""
    text = _BREAK.sub(" ", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = _CLOZE.sub(r"[\1]", text)
    return _SPACE.sub(" ", text).strip()


# Field text, as common/src/edit.rs and common/src/inbox.rs read and write it


def _tag_name(tag):
    parts = tag.lstrip("/").replace("/", " ").split()
    return parts[0].lower() if parts else ""


def has_formatting(raw):
    """Whether a field holds more than text and line breaks: has_formatting in common/src/edit.rs."""
    rest = raw or ""
    while (start := rest.find("<")) >= 0:
        end = rest.find(">", start)
        if end < 0:
            return False
        if _tag_name(rest[start + 1:end]) not in LINE_TAGS:
            return True
        rest = rest[end + 1:]
    return "[sound:" in (raw or "")


def field_text(raw):
    """A field as typed text: field_to_text in common/src/edit.rs."""
    text = ""
    rest = raw or ""
    while (start := rest.find("<")) >= 0:
        text += rest[:start]
        end = rest.find(">", start)
        if end < 0:
            text += rest[start:]
            rest = ""
            break
        tag = rest[start + 1:end]
        name = _tag_name(tag)
        opens_block = not tag.startswith("/") and name != "br"
        if name in LINE_TAGS and not (opens_block and (not text or text.endswith("\n"))):
            text += "\n"
        rest = rest[end + 1:]
    return html.unescape(text + rest).replace("\xa0", " ").strip()


def clean_typed(text):
    """Typed text as it can be stored: Anki's normalize_field on a field."""
    return unicodedata.normalize("NFC", _CONTROL.sub("", text or "")).strip()


def to_raw(text):
    """The field pbAnki stores for typed text: text_to_html in common/src/inbox.rs."""
    return clean_typed(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")


def _plain(raw):
    return plain(raw or "")


def cloze_numbers(text):
    return set(_CLOZE_NUMBER.findall(text or ""))


# What can be changed


def locks(note):
    """What stops `note` being edited or suspended here."""
    has_raw = note.get("raw_front") is not None
    other_type = has_raw and (note.get("notetype") not in EDITABLE_NOTETYPES or (note.get("fields") or 0) < 2)
    several = has_raw and (note.get("cards") or 0) != 1
    front_locked = has_formatting(note.get("raw_front"))
    back_locked = has_formatting(note.get("raw_back"))
    note_text = ""
    if not has_raw:
        note_text = "Plug in once to edit this card."
    elif other_type or several:
        note_text = "Edit this card in Anki."
    elif front_locked and back_locked:
        note_text = "This card has formatting. Edit it in Anki so it isn't lost."
    return {
        "canEdit": has_raw and not other_type and not several and not (front_locked and back_locked),
        "canSuspend": has_raw,
        "frontLocked": front_locked,
        "backLocked": back_locked,
        "lockNote": note_text,
        "isCloze": note.get("notetype") in CLOZE_NOTETYPES,
    }


# The queue


def live(rows):
    """Changes still open, without a problem."""
    return [row for row in rows if row["closed_at"] is None and not row["problem"]]


def overlay(notes, rows):
    """`notes` as they will be once what is waiting or on the reader has applied. A deleted card
    stays, marked `deleting`, so screens can still show it."""
    by_id = {note["nid"]: dict(note) for note in notes}
    for row in live(rows):
        if row["stage"] not in (WAITING, WRITTEN):
            continue
        note = by_id.get(row["nid"])
        if note is None:
            continue
        if row["op"] == EDIT:
            for side, column in (("front", "raw_front"), ("back", "raw_back")):
                if row[side] is not None:
                    note[column] = to_raw(row[side])
                    note[side] = _plain(note[column])
        elif row["op"] == SUSPEND:
            note["suspended"] = 1
        elif row["op"] == UNSUSPEND:
            note["suspended"] = 0
        elif row["op"] == DELETE:
            note["deleting"] = True
    return [by_id[note["nid"]] for note in notes]


def _latest(rows, nid, ops=None):
    found = [row for row in live(rows) if row["nid"] == nid and (ops is None or row["op"] in ops)]
    return found[-1] if found else None


def _new_row(op, note, now, **fields):
    return {"id": store_module.new_id(), "op": op, "nid": note["nid"], "deck": note.get("deck") or "",
            "expect_front": note.get("raw_front"), "expect_back": note.get("raw_back"),
            "stage": WAITING, "created_at": now, **fields}


def _note_or_refuse(store, nid):
    rows = store.card_changes(open_only=True)
    note = next((n for n in overlay(store.collection_notes(), rows) if n["nid"] == nid), None)
    if note is None:
        raise ChangeRefused("That card isn't on your reader any more.")
    if note.get("deleting"):
        raise ChangeRefused("This card is already being deleted.")
    if note.get("raw_front") is None:
        raise ChangeRefused("Plug in your reader once so pbAnki can read this card in full.")
    return note, rows


def queue_edit(store, nid, front, back, learn, now, on_reader_replaceable):
    """Queues new text for a card, or replaces what is queued and still can be. `on_reader_replaceable`
    says a change already on the reader can still be taken back. Returns the row, or None when
    the text is back to what the reader has."""
    note, rows = _note_or_refuse(store, nid)
    lock = locks(note)
    if not lock["canEdit"]:
        raise ChangeRefused(lock["lockNote"] or "This card can't be edited here.")
    front, back = clean_typed(front), clean_typed(back)
    if not front:
        raise ChangeRefused("A card needs a question.")
    earlier = _latest(rows, nid, (EDIT,))
    replaced = earlier if earlier and (earlier["stage"] == WAITING or (
        earlier["stage"] == WRITTEN and on_reader_replaceable and not earlier["claimed"])) else None
    base_front = replaced["expect_front"] if replaced else note["raw_front"]
    base_back = replaced["expect_back"] if replaced else note["raw_back"]
    front_changed = front != field_text(base_front)
    back_changed = back != field_text(base_back)
    if front_changed and lock["frontLocked"] or back_changed and lock["backLocked"]:
        raise ChangeRefused("This side has formatting. Edit it in Anki so it isn't lost.")
    if lock["isCloze"] and front_changed and cloze_numbers(front) != cloze_numbers(base_front):
        raise ChangeRefused(CLOZE_ERROR)
    with store.transaction():
        if replaced:
            store.remove_card_change(replaced["id"])
        if not front_changed and not back_changed:
            return None
        row = _new_row(EDIT, {**note, "raw_front": base_front, "raw_back": base_back}, now,
                       front=front if front_changed else None, back=back if back_changed else None,
                       learn=int(bool(learn)))
        store.add_card_change(row)
    return row


def queue_suspend(store, nid, now):
    note, rows = _note_or_refuse(store, nid)
    if note.get("suspended"):
        raise ChangeRefused("This card is already suspended.")
    row = _new_row(SUSPEND, note, now)
    store.add_card_change(row)
    return row


def queue_unsuspend(store, nid, now, on_reader_replaceable):
    """Takes back a suspend still waiting, or queues an unsuspend. Returns the new row, if any."""
    rows = store.card_changes(open_only=True)
    earlier = _latest(rows, nid, (SUSPEND,))
    if earlier and (earlier["stage"] == WAITING or (
            earlier["stage"] == WRITTEN and on_reader_replaceable and not earlier["claimed"])):
        store.remove_card_change(earlier["id"])
        return None
    note = next((n for n in overlay(store.collection_notes(), rows) if n["nid"] == nid), None)
    if note is None:
        raise ChangeRefused("That card isn't on your reader any more.")
    row = _new_row(UNSUSPEND, note, now, replaces=earlier["id"] if earlier else None)
    with store.transaction():
        if earlier:
            store.update_card_change(earlier["id"], closed_at=now)
        store.add_card_change(row)
    return row


def queue_delete(store, nid, now):
    """Queues deleting a suspended card for good, after keeping a copy of its text on this Mac."""
    note, rows = _note_or_refuse(store, nid)
    if not note.get("suspended"):
        raise ChangeRefused("Suspend this card first.")
    earlier = _latest(rows, nid, (SUSPEND,))
    copy = {key: note.get(key) for key in ("nid", "deck", "front", "back", "raw_front", "raw_back", "notetype", "tags")}
    row = _new_row(DELETE, note, now, replaces=earlier["id"] if earlier else None, copy_json=json.dumps(copy, ensure_ascii=False))
    with store.transaction():
        if earlier:
            store.update_card_change(earlier["id"], closed_at=now)
        store.add_card_change(row)
    return row


def can_undo(row, connected):
    """A change can be taken back on this Mac, or on a plugged in reader that hasn't taken it yet."""
    if row["closed_at"] is not None or row["problem"]:
        return False
    return row["stage"] == WAITING or (row["stage"] == WRITTEN and connected and not row["claimed"])


def undo(store, change_id, mount):
    """Takes a change back. On the reader, that means proving the file without it first."""
    row = store.card_change(change_id)
    if row is None:
        raise ChangeRefused("That change isn't on this computer any more.")
    connected = save.reader_connected(mount)
    if row["stage"] == WRITTEN and connected:
        read_results(store, mount)
        mark_claimed(store, mount)
        row = store.card_change(change_id)
    if not can_undo(row, connected):
        raise ChangeRefused("It's on your reader already, so it can't be taken back from here.")
    with store.transaction():
        store.remove_card_change(change_id)
        if row["replaces"]:
            store.update_card_change(row["replaces"], closed_at=None)
    if row["stage"] == WRITTEN:
        try:
            _write(store, mount)
        except (device.DeviceWriteError, device.ForbiddenPath, OSError) as e:
            with store.transaction():
                store.add_card_change({k: v for k, v in row.items() if k != "seq"} | {"seq": row["seq"]})
                if row["replaces"]:
                    store.update_card_change(row["replaces"], closed_at=row["created_at"])
            raise ChangeRefused(f"Could not take it back from the reader: {e}") from e
    return row


def use_mine(store, change_id, now):
    """Queues a refused or undone change again over whatever the reader has now."""
    row = store.card_change(change_id)
    if row is None or not row["problem"]:
        raise ChangeRefused("That change isn't waiting on you any more.")
    if row["problem"] == "gone":
        raise ChangeRefused("That card isn't on your reader any more.")
    note = next((n for n in store.collection_notes() if n["nid"] == row["nid"]), None)
    if note is None:
        raise ChangeRefused("That card isn't on your reader any more.")
    fresh = _new_row(row["op"], note, now, front=row["front"], back=row["back"], learn=row["learn"], copy_json=row["copy_json"])
    with store.transaction():
        store.update_card_change(change_id, closed_at=now)
        store.add_card_change(fresh)
    return fresh


def drop(store, change_id, now):
    row = store.card_change(change_id)
    if row is None or not row["problem"]:
        raise ChangeRefused("That change isn't waiting on you any more.")
    store.update_card_change(change_id, closed_at=now)
    return row


# Reading back


def _json_lines(data):
    for line in (data or b"").split(b"\n"):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            yield item


def read_results(store, mount, now=None):
    """Takes in what pbAnki reports. Lines for changes already settled are left as they are."""
    data = save._read_file(Path(mount) / paths.CHANGES_RESULTS_FILE)
    for item in _json_lines(data):
        row = store.card_change(item.get("id") or "")
        if row is None or row["closed_at"] is not None or row["problem"] or row["stage"] not in (WAITING, WRITTEN):
            continue
        if item.get("status") in ("applied", "already"):
            store.update_card_change(row["id"], stage=APPLIED, result_at=item.get("at") or now)
        elif item.get("status") == "refused":
            store.update_card_change(row["id"], stage=WRITTEN, problem=item.get("reason") or "failed",
                                     detail=item.get("detail") or "", reader_front=item.get("front"),
                                     reader_back=item.get("back"), result_at=item.get("at") or now)


def mark_claimed(store, mount):
    """Notes which changes pbAnki has started on. A file lost without a result sends its changes again."""
    root = Path(mount)
    claimed = {item.get("id") for item in _json_lines(save._read_file(root / paths.CHANGES_APPLYING_FILE))}
    queued = {item.get("id") for item in _json_lines(save._read_file(root / paths.CHANGES_FILE))}
    for row in live(store.card_changes(open_only=True)):
        if row["stage"] != WRITTEN:
            continue
        if row["id"] in claimed:
            store.update_card_change(row["id"], claimed=1)
        elif row["id"] not in queued:
            store.update_card_change(row["id"], stage=WAITING, claimed=0)


def _settled_edit(store, row, note, now):
    targets = [(to_raw(row["front"]), note["raw_front"]) if row["front"] is not None else None,
               (to_raw(row["back"]), note["raw_back"]) if row["back"] is not None else None]
    if all(pair is None or pair[0] == pair[1] for pair in targets):
        if note.get("usn") != -1 and row["stage"] != SYNCED:
            store.update_card_change(row["id"], stage=SYNCED, synced_at=now, on_ankiweb=1)
    elif note["raw_front"] == row["expect_front"] and note["raw_back"] == row["expect_back"]:
        store.update_card_change(row["id"], problem=UNDONE, reader_front=note["raw_front"], reader_back=note["raw_back"])
    elif row["stage"] != SYNCED:
        # Changed again elsewhere since, which is the newer word
        store.update_card_change(row["id"], stage=SYNCED, synced_at=now)


def settle(store, notes, pending_graves, now):
    """Moves applied changes along from what a fresh read of the collection says."""
    by_id = {note["nid"]: note for note in notes}
    for row in live(store.card_changes(open_only=True)):
        if row["stage"] not in (APPLIED, SYNCED):
            continue
        note = by_id.get(row["nid"])
        if row["op"] == DELETE:
            if note is not None:
                store.update_card_change(row["id"], problem=UNDONE, reader_front=note.get("raw_front"), reader_back=note.get("raw_back"))
            elif row["stage"] == APPLIED and row["nid"] not in pending_graves:
                store.update_card_change(row["id"], stage=SYNCED, synced_at=now, on_ankiweb=1)
            continue
        if note is None:
            if row["stage"] == APPLIED:
                store.update_card_change(row["id"], stage=SYNCED, synced_at=now)
            continue
        if row["op"] == EDIT:
            _settled_edit(store, row, note, now)
        elif row["op"] == SUSPEND:
            if not note.get("suspended"):
                store.update_card_change(row["id"], problem=UNDONE)
            elif note.get("card_usn") != -1 and row["stage"] == APPLIED:
                store.update_card_change(row["id"], stage=SYNCED, synced_at=now, on_ankiweb=1)
        elif row["op"] == UNSUSPEND and row["stage"] == APPLIED:
            if note.get("suspended"):
                store.update_card_change(row["id"], stage=SYNCED, synced_at=now)
            elif note.get("card_usn") != -1:
                store.update_card_change(row["id"], stage=SYNCED, synced_at=now, on_ankiweb=1)


def read_back(store, mount, notes, pending_graves, now):
    """Everything a plug-in has to say: pbAnki's results, then the collection itself."""
    read_results(store, mount, now)
    mark_claimed(store, mount)
    if notes is not None:
        settle(store, notes, pending_graves, now)


# Writing


def app_takes_changes(store, mount):
    """Whether the pbAnki on the reader can read changes.jsonl. Looks in the app's file without running it."""
    path = Path(mount) / paths.APP_FILE
    try:
        info = path.stat()
    except OSError:
        return False
    key = [info.st_size, int(info.st_mtime)]
    known = store.get_setting(APP_CHECK_SETTING)
    if known and known.get("key") == key:
        return bool(known.get("takes"))
    found = False
    keep = len(FORMAT_MARKER) - 1
    tail = b""
    try:
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                if FORMAT_MARKER in tail + chunk:
                    found = True
                    break
                tail = chunk[-keep:]
    except OSError:
        return False
    store.set_setting(APP_CHECK_SETTING, {"key": key, "takes": found})
    return found


def _line(row):
    change = {"id": row["id"], "op": row["op"], "note_id": row["nid"]}
    if row["expect_front"] is not None:
        change["expect_front"] = row["expect_front"]
    if row["expect_back"] is not None:
        change["expect_back"] = row["expect_back"]
    if row["front"] is not None:
        change["front"] = row["front"]
    if row["back"] is not None:
        change["back"] = row["back"]
    change["learn"] = bool(row["learn"])
    return save._encode(change)


def _write(store, mount):
    """Makes changes.jsonl hold what should be on the reader, and marks what it proved written."""
    root = Path(mount)
    rows = [row for row in live(store.card_changes(open_only=True)) if row["stage"] in (WAITING, WRITTEN)]
    claimed = {item.get("id") for item in _json_lines(save._read_file(root / paths.CHANGES_APPLYING_FILE))}
    rows = [row for row in rows if row["id"] not in claimed]
    target = root / paths.CHANGES_FILE
    data = "".join(_line(row) + "\n" for row in rows).encode("utf-8")
    current = save._read_file(target)
    waiting = [row for row in rows if row["stage"] == WAITING]
    if (current or b"") == data and not waiting:
        return
    device.write_files(mount, {target: data})
    for row in waiting:
        store.update_card_change(row["id"], stage=WRITTEN, written_at=time.time())


def send(store, mount):
    """Sends what is waiting to the reader, once its pbAnki can take it. Never raises for the reader's sake."""
    with save._write_lock:
        if not save.reader_connected(mount):
            return False
        waiting = [row for row in live(store.card_changes(open_only=True)) if row["stage"] == WAITING]
        try:
            if waiting:
                if not (Path(mount) / paths.PBANKI_DIR).is_dir():
                    raise device.DeviceWriteError(
                        "This reader doesn't have pbAnki on it, so your changes stayed on this computer.")
                takes = app_takes_changes(store, mount)
                store.set_setting(OLD_BUILD_SETTING, not takes)
                if not takes:
                    return False
            _write(store, mount)
        except (device.DeviceWriteError, device.ForbiddenPath) as e:
            store.set_setting(ERROR_SETTING, str(e))
            return False
        except OSError as e:
            store.set_setting(ERROR_SETTING, f"Could not read the reader: {e.strerror or e}")
            return False
        except Exception:
            _log.exception("sending changes failed")
            store.set_setting(ERROR_SETTING, "Something went wrong while sending. Your changes are safe on this computer.")
            return False
        store.set_setting(ERROR_SETTING, None)
        return True


# The Changes tab


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _steps(stage):
    done = STAGES.index(stage)
    return [{"label": label, "cls": "trk now" if i == done else "trk done" if i < done else "trk",
             "dot": "dot" if i <= done else "dot off"} for i, label in enumerate(_STEP_NAMES)]


def _shown_text(row, note):
    """Question and answer as they were, and as they will be, in plain text."""
    old_front, old_back = _plain(row["expect_front"]), _plain(row["expect_back"])
    if row["expect_front"] is None and note:
        old_front, old_back = note.get("front", ""), note.get("back", "")
    front = _plain(to_raw(row["front"])) if row["front"] is not None else old_front
    back = _plain(to_raw(row["back"])) if row["back"] is not None else old_back
    return old_front, old_back, front, back


def _item(row, note, connected):
    old_front, old_back, front, back = _shown_text(row, note)
    kinds = {EDIT: "Reworded", DELETE: "Delete for good", UNSUSPEND: "Unsuspend", SUSPEND: "Suspended"}
    return {
        "id": row["id"], "nid": row["nid"], "op": row["op"], "deck": row["deck"], "kind": kinds[row["op"]],
        "front": front, "oldFront": old_front, "frontChanged": row["front"] is not None,
        "back": back, "oldBack": old_back, "backChanged": row["back"] is not None,
        "learn": bool(row["learn"]) and row["op"] == EDIT,
        "steps": _steps(row["stage"]), "now": _STEP_NOW[STAGES.index(row["stage"])],
        "canUndo": can_undo(row, connected),
    }


def _need(row, note):
    old_front, old_back, front, back = _shown_text(row, note)
    problem = row["problem"]
    now_front = _plain(row["reader_front"]) if row["reader_front"] is not None else (note or {}).get("front", old_front)
    now_back = _plain(row["reader_back"]) if row["reader_back"] is not None else (note or {}).get("back", old_back)
    item = {"id": row["id"], "deck": row["deck"], "tag": "NOT APPLIED", "hasCompare": False, "mine": front, "theirs": "",
            "theirsLabel": "Now on your reader", "showMine": False, "mineLabel": "", "dropLabel": "OK", "title": ""}
    answer_only = row["op"] == EDIT and row["front"] is None
    if problem == "changed":
        if row["op"] == EDIT:
            item.update(title="This card changed on your reader or in Anki after you edited it, so your edit did not overwrite it.",
                        hasCompare=True, mine=back if answer_only else front, theirs=now_back if answer_only else now_front,
                        showMine=True, mineLabel="Use my text", dropLabel="Keep the reader's text")
        else:
            verb = "suspend" if row["op"] == SUSPEND else "delete"
            item.update(title=f"This card changed on your reader or in Anki after you chose to {verb} it, so it was left alone.",
                        hasCompare=True, mine=old_front, theirs=now_front, showMine=True,
                        mineLabel=f"{verb.capitalize()} anyway", dropLabel="Leave it")
    elif problem == UNDONE:
        titles = {EDIT: "This edit was applied, then a full Download from AnkiWeb put the old text back.",
                  SUSPEND: "This card was suspended, then a change elsewhere put it back in your reviews.",
                  DELETE: "This card was deleted, then a full Download from AnkiWeb brought it back.",
                  UNSUSPEND: "This card was unsuspended, then a change elsewhere suspended it again."}
        item.update(tag="UNDONE BY A DOWNLOAD" if row["op"] == EDIT else "UNDONE", title=titles[row["op"]],
                    hasCompare=row["op"] == EDIT, mine=back if answer_only else front,
                    theirs=now_back if answer_only else now_front, showMine=True, mineLabel="Apply again", dropLabel="Leave it")
    elif problem == "gone":
        item.update(tag="CARD IS GONE", hasCompare=True, theirs="No such card", theirsLabel="On your reader",
                    title="This card was deleted in Anki or on another device, so there is nothing to edit. "
                          "Your text is below in case you want it for a new card.")
    else:
        item["title"] = _NOT_APPLIED.get(problem, _NOT_APPLIED["failed"])
        if row["detail"] and problem == "failed":
            item["title"] += f" {row['detail']}"
        if problem == "failed":
            item.update(showMine=True, mineLabel="Try again")
    return item


def view(store, notes, connected, writing):
    """The Changes tab, and what other screens say about changes."""
    rows = store.card_changes()
    by_id = {note["nid"]: note for note in notes}
    open_rows = [row for row in rows if row["closed_at"] is None]
    needs = [row for row in open_rows if row["problem"]]
    fine = [row for row in open_rows if not row["problem"]]
    moving = [row for row in fine if row["op"] != SUSPEND and row["stage"] != SYNCED]
    suspended = [row for row in fine if row["op"] == SUSPEND]
    done = [row for row in fine if row["op"] != SUSPEND and row["stage"] == SYNCED]
    waiting = sum(row["stage"] == WAITING for row in fine)
    on_reader = sum(row["stage"] == WRITTEN for row in fine)
    old_build = bool(store.get_setting(OLD_BUILD_SETTING)) and waiting > 0
    error = store.get_setting(ERROR_SETTING) if waiting else None

    banner = None
    if old_build:
        banner = {"kind": "old"}
    elif error:
        banner = {"kind": "error", "text": error}
    elif connected and (writing or waiting):
        banner = {"kind": "wait", "title": "Reader found. Writing your changes...",
                  "text": "Keep it plugged in. Writing and checking the files can take a little while."}
    elif connected and on_reader:
        banner = {"kind": "eject", "title": f"{_plural(on_reader, 'change')} on your reader",
                  "text": "Eject it, then open pbAnki on it. That is when they apply. This computer finds out what happened next time you plug it in."}
    elif connected and open_rows:
        banner = {"kind": "plain", "text": "Just read your reader. This is how things stand."}
    elif waiting:
        banner = {"kind": "wait", "title": f"Plug in your PocketBook to send {_plural(waiting, 'change')}",
                  "text": "They are safe on this computer, and they go by themselves as soon as it appears."}
    elif on_reader:
        banner = {"kind": "plain", "text": "Open pbAnki on your reader to apply them. This computer finds out what happened next time you plug it in."}

    return {
        "banner": banner,
        "waiting": waiting,
        "onReader": on_reader,
        "needs": [_need(row, by_id.get(row["nid"])) for row in needs],
        "moving": [_item(row, by_id.get(row["nid"]), connected) for row in moving],
        "suspended": [_item(row, by_id.get(row["nid"]), connected) for row in suspended],
        "done": [{"id": row["id"], "front": _shown_text(row, by_id.get(row["nid"]))[2],
                  "tag": "DELETED" if row["op"] == DELETE and row["on_ankiweb"] else "ON ANKIWEB" if row["on_ankiweb"] else "DONE"}
                 for row in done[-20:]],
        "open": len(needs) + len(moving) + sum(row["stage"] != SYNCED for row in suspended),
        "saveHint": save_hint(store, connected),
    }


def save_hint(store, connected):
    if store.get_setting(OLD_BUILD_SETTING):
        return "Stays on this computer until your reader's pbAnki is updated."
    return "Goes to your reader right away." if connected else "Stays on this computer until you plug in your reader."


def card_status(rows, nid, connected):
    """What the Clashes tab says about a card's change."""
    found = None
    for row in rows:
        if row["nid"] == nid and row["closed_at"] is None and not (row["op"] == EDIT and row["stage"] == SYNCED):
            found = row
    if found is None:
        return None
    away = found["op"] in (SUSPEND, DELETE)
    if found["problem"]:
        tag = ("tg bad", "NOT APPLIED")
    elif found["op"] == UNSUSPEND:
        tag = ("tg wait", "WILL BE UNSUSPENDED") if found["stage"] in (WAITING, WRITTEN) else ("tg onr", "UNSUSPENDED")
    elif found["stage"] == WAITING:
        tag = ("tg wait", "WILL BE SUSPENDED" if away else "WAITING FOR READER")
    elif found["stage"] == WRITTEN:
        tag = ("tg onr", "ON READER, NOT OPENED YET")
    elif found["stage"] == APPLIED:
        tag = ("tg onr", "SUSPENDED ON READER" if away else "APPLIED ON READER")
    else:
        tag = ("tg done", "SUSPENDED" if away else "DONE")
    return {"row": found, "tagCls": tag[0], "tagText": tag[1], "away": away and not found["problem"],
            "canUndo": can_undo(found, connected), "isEdit": found["op"] == EDIT and not found["problem"]}
