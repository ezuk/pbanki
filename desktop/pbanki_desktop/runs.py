"""Background suggestion runs, one per book: highlight cards, then connections, then key ideas.

A Runner gets its store, AI call and event callback passed in. Everything it needs comes from
the book's cached data in SQLite, so runs work with the reader unplugged; the ePub is only read,
for a wider passage, when the reader happens to be there. No lock is held during an AI call.
"""

import copy
import logging
import os
import shutil
import threading
from collections import deque

from . import ai, cardgen, companion, epub, paths, prompts, reader
from .store import DRAFT, REMOVED, SAVED

_log = logging.getLogger(__name__)

PARALLEL = 3
MAX_CARDS = 10
HISTORY_SHOWN = 8
STOP_WAIT_SECONDS = 10

RUNNING = "running"
PAUSED = "paused"
STOPPED = "stopped"
DONE = "done"
INTERRUPTED = "interrupted"

HIGHLIGHTS = "highlights"
CONNECTIONS = "connections"
IDEAS = "ideas"

# Stage states in a run's progress
WAITING = "waiting"
WRITING = "running"
FINISHED = "done"
FAILED = "failed"
OFF = "off"
NOTHING_TO_DO = "skipped"

SCOPE_PENDING = "pending"
SCOPE_ALL = "all"

CARD_KINDS = ("qa", "cloze", "qonly")
_OPTION_KEYS = ("count", "kind", "model", "prompt", "sees", "companion", "hint", "highlights", "connections", "meta_model", "ideas")

# In a run's progress: percent_finished when key ideas last ran, kept from run to run
IDEAS_LOOKED = "ideas_looked"

_UNEXPECTED = "Something went wrong while writing these cards. Try Suggest again."
_RUN_PROBLEM = "Something went wrong and the suggestions stopped early. Your finished cards are safe."
_NOT_CACHED = "This book isn't on this computer yet. Plug in your reader and open the book first."


class RunError(Exception):
    """A request that can't be done. The message is a plain sentence shown to the user."""


class _Run:
    def __init__(self, book_id, options, progress, queue):
        self.book_id = book_id
        self.options = options
        self.progress = progress
        self.queue = deque(queue)
        self.status = RUNNING
        self.stopping = False
        self.problem = False
        self.tokens = set()
        self.save_lock = threading.Lock()
        self.finished = threading.Event()


def _stage_state(on, name, finished):
    if name in finished:
        return FINISHED
    return WAITING if on else OFF


def ideas_looked(progress):
    """percent_finished when key ideas last ran, or None if they never have."""
    looked = progress.get(IDEAS_LOOKED)
    if looked is None and IDEAS in (progress.get("finished") or []):
        return 0
    return looked


def _new_progress(total, finished, connections_on, ideas_on, previous=None):
    previous = previous or {}

    def stage(on, name):
        # A step this run has already finished keeps what it found
        found = (previous.get(name) or {}).get("found", 0) if name in finished else 0
        return {"state": _stage_state(on, name, finished), "found": found, "error": None}

    return {
        "stage": HIGHLIGHTS,
        "highlights": {"total": total, "done": 0, "failed": 0, "cards": 0},
        "writing": [],
        CONNECTIONS: stage(connections_on, CONNECTIONS),
        IDEAS: stage(ideas_on, IDEAS),
        "finished": list(finished),
        IDEAS_LOOKED: ideas_looked(previous),
        "error": None,
    }


def _joined(*parts):
    return "\n\n".join(part for part in parts if part)


def _cached_context(h):
    # The AI's view of the passage from the book's cached data
    passage = h.get("passage") or {}
    if not passage.get("found"):
        return "", ""
    before = _joined(passage.get("before2"), passage.get("before"), passage.get("pre"))
    after = _joined(passage.get("post"), passage.get("after"), passage.get("after2"))
    return before.strip(), after.strip()


def _book_about(book):
    return {"book": book.get("title"), "author": book.get("author")}


def _chapter_with_number(h):
    return h.get("chapter_card") or h.get("chapter")


def _card_chapter(book, name):
    # The AI names a chapter as the notes do. This is the book's card form, if the book lists it
    chapters = book["data"].get("chapters") or []
    for chapter in chapters:
        if name in (chapter.get("card"), chapter.get("name"), chapter.get("title")):
            return chapter.get("card") or name
    by_title = {" ".join(chapter["title"].split()): chapter for chapter in chapters if chapter.get("title")}
    block = companion.chapter_block("\n".join("### " + title for title in by_title), name)
    if block["found"] and block["heading"] in by_title:
        return by_title[block["heading"]].get("card") or name
    return name


