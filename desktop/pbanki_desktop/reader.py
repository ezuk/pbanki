"""Reads the reader: mount check, KOReader history, book settings and highlights.

Only ever reads. Malformed books come back with an `error` sentence, never an exception.
"""

import hashlib
import json
import math
import os
import re
import threading
from pathlib import Path, PurePosixPath

from . import device, epub, lua, paths

HISTORY_FILE = f"{paths.KOREADER_DIR}/history.lua"
SETTINGS_FILE = f"{paths.KOREADER_DIR}/settings.reader.lua"
PLUGIN_MAIN = f"{paths.PLUGIN_DIR}/main.lua"
DOCSETTINGS_DIR = f"{paths.KOREADER_DIR}/docsettings"
HASH_DIR = f"{paths.KOREADER_DIR}/hashdocsettings"
LEGACY_HISTORY_DIR = f"{paths.KOREADER_DIR}/history"

ON_MEMORY_CARD = "On the memory card"
SIDECAR_UNREADABLE = "This book's settings file couldn't be read."
NOT_CONNECTED = "The reader isn't plugged in."
OLD_FORMAT = "This book was last opened in an older KOReader. Open it once on the reader to see its highlights here."
NO_SETTINGS_HASH = "This book's settings can't be found, because its file isn't on the reader."

_SUFFIX = re.compile(r".*\.(.+)", re.DOTALL)
_STEM = re.compile(r"\.[^./\\]+$")
_SIDECAR_CACHE_SIZE = 256

_sidecar_cache = {}
_sidecar_lock = threading.Lock()


def is_connected(mount):
    """Whether the reader is there. A real disk must be mounted; a test's folder must exist."""
    if device.is_real_disk(mount):
        return os.path.ismount(mount)
    return Path(mount).is_dir()


def _load_lua_cached(path):
    stat = os.stat(path)
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _sidecar_lock:
        hit = _sidecar_cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    value = lua.load_file(path)
    with _sidecar_lock:
        if len(_sidecar_cache) >= _SIDECAR_CACHE_SIZE:
            _sidecar_cache.clear()
        _sidecar_cache[str(path)] = (stamp, value)
    return value


# KOReader's settings

def document_metadata_folder(mount):
    """KOReader's document_metadata_folder setting: "doc", "dir" or "hash"."""
    try:
        settings = lua.load_file(Path(mount) / SETTINGS_FILE)
    except (OSError, lua.LuaError):
        return "doc"
    value = settings.get("document_metadata_folder") if isinstance(settings, dict) else None
    return value if value in ("doc", "dir", "hash") else "doc"


def partial_md5(path):
    """KOReader's util.partialMD5: MD5 of 1 KB samples at 0 and at 1024 * 4^i for i = 0..10."""
    digest = hashlib.md5()
    try:
        with open(path, "rb") as f:
            # The first offset is lshift(1024, -2), which wraps round to 0 in LuaJIT
            for offset in [0] + [1024 << (2 * i) for i in range(11)]:
                f.seek(offset)
                sample = f.read(1024)
                if not sample:
                    break
                digest.update(sample)
    except OSError:
        return None
    return digest.hexdigest()


def _without_suffix(device_path):
    # KOReader's path:match("(.*)%.") on the whole path
    at = device_path.rfind(".")
    return device_path[:at] if at >= 0 else device_path


def _sidecar_filename(device_path):
    match = _SUFFIX.fullmatch(device_path)
    return f"metadata.{match.group(1) if match else '_'}.lua"


def _sidecar_candidates(mount, device_path):
    """Files KOReader's DocSettings:open looks at, most important first."""
    if ".." in PurePosixPath(device_path).parts:
        return []
    mount = Path(mount)
    filename = _sidecar_filename(device_path)
    without = _without_suffix(device_path).lstrip("/")
    book_file = paths.device_to_local(device_path, mount)
    candidates = []

    if book_file is not None:
        doc_dir = paths.device_to_local(_without_suffix(device_path) + ".sdr", mount)
        if doc_dir is not None and doc_dir.is_dir():
            candidates += [
                ("doc", doc_dir / filename),
                ("doc", doc_dir / f"{filename}.old"),
                ("doc", doc_dir / f"{PurePosixPath(device_path).name}.lua"),
            ]

    dir_dir = mount / DOCSETTINGS_DIR / f"{without}.sdr"
    if dir_dir.is_dir():
        candidates += [("dir", dir_dir / filename), ("dir", dir_dir / f"{filename}.old")]

    if (mount / HASH_DIR).is_dir() and book_file is not None:
        digest = partial_md5(book_file)
        if digest:
            hash_dir = mount / HASH_DIR / digest[:2] / f"{digest}.sdr"
            candidates += [("hash", hash_dir / filename), ("hash", hash_dir / f"{filename}.old")]

    if (mount / LEGACY_HISTORY_DIR).is_dir():
        # DocSettings:getHistoryPath
        slash = device_path.rfind("/")
        name = (device_path[:slash + 1] + "] " + device_path[slash + 1:]).replace("/", "#")
        legacy = mount / LEGACY_HISTORY_DIR / f"[{name}.lua"
        candidates += [("hist", legacy), ("hist", Path(f"{legacy}.old"))]

    if book_file is not None:
        candidates.append(("doc", Path(f"{book_file}.kpdfview.lua")))
    return candidates


