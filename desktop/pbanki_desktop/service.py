"""The application logic behind every HTTP route, with no knowledge of HTTP.

Each public method takes plain values and returns a JSON-ready dict. An expected failure raises
ServiceError(status, sentence). Everything the user does goes to SQLite before the method returns.

Events reach subscribers as dicts with a `topic` ("reader", "book:<id>", "prompts", "settings") and
a `type`. Slow AI work runs on its own thread: the method returns at once, the result lands in the
store, and an event says so.

One background thread polls the mount, the prompt files and any Companion being created, every 2 s.
"""

import logging
import os
import queue
import re
import shlex
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path

from . import ai, cardgen, checks, companion, device, epub, paths, prompts, reader, runs, save
from . import store as store_module
from .store import DRAFT, Store, VERIFIED, book_id_for, hid_for

_log = logging.getLogger(__name__)

POLL_SECONDS = 2.0
DETECT_SECONDS = 60
EVENT_BACKLOG = 512
MAX_MORE = 20
MAX_TERM_WORDS = 3
NOTES_MEMO_SIZE = 4096

READER = "reader"
PROMPTS = "prompts"
SETTINGS = "settings"

KIND_LABELS = {"qa": "Question and answer", "cloze": "Cloze", "qonly": "Question only", "def": "Definition"}
KINDS = [
    {"id": "qa", "label": "Q and A", "file": "questions.md"},
    {"id": "cloze", "label": "Cloze", "file": "cloze.md"},
    {"id": "qonly", "label": "Questions only", "file": "questions.md"},
]
SOURCE_AI_WITH_COMPANION = "AI, using the Book Companion"

GLYPHS = {"rev": "✓", "skip": "–", "cards": "●", "sugg": "○", "writing": "…", "todo": "○"}

_COMMENT = re.compile(r"<!--.*?-->")

_NOT_MATCHED = "This book's text couldn't be matched, so the AI sees only the highlight."
_SHOW_MORE_UNPLUGGED = "Plug in your reader to see more of the book."
_NOT_ON_COMPUTER = "That isn't on this computer any more."
_NOT_CACHED = "This book isn't on this computer yet. Plug in your reader and open the book first."
_SOMETHING_WRONG = "Something went wrong. Your work is safe on this computer."
_DATABASE_PROBLEM = "pbAnki couldn't use its database just now, so that last change may not have been stored. Try again in a moment."
_BOTH_OFF = "At least one assistant has to stay on."

# Option name to the setting that remembers it
_REMEMBERED = {
    "count": "count",
    "kind": "kind",
    "model": "model",
    "prompt": "last_prompt",
    "sees": "sees",
    "companion": "companion_send",
    "connections": "suggest_connections",
    "meta_model": "meta_model",
    "ideas": "suggest_ideas",
}

_RUN_KEYS = ("count", "kind", "model", "prompt", "sees", "companion", "hint", "connections", "meta_model", "ideas")
_SUGGEST_KEYS = ("count", "kind", "model", "prompt", "sees", "companion", "hint")
_SETTING_KEYS = (
    "mount", "auto_suggest", "assistants", "model", "meta_model", "companion_send", "sees", "kind", "count",
    "last_prompt", "suggest_connections", "suggest_ideas", "mark_connection_sources",
)


class ServiceError(Exception):
    """An expected failure: an HTTP status and a plain sentence for the user."""

    def __init__(self, status, sentence):
        super().__init__(sentence)
        self.status = status
        self.sentence = sentence

    def payload(self):
        return {"error": self.sentence}


def _route(method):
    """Turns the layers' errors into ServiceError."""

    @wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except ServiceError:
            raise
        except store_module.NotFound:
            raise ServiceError(404, _NOT_ON_COMPUTER) from None
        except (store_module.NotEditable, runs.RunError, companion.CompanionError, save.SaveBlocked,
                save.EjectError) as e:
            raise ServiceError(409, str(e)) from None
        except prompts.PromptNotFound as e:
            raise ServiceError(404, str(e)) from None
        except device.DeviceWriteError as e:
            raise ServiceError(503, str(e)) from None
        except device.ForbiddenPath as e:
            _log.error("refused to write %s", e)
            raise ServiceError(500, _SOMETHING_WRONG) from None
        except (store_module.StoreError, sqlite3.Error) as e:
            _log.error("database problem: %s", e)
            raise ServiceError(500, _DATABASE_PROBLEM) from None

    return wrapper


class Subscription:
    """One listener's queue of events. Oldest events are dropped if it falls far behind."""

    def __init__(self, hub, topics):
        self._hub = hub
        self.topics = set(topics) if topics is not None else None
        self._queue = queue.Queue(maxsize=EVENT_BACKLOG)

    def _put(self, event):
        if self.topics is not None and event.get("topic") not in self.topics:
            return
        while True:
            try:
                self._queue.put_nowait(event)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass

    def get(self, timeout=None):
        """The next event, or None after `timeout` seconds."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self._hub._remove(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class _Hub:
    def __init__(self):
        self._lock = threading.Lock()
        self._listeners = set()

    def subscribe(self, topics=None):
        listener = Subscription(self, topics)
        with self._lock:
            self._listeners.add(listener)
        return listener

    def _remove(self, listener):
        with self._lock:
            self._listeners.discard(listener)

    def publish(self, event):
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            listener._put(event)


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _tilde(path):
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text.startswith(home + "/") else text


def _open_command(args):
    # Runs `open`, or the test command in PBANKI_OPEN_CMD with the same arguments
    override = os.environ.get("PBANKI_OPEN_CMD")
    command = [*shlex.split(override), *args[1:]] if override else args
    try:
        return subprocess.run(command, capture_output=True, timeout=30, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _model_command(model):
    if model["assistant"] == "codex":
        return f"codex exec -m {model['id']}"
    return f"claude -p --model {model['id']}"


def _model_view(model):
    return {"id": model["id"], "label": model["label"], "a": model["assistant"], "cmd": _model_command(model)}


def _command_for(model_id):
    try:
        return _model_command(ai.model_info(model_id))
    except ai.AIError:
        return ""


def _when_label(stamp, now):
    """Pick a book's "Last read": Today, Oct 19, or Oct 19, 2025."""
    if not stamp:
        return ""
    try:
        day = datetime.fromtimestamp(stamp)
    except (OverflowError, OSError, ValueError):
        return ""
    today = datetime.fromtimestamp(now)
    if day.date() == today.date():
        return "Today"
    label = f"{day:%b} {day.day}"
    return label if day.year == today.year else f"{label}, {day.year}"


def _read_label(percent):
    return "" if percent is None else f"{round(percent)}%"


def _plain_front(front):
    return cardgen._CLOZE_BLOCK_RE.sub("[...]", front or "")


def _cloze_answer(front):
    return cardgen._plain_cloze(front or "")


def _cloze_parts(front):
    parts, last = [], 0
    for match in cardgen._CLOZE_BLOCK_RE.finditer(front or ""):
        if match.start() > last:
            parts.append({"text": front[last:match.start()]})
        parts.append({"gap": True})
        last = match.end()
    if last < len(front or ""):
        parts.append({"text": front[last:]})
    return parts


def _saved_row(front, back, kind=None):
    if kind == "cloze" or cardgen.is_cloze(front):
        return {"front": _plain_front(front), "back": "Cloze"}
    return {"front": front, "back": back}


def _card_view(card, warn=None, rewriting=None):
    front, back = card["front"], card["back"]
    is_cloze = card["kind"] == "cloze"
    need = card["kind"] == "qonly" and not back.strip()
    return {
        "id": card["id"],
        "hid": card["hid"],
        "book_id": card["book_id"],
        "kind": card["kind"],
        "kindLabel": KIND_LABELS.get(card["kind"], card["kind"]),
        "src": card["source"],
        "front": front,
        "back": back,
        "suggested": card["suggested"],
        "state": card["state"],
        "warn": warn,
        "isCloze": is_cloze,
        "isPlain": not is_cloze,
        "parts": _cloze_parts(front) if is_cloze else [],
        "frontPlain": _plain_front(front) if is_cloze else front,
        "backPlain": _cloze_answer(front) if is_cloze else back,
        "need": need,
        "backPh": "Write the answer yourself" if need else "Answer",
        "backCls": "ta b need" if need else "ta b",
        "rewriting": rewriting is not None,
        "rwBtn": "Rewriting..." if rewriting is not None else "Rewrite",
    }


def _passage_fields(h):
    passage = h.get("passage") or epub.empty_passage(h.get("text"))
    return {
        "found": bool(passage.get("found")),
        "before2": passage.get("before2") or "",
        "before": passage.get("before") or "",
        "pre": passage.get("pre") or "",
        "mark": passage.get("mark") or h.get("text") or "",
        "post": passage.get("post") or "",
        "after": passage.get("after") or "",
        "after2": passage.get("after2") or "",
        "endCh": bool(passage.get("endCh")),
        "endCh2": bool(passage.get("endCh2")),
    }


def _where_long(h):
    page = h.get("page")
    chapter = h.get("chapter_card") or h.get("chapter") or ""
    return " · ".join(part for part in (chapter, "" if page is None else f"p. {page}") if part)