class Runner:
    """Starts and steers runs. `query` has ai.query's signature, `on_event` gets one dict per
    change: {topic, type, book_id, ...}, with type "run", "highlight", "connections" or "ideas".

    Creating a Runner marks any run left running in the store as interrupted."""

    def __init__(self, store, query=None, on_event=None, mount=None, parallel=PARALLEL, prompt_for=None):
        self.store = store
        self._query = query
        self._on_event = on_event
        self._mount_override = mount
        self._parallel = parallel
        self._prompt_for = prompt_for
        self._cond = threading.Condition()
        self._runs = {}
        self._writing = {}
        self._loose_tokens = set()
        self._closed = False
        self._mark_interrupted()

    # Steering

    def status(self, book_id):
        """{book_id, status, options, progress, updated_at, active, offer_again}. `status` is None
        when nothing has run for this book. `offer_again` is true for an interrupted run."""
        stored = self.store.get_run(book_id)
        if stored is None:
            stored = {"book_id": book_id, "status": None, "options": {}, "progress": {}, "updated_at": None}
        with self._cond:
            active = self._live(book_id) is not None
        return {**stored, "active": active, "offer_again": stored["status"] == INTERRUPTED}

    def default_options(self, book_id=None):
        """Options for a run from the saved settings and the book's chosen prompt."""
        settings = self.store.get_settings()
        book = self.store.get_book(book_id) if book_id else None
        choice = book["data"].get("pbanki_prompt") if book else None
        picked = (settings.get("book_prompts") or {}).get(book_id)
        return {
            "count": settings["count"],
            "kind": settings["kind"],
            "model": settings["model"],
            "prompt": prompts.default_for_book(picked, prompts.default_for_book(choice, settings["last_prompt"])),
            "sees": settings["sees"],
            "companion": settings["companion_send"],
            "connections": settings["suggest_connections"],
            "meta_model": settings["meta_model"],
            "ideas": settings["suggest_ideas"],
        }

    def start(self, book_id, options=None):
        """Starts a run in the background and returns status(). A run already going is left
        alone. `options` overrides default_options(): count, kind, model, prompt, sees, companion,
        hint, highlights (false to write no highlight cards), connections, meta_model, ideas. A run
        carrying on from an unfinished one skips the steps that one finished; after a finished run,
        every step asked for runs again. Raises RunError with a plain sentence."""
        with self._cond:
            if self._closed:
                raise RunError("pbAnki is closing.")
            old = self._live(book_id)
            going = old is not None and not old.stopping
        if going:
            return self.status(book_id)
        if old is not None and not old.finished.wait(STOP_WAIT_SECONDS):
            raise RunError("The last run is still stopping. Try again in a moment.")

        book = self._book(book_id)
        chosen = self._checked_options(book, options)
        highlights = self._highlights(book)
        reviewed = self._reviewed(book_id)
        queue = [h for h in highlights if self._due(book_id, h, reviewed)] if chosen["highlights"] else []
        stored = self.store.get_run(book_id)
        previous = stored["progress"] if stored else {}
        finished = (previous.get("finished") or []) if stored and stored["status"] != DONE else []
        has_companion = self._companion_text(book) is not None
        progress = _new_progress(len(queue), finished, chosen["connections"], chosen["ideas"] and has_companion, previous)
        run = _Run(book_id, chosen, progress, queue)

        with self._cond:
            if self._closed:
                raise RunError("pbAnki is closing.")
            going = self._live(book_id) is not None
            if not going:
                self._runs[book_id] = run
        if going:
            return self.status(book_id)
        try:
            self._save(run)
        except BaseException:
            run.finished.set()
            raise
        self._emit_run(run)
        threading.Thread(target=self._drive, args=(run,), daemon=True, name=f"pbanki-run-{book_id}").start()
        return self.status(book_id)

    def pause(self, book_id):
        """No new AI calls start. Calls under way finish and their cards are kept."""
        with self._cond:
            run = self._live(book_id)
            changed = run is not None and run.status == RUNNING
            if changed:
                run.status = PAUSED
        if changed:
            self._save(run)
            self._emit_run(run)
        return self.status(book_id)

    def resume(self, book_id):
        """Continues a paused run, or starts an interrupted or stopped one again with its options."""
        with self._cond:
            run = self._live(book_id)
            changed = run is not None and run.status == PAUSED
            if changed:
                run.status = RUNNING
                self._cond.notify_all()
        if changed:
            self._save(run)
            self._emit_run(run)
        elif run is None:
            stored = self.store.get_run(book_id)
            if stored and stored["status"] in (INTERRUPTED, STOPPED):
                return self.start(book_id, stored["options"])
        return self.status(book_id)

    def stop(self, book_id):
        """Ends a run and cancels its AI calls under way. Finished work is kept. An interrupted
        run is no longer offered again."""
        with self._cond:
            run = self._live(book_id)
            tokens = []
            if run is not None:
                run.stopping = True
                run.status = STOPPED
                run.queue.clear()
                tokens = list(run.tokens)
                self._cond.notify_all()
        for token in tokens:
            token.cancel()
        if run is not None:
            self._save(run)
            self._emit_run(run)
        else:
            stored = self.store.get_run(book_id)
            if stored and stored["status"] == INTERRUPTED:
                self.store.set_run(book_id, STOPPED)
                self._emit(book_id, "run", status=STOPPED, progress=stored["progress"])
        return self.status(book_id)

    def wait(self, book_id, timeout=None):
        """Blocks until the book's run has ended. False if it is still going after `timeout`."""
        with self._cond:
            run = self._live(book_id)
        return True if run is None else run.finished.wait(timeout)

    def shutdown(self, wait=5):
        """For quitting: cancels every AI call under way. Unfinished runs stay interrupted."""
        with self._cond:
            self._closed = True
            runs = [run for run in self._runs.values() if not run.finished.is_set()]
            tokens = list(self._loose_tokens)
            for run in runs:
                run.stopping = True
                run.status = INTERRUPTED
                run.queue.clear()
                tokens.extend(run.tokens)
            self._cond.notify_all()
        for token in tokens:
            token.cancel()
        for run in runs:
            self._save(run)
        for run in runs:
            run.finished.wait(wait)

    # One-off requests

    def suggest(self, book_id, hid, options=None, cancel=None):
        """"Suggest N" for one highlight, in the caller's thread: asks for exactly `count` cards,
        telling the AI about the cards it already has and the ones removed. Returns {cards, error,
        cancelled}. An AI failure is stored on the highlight as well as returned."""
        book = self._book(book_id)
        chosen = self._checked_options(book, options)
        h = next((item for item in self._highlights(book) if item["hid"] == hid), None)
        if h is None:
            raise RunError("That highlight isn't in this book.")
        token = cancel or ai.CancelToken()
        with self._cond:
            if self._closed:
                raise RunError("pbAnki is closing.")
            writing = self._writing.setdefault(book_id, set())
            if hid in writing:
                raise RunError("Cards for this highlight are still being written. Try again in a moment.")
            writing.add(hid)
            self._loose_tokens.add(token)
        try:
            cards, error, cancelled = self._cards_outcome(
                book, h, chosen, token, {"count": chosen["count"]},
                keep=lambda: self.still_wanted(book_id, hid),
            )
        finally:
            with self._cond:
                self._writing[book_id].discard(hid)
                self._loose_tokens.discard(token)
        state = STOPPED if cancelled else FAILED if error else FINISHED
        self._emit(book_id, "highlight", hid=hid, state=state, cards=len(cards or []), error=error)
        return {"cards": cards or [], "error": error, "cancelled": cancelled}

    def find_connections(self, book_id, options=None, cancel=None):
        """Finds more connections, in the caller's thread. `options`: chapters (names, all when
        left out), scope ("pending" or "all"), count, model, prompt, hint. Returns {connections,
        error, cancelled}, where error is a plain sentence."""
        book = self._book(book_id)
        options = options or {}
        scope = options.get("scope", SCOPE_PENDING)
        rows = self._connection_rows(book, scope, options.get("chapters"))
        if len(rows) < 2:
            return {"connections": [], "error": "Connections need at least two highlights to read.", "cancelled": False}
        defaults = self.default_options(book_id)
        request = {
            "model": options.get("model") or defaults["meta_model"],
            "prompt": options.get("prompt") or defaults["prompt"],
            "count": options.get("count"),
            "hint": options.get("hint"),
        }
        token = cancel or ai.CancelToken()
        with self._cond:
            if self._closed:
                raise RunError("pbAnki is closing.")
            self._loose_tokens.add(token)
        try:
            found = self._ask_connections(book, rows, request, token)
            added = self._add_connections(book["id"], rows, found)
        except ai.AICancelled:
            return {"connections": [], "error": None, "cancelled": True}
        except ai.AIError as e:
            return {"connections": [], "error": str(e), "cancelled": False}
        finally:
            with self._cond:
                self._loose_tokens.discard(token)
        self._emit(book_id, CONNECTIONS, state=FINISHED, found=len(added))
        return {"connections": added, "error": None, "cancelled": False}

    def card_context(self, book_id, hid, options=None):
        """What a one-card AI request needs, from the cached book: {about, request, notes, note,
        options}, built as for a highlight's cards. Raises RunError with a plain sentence."""
        book = self._book(book_id)
        chosen = self._checked_options(book, options)
        h = next((item for item in self._highlights(book) if item["hid"] == hid), None)
        if h is None:
            raise RunError("That highlight isn't in this book.")
        request, about, notes, note = self._card_request(book, h, chosen, {"count": chosen["count"]})
        return {"about": about, "request": request, "notes": notes, "note": note, "options": chosen}

    def still_wanted(self, book_id, hid):
        """Whether new cards for this highlight could still be saved: it is in the cached book,
        isn't reviewed and isn't skipped."""
        state = self.store.get_highlight(book_id, hid)
        book = self.store.get_book(book_id)
        if state is None or book is None or state["skipped"] or state["key"] in self._reviewed(book_id):
            return False
        return any(h.get("key") == state["key"] and h.get("status") != "reviewed"
                   for h in book["data"].get("highlights") or [])

    def cache_book_text(self, book_id):
        """Keeps the book's file and text on this Mac while the reader is plugged in, so key idea
        quotes and what the AI reads work the same with it unplugged. True when the text was read."""
        book = self.store.get_book(book_id)
        if book is None:
            return False
        self._keep_book_copy(book)
        return self._refresh_book_text(book) is not None

    def has_book_copy(self, book_id):
        """Whether the book's file is on this Mac, for when the reader isn't plugged in."""
        return self._book_copy_path(book_id).is_file()

    # The run itself

    def _drive(self, run):
        try:
            book = self._book(run.book_id)
            if run.progress[IDEAS]["state"] == WAITING:
                self._keep_book_copy(book)
                self._refresh_book_text(book)
            self._highlight_stage(run, book)
            self._connection_stage(run, book)
            self._idea_stage(run, book)
        except Exception:
            _log.exception("A run stopped early")
            run.problem = True
        finally:
            with self._cond:
                if run.problem and run.status in (RUNNING, PAUSED):
                    run.status = INTERRUPTED
                    run.progress["error"] = _RUN_PROBLEM
                elif run.status in (RUNNING, PAUSED):
                    run.status = DONE
                run.progress["stage"] = None
                run.progress["writing"] = []
            try:
                self._save(run)
            except Exception:
                _log.exception("A run's last state could not be stored")
            self._emit_run(run)
            run.finished.set()
            self.store.close()

    def _highlight_stage(self, run, book):
        count = min(self._parallel, len(run.queue))
        workers = [threading.Thread(target=self._highlight_worker, args=(run, book), daemon=True) for _ in range(count)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

    def _highlight_worker(self, run, book):
        try:
            while True:
                claimed = self._next_highlight(run)
                if claimed is None:
                    return
                self._write_highlight(run, book, *claimed)
        except Exception:
            _log.exception("A highlight worker stopped early")
            run.problem = True
        finally:
            self.store.close()

    def _next_highlight(self, run):
        # Waits out a pause, then claims the next highlight that still wants cards.
        # The store is read with the condition released
        book_id = run.book_id
        while True:
            with self._cond:
                while run.status == PAUSED:
                    self._cond.wait()
                if run.stopping or not run.queue:
                    return None
                h = run.queue.popleft()
            due = self._due(book_id, h, self._reviewed(book_id))
            with self._cond:
                writing = self._writing.setdefault(book_id, set())
                if not due or h["hid"] in writing:
                    run.progress["highlights"]["done"] += 1
                elif run.stopping or run.status == PAUSED:
                    if not run.stopping:
                        run.queue.appendleft(h)
                else:
                    writing.add(h["hid"])
                    token = ai.CancelToken()
                    run.tokens.add(token)
                    run.progress["writing"].append(h["hid"])
                    return h, token

    def _write_highlight(self, run, book, h, token):
        hid = h["hid"]
        book_id = run.book_id

        try:
            self._save(run)
            self._emit(book_id, "highlight", hid=hid, state=WRITING, cards=0, error=None)
            cards, error, cancelled = self._cards_outcome(
                book, h, run.options, token, {"max": run.options["count"]},
                keep=lambda: self.still_wanted(book_id, hid),
            )
        finally:
            with self._cond:
                self._writing[book_id].discard(hid)
                run.tokens.discard(token)
                run.progress["writing"].remove(hid)
        with self._cond:
            counts = run.progress["highlights"]
            if not cancelled:
                counts["done"] += 1
            if error:
                counts["failed"] += 1
            counts["cards"] += len(cards or [])
        self._save(run)
        state = STOPPED if cancelled else FAILED if error else FINISHED
        self._emit(book_id, "highlight", hid=hid, state=state, cards=len(cards or []), error=error)

    def _connection_stage(self, run, book):
        stage = run.progress[CONNECTIONS]
        if stage["state"] != WAITING or not self._wait_turn(run):
            return
        rows = self._connection_rows(book, SCOPE_PENDING)
        if len(rows) < 2:
            self._set_stage(run, CONNECTIONS, NOTHING_TO_DO)
            return
        request = {"model": run.options["meta_model"], "prompt": run.options["prompt"], "count": None, "hint": None}

        def ask(token):
            return self._ask_connections(book, rows, request, token)

        def write(found):
            return self._add_connections(book["id"], rows, found)

        self._ask_stage(run, CONNECTIONS, ask, write)

    def _idea_stage(self, run, book):
        stage = run.progress[IDEAS]
        if stage["state"] != WAITING or not self._wait_turn(run):
            return
        if not self._whole_companion(book):
            self._set_stage(run, IDEAS, NOTHING_TO_DO)
            return
        request = {"model": run.options["meta_model"], "prompt": run.options["prompt"]}

        def ask(token):
            return self._ask_ideas(book, request, token)

        def write(found):
            added = self._add_ideas(book, found)
            with self._cond:
                run.progress[IDEAS_LOOKED] = book["data"].get("percent_finished") or 0
            return added

        self._ask_stage(run, IDEAS, ask, write)

    def _ask_stage(self, run, name, ask, write):
        # One AI call for the whole book. Its results and "finished" are stored together
        token = self._stage_token(run)
        self._set_stage(run, name, WRITING)
        try:
            found = ask(token)
        except ai.AICancelled:
            self._set_stage(run, name, WAITING)
            return
        except ai.AIError as e:
            self._set_stage(run, name, FAILED, error=str(e))
            return
        except Exception:
            _log.exception("The %s step failed", name)
            self._set_stage(run, name, FAILED, error=_RUN_PROBLEM)
            return
        finally:
            with self._cond:
                run.tokens.discard(token)

        added = []

        def store_results():
            added.extend(write(found))
            with self._cond:
                stage = run.progress[name]
                stage.update(state=FINISHED, found=len(added), error=None)
                run.progress["finished"].append(name)

        self._save(run, store_results)
        self._emit(run.book_id, name, state=FINISHED, found=len(added), error=None)

    def _stage_token(self, run):
        token = ai.CancelToken()
        with self._cond:
            run.tokens.add(token)
            if run.stopping:
                token.cancel()
        return token

    def _set_stage(self, run, name, state, error=None):
        with self._cond:
            run.progress[name].update(state=state, error=error)
            if state == WRITING:
                run.progress["stage"] = name
        self._save(run)
        self._emit(run.book_id, name, state=state, found=0, error=error)

    def _wait_turn(self, run):
        with self._cond:
            while run.status == PAUSED:
                self._cond.wait()
            return not run.stopping

    # Highlight cards

    def _cards_outcome(self, book, h, options, token, amount, keep=None):
        # (cards, error sentence, cancelled). Failures are stored on the highlight
        book_id = book["id"]
        try:
            return self._ask_cards(book, h, options, token, amount, keep), None, False
        except ai.AICancelled:
            return None, None, True
        except ai.AIError as e:
            error = str(e)
        except Exception:
            _log.exception("Writing cards for a highlight failed")
            error = _UNEXPECTED
        self.store.set_highlight_error(book_id, h["hid"], error)
        return None, error, False

    def _ask_cards(self, book, h, options, token, amount, keep):
        request, about, notes, note = self._card_request(book, h, options, amount)
        messages = cardgen.build_messages(about, request, notes, note)
        answer = self._call(options["model"], messages, token)
        cards, error = cardgen.parse_reply(answer, request, about)
        if error:
            raise ai.AIError(error)
        later = cardgen.parse_later_research(answer) if notes else None
        added = []
        book_id, hid = book["id"], h["hid"]
        with self.store.transaction():
            if keep is not None and not keep():
                return []
            for card in cards:
                suggested = {"front": card["front"], "back": card["back"]}
                added.append(self.store.add_card(book_id, hid, options["kind"], card["front"], card["back"], suggested=suggested))
            self.store.set_highlight_error(book_id, hid, None)
            if later:
                self.store.set_later_research(book_id, hid, later)
        return added

    def _card_request(self, book, h, options, amount):
        book_id, hid = book["id"], h["hid"]
        data = book["data"]
        kind = options["kind"]
        request = {
            **amount,
            "prompt": self._prompt(options["prompt"]),
            "recent": cardgen.pick_recent(data.get("card_history") or [], HISTORY_SHOWN),
            "cloze": kind == "cloze",
            "questions_only": kind == "qonly",
            "later_research": True,
            "existing": self.store.existing_cards(book_id, hid),
            "rejected": self.store.rejected_cards(book_id, hid),
            "guidance": options.get("hint") or None,
        }
        before, after = self._around(book, h, options["sees"])
        about = {
            **_book_about(book),
            "chapter": h.get("chapter"),
            "percent": h.get("percent"),
            "before": before,
            "after": after,
            "text": h["text"],
            "note": h.get("note"),
        }
        text = self._companion_text(book)
        percent = h.get("percent") or 0
        notes = companion.context_for_highlight(text, options["companion"], h.get("chapter"), percent)
        note = None
        if notes:
            _sliced, info = companion.slice_text(text, percent, keep_current_synopsis=True)
            note = companion.position_note(info)
        return request, about, notes, note

    def _around(self, book, h, sees):
        location = h.get("location") or {}
        opened = self._open_epub(book) if location.get("found") else None
        if opened is not None:
            before, after = opened.ai_context(location, sees)
            if before or after:
                return before, after
        return _cached_context(h)

    # Connections and key ideas

    def _connection_rows(self, book, scope, chapters=None):
        book_id = book["id"]
        states = self.store.list_highlights(book_id)
        reviewed = self._reviewed(book_id)
        rows = []
        for h in self._highlights(book):
            if scope != SCOPE_ALL:
                skipped = states.get(h["hid"], {}).get("skipped")
                if skipped or h.get("status") == "reviewed" or h["key"] in reviewed:
                    continue
            if chapters is not None and not {h.get("chapter"), h.get("chapter_card"), h.get("chapter_name")} & set(chapters):
                continue
            rows.append({"hid": h["hid"], "text": h["text"], "chapter": _chapter_with_number(h),
                         "page": h.get("page"), "note": h.get("note")})
        return rows

    def _ask_connections(self, book, rows, request, token):
        book_id = book["id"]
        ask = {
            "prompt": self._prompt(request["prompt"]),
            "count": request.get("count"),
            "guidance": request.get("hint") or None,
            "existing": self.store.list_connections(book_id, (DRAFT, SAVED)),
            "rejected": self.store.list_connections(book_id, (REMOVED,)),
        }
        notes = self._whole_companion(book)
        messages = cardgen.build_connections(rows, ask, notes, _book_about(book))
        answer = self._call(request["model"], messages, token)
        found, error = cardgen.parse_connections(answer, len(rows), limit=request.get("count"))
        if error:
            raise ai.AIError(error)
        return found

    def _add_connections(self, book_id, rows, found):
        added = []
        with self.store.transaction():
            for item in found:
                sources = [rows[number - 1]["hid"] for number in item["sources"]]
                added.append(self.store.add_connection(book_id, item["title"], item["front"], item["back"], sources))
        return added

    def _ask_ideas(self, book, request, token):
        notes = self._whole_companion(book)
        rows = [{"text": h["text"], "chapter": _chapter_with_number(h), "page": h.get("page"), "note": h.get("note")}
                for h in self._highlights(book)]
        ask = {
            "prompt": self._prompt(request["prompt"]),
            "rejected": self.store.list_ideas(book["id"], (REMOVED,)),
            "existing": self.store.list_ideas(book["id"], (DRAFT, SAVED)),
        }
        messages = cardgen.build_ideas(rows, ask, notes, _book_about(book))
        answer = self._call(request["model"], messages, token)
        found, error = cardgen.parse_ideas(
            answer,
            book_text=self._book_text(book),
            chapter_allowed=lambda name: companion.chapter_block(notes, name)["found"],
        )
        if error:
            raise ai.AIError(error)
        return found

    def _add_ideas(self, book, found):
        # A chapter note's link to an idea is stored as the idea's id. An idea written before
        # isn't added twice: its note links to the one there, or to none if it was removed
        book_id = book["id"]
        added = []
        by_index = []
        known = {}
        for state in (DRAFT, SAVED, REMOVED):
            for item in self.store.list_ideas(book_id, (state,)):
                known.setdefault(cardgen.words(item["title"]), item)
        with self.store.transaction():
            for idea in found["ideas"]:
                earlier = known.get(cardgen.words(idea["title"]))
                if earlier is not None:
                    by_index.append(earlier)
                    continue
                item = self.store.add_idea(book_id, _card_chapter(book, idea["chapter"]), idea["title"], idea["note"],
                                           idea["quote"], idea["front"], idea["back"])
                added.append(item)
                by_index.append(item)
            self.store.clear_chapter_notes(book_id)
            for chapter in found["chapters"]:
                points = []
                for point in chapter["points"]:
                    stored = {**point}
                    if "idea" in point:
                        linked = by_index[point["idea"]]
                        stored.pop("idea")
                        if linked["state"] != REMOVED:
                            stored["idea"] = linked["id"]
                    points.append(stored)
                self.store.set_chapter_notes(book_id, chapter["chapter"], chapter["big"], points)
        return added

    # Book data

    def _book(self, book_id):
        book = self.store.get_book(book_id)
        if book is None:
            raise RunError(_NOT_CACHED)
        return book

    def _highlights(self, book):
        # The cached highlights with their ids, each key once
        rows = {}
        for h in book["data"].get("highlights") or []:
            if h.get("key") and h.get("text") and h["key"] not in rows:
                rows[h["key"]] = h
        hids = self.store.ensure_highlights(book["id"], list(rows))
        return [{**h, "hid": hids[key]} for key, h in rows.items()]

    def _reviewed(self, book_id):
        return self.store.reader_reviewed(book_id) | self.store.unverified_reviewed_keys(book_id)

    def _due(self, book_id, h, reviewed):
        # A highlight the reader went through, by keeping or removing suggestions, isn't asked again
        if h.get("status") == "reviewed" or h["key"] in reviewed:
            return False
        state = self.store.get_highlight(book_id, h["hid"])
        if state and state["skipped"]:
            return False
        if self.store.list_cards(book_id, h["hid"], (DRAFT,)):
            return False
        return not self.store.rejected_cards(book_id, h["hid"])

    def _mount(self):
        return self._mount_override or self.store.get_settings()["mount"]

    def _on_reader(self, book):
        # The book's file if the reader is plugged in and has it, else None
        mount = self._mount()
        local = paths.device_to_local(book["device_path"], mount)
        if local is None or not reader.is_connected(mount) or not local.is_file():
            return None
        return local

    def _open_epub(self, book):
        # The book's ePub from the reader, else the copy kept on this Mac, else None
        if str(book.get("ext") or "").lower() != "epub":
            return None
        try:
            local = self._on_reader(book)
            if local is not None:
                return epub.open_epub(local)
        except (epub.EpubError, OSError):
            pass
        copy_path = self._book_copy_path(book["id"])
        try:
            return epub.open_epub(copy_path) if copy_path.is_file() else None
        except (epub.EpubError, OSError):
            return None

    def _book_copy_path(self, book_id):
        return paths.home() / "book-text" / f"{book_id}.epub"

    def _keep_book_copy(self, book):
        # Reads the reader and writes only on this Mac
        if str(book.get("ext") or "").lower() != "epub":
            return
        target = self._book_copy_path(book["id"])
        try:
            local = self._on_reader(book)
            if local is None:
                return
            found = local.stat()
            if target.is_file():
                kept = target.stat()
                if kept.st_size == found.st_size and int(kept.st_mtime) == int(found.st_mtime):
                    return
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(f".{threading.get_ident()}.tmp")
            shutil.copy2(local, temp)
            os.replace(temp, target)
        except OSError:
            _log.warning("The book's file could not be kept on this computer")

    def _companion_text(self, book):
        try:
            return companion.read_text(companion.find(book["device_path"], self._mount()))
        except OSError:
            return None

    def _whole_companion(self, book):
        return companion.context_for_book(self._companion_text(book), book["data"].get("percent_finished"))

    def _book_text_path(self, book_id):
        return paths.home() / "book-text" / f"{book_id}.txt"

    def _refresh_book_text(self, book):
        opened = self._open_epub(book)
        if opened is None:
            return None
        text = "\n\n".join(opened.doc(number).text for number in range(1, len(opened.spine) + 1))
        target = self._book_text_path(book["id"])
        try:
            data = text.encode("utf-8", "replace")
            if not target.is_file() or target.read_bytes() != data:
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix(f".{threading.get_ident()}.tmp")
                temp.write_bytes(data)
                os.replace(temp, target)
        except OSError:
            _log.warning("The book's text could not be kept on this computer")
        return text

    def _book_text(self, book):
        # The whole book when it can be had, else only the passages cached around the highlights
        text = self._refresh_book_text(book)
        if text is not None:
            return text
        try:
            return self._book_text_path(book["id"]).read_bytes().decode("utf-8", "replace")
        except OSError:
            pass
        parts = []
        for h in self._highlights(book):
            before, after = _cached_context(h)
            parts.append(_joined(before, h["text"], after))
        return "\n\n".join(parts)

    def _prompt(self, name):
        return (self._prompt_for or prompts.effective)(name)

    # Options

    def _checked_options(self, book, options):
        given = {key: value for key, value in (options or {}).items() if key in _OPTION_KEYS and value is not None}
        chosen = {**self.default_options(book["id"]), **given}
        count = chosen["count"]
        if isinstance(count, bool) or not isinstance(count, int):
            raise RunError("The number of cards has to be a whole number.")
        chosen["count"] = max(1, min(count, MAX_CARDS))
        if chosen["kind"] not in CARD_KINDS:
            raise RunError("That kind of card isn't one pbAnki can suggest.")
        if chosen["sees"] not in epub.AI_MODES:
            raise RunError("That choice of how much the AI reads isn't one pbAnki knows.")
        if chosen["companion"] not in companion.SEND_OPTIONS:
            raise RunError("That choice for the Book Companion isn't one pbAnki knows.")
        chosen["highlights"] = bool(chosen.get("highlights", True))
        chosen["connections"] = bool(chosen["connections"])
        chosen["ideas"] = bool(chosen["ideas"])
        return chosen

    # Plumbing

    def _call(self, model, messages, token):
        if token.cancelled:
            raise ai.AICancelled()
        return (self._query or ai.query)(model, messages, cancel=token)

    def _live(self, book_id):
        run = self._runs.get(book_id)
        return run if run is not None and not run.finished.is_set() else None

    def _save(self, run, write=None):
        with run.save_lock:
            with self.store.transaction():
                if write is not None:
                    write()
                with self._cond:
                    status, progress = run.status, copy.deepcopy(run.progress)
                self.store.set_run(run.book_id, status, run.options, progress)

    def _emit_run(self, run):
        with self._cond:
            status, progress = run.status, copy.deepcopy(run.progress)
        self._emit(run.book_id, "run", status=status, progress=progress)

    def _emit(self, book_id, kind, **fields):
        if self._on_event is None:
            return
        try:
            self._on_event({"topic": f"book:{book_id}", "type": kind, "book_id": book_id, **fields})
        except Exception:
            _log.exception("A run event could not be delivered")

    def _mark_interrupted(self):
        for stored in self.store.list_runs():
            if stored["status"] not in (RUNNING, PAUSED):
                continue
            progress = {**stored["progress"], "stage": None, "writing": []}
            for name in (CONNECTIONS, IDEAS):
                stage = progress.get(name)
                if isinstance(stage, dict) and stage.get("state") == WRITING:
                    stage["state"] = WAITING
            self.store.set_run(stored["book_id"], INTERRUPTED, progress=progress)