def _existing_most_recent_first(candidates):
    # DocSettings' buildCandidates: most recent first, ties by priority, a backup never ahead of its file
    found = []
    previous_exists = False
    for priority, (location, path) in enumerate(candidates):
        try:
            stat = path.stat()
            if not path.is_file():
                raise OSError
        except OSError:
            previous_exists = False
            continue
        mtime = stat.st_mtime
        if path.name.endswith(".old") and previous_exists:
            if found[-1]["mtime"] < mtime:
                found[-1]["mtime"] = mtime
        found.append({"location": location, "path": path, "mtime": mtime, "priority": priority, "size": stat.st_size})
        previous_exists = True
    found.sort(key=lambda c: (-c["mtime"], c["priority"]))
    return found


def read_sidecar(mount, device_path):
    """Picks the settings file as KOReader's DocSettings:open does.

    KOReader doesn't consult document_metadata_folder when loading: of all the locations
    it reads the most recently modified file that holds a non-empty table.
    Returns (data, path, location, error). `data` is None when there's nothing usable.
    """
    bad = False
    for candidate in _existing_most_recent_first(_sidecar_candidates(mount, device_path)):
        if candidate["size"] <= 0:
            continue
        try:
            data = _load_lua_cached(candidate["path"])
        except (OSError, lua.LuaError):
            bad = True
            continue
        if isinstance(data, dict) and data:
            return data, candidate["path"], candidate["location"], None
        if not isinstance(data, dict):
            bad = True
    return None, None, None, SIDECAR_UNREADABLE if bad else None


# History and desktop saves

def read_history(mount):
    """KOReader's history, as [{"time", "file"}]. Raises lua.LuaError or OSError."""
    data = lua.load_file(Path(mount) / HISTORY_FILE)
    if not isinstance(data, (list, dict)):
        raise lua.LuaError("history is not a table")
    seen = set()
    out = []
    for item in lua.sequence(data):
        if isinstance(item, dict) and isinstance(item.get("file"), str) and item["file"] not in seen:
            seen.add(item["file"])
            out.append({"file": item["file"], "time": _number(item.get("time"))})
    return out


def _valid_desktop_card(card):
    # cardgen.lua's desktopCard
    if not isinstance(card, dict) or not isinstance(card.get("front"), str):
        return False
    return card.get("back") is None or isinstance(card["back"], str)


def _desktop_save(line):
    # cardgen.lua's desktopSave: the plugin keeps a malformed line and never applies it
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or isinstance(data.get("v"), bool) or data.get("v") != 1:
        return None
    if not isinstance(data.get("file"), str) or not isinstance(data.get("key"), str) or not data["key"]:
        return None
    cards = data.get("cards")
    if cards is not None and not isinstance(cards, (list, dict)):
        return None
    if isinstance(cards, list) and not all(_valid_desktop_card(card) for card in cards):
        return None
    return data


def reviewed_keys(mount):
    """{device path: set of highlight keys} from lines in reviewed.jsonl the plugin would apply."""
    out = {}
    try:
        raw = (Path(mount) / paths.REVIEWED_FILE).read_bytes()
    except OSError:
        return out
    for line in raw.decode("utf-8", "replace").split("\n"):
        if line.strip():
            save = _desktop_save(line)
            if save:
                out.setdefault(save["file"], set()).add(save["key"])
    return out


# Highlights

def highlight_key(item):
    """The plugin's highlightKey: tostring(datetime) .. "|" .. pos."""
    pos0 = item.get("pos0")
    # lua.py returns a table with only numbered entries as a list, which has no `page`
    if isinstance(pos0, dict):
        pos = lua.tostring(pos0.get("page"))
    else:
        pos = lua.tostring(None if isinstance(pos0, list) else pos0)
    return f"{lua.tostring(item.get('datetime'))}|{pos}"