class _Context:
    """One book's rows, read once for building a response."""

    def __init__(self, service, book):
        store = service._store
        book_id = book["id"]
        self.book = book
        self.settings = store.get_settings()
        rows = service._highlights(book)
        sidecar = {h["key"] for h in rows if h.get("status") == "reviewed"}
        self.reviewed = save.reviewed_keys(store, book, sidecar, None)
        self.highlights = [{**h, "status": "reviewed" if h["key"] in self.reviewed else "pending"} for h in rows]
        self.by_hid = {h["hid"]: h for h in self.highlights}
        self.states = store.list_highlights(book_id)
        self.drafts = {}
        for card in store.list_cards(book_id):
            self.drafts.setdefault(card["hid"], []).append(card)
        self.saved = {}
        for card in store.list_saved_cards(book_id):
            self.saved.setdefault(card["hid"], []).append(card)
        self.connections = store.list_connections(book_id)
        self.ideas = store.list_ideas(book_id)
        self.chapter_notes = store.get_chapter_notes(book_id)
        self.run = service.runner.status(book_id)
        self.writing = set((self.run.get("progress") or {}).get("writing") or [])
        self.jobs = service._jobs_of(book_id)
        self.companion = service._find_companion(book)
        self.companion_text = companion.read_text(self.companion)
        self.history = book["data"].get("card_history") or []

    def is_skipped(self, h):
        state = self.states.get(h["hid"]) or {}
        return bool(state.get("skipped")) and h["status"] != "reviewed"

    def open_drafts(self):
        """Written cards on highlights still to do, whether opened or not."""
        return sum(
            1 for h in self.highlights if h["status"] != "reviewed" and not self.is_skipped(h)
            for card in self.drafts.get(h["hid"], []) if card["front"].strip()
        )

    def counts(self):
        total = len(self.highlights)
        reviewed = sum(1 for h in self.highlights if h["status"] == "reviewed")
        skipped = sum(1 for h in self.highlights if self.is_skipped(h))
        return {"all": total, "todo": total - reviewed - skipped, "rev": reviewed + skipped,
                "reviewed": reviewed, "skipped": skipped}