def _is_highlight(item):
    # The plugin's pendingHighlights test, before its reviewed check
    if not isinstance(item, dict):
        return False
    drawer = item.get("drawer")
    text = item.get("text")
    return drawer is not None and drawer is not False and text is not None and text is not False and text != ""


def _annotations(sidecar):
    return [item for item in lua.sequence(sidecar.get("annotations")) if _is_highlight(item)]


def _reviewed_set(sidecar, extra):
    marked = sidecar.get("pbanki_reviewed")
    # A false value is not reviewed, as in `not reviewed[key]`
    if isinstance(marked, dict):
        keys = {key for key, value in marked.items() if isinstance(key, str) and value is not False}
    else:
        keys = set()
    return keys | (extra or set())


def _counts(items, reviewed):
    total = len(items)
    done = sum(1 for item in items if highlight_key(item) in reviewed)
    return {"total": total, "pending": total - done, "reviewed": done}


def _number(value):
    # Infinity and NaN aren't valid JSON, so a damaged file's values don't get through
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _finite_copy(value):
    """A deep copy with every number that isn't finite replaced by None."""
    if isinstance(value, dict):
        return {key: _finite_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite_copy(item) for item in value]
    return value if type(value) not in (int, float) else _number(value)


def _percent(part, whole):
    try:
        return _number(100 * part / whole)
    except (OverflowError, ZeroDivisionError):
        return None


def _page_of(item):
    pos0 = item.get("pos0")
    if isinstance(pos0, dict):
        page = _number(pos0.get("page"))
        return page if page is not None else _number(item.get("page"))
    return _number(item.get("pageno"))


def _total_pages(sidecar):
    total = _number(sidecar.get("doc_pages"))
    if total is None and isinstance(sidecar.get("stats"), dict):
        total = _number(sidecar["stats"].get("pages"))
    return total


def _text_field(table, name):
    value = table.get(name) if isinstance(table, dict) else None
    return value if isinstance(value, str) and value.strip() and value != "N/A" else None


def _title_and_author(sidecar, stem):
    props = sidecar.get("doc_props")
    stats = sidecar.get("stats")
    title = _text_field(props, "title") or _text_field(stats, "title") or stem
    author = _text_field(props, "authors") or _text_field(stats, "authors") or ""
    return title, author.replace("\n", ", ")


def _name_parts(device_path):
    name = PurePosixPath(device_path).name
    stem = _STEM.sub("", name)
    return stem, name[len(stem) + 1:]


def _companion_path(local_book):
    # assistant_companion.lua: <book stem>.companion.md beside the book, empty counts as absent
    if local_book is None:
        return None
    path = local_book.parent / f"{_STEM.sub('', local_book.name)}.companion.md"
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def _percent_read(sidecar):
    finished = _number(sidecar.get("percent_finished"))
    return None if finished is None else _percent(finished, 1)


def _base_record(mount, device_path, when):
    local = paths.device_to_local(device_path, mount)
    stem, ext = _name_parts(device_path)
    return {
        "device_path": device_path,
        "local_path": str(local) if local else None,
        "title": stem,
        "author": "",
        "stem": stem,
        "ext": ext,
        "last_read": when,
        "percent": None,
        "on_memory_card": local is None,
        "reachable_note": ON_MEMORY_CARD if local is None else None,
        "missing": False,
        "has_companion": False,
        "companion_path": None,
        "pbanki_prompt": None,
        "sidecar_path": None,
        "sidecar_location": None,
        "counts": {"total": 0, "pending": 0, "reviewed": 0},
        "error": None,
    }


def _fill_record(record, mount, sidecar_extra):
    """Adds what the book's own files say. Returns the sidecar, or None."""
    device_path = record["device_path"]
    local = Path(record["local_path"])
    record["missing"] = not local.is_file()
    companion = _companion_path(local)
    record["has_companion"] = companion is not None
    record["companion_path"] = str(companion) if companion else None
    sidecar, path, location, error = read_sidecar(mount, device_path)
    record["error"] = error
    if sidecar is None:
        if not error and record["missing"] and document_metadata_folder(mount) == "hash":
            record["error"] = NO_SETTINGS_HASH
        return None
    record["sidecar_path"] = str(path)
    record["sidecar_location"] = location
    record["title"], record["author"] = _title_and_author(sidecar, record["stem"])
    record["percent"] = _percent_read(sidecar)
    prompt = sidecar.get("pbanki_prompt")
    record["pbanki_prompt"] = prompt if isinstance(prompt, str) else None
    reviewed = _reviewed_set(sidecar, sidecar_extra.get(device_path))
    record["counts"] = _counts(_annotations(sidecar), reviewed)
    if "annotations" not in sidecar and (lua.sequence(sidecar.get("bookmarks")) or sidecar.get("highlight")):
        record["error"] = OLD_FORMAT
    return sidecar


def list_books(mount):
    """Rows for Pick a book, in KOReader's history order. An unreadable history gives no rows."""
    try:
        history = read_history(mount)
    except (OSError, lua.LuaError):
        return []
    extra = reviewed_keys(mount)
    rows = []
    for item in history:
        record = _base_record(mount, item["file"], item["time"])
        if not record["on_memory_card"]:
            try:
                _fill_record(record, mount, extra)
            except Exception:
                record["error"] = SIDECAR_UNREADABLE
        rows.append(record)
    return rows


# One book in full

def _plain_chapter_list(titles):
    return [{"number": None, "title": t, "name": t, "card": t, "doc": None} for t in titles]


def _load_highlights(sidecar, reviewed, book):
    total = _total_pages(sidecar)
    highlights = []
    for item in _annotations(sidecar):
        key = highlight_key(item)
        text = item.get("text")
        pos0 = item.get("pos0")
        fixed = isinstance(pos0, dict)
        page = _page_of(item)
        percent = _percent(page, total) if page is not None and total else None
        position = (lua.tostring(page) if page is not None else "") if fixed else (pos0 if isinstance(pos0, str) else "")
        note = item.get("note")
        chapter = item.get("chapter")
        chapter = chapter if isinstance(chapter, str) and chapter else None
        highlight = {
            "key": key,
            "text": text if isinstance(text, str) else lua.tostring(text),
            "note": note if isinstance(note, str) and note else None,
            "chapter": chapter,
            "chapter_number": None,
            "page": page,
            "position": position,
            "pos0": _finite_copy(pos0),
            "pos1": _finite_copy(item.get("pos1")),
            "datetime": lua.tostring(item.get("datetime")),
            "percent": percent,
            "status": "reviewed" if key in reviewed else "pending",
            "location": dict(epub.NOT_FOUND),
            "passage": None,
        }
        highlights.append(highlight)
    for highlight in highlights:
        if book is not None:
            try:
                _place_in_book(highlight, book)
            except Exception:
                highlight["location"] = dict(epub.NOT_FOUND)
        if not highlight["location"].get("found"):
            highlight["passage"] = epub.empty_passage(highlight["text"])
    return highlights


def _place_in_book(highlight, book):
    location = dict(epub.NOT_FOUND)
    if not isinstance(highlight["pos0"], dict):
        location = book.locate(highlight)
    highlight["location"] = location
    if location.get("found"):
        highlight["passage"] = book.passage(location)
    if not highlight["chapter"]:
        highlight["chapter"] = book.toc_title_at(location)
    chapter = book.chapter_for(highlight["chapter"], location)
    if chapter:
        highlight["chapter_number"] = chapter["number"]


def _chapter_list(book, highlights):
    """Table-of-contents chapters, or chapter titles in reading order when there is no table."""
    chapters = book.chapters() if book is not None else []
    known = {chapter["title"] for chapter in chapters}
    extra = []
    for highlight in highlights:
        title = highlight["chapter"]
        if highlight["chapter_number"] is None and title and title not in known and title not in extra:
            extra.append(title)
    chapters = chapters + _plain_chapter_list(extra)
    by_number = {c["number"]: c for c in chapters if c["number"] is not None}
    by_title = {c["title"]: c for c in chapters}
    for chapter in chapters:
        chapter["count"] = 0
        chapter["pending"] = 0
    for highlight in highlights:
        chapter = by_number.get(highlight["chapter_number"]) or by_title.get(highlight["chapter"])
        highlight["chapter_name"] = chapter["name"] if chapter else highlight["chapter"]
        highlight["chapter_card"] = chapter["card"] if chapter else highlight["chapter"]
        if chapter:
            chapter["count"] += 1
            if highlight["status"] == "pending":
                chapter["pending"] += 1
    return chapters


def load_book(mount, device_path):
    """A book in full: title, author, stem, ext, chapters, highlights, card history and more.

    Never raises. `error` holds a plain sentence when part of it couldn't be read; the
    highlights that could be read are still there.
    """
    when = None
    record = _base_record(mount, device_path, when)
    record.update(
        {
            "is_epub": record["ext"].lower() == "epub",
            "percent_finished": None,
            "doc_pages": None,
            "card_history": [],
            "chapters": [],
            "highlights": [],
        }
    )
    if record["on_memory_card"]:
        return record
    if not is_connected(mount):
        record["error"] = NOT_CONNECTED
        return record
    try:
        return _load_book(record, mount, device_path)
    except Exception:
        record["error"] = record["error"] or SIDECAR_UNREADABLE
        return record