class Service:
    def __init__(self, store=None, query=None, launch=None, eject=None, poll_seconds=POLL_SECONDS, now=None):
        """`query` has ai.query's signature. `launch(args)` opens a file and returns success.
        `eject(mount)` raises save.EjectError on failure. All three are replaceable in tests."""
        paths.home().mkdir(parents=True, exist_ok=True)
        self._store = store or Store()
        self._query = query
        self._launch = launch or _open_command
        self._eject = eject or save.eject
        self._poll_seconds = poll_seconds
        self._now = now or time.time
        self._hub = _Hub()
        prompts.seed()
        self._prompt_watcher = prompts.PromptWatcher(on_change=self._prompts_changed)
        self.runner = runs.Runner(self._store, query=query, on_event=self._hub.publish)

        self._stop_event = threading.Event()
        self._thread = None
        self._refresh_thread = None
        self._refresh_lock = threading.Lock()
        self._seen_connected = None
        self._seen_mount = None
        self._checked_at = self._now()
        self._last_reader_view = None

        self._jobs = {}
        self._jobs_lock = threading.Lock()
        self._closing = False
        self._save_lock = threading.Lock()
        self._saving = 0
        self._saving_lock = threading.Lock()
        self._detected = None
        self._detected_at = 0
        self._creations = {}
        self._creations_lock = threading.Lock()
        self._create_lock = threading.Lock()
        self._notes_memo = {}
        self._find_errors = {}

    # Life cycle

    def start(self):
        """Starts the background watcher. Waiting saves continue if the reader is there."""
        if self._thread is not None:
            return
        self._track_creations()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._watch, name="pbanki-watcher", daemon=True)
        self._thread.start()

    def stop(self, wait=5):
        """For quitting: stops the watcher and AI calls. Unfinished runs are offered again next time."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(wait)
            self._thread = None
        if self._refresh_thread is not None:
            self._refresh_thread.join(wait)
        with self._jobs_lock:
            self._closing = True
            jobs = list(self._jobs.values())
        for job in jobs:
            job["token"].cancel()
        for job in jobs:
            job["thread"].join(wait)
        self.runner.shutdown(wait=wait)
        self.release_thread()

    def release_thread(self):
        """Closes this thread's database connection. A server calls it when a request thread ends."""
        self._store.close()

    def subscribe(self, topics=None):
        """A Subscription to events, all topics when `topics` is None. Close it when done."""
        return self._hub.subscribe(topics)

    def tick(self):
        """One round of the watcher's checks."""
        for step in (self._poll_reader, self._prompt_watcher.check, self._poll_creations):
            try:
                step()
            except Exception:
                _log.exception("A background check failed")

    def wait_for_refresh(self, timeout=None):
        """Waits for the cache refresh started when the reader appeared. For tests."""
        thread = self._refresh_thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def _watch(self):
        try:
            while not self._stop_event.is_set():
                self.tick()
                self._stop_event.wait(self._poll_seconds)
        finally:
            self.release_thread()

    # Reader state

    def _mount(self):
        return self._store.get_settings()["mount"]

    def _connected(self):
        # A remount only happens to a disk that is there
        if device.remount_lock.locked():
            return True
        return reader.is_connected(self._mount())

    def _read_reader(self, read):
        """`read(mount)`, or None when the reader isn't there or left during the read."""
        mount = self._mount()
        if device.remount_lock.locked() or not reader.is_connected(mount):
            return None
        result = read(mount)
        if device.remount_lock.locked() or not reader.is_connected(mount):
            return None
        return result

    def _poll_reader(self):
        mount = self._mount()
        with device.remount_lock:
            connected = reader.is_connected(mount)
        self._checked_at = self._now()
        changed = connected != self._seen_connected or mount != self._seen_mount
        self._seen_connected, self._seen_mount = connected, mount
        if changed:
            self._hub.publish({"topic": READER, "type": "changed", "connected": connected, "mount": str(mount)})
            if connected:
                self._reader_appeared()
        elif not connected:
            self._hub.publish({"topic": READER, "type": "checked", "connected": False, "mount": str(mount)})

    def _reader_appeared(self):
        self._finish_saves()
        self._start_refresh()

    def _start_refresh(self):
        with self._refresh_lock:
            if self._refresh_thread is not None and self._refresh_thread.is_alive():
                return
            self._refresh_thread = threading.Thread(target=self._refresh_all, name="pbanki-refresh", daemon=True)
            self._refresh_thread.start()

    def _reader_view(self):
        status = self._read_reader(lambda mount: reader.reader_status(mount, save.plugin_update_cut_off()))
        if status is None:
            if device.remount_lock.locked() and self._last_reader_view is not None:
                return {**self._last_reader_view, "checked_ago": self._checked_ago(), "saving": self._is_saving()}
            status = reader.reader_status(self._mount())
        rows = []
        for row in status["rows"]:
            warning = row["level"] == "warning"
            rows.append({
                **row,
                "glyph": "✓" if row["ok"] else "!" if warning else "✕",
                "cls": "chk" if row["ok"] else "chk warn" if warning else "chk bad",
            })
        view = {
            **status,
            "rows": rows,
            "title": "Your reader is ready" if status["ready"] else "Something is missing on the reader",
            "label": f"{status['name']} connected" if status["connected"] else "Reader unplugged",
            "dot": "dot" if status["connected"] else "dot off",
            "checked_ago": self._checked_ago(),
            "saving": self._is_saving(),
        }
        self._last_reader_view = view
        return view

    def _reader_small(self):
        connected = self._connected()
        mount = self._mount()
        name = Path(mount).name or str(mount)
        return {
            "connected": connected,
            "mount": str(mount),
            "label": f"{name} connected" if connected else "Reader unplugged",
            "dot": "dot" if connected else "dot off",
            "saving": self._is_saving(),
        }

    def _checked_ago(self):
        return max(int(self._now() - self._checked_at), 0)

    # Assistants and models

    def _assistants(self):
        now = time.monotonic()
        if self._detected is None or now - self._detected_at > DETECT_SECONDS:
            self._detected = ai.detect()
            self._detected_at = now
        return self._detected

    def _usable_models(self, settings=None):
        settings = settings or self._store.get_settings()
        return [m for m in ai.enabled_models(settings["assistants"]) if ai.find_binary(m["assistant"])]

    def _check_model(self, model_id, settings=None):
        settings = settings or self._store.get_settings()
        try:
            model = ai.model_info(model_id)
        except ai.AIError as e:
            raise ServiceError(400, str(e)) from None
        info = ai.ASSISTANTS[model["assistant"]]
        if not settings["assistants"].get(model["assistant"]):
            raise ServiceError(409, f"{info['name']} is turned off in Settings.")
        if not ai.find_binary(model["assistant"]):
            raise ServiceError(409, f"{info['name']} isn't installed, or pbAnki can't find it.")
        return model

    # GET /api/state

    @_route
    def state(self):
        settings = self._store.get_settings()
        enabled = settings["assistants"]
        current = settings.get("current_book")
        skill = companion.skill_status()
        return {
            "reader": self._reader_view(),
            "settings": settings,
            "current_book": current,
            "assistants": [
                {
                    "id": a["id"],
                    "name": a["name"],
                    "path": a["display"] or "",
                    "version": a["version"],
                    "found": a["found"],
                    "on": bool(enabled.get(a["id"])),
                    "status": "Found" if a["found"] else "Not found",
                }
                for a in self._assistants()
            ],
            "models": [_model_view(m) for m in self._usable_models(settings)],
            "kinds": KINDS,
            "comp_opts": companion.SEND_OPTIONS,
            "sees_opts": list(epub.AI_MODES),
            "prompts": self._prompts_view(),
            "files": {
                "prompts": _tilde(paths.prompts_dir()),
                "db": _tilde(paths.db_path()),
                "assistant": _tilde(skill["path"]),
            },
            "skill": skill,
            "resume": self._resume_view(current),
            "pending_saves": self._pending_saves_view(),
        }

    def _prompts_view(self):
        folders = prompts.list_folders()
        for folder in folders:
            for row in folder["files"]:
                row["display"] = _tilde(row["own_path"])
        return {
            "names": [folder["name"] for folder in folders],
            "folders": folders,
            "dir": _tilde(paths.prompts_dir()),
            "reloadAgo": self._prompt_watcher.seconds_since_reload(),
        }

    def _resume_view(self, current):
        drafts = self._store.draft_counts()
        books = sorted(self._store.list_books(), key=lambda b: b["id"] != current)
        for book in books:
            if book["id"] not in drafts and not self._has_openable_work(book["id"]):
                continue
            context = _Context(self, book)
            count = self._preview(context)["count"] or context.open_drafts()
            if not count:
                continue
            verb = "is" if count == 1 else "are"
            return {
                "book_id": book["id"],
                "title": book["title"],
                "count": count,
                "text": f"{_plural(count, 'unsaved card')} {verb} safe on this computer",
                "detail": f"{book['title']}. You can keep working now and plug in when you save.",
            }
        return None

    def _pending_saves_view(self, book_id=None):
        return [self._save_result_view(result) for result in save.pending_saves(self._store, book_id)]

    # GET /api/books

    @_route
    def books(self):
        rows = self._read_reader(reader.list_books)
        if rows:
            self._cache_rows(rows)
        settings = self._store.get_settings()
        cached = self._store.list_books()
        drafts = self._store.draft_counts()
        by_id = {book["id"]: book for book in cached}
        chosen = []
        if rows:
            chosen = [by_id[book_id_for(row["device_path"])] for row in rows if book_id_for(row["device_path"]) in by_id]
            listed = {book["id"] for book in chosen}
            # A book gone from KOReader's history keeps its unsaved work on view
            chosen += [b for b in cached if b["id"] not in listed and (b["id"] in drafts or self._has_waiting_save(b["id"]))]
        else:
            chosen = sorted(cached, key=lambda b: b["data"].get("last_read") or 0, reverse=True)
        return {
            "books": [self._book_row(book, drafts, settings.get("current_book")) for book in chosen],
            "connected": self._connected(),
        }

    def _has_waiting_save(self, book_id):
        return bool(self._store.unverified_batches(book_id))

    def _book_row(self, book, drafts, current):
        data = book["data"]
        highlights = data.get("highlights")
        counts = self._row_counts(book, highlights)
        total, todo = counts["all"], counts["todo"]
        on_card = bool(data.get("on_memory_card"))
        none = total == 0 or on_card
        if on_card:
            label = data.get("reachable_note") or reader.ON_MEMORY_CARD
        elif total == 0:
            label = "No highlights"
        elif todo == 0:
            label = f"All {total} reviewed"
        else:
            label = f"{todo} to do of {total}"
        unsaved = 0
        if book["id"] in drafts or self._has_openable_work(book["id"]):
            unsaved = self._preview(_Context(self, book))["count"]
        has_drafts = book["id"] in drafts
        return {
            "id": book["id"],
            "device_path": book["device_path"],
            "title": book["title"],
            "author": book["author"],
            "last": _when_label(data.get("last_read"), self._now()),
            "last_read": data.get("last_read"),
            "read": _read_label(data.get("percent")),
            "percent": data.get("percent"),
            "hl": label,
            "counts": counts,
            "comp": "" if none else self._companion_label(book),
            "action": "" if none else "Continue" if has_drafts or unsaved or book["id"] == current else "Open",
            "dim": none or todo == 0,
            "none": none,
            "draft": _plural(unsaved, "unsaved card") if unsaved else "",
            "unsaved": unsaved,
            "note": data.get("error") or data.get("reachable_note") or "",
            "missing": bool(data.get("missing")),
            "on_memory_card": on_card,
        }

    def _has_openable_work(self, book_id):
        return bool(self._store.list_connections(book_id) or self._store.list_ideas(book_id))

    def _row_counts(self, book, highlights):
        data = book["data"]
        stored = data.get("counts") or {}
        if not highlights and stored.get("total"):
            total, reviewed = stored["total"], stored.get("reviewed", 0)
            return {"all": total, "todo": total - reviewed, "rev": reviewed, "reviewed": reviewed, "skipped": 0}
        keys = {h["key"] for h in highlights or [] if h.get("key") and h.get("text")}
        sidecar = {h["key"] for h in highlights or [] if h.get("status") == "reviewed"}
        reviewed = save.reviewed_keys(self._store, book, sidecar, None) & keys
        skipped = {
            state["key"] for state in self._store.list_highlights(book["id"]).values() if state["skipped"]
        } & (keys - reviewed)
        return {"all": len(keys), "todo": len(keys) - len(reviewed) - len(skipped), "rev": len(reviewed) + len(skipped),
                "reviewed": len(reviewed), "skipped": len(skipped)}

    def _companion_label(self, book):
        if self._find_companion(book)["found"]:
            return "Yes"
        return "Creating" if self._creation_stage(book["device_path"]) in ("running", "found") else "None"

    # Keeping the cache fresh

    def _cache_rows(self, rows):
        with self._store.transaction():
            for row in rows:
                old = self._store.get_book(book_id_for(row["device_path"]))
                old_data = old["data"] if old else {}
                # A row only knows the title from the settings file; a full read may know better
                title = row["title"]
                author = row["author"]
                if old and title == row["stem"]:
                    title = old["title"]
                if old and not author:
                    author = old["author"]
                data = {**old_data, **row, "title": title, "author": author}
                if old and old_data == data:
                    continue
                self._store.upsert_book({
                    "device_path": row["device_path"], "title": title, "author": author,
                    "stem": row["stem"], "ext": row["ext"], "data": data,
                })

    @staticmethod
    def _stamp(record):
        stamp = []
        for path in (record.get("sidecar_path"), record.get("local_path")):
            try:
                info = os.stat(path)
                stamp += [info.st_mtime_ns, info.st_size]
            except (OSError, TypeError):
                stamp += [None, None]
        return stamp

    def _merged_record(self, book_id, record, old):
        """The record to cache: a failed read keeps the highlights already here, and a highlight
        that left the reader stays while it has unsaved cards or an unsaved connection cites it."""
        old_rows = old.get("highlights") or []
        if record.get("error") and not record["highlights"] and old_rows:
            return {**old, "error": record["error"]}
        by_key = {h["key"]: h for h in old_rows if h.get("key")}
        for h in record["highlights"]:
            prior = by_key.get(h["key"])
            if prior and record.get("missing") and (prior.get("location") or {}).get("found"):
                for field in ("location", "passage", "chapter", "chapter_number", "chapter_name", "chapter_card"):
                    h[field] = prior.get(field)
        fresh = {h["key"] for h in record["highlights"]}
        needed = {card["hid"] for card in self._store.list_cards(book_id)}
        for connection in self._store.list_connections(book_id):
            needed.update(connection["sources"])
        for prior in old_rows:
            key = prior.get("key")
            if key and key not in fresh and hid_for(key) in needed:
                record["highlights"].append({**prior, "removed_on_reader": True})
        return record

    def _refresh_book(self, device_path, row=None):
        """Re-reads one book from the reader into the cache. False when the reader isn't there."""
        book_id = book_id_for(device_path)
        old = self._store.get_book(book_id)
        old_data = old["data"] if old else {}
        if row is not None and old_data.get("highlights") and old_data.get("stamp") == self._stamp(row):
            return self._refresh_reviewed(old)
        record = self._read_reader(lambda mount: reader.load_book(mount, device_path))
        if record is None:
            return False
        record["stamp"] = self._stamp(record)
        changed = record["stamp"] != old_data.get("stamp")
        title, author = record["title"], record["author"]
        # An unreadable settings file keeps the cached title and author
        if record.get("error") and old and old["title"] != old["stem"]:
            title, author = old["title"], old["author"]
        # One write, so a card arriving meanwhile sees either the old highlights or the new
        with self._store.transaction():
            data = self._merged_record(book_id, record, old_data)
            self._store.upsert_book({
                "device_path": device_path, "title": title, "author": author,
                "stem": record["stem"], "ext": record["ext"], "data": {**data, "title": title, "author": author},
            })
        book = self._store.get_book(book_id)
        self._refresh_reviewed(book)
        if changed and record.get("is_epub") and not record.get("missing"):
            try:
                self.runner.cache_book_text(book_id)
            except (epub.EpubError, OSError):
                _log.warning("The book's text could not be read")
        return True

    def _refresh_reviewed(self, book):
        found = self._read_reader(lambda mount: save.reviewed_keys(self._store, book, (), mount))
        return found is not None

    def _refresh_all(self):
        try:
            rows = self._read_reader(reader.list_books)
            if not rows:
                return
            self._cache_rows(rows)
            for row in rows:
                if self._stop_event.is_set():
                    return
                if row["on_memory_card"] or not row["counts"]["total"]:
                    continue
                try:
                    if self._refresh_book(row["device_path"], row):
                        self._publish_book(book_id_for(row["device_path"]), "book")
                except Exception:
                    _log.exception("A book could not be refreshed")
            self._hub.publish({"topic": READER, "type": "books"})
        except Exception:
            _log.exception("The books could not be refreshed")
        finally:
            self.release_thread()

    def _publish_book(self, book_id, kind, **fields):
        self._hub.publish({"topic": f"book:{book_id}", "type": kind, "book_id": book_id, **fields})

    # Looking things up

    def _book(self, book_id):
        book = self._store.get_book(book_id)
        if book is None:
            raise ServiceError(404, "That book isn't on this computer.")
        return book

    def _highlights(self, book):
        rows = {}
        for h in book["data"].get("highlights") or []:
            if h.get("key") and h.get("text") and h["key"] not in rows:
                rows[h["key"]] = h
        known = {state["key"] for state in self._store.list_highlights(book["id"]).values()}
        if rows.keys() - known:
            self._store.ensure_highlights(book["id"], list(rows))
        return [{**h, "hid": hid_for(key)} for key, h in rows.items()]

    def _highlight_in(self, context, hid):
        h = context.by_hid.get(hid)
        if h is None:
            raise ServiceError(404, "That highlight isn't in this book.")
        return h

    def _require_editable(self, context, h):
        if h["status"] == "reviewed":
            raise ServiceError(409, "This highlight is already reviewed.")
        if context.is_skipped(h):
            raise ServiceError(409, "This highlight is skipped. Undo the skip to work on it.")

    def _find_companion(self, book):
        return companion.find(book["device_path"], self._mount())

    # GET /api/books/<id>

    @_route
    def book(self, book_id):
        book = self._book(book_id)
        return self._book_view(_Context(self, book))

    def _book_view(self, context):
        book = context.book
        data = book["data"]
        counts = context.counts()
        preview = self._preview(context)
        companion_view = self._companion_view(book, context.companion)
        total = len(context.highlights)
        views = [self._highlight_view(context, h, i, total) for i, h in enumerate(context.highlights)]
        connection_views = [self._connection_view(context, item) for item in context.connections]
        idea_views = [self._idea_view(item) for item in context.ideas]
        have = companion_view["have"]
        pending = counts["todo"]
        run_view = self._run_view(context, have)
        stored_counts = data.get("counts") or {}
        return {
            "id": book["id"],
            "device_path": book["device_path"],
            "title": book["title"],
            "author": book["author"],
            "stem": book["stem"],
            "ext": book["ext"],
            "deck": save.deck_name(book["title"]),
            "percent": data.get("percent"),
            "percent_finished": data.get("percent_finished"),
            "is_epub": bool(data.get("is_epub", book["ext"].lower() == "epub")),
            "missing": bool(data.get("missing")),
            "on_memory_card": bool(data.get("on_memory_card")),
            "error": data.get("error"),
            "not_loaded": not data.get("highlights") and bool(stored_counts.get("total")),
            "chapters": data.get("chapters") or [],
            "highlights": views,
            "connections": connection_views,
            "ideas": idea_views,
            "counts": {
                **counts,
                "cards": sum(len(context.drafts.get(h["hid"], [])) for h in context.highlights),
                "metaCount": len(connection_views),
                "ideaCount": len(idea_views),
            },
            "filters": [
                {"id": "all", "label": f"All {counts['all']}"},
                {"id": "todo", "label": f"To do {counts['todo']}"},
                {"id": "rev", "label": f"Reviewed {counts['rev']}"},
            ],
            "options": self.runner.default_options(book["id"]),
            "companion": companion_view,
            "run": run_view,
            "suggest": {
                "pending": pending,
                "lede": (
                    "Using your last choices. Only the cards you open get saved."
                    if run_view["phase"] != "idle"
                    else f"The AI writes cards for each of your {pending} pending highlights, "
                         "then reads them together to find connections across chapters."
                ),
                "sugBtn": f"Suggest cards for {_plural(pending, 'highlight')}",
            },
            "find": self._find_view(context),
            "ideaEmptyText": "No key ideas left. Your highlights and cards cover the rest." if have else "Nothing to compare against yet.",
            "ideasNeedComp": not have,
            "save": self._save_small(preview, context),
            "pending_saves": self._pending_saves_view(book["id"]),
            "reader": self._reader_small(),
        }

    def _highlight_view(self, context, h, index=None, total=None):
        hid = h["hid"]
        state = context.states.get(hid) or {}
        reviewed = h["status"] == "reviewed"
        skipped = context.is_skipped(h)
        opened = bool(state.get("opened"))
        drafts = context.drafts.get(hid, [])
        warnings = checks.check_cards(drafts) if drafts else []
        job = context.jobs.get(f"highlight:{hid}")
        writing = hid in context.writing
        if reviewed:
            mode = "rev"
        elif skipped:
            mode = "skip"
        elif drafts:
            mode = "cards" if opened else "sugg"
        elif writing or job:
            mode = "writing"
        else:
            mode = "todo"
        n = len(drafts)
        have = context.companion["found"]
        later = state.get("later_research") or ""
        count_text = {
            "rev": "Reviewed",
            "skip": "Skipped",
            "writing": "Writing",
            "sugg": f"{n} suggested",
            "cards": _plural(n, "card"),
        }.get(mode, "")
        meta = [count_text, "Note" if h.get("note") else "", "Later research" if have and later else "",
                "Failed" if state.get("error") and mode == "todo" else ""]
        saved = self._saved_rows(context, h) if reviewed else []
        banner = ""
        if reviewed:
            banner = (
                "Reviewed. These are the cards you saved from it." if saved else
                "Reviewed, with no cards on record. You skipped it, or its cards are older than the 40 the plugin remembers."
            )
        elif skipped:
            banner = "Skipped. It will count as reviewed when you save."
        passage = _passage_fields(h)
        view = {
            "id": hid,
            "hid": hid,
            "key": h["key"],
            "text": h["text"],
            **passage,
            "passage_note": "" if passage["found"] else _NOT_MATCHED,
            "note": h.get("note"),
            "ch": h.get("chapter"),
            "chapter_number": h.get("chapter_number"),
            "chapter_name": h.get("chapter_name") or h.get("chapter") or "No chapter",
            "chapterFull": h.get("chapter_card") or h.get("chapter") or "",
            "chapterShort": h.get("chapter_card") or h.get("chapter") or "",
            "where": save._where(h),
            "whereLong": _where_long(h),
            "page": h.get("page"),
            "percent": h.get("percent"),
            "status": h["status"],
            "state": mode,
            "glyph": GLYPHS[mode],
            "meta": " · ".join(part for part in meta if part),
            "opened": opened,
            "skipped": skipped,
            "error": state.get("error"),
            "later": later if have else "",
            "cards": [
                _card_view(card, warn, context.jobs.get(f"card:{card['id']}"))
                for card, warn in zip(drafts, warnings)
            ],
            "saved": saved,
            "banner": banner,
            "cardsTitle": "Saved cards" if reviewed else "Cards for this highlight",
            "canEdit": not reviewed and not skipped,
            "canSkip": not reviewed and not skipped,
            "isSkipped": skipped,
            "writing": writing,
            "busy": job is not None,
            "busyCmd": job["command"] if job else "",
            "comp": self._notes_view(context, h),
            "removed_on_reader": bool(h.get("removed_on_reader")),
        }
        if index is not None:
            view["curPos"] = f"{index + 1} of {total}"
            view["index"] = index
        return view

    def _saved_rows(self, context, h):
        rows, seen = [], set()
        for card in context.saved.get(h["hid"], []):
            if (card["front"], card["back"]) not in seen:
                seen.add((card["front"], card["back"]))
                rows.append(_saved_row(card["front"], card["back"], card["kind"]))
        for entry in context.history:
            if not isinstance(entry, dict) or entry.get("quote") != h["text"]:
                continue
            front, back = entry.get("front") or "", entry.get("back") or ""
            if (front, back) not in seen:
                seen.add((front, back))
                rows.append(_saved_row(front, back))
        return rows

    def _ai_notes(self, context, h):
        # The AI wrote its own chapter names, so a name that isn't exact is matched as headings are
        notes = context.chapter_notes
        if not notes:
            return None
        for name in (h.get("chapter"), h.get("chapter_card"), h.get("chapter_name")):
            if name and name in notes:
                return notes[name]
        chapter = h.get("chapter")
        if not chapter:
            return None
        headings = "\n".join("### " + " ".join(name.split()) for name in notes)
        block = companion.chapter_block(headings, chapter)
        if not block["found"]:
            return None
        for name, value in notes.items():
            if _COMMENT.sub("", " ".join(name.split())).strip() == block["heading"]:
                return value
        return None

    def _notes_view(self, context, h):
        if context.companion_text is None:
            return None
        written = self._ai_notes(context, h)
        if written:
            live = {idea["id"] for idea in context.ideas}
            points = []
            for point in written["points"]:
                covered = bool(point.get("covered"))
                idea = point.get("idea")
                points.append({"t": point.get("text") or "", "glyph": "✓" if covered else "○", "covered": covered,
                               "idea": idea if idea in live else None})
            return {"found": True, "tagged": True, "big": written["big"], "pts": points}
        panel = self._notes_panel(context, h)
        if not panel["found"]:
            return {"found": False, "tagged": False, "big": "", "pts": []}
        points = [{"t": point, "glyph": "", "covered": None, "idea": None} for point in panel["points"]]
        return {"found": True, "tagged": False, "big": panel["big"], "pts": points}

    def _notes_panel(self, context, h):
        memo_key = (hash(context.companion_text), h.get("chapter"), h.get("percent"))
        panel = self._notes_memo.get(memo_key)
        if panel is None:
            if len(self._notes_memo) > NOTES_MEMO_SIZE:
                self._notes_memo.clear()
            panel = companion.notes_panel(context.companion_text, h.get("chapter"), h.get("percent"))
            self._notes_memo[memo_key] = panel
        return panel

    def _connection_view(self, context, item):
        sources = []
        for hid in item["sources"]:
            h = context.by_hid.get(hid)
            if h is not None:
                sources.append({"hid": hid, "mark": _passage_fields(h)["mark"], "where": save._where(h), "whereLong": _where_long(h)})
        chapters = save.connection_chapters(item, context.by_hid)
        n = len(sources)
        src_count = _plural(n, "highlight")
        return {
            "id": item["id"],
            "title": item["title"],
            "src": item["source"],
            "front": item["front"],
            "back": item["back"],
            "opened": item["opened"],
            "state": item["state"],
            "sources": sources,
            "chapters": chapters,
            "srcCount": src_count,
            "meta": " · ".join(part for part in (chapters, src_count, "" if item["opened"] else "Not opened") if part),
        }

    def _idea_view(self, item):
        return {
            "id": item["id"],
            "ch": item["chapter"],
            "chapter": item["chapter"],
            "title": item["title"],
            "note": item["note"],
            "quote": item["quote"],
            "src": item["source"],
            "front": item["front"],
            "back": item["back"],
            "opened": item["opened"],
            "state": item["state"],
            "meta": " · ".join(part for part in (item["chapter"], "" if item["opened"] else "Not opened") if part),
        }

    def _find_view(self, context):
        picks = {}
        for h in context.highlights:
            chapter = h.get("chapter")
            pick = picks.setdefault(chapter, {"id": chapter, "name": h.get("chapter_name") or chapter or "No chapter",
                                              "pending": 0, "all": 0})
            pick["all"] += 1
            if h["status"] != "reviewed" and not context.is_skipped(h):
                pick["pending"] += 1
        job = context.jobs.get("connections")
        return {
            "picks": list(picks.values()),
            "busy": job is not None,
            "busyCmd": job["command"] if job else "",
            "error": self._find_errors.get(context.book["id"]),
        }

    def _run_view(self, context, have):
        status = context.run
        progress = status.get("progress") or {}
        run_status = status.get("status")
        active = bool(status.get("active"))
        paused = run_status == runs.PAUSED
        counts = progress.get("highlights") or {}
        total, done = counts.get("total", 0), counts.get("done", 0)
        stage = progress.get("stage")

        rows = []
        if run_status is not None:
            finished = done >= total
            failed = counts.get("failed", 0) if finished else 0
            if failed:
                rows.append({"title": "Highlight cards", "path": f"{failed} of {_plural(total, 'highlight')} failed",
                             "ok": False, "failed": True})
            else:
                rows.append({
                    "title": "Highlight cards",
                    "path": f"Done · {total} of {total} highlights" if finished else f"{done} of {total} highlights",
                    "ok": finished,
                })
            connections = progress.get(runs.CONNECTIONS) or {}
            if connections.get("state") not in (None, runs.OFF):
                rows.append(self._stage_row(
                    "Connections", connections,
                    "Waiting for highlight cards", "Reading all highlights together",
                    "Fewer than two pending highlights to read",
                ))
            ideas = progress.get(runs.IDEAS) or {}
            if have and ideas.get("state") not in (None, runs.OFF):
                rows.append(self._stage_row(
                    "Key ideas you didn't highlight", ideas,
                    "Waiting", "Comparing the Book Companion with your highlights",
                    "Nothing in the Book Companion to compare yet",
                ))
        for row in rows:
            row["glyph"] = "✓" if row["ok"] else "✕" if row.get("failed") else "…"
            row["cls"] = "chk bad" if row.get("failed") else "chk"

        if active and paused:
            label = "Suggestions paused"
        elif stage == runs.CONNECTIONS:
            label = "Reading all highlights together"
        elif stage == runs.IDEAS:
            label = "Comparing the Book Companion with your highlights"
        else:
            label = f"Writing suggestions {min(done + 1, max(total, 1))} of {total}"
        offer = ""
        if status.get("offer_again"):
            offer = f"Suggestions stopped when pbAnki closed, after {done} of {_plural(total, 'highlight')}. Carry on?"
        return {
            **status,
            "phase": "busy" if active else "done" if run_status == runs.DONE else "idle",
            "paused": paused,
            "rows": rows,
            "offer": offer,
            "error": progress.get("error"),
            "bg": {
                "show": active,
                "active": active,
                "label": label,
                "btn": "Resume" if paused else "Pause",
                "cells": [{"cls": "bgc on" if i < done else "bgc"} for i in range(total)],
            },
        }

    @staticmethod
    def _stage_row(title, stage, waiting_text, running_text, skipped_text):
        state = stage.get("state")
        if state == runs.FINISHED:
            return {"title": title, "path": f"Done · {stage.get('found') or 0} found", "ok": True}
        if state == runs.FAILED:
            return {"title": title, "path": stage.get("error") or "This step failed", "ok": False, "failed": True}
        if state == runs.NOTHING_TO_DO:
            return {"title": title, "path": skipped_text, "ok": True}
        return {"title": title, "path": running_text if state == runs.WRITING else waiting_text, "ok": False}

    def _companion_view(self, book, found=None):
        device_path = book["device_path"]
        found = found if found is not None else self._find_companion(book)
        stage = self._creation_stage(device_path)
        have = found["found"] and stage != "found"
        preview = companion.create_preview(device_path)
        skill = companion.skill_status()
        if have:
            status = f"Book Companion {found['label']}"
        else:
            status = {"running": "Being created", "found": "Ready to copy"}.get(stage, "None yet")
        connected = self._connected()
        return {
            "have": have,
            "none": not have and stage not in ("running", "found"),
            "running": not have and stage == "running",
            "ready": not have and stage == "found",
            "busy": not have and stage in ("running", "found"),
            "busyText": {
                "running": "Creating the Book Companion in Terminal. You can keep working.",
                "found": "Your Book Companion is ready to copy to the reader.",
            }.get(stage, ""),
            "stage": "have" if have else stage if stage in ("running", "found") else "none",
            "status": status,
            "where": found["where"],
            "label": found["label"],
            "path": found["path"],
            "file_name": found["file_name"],
            "dest": f"Beside the book: {found['file_name']}",
            "command": preview["command"],
            "watching": preview["watching"],
            "skill": skill,
            "can_create": skill["ok"] and connected,
            "can_copy": connected,
            "cutOff": companion.copy_cut_off(device_path),
            "problem": skill["problem"] or ("" if connected else "Plug in the reader first, so pbAnki can copy the book."),
        }

    # POST /api/books/<id>/open

    @_route
    def open_book(self, book_id):
        """Marks the book current, refreshes it from the reader and starts the automatic run if due."""
        book = self._store.get_book(book_id)
        if book is None:
            self.books()
            book = self._book(book_id)
        data = book["data"]
        if data.get("on_memory_card"):
            raise ServiceError(409, "This book is on the memory card, which pbAnki can't reach.")
        self._refresh_book(book["device_path"])
        book = self._book(book_id)
        data = book["data"]
        if not data.get("highlights") and (data.get("counts") or {}).get("total"):
            raise ServiceError(409, _NOT_CACHED)
        if self._store.get_setting("current_book") != book_id:
            self._store.set_setting("current_book", book_id)
            self._hub.publish({"topic": SETTINGS, "type": "current_book", "book_id": book_id})
        started = self._auto_start(book_id)
        return {**self._book_view(_Context(self, book)), "auto_started": started}

    def _auto_start(self, book_id):
        settings = self._store.get_settings()
        usable = {model["id"] for model in self._usable_models(settings)}
        if settings["model"] not in usable:
            return False
        try:
            return self.runner.auto_start(book_id) is not None
        except runs.RunError:
            return False

    # POST /api/books/<id>/run

    @_route
    def run(self, book_id, action, options=None):
        """`action` is start, pause, resume or stop. `options` applies to start."""
        self._book(book_id)
        if action == "start":
            chosen = self._checked_options(book_id, options, _RUN_KEYS)
            self._check_model(chosen.get("model") or self.runner.default_options(book_id)["model"])
            self._remember(chosen)
            self.runner.start(book_id, chosen)
        elif action == "pause":
            self.runner.pause(book_id)
        elif action == "resume":
            self.runner.resume(book_id)
        elif action == "stop":
            self.runner.stop(book_id)
        else:
            raise ServiceError(400, "A run can only start, pause, resume or stop.")
        context = _Context(self, self._book(book_id))
        return {"run": self._run_view(context, context.companion["found"])}

    def _checked_options(self, book_id, options, keys):
        """The options given, checked. Left-out ones stay out, so the book's defaults apply."""
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise ServiceError(400, "Those options couldn't be read.")
        given = {key: options[key] for key in keys if options.get(key) is not None}
        if "count" in given:
            count = given["count"]
            if isinstance(count, bool) or not isinstance(count, int):
                raise ServiceError(400, "The number of cards has to be a whole number.")
            given["count"] = max(1, min(count, runs.MAX_CARDS))
        if given.get("kind", "qa") not in runs.CARD_KINDS:
            raise ServiceError(400, "That kind of card isn't one pbAnki can suggest.")
        if given.get("sees", epub.MODE_WORDS) not in epub.AI_MODES:
            raise ServiceError(400, "That choice of how much the AI reads isn't one pbAnki knows.")
        if given.get("companion", companion.OFF) not in companion.SEND_OPTIONS:
            raise ServiceError(400, "That choice for the Book Companion isn't one pbAnki knows.")
        if "prompt" in given:
            prompts.check_name(given["prompt"])
        if "hint" in given and not isinstance(given["hint"], str):
            raise ServiceError(400, "The note to the AI has to be text.")
        for key in ("connections", "ideas"):
            if key in given and not isinstance(given[key], bool):
                raise ServiceError(400, "That choice has to be on or off.")
        for key in ("model", "meta_model"):
            if key in given:
                self._check_model(given[key])
        return given

    def _remember(self, chosen):
        """Keeps the choices just used, so the next book starts with them."""
        current = self._store.get_settings()
        changes = {
            setting: chosen[option]
            for option, setting in _REMEMBERED.items()
            if option in chosen and current.get(setting) != chosen[option]
        }
        if changes:
            self._store.set_settings(changes)
            self._hub.publish({"topic": SETTINGS, "type": "changed", "keys": sorted(changes)})

    # Highlights

    def _highlight_response(self, book_id, hid):
        context = _Context(self, self._book(book_id))
        h = self._highlight_in(context, hid)
        preview = self._preview(context)
        index = context.highlights.index(h)
        return {
            "highlight": self._highlight_view(context, h, index, len(context.highlights)),
            "counts": context.counts(),
            "save": self._save_small(preview, context),
        }

    @_route
    def open_highlight(self, book_id, hid):
        context = _Context(self, self._book(book_id))
        self._highlight_in(context, hid)
        self._store.set_opened(book_id, hid, True)
        self._publish_book(book_id, "opened", hid=hid)
        return self._highlight_response(book_id, hid)

    @_route
    def skip_highlight(self, book_id, hid, skipped=True):
        context = _Context(self, self._book(book_id))
        h = self._highlight_in(context, hid)
        if not isinstance(skipped, bool):
            raise ServiceError(400, "Skip has to be on or off.")
        if h["status"] == "reviewed":
            raise ServiceError(409, "This highlight is already reviewed.")
        self._store.set_skipped(book_id, hid, skipped)
        self._publish_book(book_id, "skipped", hid=hid, skipped=skipped)
        return self._highlight_response(book_id, hid)

    @_route
    def add_card(self, book_id, hid, kind="qa"):
        """Adds a card to write by hand. A cloze card starts with the highlight's text to mark up."""
        if kind not in runs.CARD_KINDS:
            raise ServiceError(400, "That kind of card isn't one pbAnki can add.")
        context = _Context(self, self._book(book_id))
        h = self._highlight_in(context, hid)
        self._require_editable(context, h)
        front = _passage_fields(h)["mark"] if kind == "cloze" else ""
        with self._store.transaction():
            card = self._store.add_card(book_id, hid, kind, front=front, back="")
            self._store.set_opened(book_id, hid, True)
        self._publish_book(book_id, "card", hid=hid, card_id=card["id"], change="added")
        return self._card_response(card)

    @_route
    def suggest(self, book_id, hid, options=None):
        """"Suggest N". Starts the AI on a thread and returns at once. The cards land in the store,
        then a book event of type "suggest" says it finished."""
        context = _Context(self, self._book(book_id))
        h = self._highlight_in(context, hid)
        self._require_editable(context, h)
        if hid in context.writing:
            raise ServiceError(409, "Cards for this highlight are still being written. Try again in a moment.")
        chosen = self._checked_options(book_id, options, _SUGGEST_KEYS)
        model_id = chosen.get("model") or self.runner.default_options(book_id)["model"]
        self._check_model(model_id)
        self._remember(chosen)
        asked = dict(chosen)
        kind = asked.get("kind") or self.runner.default_options(book_id)["kind"]
        if kind == "cloze":
            asked["count"] = 1

        def work(token):
            result = self.runner.suggest(book_id, hid, asked, cancel=token)
            return {
                "state": runs.STOPPED if result["cancelled"] else runs.FAILED if result["error"] else runs.FINISHED,
                "error": result["error"],
                "cards": len(result["cards"]),
            }

        def failed(error):
            self._store.set_highlight_error(book_id, hid, error)

        self._start_job(book_id, f"highlight:{hid}", "suggest", model_id, work, failed, hid=hid)
        return {"working": True, **self._highlight_response(book_id, hid)}

    @_route
    def define(self, book_id, hid, term, model=None):
        """Definition cards for 1 to 3 selected words, on a thread. A book event of type "define"
        says it finished."""
        words = str(term or "").split()
        if not words or len(words) > MAX_TERM_WORDS:
            raise ServiceError(400, "Select 1 to 3 words to define them.")
        term = " ".join(words)
        context = _Context(self, self._book(book_id))
        h = self._highlight_in(context, hid)
        self._require_editable(context, h)
        model_id = model or context.settings["model"]
        self._check_model(model_id)

        def work(token):
            about = self.runner.card_context(book_id, hid, {"model": model_id})
            messages = cardgen.build_definition(term, about["about"], {"prompt": about["request"]["prompt"]}, about["notes"])
            answer = self._ask(model_id, messages, token)
            cards, error = cardgen.definition_cards(answer)
            if error:
                raise ai.AIError(error)
            source = SOURCE_AI_WITH_COMPANION if about["notes"] else store_module.SOURCE_AI
            with self._store.transaction():
                if not self.runner.still_wanted(book_id, hid):
                    return {"state": runs.FINISHED, "error": None, "cards": 0}
                for card in cards:
                    self._store.add_card(book_id, hid, "def", card["front"], card["back"], source=source, suggested=card)
                self._store.set_highlight_error(book_id, hid, None)
            return {"state": runs.FINISHED, "error": None, "cards": len(cards)}

        def failed(error):
            self._store.set_highlight_error(book_id, hid, error)

        self._start_job(book_id, f"highlight:{hid}", "define", model_id, work, failed, hid=hid, term=term)
        return {"working": True, **self._highlight_response(book_id, hid)}

    @_route
    def passage(self, book_id, hid, more=0):
        """Text around a highlight, widened by `more` paragraphs each way. The cached passage is
        what shows with the reader unplugged, since wider text needs the book itself."""
        try:
            more = max(0, min(int(more or 0), MAX_MORE))
        except (TypeError, ValueError):
            raise ServiceError(400, "That amount of text couldn't be read.") from None
        context = _Context(self, self._book(book_id))
        h = self._highlight_in(context, hid)
        fields = _passage_fields(h)
        limited = False
        if more > 0 and fields["found"]:
            wider = self._wider_passage(context, h, more)
            if wider is not None and wider.get("found"):
                fields = _passage_fields({"passage": wider, "text": h["text"]})
            else:
                limited = True
        return {
            **fields,
            "more": more,
            "limited": limited,
            "limit_note": _SHOW_MORE_UNPLUGGED if limited else "",
            "passage_note": "" if fields["found"] else _NOT_MATCHED,
        }

    def _wider_passage(self, context, h, more):
        location = h.get("location") or {}
        device_path = context.book["device_path"]
        if not location.get("found") or context.book["ext"].lower() != "epub":
            return None

        def read(mount):
            local = paths.device_to_local(device_path, mount)
            if local is None:
                return None
            try:
                return epub.open_epub(local).passage(location, more)
            except (epub.EpubError, OSError):
                return None

        return self._read_reader(read)

    # Cards

    def _card_response(self, card):
        book_id, hid = card["book_id"], card["hid"]
        response = self._highlight_response(book_id, hid)
        current = next((c for c in response["highlight"]["cards"] if c["id"] == card["id"]), None)
        return {
            "card": current or _card_view(card),
            "cards": response["highlight"]["cards"],
            "highlight": response["highlight"],
            "counts": response["counts"],
            "save": response["save"],
        }

    def _card(self, card_id):
        card = self._store.get_card(card_id)
        if card is None:
            raise ServiceError(404, "That card isn't on this computer any more.")
        return card

    @_route
    def update_card(self, card_id, front=None, back=None):
        """Edits a draft. An AI card turns "AI, edited" by was_edited's rule."""
        self._check_texts(front, back)
        card = self._store.update_card(card_id, front=front, back=back)
        self._publish_book(card["book_id"], "card", hid=card["hid"], card_id=card_id, change="edited")
        return self._card_response(card)

    @_route
    def remove_card(self, card_id):
        card = self._store.remove_card(card_id)
        self._publish_book(card["book_id"], "card", hid=card["hid"], card_id=card_id, change="removed")
        return {
            "removed": _card_view(card),
            "undo": "Card removed. The AI won't suggest it again.",
            **{k: v for k, v in self._card_response(card).items() if k != "card"},
        }

    @_route
    def restore_card(self, card_id):
        card = self._card(card_id)
        if card["hid"] not in _Context(self, self._book(card["book_id"])).by_hid:
            raise ServiceError(409, "That highlight isn't on the reader any more, so this card can't come back.")
        card = self._store.restore_card(card_id)
        self._publish_book(card["book_id"], "card", hid=card["hid"], card_id=card_id, change="restored")
        return self._card_response(card)

    @_route
    def rewrite_card(self, card_id, instruction=None, model=None):
        """Asks the AI to redo one card, on a thread. A book event of type "rewrite" says it finished."""
        card = self._card(card_id)
        if card["state"] != DRAFT:
            raise ServiceError(409, "This card can't be edited any more.")
        if instruction is not None and not isinstance(instruction, str):
            raise ServiceError(400, "The note to the AI has to be text.")
        book_id, hid = card["book_id"], card["hid"]
        model_id = model or self._store.get_settings()["model"]
        self._check_model(model_id)
        original = {"front": card["front"], "back": card["back"]}

        def work(token):
            about = self.runner.card_context(book_id, hid, {"model": model_id})
            messages = cardgen.build_rewrite(
                original, about["about"], instruction or None, about["request"]["prompt"], about["notes"],
            )
            answer = self._ask(model_id, messages, token)
            rewritten, error = cardgen.parse_rewrite(answer, original)
            if error:
                raise ai.AIError(error)
            with self._store.transaction():
                now = self._store.get_card(card_id)
                if now is None or now["state"] != DRAFT:
                    raise ai.AIError("This card was removed while the AI was rewriting it.")
                if now["front"] != original["front"] or now["back"] != original["back"]:
                    raise ai.AIError("The card changed while the AI was rewriting it, so your version was kept.")
                self._store.rewrite_card(card_id, rewritten["front"], rewritten["back"])
                self._store.set_highlight_error(book_id, hid, None)
            return {"state": runs.FINISHED, "error": None}

        def failed(error):
            self._store.set_highlight_error(book_id, hid, error)

        self._start_job(book_id, f"card:{card_id}", "rewrite", model_id, work, failed, hid=hid, card_id=card_id)
        return {"working": True, **self._card_response(self._card(card_id))}

    @staticmethod
    def _check_texts(front, back):
        for value in (front, back):
            if value is not None and not isinstance(value, str):
                raise ServiceError(400, "Card text has to be text.")

    # Connections

    @_route
    def find_connections(self, book_id, options=None):
        """Finds connections on a thread. `options`: chapters, scope, count, model, prompt, hint.
        A book event of type "find" says it finished."""
        self._book(book_id)
        options = options if options is not None else {}
        if not isinstance(options, dict):
            raise ServiceError(400, "Those options couldn't be read.")
        scope = options.get("scope") or runs.SCOPE_PENDING
        if scope not in (runs.SCOPE_PENDING, runs.SCOPE_ALL):
            raise ServiceError(400, "The highlights to read have to be pending or all.")
        chapters = options.get("chapters")
        if chapters is not None and (not isinstance(chapters, list) or not all(c is None or isinstance(c, str) for c in chapters)):
            raise ServiceError(400, "Those chapters couldn't be read.")
        count = options.get("count")
        if count is not None:
            if isinstance(count, bool) or not isinstance(count, int):
                raise ServiceError(400, "The number of connections has to be a whole number.")
            count = max(1, min(count, runs.MAX_CARDS))
        hint = options.get("hint")
        if hint is not None and not isinstance(hint, str):
            raise ServiceError(400, "The note to the AI has to be text.")
        prompt = options.get("prompt")
        if prompt is not None:
            prompts.check_name(prompt)
        defaults = self.runner.default_options(book_id)
        model_id = options.get("model") or defaults["meta_model"]
        self._check_model(model_id)
        self._remember({key: value for key, value in (("meta_model", options.get("model")), ("prompt", prompt)) if value})
        asked = {"chapters": chapters, "scope": scope, "count": count, "model": model_id,
                 "prompt": prompt or defaults["prompt"], "hint": hint}

        def work(token):
            result = self.runner.find_connections(book_id, asked, cancel=token)
            self._find_errors[book_id] = result["error"]
            added = [item["id"] for item in result["connections"]]
            return {
                "state": runs.STOPPED if result["cancelled"] else runs.FAILED if result["error"] else runs.FINISHED,
                "error": result["error"],
                "found": len(added),
                "connections": added,
            }

        def failed(error):
            self._find_errors[book_id] = error

        self._find_errors.pop(book_id, None)
        self._start_job(book_id, "connections", "find", model_id, work, failed)
        context = _Context(self, self._book(book_id))
        return {"working": True, "find": self._find_view(context)}

    def _connection_response(self, item):
        context = _Context(self, self._book(item["book_id"]))
        return {
            "connection": self._connection_view(context, item),
            "connections": [self._connection_view(context, row) for row in context.connections],
            "save": self._save_small(self._preview(context), context),
        }

    def _connection(self, connection_id):
        item = self._store.get_connection(connection_id)
        if item is None:
            raise ServiceError(404, "That connection isn't on this computer any more.")
        return item

    def _idea(self, idea_id):
        item = self._store.get_idea(idea_id)
        if item is None:
            raise ServiceError(404, "That key idea isn't on this computer any more.")
        return item

    @_route
    def open_connection(self, connection_id):
        self._connection(connection_id)
        self._store.open_connection(connection_id)
        item = self._connection(connection_id)
        self._publish_book(item["book_id"], "connection", connection_id=connection_id, change="opened")
        return self._connection_response(item)

    @_route
    def update_connection(self, connection_id, front=None, back=None):
        self._check_texts(front, back)
        item = self._store.update_connection(connection_id, front=front, back=back)
        self._publish_book(item["book_id"], "connection", connection_id=connection_id, change="edited")
        return self._connection_response(item)

    @_route
    def remove_connection(self, connection_id):
        self._connection(connection_id)
        self._store.remove_connection(connection_id)
        item = self._connection(connection_id)
        self._publish_book(item["book_id"], "connection", connection_id=connection_id, change="removed")
        return self._connection_response(item)

    # Key ideas

    def _idea_response(self, item):
        context = _Context(self, self._book(item["book_id"]))
        return {
            "idea": self._idea_view(item),
            "ideas": [self._idea_view(row) for row in context.ideas],
            "save": self._save_small(self._preview(context), context),
        }

    @_route
    def open_idea(self, idea_id):
        self._idea(idea_id)
        self._store.open_idea(idea_id)
        item = self._idea(idea_id)
        self._publish_book(item["book_id"], "idea", idea_id=idea_id, change="opened")
        return self._idea_response(item)

    @_route
    def update_idea(self, idea_id, front=None, back=None):
        self._check_texts(front, back)
        item = self._store.update_idea(idea_id, front=front, back=back)
        self._publish_book(item["book_id"], "idea", idea_id=idea_id, change="edited")
        return self._idea_response(item)

    @_route
    def remove_idea(self, idea_id):
        self._idea(idea_id)
        self._store.remove_idea(idea_id)
        item = self._idea(idea_id)
        self._publish_book(item["book_id"], "idea", idea_id=idea_id, change="removed")
        return self._idea_response(item)

    # Save

    def _preview(self, context, mark=None):
        if mark is None:
            mark = context.settings.get("mark_connection_sources", True)
        return save.preview(self._store, context.book, context.highlights, bool(mark))

    def _save_small(self, preview, context):
        waiting = save.pending_saves(self._store, context.book["id"])
        count = save.with_held(preview, waiting)["count"]
        return {
            "count": count,
            "saveLabel": f"Save {_plural(count, 'card')}",
            "noCards": count == 0,
            "waiting": any(r["status"] == save.WAITING for r in waiting),
            "failed": any(r["status"] == save.FAILED for r in waiting),
            "saving": self._is_saving(),
        }

    def _is_saving(self):
        with self._saving_lock:
            return self._saving > 0

    @contextmanager
    def _save_under_way(self):
        with self._saving_lock:
            self._saving += 1
        try:
            yield
        finally:
            with self._saving_lock:
                self._saving -= 1

    def _done_view(self, summary):
        cards, marked = summary.get("cards", 0), summary.get("marked", 0)
        return {
            "doneTitle": f"{_plural(cards, 'card')} on your reader",
            "lede": f"Open pbAnki on the reader. They'll be waiting in the deck {summary.get('deck', '')}.",
            "deck": summary.get("deck", ""),
            "doneRows": [
                {"t": "Backed up on this computer", "p": _tilde(paths.db_path())},
                {"t": "Added to pbAnki's inbox", "p": paths.INBOX_FILE},
                {"t": f"{_plural(marked, 'highlight')} noted as reviewed for the plugin",
                 "p": f"{paths.REVIEWED_FILE}. Takes effect when you next open the book in KOReader."},
                {"t": "Read back from the reader", "p": "Both files landed"},
            ],
            "saved": cards,
            "savedMark": marked,
            "ejectLabel": "Eject reader",
        }

    def _save_result_view(self, result):
        view = {**result}
        view["done"] = self._done_view(result["summary"]) if result["status"] == VERIFIED else None
        return view

    def _publish_save(self, result):
        view = self._save_result_view(result)
        self._publish_book(view.pop("book_id"), "save", **view)

    def _finish_saves(self):
        pending = self._store.unverified_batches()
        if not pending:
            return []
        with self._save_under_way():
            results = save.finish_pending(self._store, self._mount())
        for result in results:
            self._publish_save(result)
        return results

    @_route
    def save_preview(self, book_id, mark_connection_sources=None):
        """The Save cards screen. Left out, the checkbox is as last saved."""
        self._refresh_reviewed(self._book(book_id))
        context = _Context(self, self._book(book_id))
        mark = mark_connection_sources
        if mark is None:
            mark = context.settings.get("mark_connection_sources", True)
        elif not isinstance(mark, bool):
            raise ServiceError(400, "That choice has to be on or off.")
        pending = self._pending_saves_view(book_id)
        fresh = self._preview(context, mark)
        preview = save.with_held(fresh, pending)
        waiting = [r for r in pending if r["status"] == save.WAITING]
        failed = [r for r in pending if r["status"] == save.FAILED]
        verified = self._store.list_batches(book_id, (VERIFIED,))
        connected = self._connected()
        mount = self._mount()
        saving = self._is_saving()
        return {
            **preview,
            "markMeta": bool(mark),
            "saveCount": preview["count"],
            "heldCount": preview["count"] - fresh["count"],
            "saveNoun": preview["noun"],
            "needCount": len(preview["needs"]),
            "needText": preview["need_text"],
            "markText": preview["mark_text"],
            "leftText": preview["left_text"],
            "saveBlocked": preview["blocked"] or saving,
            "saveBtn": "Saving..." if saving else "Save to reader" if connected else "Back up and wait for reader",
            "saveWaiting": bool(waiting),
            "saveUnplugNote": not connected and not waiting,
            "waitText": f"Step 1 is done. The rest happens by itself when {mount} appears.",
            "waiting": waiting,
            "failed": failed,
            "saving": saving,
            "connected": connected,
            "steps": [
                {"n": 1, "title": "Copied to the backup on this computer", "detail": _tilde(paths.db_path())},
                {"n": 2, "title": "Added to pbAnki's inbox on the reader", "detail": paths.INBOX_FILE},
                {"n": 3, "title": preview["mark_text"],
                 "detail": f"{paths.REVIEWED_FILE}. The plugin reads this when you next open the book in KOReader. "
                           "It stops offering these highlights, and adds the cards to the history it learns your style from."},
                {"n": 4, "title": "Read back from the reader to check",
                 "detail": "This reader has lost newly copied files before, so pbAnki confirms they landed."},
            ],
            "last_saved": self._save_result_view(save.batch_result(verified[-1])) if verified else None,
        }

    @_route
    def save_book(self, book_id, mark_connection_sources=None):
        """Saves what the preview shows. Blocks until the reader has proven the write, or the cards
        are backed up and waiting for it. A second save during this one is refused."""
        self._refresh_reviewed(self._book(book_id))
        context = _Context(self, self._book(book_id))
        if mark_connection_sources is not None:
            if not isinstance(mark_connection_sources, bool):
                raise ServiceError(400, "That choice has to be on or off.")
            self._store.set_setting("mark_connection_sources", mark_connection_sources)
            mark = mark_connection_sources
        else:
            mark = context.settings.get("mark_connection_sources", True)
        if not self._save_lock.acquire(blocking=False):
            raise ServiceError(409, "A save is already under way.")
        try:
            before = [batch["id"] for batch in self._store.unverified_batches()]
            with self._save_under_way():
                self._publish_book(book_id, "save", status="saving", batch=None, error=None, summary=None, done=None)
                result = save.save(self._store, context.book, context.highlights, self._mount(), bool(mark))
            for batch_id in before:
                other = self._store.get_batch(batch_id)
                if other is not None:
                    self._publish_save(save.batch_result(other))
            self._publish_save(result)
        finally:
            self._save_lock.release()
        return {**self._save_result_view(result), "save": self._save_small(self._preview(_Context(self, self._book(book_id))), context)}

    @_route
    def save_retry(self, book_id):
        """Try again: finishes every waiting batch, oldest first, and reports this book's."""
        self._book(book_id)
        if not self._store.unverified_batches(book_id):
            raise ServiceError(409, "There's nothing waiting to be saved.")
        if not self._save_lock.acquire(blocking=False):
            raise ServiceError(409, "A save is already under way.")
        try:
            results = self._finish_saves()
        finally:
            self._save_lock.release()
        mine = [r for r in results if r["book_id"] == book_id]
        # The watcher may have finished them just before this call
        if not mine:
            mine = [save.batch_result(self._store.list_batches(book_id)[-1])]
        return {**self._save_result_view(mine[-1]), "results": [self._save_result_view(r) for r in mine]}

    # Reader actions

    @_route
    def reader_eject(self):
        """Ejects the reader, or says plainly why it can't."""
        mount = self._mount()
        if not self._connected():
            return {"ejected": True, "ejectLabel": "Safe to unplug"}
        self._eject(mount)
        self._hub.publish({"topic": READER, "type": "ejected", "mount": str(mount)})
        return {"ejected": True, "ejectLabel": "Safe to unplug"}

    @_route
    def reader_plugin_update(self):
        """Copies the plugin to the reader, on the button only, as a proven write."""
        if not self._connected():
            raise ServiceError(409, "Plug in your PocketBook to update the plugin.")
        result = save.plugin_update(self._mount())
        self._hub.publish({"topic": READER, "type": "plugin", **result})
        return {**result, "reader": self._reader_view()}

    # Settings

    @_route
    def update_settings(self, changes):
        """Changes settings, then returns state(). At least one assistant stays on, and a model
        whose assistant was turned off moves to the first one left."""
        if not isinstance(changes, dict):
            raise ServiceError(400, "Those settings couldn't be read.")
        unknown = [key for key in changes if key not in _SETTING_KEYS]
        if unknown:
            raise ServiceError(400, "pbAnki doesn't know that setting.")
        current = self._store.get_settings()
        new = self._checked_settings(changes, current)
        merged = {**current, **new}
        if "assistants" in new:
            left = ai.enabled_models(merged["assistants"])
            for key in ("model", "meta_model"):
                if merged[key] not in {m["id"] for m in left}:
                    new[key] = merged[key] = left[0]["id"]
            self._detected = None
        for key in ("model", "meta_model"):
            if key in changes:
                self._check_model(merged[key], merged)
        self._store.set_settings(new)
        self._hub.publish({"topic": SETTINGS, "type": "changed", "keys": sorted(new)})
        return self.state()

    def _checked_settings(self, changes, current):
        new = {}
        for key, value in changes.items():
            if key == "mount":
                if not isinstance(value, str) or not value.strip():
                    raise ServiceError(400, "Say where the reader mounts, such as /Volumes/PB629.")
                expanded = os.path.expanduser(value.strip())
                if not os.path.isabs(expanded):
                    raise ServiceError(400, "Where the reader mounts has to be a full path, such as /Volumes/PB629.")
                new[key] = expanded.rstrip("/") or "/"
            elif key == "assistants":
                new[key] = self._checked_assistants(value, current["assistants"])
            elif key in ("auto_suggest", "suggest_connections", "suggest_ideas", "mark_connection_sources"):
                if not isinstance(value, bool):
                    raise ServiceError(400, "That setting has to be on or off.")
                new[key] = value
            elif key in ("model", "meta_model"):
                try:
                    ai.model_info(value)
                except ai.AIError as e:
                    raise ServiceError(400, str(e)) from None
                new[key] = value
            elif key == "companion_send":
                if value not in companion.SEND_OPTIONS:
                    raise ServiceError(400, "That choice for the Book Companion isn't one pbAnki knows.")
                new[key] = value
            elif key == "sees":
                if value not in epub.AI_MODES:
                    raise ServiceError(400, "That choice of how much the AI reads isn't one pbAnki knows.")
                new[key] = value
            elif key == "kind":
                if value not in runs.CARD_KINDS:
                    raise ServiceError(400, "That kind of card isn't one pbAnki can suggest.")
                new[key] = value
            elif key == "count":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ServiceError(400, "The number of cards has to be a whole number.")
                new[key] = max(1, min(value, runs.MAX_CARDS))
            elif key == "last_prompt":
                new[key] = prompts.check_name(value)
        return new

    @staticmethod
    def _checked_assistants(value, current):
        if not isinstance(value, dict) or not all(k in ai.ASSISTANTS and isinstance(v, bool) for k, v in value.items()):
            raise ServiceError(400, "Those assistants couldn't be read.")
        merged = {**current, **value}
        if not any(merged.values()):
            raise ServiceError(400, _BOTH_OFF)
        return merged

    # Prompts

    def _prompts_changed(self, changed):
        self._hub.publish({"topic": PROMPTS, "type": "changed", "paths": [str(path) for path in changed]})

    @_route
    def prompt(self, name):
        """A prompt folder's files and their text, for the Prompt file drawer."""
        prompts.check_name(name)
        files = []
        for kind in prompts.KINDS:
            row = prompts.read_one(name, kind["key"])
            files.append({
                "kind": row["kind"],
                "label": row["label"],
                "file": row["file"],
                "name": row["file"],
                "status": row["status"],
                "note": row["status"],
                "path": str(row["path"]),
                "display": _tilde(row["own_path"]),
                "text": row["text"],
                "undone_on_start": name == prompts.STANDARD,
            })
        return {
            "name": name,
            "dir": _tilde(paths.prompts_dir() / name),
            "files": files,
            "reloadAgo": self._prompt_watcher.seconds_since_reload(),
        }

    def _launch_prompt(self, row, target, flag):
        if not self._launch(["open", flag, target]):
            raise ServiceError(500, "That prompt file couldn't be opened.")
        return {"opened": target, "display": _tilde(row["own_path"]), "file": row["file"], "status": row["status"]}

    @_route
    def open_prompt(self, name, file_name):
        """Opens the file in the chosen folder. One taken from Standard gets its own copy first,
        because edits to Standard are undone on the next start."""
        try:
            row = prompts.make_own(name, file_name)
        except OSError:
            raise ServiceError(500, "That prompt file couldn't be opened.") from None
        return self._launch_prompt(row, row["own_path"], "-t")

    @_route
    def reveal_prompt(self, name, file_name):
        """Shows the file in the chosen folder in Finder, or the folder when the file isn't there."""
        row = prompts.read_one(name, file_name)
        own = Path(row["own_path"])
        return self._launch_prompt(row, str(own if own.exists() else own.parent), "-R")

    # Book Companion

    def _creation_stage(self, device_path):
        try:
            return companion.creation_status(device_path)["stage"]
        except companion.CompanionError:
            return "none"

    def _track_creations(self):
        for book in self._store.list_books():
            stage = self._creation_stage(book["device_path"])
            if stage in ("running", "found"):
                with self._creations_lock:
                    self._creations[book["id"]] = {"device_path": book["device_path"], "stage": stage}

    def _poll_creations(self):
        with self._creations_lock:
            tracked = list(self._creations.items())
        for book_id, item in tracked:
            stage = self._creation_stage(item["device_path"])
            if stage == item["stage"]:
                continue
            with self._creations_lock:
                if stage in ("running", "found"):
                    self._creations[book_id]["stage"] = stage
                else:
                    self._creations.pop(book_id, None)
            self._publish_book(book_id, "companion", stage=stage)

    def _companion_response(self, book_id):
        book = self._book(book_id)
        return {"companion": self._companion_view(book)}

    @_route
    def companion_create(self, book_id):
        """Opens the Book Companion skill in Terminal. Only ever runs on the user's click."""
        book = self._book(book_id)
        with self._create_lock:
            if self._find_companion(book)["found"]:
                raise ServiceError(409, "This book already has a Book Companion.")
            stage = self._creation_stage(book["device_path"])
            if stage == "running":
                raise ServiceError(409, "This Book Companion is already being created.")
            if stage == "found":
                raise ServiceError(409, "This Book Companion is ready. Copy it to the reader or keep it on this computer.")
            companion.create(book["device_path"], self._mount())
        with self._creations_lock:
            self._creations[book_id] = {"device_path": book["device_path"], "stage": "running"}
        self._publish_book(book_id, "companion", stage="running")
        return self._companion_response(book_id)

    def _require_ready_companion(self, book):
        if self._creation_stage(book["device_path"]) != "found" and not companion.copy_cut_off(book["device_path"]):
            raise ServiceError(409, "The Book Companion isn't ready yet.")

    @_route
    def companion_copy(self, book_id):
        """Copy to reader: a proven write beside the book."""
        book = self._book(book_id)
        self._require_ready_companion(book)
        if not self._connected():
            raise ServiceError(409, "Plug in your reader to copy it.")
        with save._write_lock:
            companion.copy_to_reader(book["device_path"], self._mount())
        self._companion_finished(book_id)
        return self._companion_response(book_id)

    @_route
    def companion_keep(self, book_id):
        """Keep it on this computer."""
        book = self._book(book_id)
        self._require_ready_companion(book)
        companion.keep_local(book["device_path"])
        self._companion_finished(book_id)
        return self._companion_response(book_id)

    def _companion_finished(self, book_id):
        with self._creations_lock:
            self._creations.pop(book_id, None)
        self._publish_book(book_id, "companion", stage="have")

    # Background AI work

    def _ask(self, model_id, messages, token):
        if token.cancelled:
            raise ai.AICancelled()
        return (self._query or ai.query)(model_id, messages, cancel=token)

    def _jobs_of(self, book_id):
        with self._jobs_lock:
            return {target: job for (owner, target), job in self._jobs.items() if owner == book_id}

    def _start_job(self, book_id, target, kind, model_id, work, on_failure, **fields):
        """Runs `work(token)` on its own thread and publishes a book event of type `kind` once it
        has finished and no longer shows as working. A second job on the same target is refused."""
        key = (book_id, target)
        token = ai.CancelToken()
        job = {"kind": kind, "model": model_id, "command": _command_for(model_id), "token": token}

        def body():
            outcome = {}
            try:
                outcome = work(token)
            except ai.AICancelled:
                outcome = {"state": runs.STOPPED, "error": None}
            except (ai.AIError, runs.RunError, store_module.NotEditable, store_module.NotFound) as e:
                outcome = self._job_failed(str(e) or _SOMETHING_WRONG, on_failure)
            except Exception:
                _log.exception("A %s request failed", kind)
                outcome = self._job_failed(_SOMETHING_WRONG, on_failure)
            finally:
                with self._jobs_lock:
                    self._jobs.pop(key, None)
            try:
                self._publish_book(book_id, kind, **fields, **outcome)
            finally:
                self.release_thread()

        with self._jobs_lock:
            if self._closing:
                raise ServiceError(503, "pbAnki is closing.")
            if key in self._jobs:
                raise ServiceError(409, "The AI is still working on this. Try again in a moment.")
            job["thread"] = threading.Thread(target=body, name=f"pbanki-{kind}", daemon=True)
            self._jobs[key] = job
        job["thread"].start()

    def _job_failed(self, error, on_failure):
        if on_failure is not None:
            try:
                on_failure(error)
            except Exception:
                _log.exception("A failure could not be stored")
        return {"state": runs.FAILED, "error": error}