def _load_book(record, mount, device_path):
    try:
        for item in read_history(mount):
            if item["file"] == device_path:
                record["last_read"] = item["time"]
    except (OSError, lua.LuaError):
        pass
    extra = reviewed_keys(mount)
    sidecar = _fill_record(record, mount, extra)

    book = None
    if record["is_epub"] and not record["missing"]:
        try:
            book = epub.open_epub(record["local_path"])
        except epub.EpubError as e:
            record["error"] = record["error"] or str(e)

    if sidecar is None:
        if book is not None and record["title"] == record["stem"] and book.title:
            record["title"] = book.title
            record["author"] = record["author"] or book.author
        record["chapters"] = _chapter_list(book, [])
        return record

    record["percent_finished"] = _number(sidecar.get("percent_finished"))
    record["doc_pages"] = _total_pages(sidecar)
    history = lua.sequence(sidecar.get("pbanki_card_history"))
    record["card_history"] = [_finite_copy(card) for card in history if isinstance(card, dict)]
    if book is not None:
        props = sidecar.get("doc_props")
        stats = sidecar.get("stats")
        if not (_text_field(props, "title") or _text_field(stats, "title")) and book.title:
            record["title"] = book.title
        if not record["author"]:
            record["author"] = book.author
    highlights = _load_highlights(sidecar, _reviewed_set(sidecar, extra.get(device_path)), book)
    record["chapters"] = _chapter_list(book, highlights)
    record["highlights"] = highlights
    return record


# The reader check screen

def _plugin_reads_desktop_saves(mount):
    try:
        return b"reviewed.jsonl" in (Path(mount) / PLUGIN_MAIN).read_bytes()
    except OSError:
        return False


def reader_status(mount):
    """The Reader check screen: {connected, ready, ..., rows} with rows {title, path, ok, level}.

    `level` is "ok", "error", which blocks the app, or "warning", which doesn't. The plugin
    row has `action` "update_plugin" and a `detail` sentence when it needs the update.
    """
    mount_path = Path(mount)
    name = mount_path.name or str(mount)
    connected = is_connected(mount)
    koreader = connected and (mount_path / paths.KOREADER_DIR).is_dir()
    pbanki = connected and (mount_path / paths.PBANKI_DIR).is_dir()

    history_ok = False
    history_count = 0
    if connected and koreader:
        try:
            history_count = len(read_history(mount))
            history_ok = True
        except FileNotFoundError:
            history_ok = True
        except (OSError, lua.LuaError):
            history_ok = False
    plugin_ok = connected and _plugin_reads_desktop_saves(mount)

    def row(title, path, ok, level=None):
        return {"title": title, "path": path, "ok": ok, "level": level or ("ok" if ok else "error")}

    rows = [
        row(f"{name} is connected" if connected else f"{name} isn't connected", str(mount), connected),
        row(
            "KOReader found" if koreader else "KOReader not found. Install it on the reader, then check again.",
            paths.KOREADER_DIR,
            koreader,
        ),
        row(
            "pbAnki found" if pbanki else "pbAnki not found. Install it on the reader, then check again.",
            paths.PBANKI_DIR,
            pbanki,
        ),
        row(
            "KOReader history read" if history_ok else "KOReader history couldn't be read",
            (f"{history_count} book" if history_count == 1 else f"{history_count} books") if history_ok else HISTORY_FILE,
            history_ok,
        ),
    ]
    plugin_row = row(
        "pbAnki plugin can read desktop saves" if plugin_ok else "pbAnki plugin can't read desktop saves yet",
        PLUGIN_MAIN,
        plugin_ok,
        "ok" if plugin_ok else "warning",
    )
    if not plugin_ok:
        plugin_row["action"] = "update_plugin"
        plugin_row["detail"] = "Without the update, saved highlights keep coming up as new on the reader."
    rows.append(plugin_row)

    return {
        "mount": str(mount),
        "name": name,
        "connected": connected,
        "koreader": koreader,
        "pbanki": pbanki,
        "history_ok": history_ok,
        "history_count": history_count,
        "plugin_ok": plugin_ok,
        "ready": connected and koreader and pbanki and history_ok,
        "document_metadata_folder": document_metadata_folder(mount) if connected else None,
        "rows": rows,
    }
