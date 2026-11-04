import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["PBANKI_SYNC_DELAY"] = "0"

from fixtures.make_reader import build_reader
from pbanki_desktop import ai, companion, reader, runs
from pbanki_desktop.store import Store, hid_for

FAKE_DIR = Path(__file__).resolve().parent / "fixtures" / "fake_ai"
_spec = importlib.util.spec_from_file_location("fake_ai_logic", FAKE_DIR / "_fake.py")
_fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fake)

DEVICE_PATH = "/mnt/ext1/English/Test Book.epub"
LONG = 30
COMPANION = """# Test Book

## 1. Overview

A book about tests.

## 2. The chapters

### Chapter One <!-- pos: 0.1000 -->

- First point.

### Chapter Two <!-- pos: 0.4000 -->

- Second point. Since publication: later studies questioned this.

### Chapter Three <!-- pos: 0.9000 -->

- A spoiler point.
"""


def wait_until(check, timeout=LONG):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.005)
    return bool(check())


def lock_is_free(lock):
    # Taken within a moment, so nothing is sitting on it
    if lock.acquire(timeout=2):
        lock.release()
        return True
    return False


def section(prompt, tag):
    match = re.search(f"<{tag}>\n(.*?)\n</{tag}>", prompt, re.S)
    return match.group(1) if match else None


def kind_of(prompt):
    if '{"connections":' in prompt:
        return "connections"
    if '{"chapters":' in prompt:
        return "ideas"
    return "highlight"


class FakeAI:
    """Stands in for ai.query. Holds calls at gates, records them, and honours cancel."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.finished = []
        self.in_flight = 0
        self.peak = 0
        self.gate = threading.Event()
        self.gate.set()
        self.gates = {}
        self.replies = {}
        self.ignore_cancel = False

    def hold(self, name):
        """Holds calls of a kind ("connections", "ideas") or for a highlight with this text."""
        gate = threading.Event()
        self.gates[name] = gate
        return gate

    def reply(self, name, value):
        """A reply text, or an exception to raise, for a kind or a highlight's text."""
        self.replies[name] = value

    def __call__(self, model, messages, timeout=None, cancel=None):
        prompt = "\n\n".join(m["content"] for m in messages)
        kind = kind_of(prompt)
        text = section(prompt, "highlight") if kind == "highlight" else None
        with self.lock:
            self.calls.append({"model": model, "kind": kind, "text": text, "prompt": prompt, "token": cancel})
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            self._wait(self.gates.get(kind) or self.gates.get(text) or self.gate, cancel)
            value = self.replies.get(kind, self.replies.get(text))
            if isinstance(value, BaseException):
                raise value
            answer = value if value is not None else json.dumps(_fake.valid_reply(prompt))
            with self.lock:
                self.finished.append(kind if kind != "highlight" else text)
            return answer
        finally:
            with self.lock:
                self.in_flight -= 1

    def _wait(self, gate, cancel):
        while not gate.wait(0.005):
            if cancel is not None and cancel.cancelled and not self.ignore_cancel:
                raise ai.AICancelled()
        if cancel is not None and cancel.cancelled and not self.ignore_cancel:
            raise ai.AICancelled()

    def kinds(self):
        with self.lock:
            return [call["kind"] for call in self.calls]

    def texts(self):
        with self.lock:
            return [call["text"] for call in self.calls if call["kind"] == "highlight"]

    def prompt_for(self, text):
        with self.lock:
            return next(call["prompt"] for call in self.calls if call["text"] == text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pbanki-runs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mount = self.tmp / "reader"
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = {
            "PBANKI_MOUNT": str(self.mount),
            "PBANKI_DESKTOP_HOME": str(self.home),
            "PBANKI_SYNC_DELAY": "0",
            "PBANKI_CLAUDE_BIN": str(FAKE_DIR / "claude"),
            "PBANKI_CODEX_BIN": str(FAKE_DIR / "codex"),
            "PBANKI_TERMINAL_CMD": "/usr/bin/true",
            "FAKE_AI_MODE": "valid",
            "FAKE_AI_SLOW_SECONDS": "30",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = Store(self.home / "pbanki.db")
        self.addCleanup(self.store.close)
        self.ai = FakeAI()
        self.events = []
        self.runners = []
        self.addCleanup(self._finish_runners)
        self.addCleanup(self.ai.gate.set)

    def _finish_runners(self):
        for runner in self.runners:
            runner.shutdown(wait=LONG)

    def runner(self, query="fake", **kwargs):
        kwargs.setdefault("on_event", self.events.append)
        made = runs.Runner(self.store, query=self.ai if query == "fake" else query, **kwargs)
        self.runners.append(made)
        return made

    def in_thread(self, work):
        """Runs `work` on a new thread, the way a server thread would, closing its connection."""

        def body():
            try:
                work()
            finally:
                self.store.close()

        thread = threading.Thread(target=body)
        thread.start()
        return thread

    def add_book(self, count=4, companion_text=None, percent_finished=1.0, device_path=DEVICE_PATH, **fields):
        """A book that exists only in the store's cache, with the reader unplugged."""
        highlights = []
        for number in range(1, count + 1):
            text = f"Highlight number {number} says something worth keeping."
            highlights.append({
                "key": f"2026-01-01 10:00:{number:02}|/body/DocFragment[1]/p[{number}]",
                "text": text,
                "note": None,
                "chapter": "Chapter Two",
                "chapter_number": 2,
                "chapter_card": "Chapter Two",
                "chapter_name": "Chapter Two",
                "page": number,
                "percent": 45.0,
                "status": "pending",
                "location": {"found": False},
                "passage": {"found": True, "before2": "", "before": f"Words before {number}.", "pre": "",
                            "mark": text, "post": "", "after": f"Words after {number}.", "after2": ""},
            })
        data = {"highlights": highlights, "chapters": [], "card_history": [], "percent_finished": percent_finished}
        data.update(fields)
        book_id = self.store.upsert_book({"device_path": device_path, "title": "Test Book", "author": "Ann Author",
                                          "stem": Path(device_path).stem, "ext": "epub", "data": data})
        if companion_text:
            folder = self.home / "companions"
            folder.mkdir(exist_ok=True)
            (folder / companion.file_name(device_path)).write_text(companion_text, "utf-8")
        return book_id

    def text(self, number):
        return f"Highlight number {number} says something worth keeping."

    def hid(self, book_id, number):
        key = self.store.get_book(book_id)["data"]["highlights"][number - 1]["key"]
        self.store.ensure_highlights(book_id, [key])
        return hid_for(key)

    def cards_of(self, book_id, number):
        return self.store.list_cards(book_id, self.hid(book_id, number))

    def finish(self, runner, book_id):
        self.assertTrue(runner.wait(book_id, LONG), "the run did not end")
        return runner.status(book_id)

    def run_all(self, runner, book_id, options=None):
        runner.start(book_id, options)
        return self.finish(runner, book_id)


class OrderTest(Base):
    def test_highlights_then_connections_then_ideas(self):
        book = self.add_book(5, COMPANION)
        self.ai.gate.clear()
        connections = self.ai.hold("connections")
        ideas = self.ai.hold("ideas")
        runner = self.runner()
        runner.start(book)
        self.assertTrue(wait_until(lambda: len(self.ai.calls) == 3))
        self.ai.gate.set()
        self.assertTrue(wait_until(lambda: "connections" in self.ai.kinds()))
        # Every highlight has finished before the connections call starts
        self.assertEqual(len([x for x in self.ai.finished if x.startswith("Highlight")]), 5)
        self.assertNotIn("ideas", self.ai.kinds())
        self.assertEqual(runner.status(book)["progress"]["stage"], "connections")
        connections.set()
        self.assertTrue(wait_until(lambda: "ideas" in self.ai.kinds()))
        self.assertEqual(self.ai.in_flight, 1)
        ideas.set()
        status = self.finish(runner, book)
        self.assertEqual(status["status"], "done")
        self.assertEqual(self.ai.kinds(), ["highlight"] * 5 + ["connections", "ideas"])
        self.assertEqual(status["progress"]["highlights"], {"total": 5, "done": 5, "failed": 0, "cards": 10})
        self.assertEqual(status["progress"]["stage"], None)
        self.assertEqual(status["progress"]["writing"], [])
        self.assertEqual(status["progress"]["connections"]["state"], "done")
        self.assertEqual(status["progress"]["ideas"]["state"], "done")

    def test_highlights_start_in_book_order(self):
        book = self.add_book(6)
        self.run_all(self.runner(), book, {"connections": False})
        self.assertEqual(sorted(self.ai.texts()), sorted(self.text(n) for n in range(1, 7)))
        runner = self.runner(parallel=1)
        book2 = self.add_book(6, device_path="/mnt/ext1/English/Second.epub")
        self.ai.calls.clear()
        self.run_all(runner, book2, {"connections": False})
        self.assertEqual(self.ai.texts(), [self.text(n) for n in range(1, 7)])

    def test_connections_box_off(self):
        book = self.add_book(3, COMPANION)
        status = self.run_all(self.runner(), book, {"connections": False})
        self.assertEqual(self.ai.kinds(), ["highlight"] * 3 + ["ideas"])
        self.assertEqual(status["progress"]["connections"]["state"], "off")

    def test_ideas_box_off(self):
        book = self.add_book(3, COMPANION)
        status = self.run_all(self.runner(), book, {"ideas": False})
        self.assertEqual(self.ai.kinds(), ["highlight"] * 3 + ["connections"])
        self.assertEqual(status["progress"]["ideas"]["state"], "off")

    def test_no_companion_means_no_key_ideas(self):
        book = self.add_book(3)
        status = self.run_all(self.runner(), book)
        self.assertEqual(self.ai.kinds(), ["highlight"] * 3 + ["connections"])
        self.assertEqual(status["progress"]["ideas"]["state"], "off")

    def test_one_highlight_cant_make_connections(self):
        book = self.add_book(1)
        status = self.run_all(self.runner(), book)
        self.assertEqual(self.ai.kinds(), ["highlight"])
        self.assertEqual(status["progress"]["connections"]["state"], "skipped")
        self.assertNotIn("connections", status["progress"]["finished"])


class ConcurrencyTest(Base):
    def test_never_more_than_three_at_a_time(self):
        book = self.add_book(8)
        self.ai.gate.clear()
        runner = self.runner()
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 3))
        time.sleep(0.15)
        self.assertEqual(len(self.ai.calls), 3)
        self.assertEqual(self.ai.in_flight, 3)
        self.assertEqual(len(runner.status(book)["progress"]["writing"]), 3)
        self.ai.gate.set()
        self.finish(runner, book)
        self.assertEqual(self.ai.peak, 3)
        self.assertEqual(len(self.ai.calls), 8)

    def test_a_free_slot_is_filled_at_once(self):
        book = self.add_book(5)
        self.ai.hold(self.text(1))
        self.ai.hold(self.text(2))
        self.ai.gate.clear()
        self.ai.hold(self.text(3)).set()
        self.ai.hold(self.text(4))
        self.ai.hold(self.text(5))
        runner = self.runner()
        runner.start(book, {"connections": False})
        # The third call returns, so the fourth starts with the first two still out
        self.assertTrue(wait_until(lambda: self.text(4) in self.ai.texts() and self.ai.in_flight == 3))
        self.assertNotIn(self.text(5), self.ai.texts())
        for gate in self.ai.gates.values():
            gate.set()
        self.finish(runner, book)
        self.assertEqual(self.ai.peak, 3)

    def test_each_book_has_its_own_limit(self):
        one = self.add_book(4)
        two = self.add_book(4, device_path="/mnt/ext1/English/Second.epub")
        self.ai.gate.clear()
        runner = self.runner()
        runner.start(one, {"connections": False})
        runner.start(two, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 6))
        self.ai.gate.set()
        self.finish(runner, one)
        self.finish(runner, two)
        self.assertEqual(self.ai.peak, 6)


class RequestTest(Base):
    def test_a_run_uses_max_and_lets_the_ai_choose(self):
        book = self.add_book(2)
        self.run_all(self.runner(), book, {"count": 3, "connections": False})
        prompt = self.ai.calls[0]["prompt"]
        self.assertIn("from 1 to 3", prompt)
        self.assertNotIn("Write 3 cards", prompt)

    def test_suggest_uses_count_and_knows_existing_and_removed_cards(self):
        book = self.add_book(2)
        runner = self.runner()
        hid = self.hid(book, 1)
        kept = self.store.add_card(book, hid, "qa", "Kept front?", "Kept back.", suggested={"front": "Kept front?", "back": "Kept back."})
        gone = self.store.add_card(book, hid, "qa", "Gone front?", "Gone back.", suggested={"front": "Gone front?", "back": "Gone back."})
        self.store.remove_card(gone["id"])
        result = runner.suggest(book, hid, {"count": 2, "hint": "ask about dates"})
        prompt = self.ai.calls[0]["prompt"]
        self.assertIn("Write 2 cards", prompt)
        self.assertNotIn("from 1 to", prompt)
        self.assertIn("Kept front?", section(prompt, "existing_cards"))
        self.assertIn("Gone front?", section(prompt, "removed_cards"))
        self.assertEqual(section(prompt, "reader_request"), "ask about dates")
        self.assertEqual(result["error"], None)
        self.assertEqual(len(result["cards"]), 2)
        self.assertEqual([c["id"] for c in self.cards_of(book, 1)][0], kept["id"])
        self.assertEqual(len(self.cards_of(book, 1)), 3)
        self.assertEqual(self.events[-1]["type"], "highlight")
        self.assertEqual(self.events[-1]["hid"], hid)

    def test_suggest_stores_a_failure_on_the_highlight(self):
        book = self.add_book(2)
        self.ai.reply(self.text(1), ai.AIError("Claude Code isn't signed in."))
        runner = self.runner()
        result = runner.suggest(book, self.hid(book, 1), {"count": 1})
        self.assertEqual(result, {"cards": [], "error": "Claude Code isn't signed in.", "cancelled": False})
        self.assertEqual(self.store.get_highlight(book, self.hid(book, 1))["error"], "Claude Code isn't signed in.")

    def test_suggest_can_be_cancelled(self):
        book = self.add_book(1)
        self.ai.gate.clear()
        token = ai.CancelToken()
        runner = self.runner()
        results = []
        hid = self.hid(book, 1)
        thread = self.in_thread(lambda: results.append(runner.suggest(book, hid, {"count": 1}, cancel=token)))
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 1))
        token.cancel()
        thread.join(LONG)
        self.assertEqual(results, [{"cards": [], "error": None, "cancelled": True}])
        self.assertIsNone(self.store.get_highlight(book, self.hid(book, 1))["error"])

    def test_suggest_for_an_unknown_highlight(self):
        book = self.add_book(1)
        with self.assertRaisesRegex(runs.RunError, "isn't in this book"):
            self.runner().suggest(book, "nope")

    def test_card_kinds(self):
        book = self.add_book(1)
        self.run_all(self.runner(), book, {"kind": "qonly", "connections": False})
        cards = self.cards_of(book, 1)
        self.assertTrue(all(c["kind"] == "qonly" and c["back"] == "" for c in cards))
        self.assertEqual(cards[0]["source"], "AI")
        self.assertEqual(cards[0]["suggested"]["front"], cards[0]["front"])

        cloze_book = self.add_book(1, device_path="/mnt/ext1/English/Cloze.epub")
        self.run_all(self.runner(), cloze_book, {"kind": "cloze", "connections": False})
        cards = self.cards_of(cloze_book, 1)
        self.assertEqual(cards[0]["kind"], "cloze")
        self.assertIn("{{c1::", cards[0]["front"])

    def test_card_history_and_passage_reach_the_ai(self):
        history = [{"front": "Old question?", "back": "Old answer.", "quote": "Old highlight."}]
        book = self.add_book(1, card_history=history)
        self.run_all(self.runner(), book, {"connections": False})
        prompt = self.ai.calls[0]["prompt"]
        self.assertIn("Old question?", section(prompt, "recent_cards"))
        self.assertIn("Words before 1.", section(prompt, "passage"))
        self.assertIn("Words after 1.", section(prompt, "passage"))
        self.assertIn("Book: Test Book", prompt)
        self.assertIn("Chapter: Chapter Two", prompt)

    def test_later_research_is_kept_from_the_reply(self):
        book = self.add_book(2, COMPANION)
        reply = json.dumps({"cards": [{"front": "A?", "back": "B."}], "later_research": "Later work disagrees."})
        self.ai.reply(self.text(1), reply)
        self.run_all(self.runner(), book, {"connections": False, "ideas": False})
        self.assertIn("later_research", self.ai.prompt_for(self.text(1)))
        self.assertEqual(self.store.get_highlight(book, self.hid(book, 1))["later_research"], "Later work disagrees.")
        self.assertIsNone(self.store.get_highlight(book, self.hid(book, 2))["later_research"])

    def test_later_research_is_ignored_without_notes(self):
        book = self.add_book(1)
        reply = json.dumps({"cards": [{"front": "A?", "back": "B."}], "later_research": "Invented."})
        self.ai.reply(self.text(1), reply)
        self.run_all(self.runner(), book, {"connections": False})
        self.assertNotIn("later_research", self.ai.prompt_for(self.text(1)))
        self.assertIsNone(self.store.get_highlight(book, self.hid(book, 1))["later_research"])

    def test_companion_setting_controls_the_notes_sent(self):
        book = self.add_book(1, COMPANION)
        self.run_all(self.runner(), book, {"connections": False, "ideas": False})
        notes = section(self.ai.calls[0]["prompt"], "book_notes")
        self.assertIn("A book about tests.", notes)
        self.assertIn("Second point.", notes)
        self.assertNotIn("First point.", notes)
        self.assertNotIn("A spoiler point.", notes)
        self.assertIn("[Position note:", self.ai.calls[0]["prompt"])

        whole = self.add_book(1, COMPANION, device_path="/mnt/ext1/English/Whole.epub")
        (self.home / "companions" / companion.file_name("/mnt/ext1/English/Whole.epub")).write_text(COMPANION, "utf-8")
        self.ai.calls.clear()
        self.run_all(self.runner(), whole, {"companion": companion.WHOLE, "connections": False, "ideas": False})
        notes = section(self.ai.calls[0]["prompt"], "book_notes")
        self.assertIn("First point.", notes)
        self.assertNotIn("A spoiler point.", notes)

        off = self.add_book(1, COMPANION, device_path="/mnt/ext1/English/Off.epub")
        self.ai.calls.clear()
        self.run_all(self.runner(), off, {"companion": companion.OFF, "connections": False, "ideas": False})
        self.assertIsNone(section(self.ai.calls[0]["prompt"], "book_notes"))

    def test_prompt_files_are_read_for_every_call(self):
        folder = self.home / "prompts" / "Standard"
        folder.mkdir(parents=True)
        (folder / "questions.md").write_text("First instructions.", "utf-8")
        book = self.add_book(2)
        second_may_start = self.ai.hold(self.text(1))
        runner = self.runner(parallel=1)
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 1))
        (folder / "questions.md").write_text("Second instructions.", "utf-8")
        second_may_start.set()
        self.finish(runner, book)
        self.assertIn("First instructions.", self.ai.calls[0]["prompt"])
        self.assertIn("Second instructions.", self.ai.calls[1]["prompt"])

    def test_options_are_checked_with_plain_sentences(self):
        book = self.add_book(1)
        runner = self.runner()
        for options, sentence in (
            ({"kind": "def"}, "kind of card"),
            ({"count": "many"}, "whole number"),
            ({"count": True}, "whole number"),
            ({"sees": "Everything"}, "how much the AI reads"),
            ({"companion": "Some"}, "Book Companion"),
        ):
            with self.assertRaisesRegex(runs.RunError, sentence):
                runner.start(book, options)
        self.assertIsNone(self.store.get_run(book))

    def test_count_is_limited_like_the_plugin(self):
        book = self.add_book(1)
        self.run_all(self.runner(), book, {"count": 99, "connections": False})
        self.assertIn("from 1 to 10", self.ai.calls[0]["prompt"])
        self.assertEqual(self.store.get_run(book)["options"]["count"], 10)

    def test_defaults_come_from_settings_and_the_book(self):
        self.store.set_settings({"count": 2, "model": "claude-haiku-5-5", "meta_model": "claude-fable-5-1", "suggest_connections": False})
        folder = self.home / "prompts" / "Self Help"
        folder.mkdir(parents=True)
        book = self.add_book(1, pbanki_prompt="Self Help")
        options = self.runner().default_options(book)
        self.assertEqual(options["count"], 2)
        self.assertEqual(options["model"], "claude-haiku-5-5")
        self.assertEqual(options["meta_model"], "claude-fable-5-1")
        self.assertEqual(options["connections"], False)
        self.assertEqual(options["prompt"], "Self Help")

    def test_models_are_used_for_their_own_work(self):
        book = self.add_book(2, COMPANION)
        self.run_all(self.runner(), book, {"model": "m-highlight", "meta_model": "m-meta"})
        models = {call["kind"]: call["model"] for call in self.ai.calls}
        self.assertEqual(models, {"highlight": "m-highlight", "connections": "m-meta", "ideas": "m-meta"})


class PauseTest(Base):
    def test_pause_lets_calls_finish_and_starts_no_new_ones(self):
        book = self.add_book(6)
        self.ai.gate.clear()
        runner = self.runner()
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 3))
        status = runner.pause(book)
        self.assertEqual(status["status"], "paused")
        self.assertEqual(self.store.get_run(book)["status"], "paused")
        self.ai.gate.set()
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 0))
        self.assertTrue(wait_until(lambda: runner.status(book)["progress"]["highlights"]["done"] == 3))
        time.sleep(0.15)
        self.assertEqual(len(self.ai.calls), 3)
        self.assertEqual(sum(len(self.cards_of(book, n)) for n in range(1, 7)), 6)
        self.assertEqual(runner.status(book)["status"], "paused")
        self.assertTrue(runner.status(book)["active"])

        status = runner.resume(book)
        self.assertEqual(status["status"], "running")
        status = self.finish(runner, book)
        self.assertEqual(status["status"], "done")
        self.assertEqual(sorted(self.ai.texts()), sorted(self.text(n) for n in range(1, 7)))

    def test_pause_holds_back_connections(self):
        book = self.add_book(2)
        last = self.ai.hold(self.text(2))
        runner = self.runner(parallel=1)
        runner.start(book)
        self.assertTrue(wait_until(lambda: self.text(2) in self.ai.texts()))
        runner.pause(book)
        last.set()
        self.assertTrue(wait_until(lambda: runner.status(book)["progress"]["highlights"]["done"] == 2))
        time.sleep(0.15)
        self.assertNotIn("connections", self.ai.kinds())
        runner.resume(book)
        self.finish(runner, book)
        self.assertIn("connections", self.ai.kinds())

    def test_pause_and_resume_do_nothing_without_a_run(self):
        book = self.add_book(1)
        runner = self.runner()
        self.assertIsNone(runner.pause(book)["status"])
        self.assertIsNone(runner.resume(book)["status"])
        self.assertEqual(self.ai.calls, [])

    def test_a_finished_run_is_not_resumed(self):
        book = self.add_book(2)
        runner = self.runner()
        self.run_all(runner, book)
        count = len(self.ai.calls)
        self.assertEqual(runner.resume(book)["status"], "done")
        self.assertEqual(runner.pause(book)["status"], "done")
        self.assertEqual(len(self.ai.calls), count)


class StopTest(Base):
    def test_stop_cancels_calls_under_way_and_keeps_finished_work(self):
        book = self.add_book(6, COMPANION)
        self.ai.gate.clear()
        self.ai.hold(self.text(1)).set()
        self.ai.hold(self.text(2)).set()
        runner = self.runner()
        runner.start(book)
        # Highlights 1 and 2 return; 3, 4 and 5 stay out
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 3 and len(self.ai.finished) == 2))
        self.assertTrue(wait_until(lambda: len(self.cards_of(book, 1)) == 2 and len(self.cards_of(book, 2)) == 2))
        status = runner.stop(book)
        self.assertEqual(status["status"], "stopped")
        cancelled = {call["text"]: call["token"].cancelled for call in self.ai.calls}
        self.assertEqual(cancelled, {self.text(n): n > 2 for n in range(1, 6)})
        status = self.finish(runner, book)
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["progress"]["highlights"], {"total": 6, "done": 2, "failed": 0, "cards": 4})
        self.assertEqual(len(self.ai.calls), 5)
        self.assertEqual(self.ai.kinds(), ["highlight"] * 5)
        for number in (3, 4, 5, 6):
            self.assertEqual(self.cards_of(book, number), [])
            self.assertIsNone(self.store.get_highlight(book, self.hid(book, number))["error"])
        self.assertEqual(len(self.cards_of(book, 1)), 2)

    def test_start_after_stop_asks_only_for_what_is_left(self):
        book = self.add_book(4)
        self.ai.gate.clear()
        self.ai.hold(self.text(1)).set()
        runner = self.runner()
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: len(self.cards_of(book, 1)) == 2 and self.ai.in_flight == 3))
        runner.stop(book)
        self.ai.gate.set()
        self.ai.calls.clear()
        status = self.run_all(runner, book, {"connections": False, "count": 2})
        self.assertEqual(status["status"], "done")
        self.assertEqual(sorted(self.ai.texts()), [self.text(2), self.text(3), self.text(4)])
        self.assertEqual(status["progress"]["highlights"]["total"], 3)

    def test_resume_starts_a_stopped_run_again(self):
        book = self.add_book(2)
        self.ai.gate.clear()
        runner = self.runner()
        runner.start(book, {"count": 2, "connections": False, "hint": "only dates"})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 2))
        runner.stop(book)
        self.assertTrue(runner.wait(book, LONG))
        self.ai.gate.set()
        self.ai.calls.clear()
        runner.resume(book)
        self.assertEqual(self.finish(runner, book)["status"], "done")
        self.assertIn("from 1 to 2", self.ai.calls[0]["prompt"])
        self.assertEqual(section(self.ai.calls[0]["prompt"], "reader_request"), "only dates")

    def test_a_reply_arriving_after_stop_is_kept(self):
        book = self.add_book(1)
        self.ai.ignore_cancel = True
        self.ai.gate.clear()
        runner = self.runner()
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 1))
        runner.stop(book)
        self.ai.gate.set()
        status = self.finish(runner, book)
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(len(self.cards_of(book, 1)), 2)

    def test_stop_during_connections_then_they_run_again(self):
        book = self.add_book(2)
        self.ai.hold("connections")
        runner = self.runner()
        runner.start(book)
        self.assertTrue(wait_until(lambda: "connections" in self.ai.kinds()))
        runner.stop(book)
        status = self.finish(runner, book)
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["progress"]["connections"]["state"], "waiting")
        self.assertEqual(self.store.list_connections(book), [])
        self.assertIsNone(status["progress"]["connections"]["error"])

        self.ai.gates["connections"].set()
        self.ai.calls.clear()
        self.run_all(runner, book)
        self.assertEqual(self.ai.kinds(), ["connections"])
        self.assertEqual(len(self.store.list_connections(book)), 1)

    def test_a_new_start_waits_for_the_stopping_run(self):
        book = self.add_book(1)
        self.ai.ignore_cancel = True
        self.ai.gate.clear()
        runner = self.runner()
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 1))
        runner.stop(book)
        with mock.patch.object(runs, "STOP_WAIT_SECONDS", 0.05):
            with self.assertRaisesRegex(runs.RunError, "still stopping"):
                runner.start(book)
        self.ai.gate.set()
        self.finish(runner, book)
        self.assertEqual(len(self.ai.calls), 1)


class FailureTest(Base):
    def test_one_failure_is_stored_and_the_rest_carry_on(self):
        book = self.add_book(4)
        self.ai.reply(self.text(2), ai.AIError("Claude Code couldn't answer: the fake model is overloaded"))
        status = self.run_all(self.runner(), book)
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["progress"]["highlights"]["failed"], 1)
        self.assertEqual(status["progress"]["highlights"]["done"], 4)
        state = self.store.get_highlight(book, self.hid(book, 2))
        self.assertEqual(state["error"], "Claude Code couldn't answer: the fake model is overloaded")
        self.assertEqual(self.cards_of(book, 2), [])
        for number in (1, 3, 4):
            self.assertEqual(len(self.cards_of(book, number)), 2)
        self.assertIn("connections", self.ai.kinds())
        failed = [e for e in self.events if e["type"] == "highlight" and e["state"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("overloaded", failed[0]["error"])

    def test_a_malformed_reply_is_a_plain_failure(self):
        book = self.add_book(2)
        self.ai.reply(self.text(1), "Sorry, here are your cards: {not json at all}")
        status = self.run_all(self.runner(), book, {"connections": False})
        self.assertEqual(self.store.get_highlight(book, self.hid(book, 1))["error"], "The AI reply could not be read.")
        self.assertEqual(status["progress"]["highlights"]["failed"], 1)
        self.assertEqual(len(self.cards_of(book, 2)), 2)

    def test_a_reply_with_no_cards_is_a_plain_failure(self):
        book = self.add_book(1)
        self.ai.reply(self.text(1), '{"cards": []}')
        self.run_all(self.runner(), book, {"connections": False})
        self.assertEqual(self.store.get_highlight(book, self.hid(book, 1))["error"], "The AI reply had no cards.")

    def test_an_unexpected_error_never_shows_a_stack_trace(self):
        book = self.add_book(2)
        self.ai.reply(self.text(1), RuntimeError("Traceback: secret detail"))
        with self.assertLogs("pbanki_desktop.runs", "ERROR"):
            status = self.run_all(self.runner(), book, {"connections": False})
        error = self.store.get_highlight(book, self.hid(book, 1))["error"]
        self.assertEqual(error, runs._UNEXPECTED)
        self.assertNotIn("secret", error)
        self.assertEqual(status["status"], "done")
        self.assertEqual(len(self.cards_of(book, 2)), 2)

    def test_a_failed_highlight_is_tried_again_by_the_next_run_and_clears_its_error(self):
        book = self.add_book(3)
        self.ai.reply(self.text(2), ai.AIError("It failed."))
        runner = self.runner()
        self.run_all(runner, book, {"connections": False})
        del self.ai.replies[self.text(2)]
        self.ai.calls.clear()
        self.run_all(runner, book, {"connections": False})
        self.assertEqual(self.ai.texts(), [self.text(2)])
        self.assertIsNone(self.store.get_highlight(book, self.hid(book, 2))["error"])
        self.assertEqual(len(self.cards_of(book, 2)), 2)

    def test_a_failed_connections_call_doesnt_stop_key_ideas(self):
        book = self.add_book(3, COMPANION)
        self.ai.reply("connections", ai.AIError("Connections failed."))
        status = self.run_all(self.runner(), book)
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["progress"]["connections"]["state"], "failed")
        self.assertEqual(status["progress"]["connections"]["error"], "Connections failed.")
        self.assertEqual(status["progress"]["ideas"]["state"], "done")
        self.assertEqual(status["progress"]["finished"], ["ideas"])

    def test_malformed_connections_and_ideas_are_plain_failures_and_run_again_next_time(self):
        book = self.add_book(3, COMPANION)
        self.ai.reply("connections", "not json")
        self.ai.reply("ideas", '{"nothing": true}')
        runner = self.runner()
        status = self.run_all(runner, book)
        self.assertEqual(status["progress"]["connections"]["error"], "The AI reply could not be read.")
        self.assertEqual(status["progress"]["ideas"]["error"], "The AI reply had no key ideas.")
        self.assertEqual(self.store.list_connections(book), [])
        self.assertEqual(self.store.list_ideas(book), [])

        self.ai.replies.clear()
        self.ai.calls.clear()
        status = self.run_all(runner, book)
        self.assertEqual(self.ai.kinds(), ["connections", "ideas"])
        self.assertEqual(status["progress"]["finished"], ["connections", "ideas"])
        self.assertIsNone(status["progress"]["connections"]["error"])

    def test_a_store_failure_ends_the_run_in_plain_words(self):
        book = self.add_book(2)
        runner = self.runner()
        with mock.patch.object(self.store, "add_card", side_effect=OSError("disk full")):
            with mock.patch.object(self.store, "set_highlight_error", side_effect=OSError("disk full")):
                with self.assertLogs("pbanki_desktop.runs", "ERROR"):
                    status = self.run_all(runner, book, {"connections": False})
        self.assertEqual(status["status"], "interrupted")
        self.assertEqual(status["progress"]["error"], runs._RUN_PROBLEM)
        self.assertTrue(status["offer_again"])


class NoDuplicatesTest(Base):
    def test_highlights_that_are_done_with_are_not_requested(self):
        book = self.add_book(8)
        hid = lambda n: self.hid(book, n)
        self.store.add_card(book, hid(1), "qa", "Has a draft?", "Yes.", suggested={"front": "Has a draft?", "back": "Yes."})
        self.store.add_card(book, hid(2), "qa")
        removed = self.store.add_card(book, hid(3), "qa", "Removed?", "Yes.", suggested={"front": "Removed?", "back": "Yes."})
        self.store.remove_card(removed["id"])
        self.store.set_skipped(book, hid(4))
        data = self.store.get_book(book)["data"]
        data["highlights"][4]["status"] = "reviewed"
        self.store.upsert_book({"device_path": DEVICE_PATH, "data": data})
        self.store.set_reader_reviewed(book, {data["highlights"][5]["key"]})
        with mock.patch.object(self.store, "unverified_reviewed_keys", return_value={data["highlights"][6]["key"]}):
            status = self.run_all(self.runner(), book, {"connections": False})
        self.assertEqual(self.ai.texts(), [self.text(8)])
        self.assertEqual(status["progress"]["highlights"]["total"], 1)

    def test_a_highlight_with_only_saved_cards_is_asked_again_with_those_cards_shown(self):
        book = self.add_book(1)
        card = self.store.add_card(book, self.hid(book, 1), "qa", "Saved one?", "Yes.")
        with self.store.transaction() as conn:
            conn.execute("UPDATE cards SET state = 'saved' WHERE id = ?", (card["id"],))
        self.run_all(self.runner(), book, {"connections": False})
        self.assertIn("Saved one?", section(self.ai.calls[0]["prompt"], "existing_cards"))

    def test_start_while_running_is_left_alone(self):
        book = self.add_book(4)
        self.ai.gate.clear()
        runner = self.runner()
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 3))
        status = runner.start(book, {"connections": False, "count": 9})
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["options"]["count"], 3)
        self.ai.gate.set()
        self.finish(runner, book)
        self.assertEqual(sorted(self.ai.texts()), sorted(self.text(n) for n in range(1, 5)))

    def test_many_starts_at_once_make_one_run(self):
        book = self.add_book(4)
        self.ai.gate.clear()
        runner = self.runner()
        threads = [self.in_thread(lambda: runner.start(book, {"connections": False})) for _ in range(8)]
        for thread in threads:
            thread.join(LONG)
        self.ai.gate.set()
        self.assertTrue(wait_until(lambda: runner.status(book)["status"] == "done"))
        self.assertEqual(len(self.ai.texts()), 4)
        self.assertEqual(len(set(self.ai.texts())), 4)

    def test_a_second_run_asks_nothing_when_nothing_is_ticked(self):
        book = self.add_book(3, COMPANION)
        runner = self.runner()
        self.run_all(runner, book)
        self.ai.calls.clear()
        cards = len(self.store.list_cards(book))
        connections = len(self.store.list_connections(book))
        ideas = len(self.store.list_ideas(book))
        status = self.run_all(runner, book, {"connections": False, "ideas": False})
        self.assertEqual(self.ai.calls, [])
        self.assertEqual(status["status"], "done")
        self.assertEqual(len(self.store.list_cards(book)), cards)
        self.assertEqual(len(self.store.list_connections(book)), connections)
        self.assertEqual(len(self.store.list_ideas(book)), ideas)

    def test_a_second_run_looks_again_but_adds_no_idea_twice(self):
        book = self.add_book(3, COMPANION)
        runner = self.runner()
        self.run_all(runner, book)
        self.ai.calls.clear()
        cards = len(self.store.list_cards(book))
        ideas = [idea["title"] for idea in self.store.list_ideas(book)]
        self.assertTrue(ideas)
        front = self.store.list_ideas(book)[0]["front"]
        status = self.run_all(runner, book)
        self.assertEqual(sorted(call["kind"] for call in self.ai.calls), ["connections", "ideas"])
        idea_call = next(call for call in self.ai.calls if call["kind"] == "ideas")
        self.assertIn(front, idea_call["prompt"])
        self.assertEqual(len(self.store.list_cards(book)), cards)
        self.assertEqual([idea["title"] for idea in self.store.list_ideas(book)], ideas)
        self.assertEqual(status["progress"]["ideas"]["found"], 0)

    def test_carrying_on_skips_finished_steps_and_keeps_their_counts(self):
        book = self.add_book(2, COMPANION)
        runner = self.runner()
        self.run_all(runner, book)
        found = self.store.get_run(book)["progress"]["connections"]["found"]
        self.store.set_run(book, runs.INTERRUPTED)
        self.ai.calls.clear()
        status = self.run_all(runner, book)
        self.assertEqual(self.ai.calls, [])
        self.assertEqual(status["progress"]["connections"]["found"], found)
        self.assertEqual(status["progress"]["ideas"]["state"], "done")

    def test_key_ideas_alone_on_a_book_with_every_highlight_reviewed(self):
        book = self.add_book(2, COMPANION)
        self.store.set_reader_reviewed(book, [h["key"] for h in self.store.get_book(book)["data"]["highlights"]])
        status = self.run_all(self.runner(), book, {"highlights": False, "connections": False, "ideas": True})
        self.assertEqual([call["kind"] for call in self.ai.calls], ["ideas"])
        self.assertEqual(status["progress"]["highlights"]["total"], 0)
        self.assertEqual(status["progress"]["ideas"]["state"], "done")
        self.assertIsNotNone(runs.ideas_looked(status["progress"]))

    def test_ideas_looked_outlives_a_later_run_without_them(self):
        book = self.add_book(2, COMPANION, percent_finished=0.5)
        runner = self.runner()
        self.run_all(runner, book)
        status = self.run_all(runner, book, {"connections": False, "ideas": False})
        self.assertEqual(runs.ideas_looked(status["progress"]), 0.5)

    def test_reading_further_reaches_ideas_that_were_past_the_progress(self):
        book = self.add_book(2, COMPANION, percent_finished=0.5)
        runner = self.runner()
        self.run_all(runner, book)
        first = next(call for call in self.ai.calls if call["kind"] == "ideas")
        self.assertNotIn("A spoiler point", first["prompt"])
        record = self.store.get_book(book)
        self.store.upsert_book({"device_path": record["device_path"], "title": record["title"], "author": record["author"],
                                "stem": record["stem"], "ext": record["ext"], "data": {**record["data"], "percent_finished": 1.0}})
        self.ai.calls.clear()
        self.run_all(runner, book)
        again = next(call for call in self.ai.calls if call["kind"] == "ideas")
        self.assertIn("A spoiler point", again["prompt"])

    def test_skipping_during_a_call_drops_its_cards(self):
        book = self.add_book(2)
        gate = self.ai.hold(self.text(1))
        runner = self.runner()
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.text(1) in self.ai.texts()))
        self.store.set_skipped(book, self.hid(book, 1))
        gate.set()
        status = self.finish(runner, book)
        self.assertEqual(self.cards_of(book, 1), [])
        self.assertEqual(status["progress"]["highlights"]["cards"], 2)
        self.assertEqual(status["progress"]["highlights"]["done"], 2)

    def test_skipping_before_its_turn_means_it_is_never_requested(self):
        book = self.add_book(3)
        gate = self.ai.hold(self.text(1))
        runner = self.runner(parallel=1)
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 1))
        self.store.set_skipped(book, self.hid(book, 2))
        gate.set()
        self.finish(runner, book)
        self.assertEqual(self.ai.texts(), [self.text(1), self.text(3)])

    def test_a_card_typed_before_its_turn_means_it_is_never_requested(self):
        book = self.add_book(3)
        gate = self.ai.hold(self.text(1))
        runner = self.runner(parallel=1)
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 1))
        self.store.add_card(book, self.hid(book, 2), "qa", "Mine", "Mine too")
        gate.set()
        self.finish(runner, book)
        self.assertEqual(self.ai.texts(), [self.text(1), self.text(3)])

    def test_suggest_refuses_a_highlight_being_written(self):
        book = self.add_book(1)
        self.ai.gate.clear()
        runner = self.runner()
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 1))
        with self.assertRaisesRegex(runs.RunError, "still being written"):
            runner.suggest(book, self.hid(book, 1))
        self.ai.gate.set()
        self.finish(runner, book)
        self.assertEqual(len(self.ai.calls), 1)

    def test_a_run_passes_over_a_highlight_being_suggested(self):
        book = self.add_book(2)
        self.ai.gate.clear()
        runner = self.runner()
        hid = self.hid(book, 1)
        thread = self.in_thread(lambda: runner.suggest(book, hid, {"count": 1}))
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 1))
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 2))
        self.ai.gate.set()
        thread.join(LONG)
        self.finish(runner, book)
        self.assertEqual(sorted(self.ai.texts()), [self.text(1), self.text(2)])

    def test_a_repeated_key_is_one_highlight(self):
        book = self.add_book(2)
        data = self.store.get_book(book)["data"]
        data["highlights"].append(dict(data["highlights"][0]))
        self.store.upsert_book({"device_path": DEVICE_PATH, "data": data})
        self.run_all(self.runner(), book, {"connections": False})
        self.assertEqual(sorted(self.ai.texts()), [self.text(1), self.text(2)])


class RestartTest(Base):
    def test_runs_left_going_show_as_interrupted_and_nothing_starts(self):
        one = self.add_book(2)
        two = self.add_book(2, device_path="/mnt/ext1/English/Two.epub")
        three = self.add_book(2, device_path="/mnt/ext1/English/Three.epub")
        progress = {"stage": "connections", "writing": ["x"], "connections": {"state": "running", "found": 0, "error": None}}
        self.store.set_run(one, "running", {"count": 2}, progress)
        self.store.set_run(two, "paused", {"count": 2}, {"stage": "highlights", "writing": []})
        self.store.set_run(three, "done", {"count": 2}, {"stage": None, "writing": []})
        runner = self.runner()
        status = runner.status(one)
        self.assertEqual(status["status"], "interrupted")
        self.assertTrue(status["offer_again"])
        self.assertFalse(status["active"])
        self.assertEqual(status["options"], {"count": 2})
        self.assertEqual(status["progress"]["stage"], None)
        self.assertEqual(status["progress"]["writing"], [])
        self.assertEqual(status["progress"]["connections"]["state"], "waiting")
        self.assertEqual(runner.status(two)["status"], "interrupted")
        self.assertEqual(runner.status(three)["status"], "done")
        self.assertFalse(runner.status(three)["offer_again"])
        time.sleep(0.1)
        self.assertEqual(self.ai.calls, [])

    def test_a_run_cut_off_by_quitting_is_offered_and_continues_with_what_is_left(self):
        book = self.add_book(5, COMPANION)
        self.ai.gate.clear()
        self.ai.hold(self.text(1)).set()
        first = self.runner()
        first.start(book, {"count": 2, "meta_model": "m-meta"})
        self.assertTrue(wait_until(lambda: len(self.cards_of(book, 1)) == 2 and self.ai.in_flight == 3))
        first.shutdown(wait=LONG)
        self.assertEqual(self.store.get_run(book)["status"], "interrupted")
        self.ai.gate.set()
        self.ai.calls.clear()

        second = self.runner()
        status = second.status(book)
        self.assertTrue(status["offer_again"])
        self.assertEqual(self.ai.calls, [])
        second.resume(book)
        status = self.finish(second, book)
        self.assertEqual(status["status"], "done")
        self.assertEqual(sorted(self.ai.texts()), [self.text(n) for n in range(2, 6)])
        self.assertEqual(self.ai.kinds().count("connections"), 1)
        self.assertEqual(self.ai.kinds().count("ideas"), 1)
        self.assertEqual(status["options"]["meta_model"], "m-meta")
        self.assertEqual(len(self.cards_of(book, 1)), 2)

    def test_a_crash_after_key_steps_doesnt_repeat_them(self):
        book = self.add_book(3, COMPANION)
        runner = self.runner()
        runner.start(book)
        self.finish(runner, book)
        # As if the app died near the end, before the run was marked done
        self.store.set_run(book, "running")
        self.ai.calls.clear()
        restarted = self.runner()
        self.assertEqual(restarted.status(book)["status"], "interrupted")
        restarted.resume(book)
        self.finish(restarted, book)
        self.assertEqual(self.ai.calls, [])
        self.assertEqual(len(self.store.list_connections(book)), 1)

    def test_a_crash_during_connections_asks_them_again_once(self):
        book = self.add_book(2)
        self.ai.hold("connections")
        first = self.runner()
        first.start(book)
        self.assertTrue(wait_until(lambda: "connections" in self.ai.kinds()))
        # A new Runner on the same database, as after the app died
        progress = self.store.get_run(book)["progress"]
        self.assertEqual(progress["connections"]["state"], "running")
        first.shutdown(wait=LONG)
        self.store.set_run(book, "running", progress=progress)
        self.ai.gates["connections"].set()
        self.ai.calls.clear()
        second = self.runner()
        self.assertEqual(second.status(book)["status"], "interrupted")
        second.resume(book)
        self.finish(second, book)
        self.assertEqual(self.ai.kinds(), ["connections"])
        self.assertEqual(len(self.store.list_connections(book)), 1)

    def test_stop_withdraws_the_offer(self):
        book = self.add_book(2)
        self.store.set_run(book, "running", {"count": 2})
        runner = self.runner()
        status = runner.stop(book)
        self.assertEqual(status["status"], "stopped")
        self.assertFalse(status["offer_again"])
        self.assertEqual(self.ai.calls, [])
        self.assertEqual(self.events[-1]["status"], "stopped")

    def test_starting_fresh_works_on_an_interrupted_book(self):
        book = self.add_book(1)
        self.store.set_run(book, "running", {"count": 2, "kind": "qa"})
        runner = self.runner()
        status = self.run_all(runner, book, {"count": 1, "connections": False})
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["options"]["count"], 1)

    def test_shutdown_ends_paused_runs_and_blocks_new_ones(self):
        book = self.add_book(2)
        self.ai.gate.clear()
        runner = self.runner()
        runner.start(book)
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 2))
        runner.pause(book)
        runner.shutdown(wait=LONG)
        self.assertFalse(runner.status(book)["active"])
        self.assertEqual(runner.status(book)["status"], "interrupted")
        with self.assertRaisesRegex(runs.RunError, "closing"):
            runner.start(book)

    def test_shutdown_cancels_a_lone_suggest(self):
        book = self.add_book(1)
        self.ai.gate.clear()
        runner = self.runner()
        results = []
        hid = self.hid(book, 1)
        thread = self.in_thread(lambda: results.append(runner.suggest(book, hid)))
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 1))
        runner.shutdown(wait=LONG)
        thread.join(LONG)
        self.assertEqual(results[0]["cancelled"], True)


class SlowReplyTest(Base):
    def test_a_slow_reply_doesnt_hold_up_the_others(self):
        book = self.add_book(6)
        slow = self.ai.hold(self.text(1))
        runner = self.runner()
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: runner.status(book)["progress"]["highlights"]["done"] == 5))
        self.assertEqual(self.ai.in_flight, 1)
        self.assertEqual(self.cards_of(book, 1), [])
        self.assertEqual(len(self.cards_of(book, 6)), 2)
        self.assertEqual(runner.status(book)["status"], "running")
        slow.set()
        self.assertEqual(self.finish(runner, book)["status"], "done")
        self.assertEqual(len(self.cards_of(book, 1)), 2)

    def test_no_lock_is_held_during_an_ai_call(self):
        book = self.add_book(3)
        self.ai.gate.clear()
        runner = self.runner()
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 3))
        self.assertTrue(wait_until(lambda: len(runner.status(book)["progress"]["writing"]) == 3))

        done = []
        hid = self.hid(book, 1)

        def other_thread():
            store = Store(self.home / "pbanki.db")
            with store.transaction():
                store.add_card(book, hid, "qa", "Written during the call", "x")
            store.close()
            done.append(True)

        started = time.monotonic()
        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(5)
        self.assertEqual(done, [True])
        self.assertTrue(lock_is_free(runner._cond))
        for run in runner._runs.values():
            self.assertTrue(lock_is_free(run.save_lock))
        self.assertEqual(runner.status(book)["status"], "running")
        self.assertEqual(runner.pause(book)["status"], "paused")
        self.assertEqual(runner.resume(book)["status"], "running")
        self.assertLess(time.monotonic() - started, 5)
        self.ai.gate.set()
        self.finish(runner, book)

    def test_an_event_callback_can_ask_for_status(self):
        book = self.add_book(3)
        seen = []
        holder = {}

        def on_event(event):
            seen.append(holder["runner"].status(book)["status"])

        runner = self.runner(on_event=on_event)
        holder["runner"] = runner
        self.run_all(runner, book, {"connections": False})
        self.assertTrue(seen)

    def test_an_ai_timeout_is_stored_as_a_failure(self):
        book = self.add_book(1)
        self.ai.reply(self.text(1), ai.AIError("Claude Code took longer than 5 minutes and was stopped."))
        self.run_all(self.runner(), book, {"connections": False})
        self.assertIn("took longer", self.store.get_highlight(book, self.hid(book, 1))["error"])


class AutoStartTest(Base):
    def test_a_finished_book_with_a_companion_still_looks_for_key_ideas(self):
        book = self.add_book(2, COMPANION)
        self.store.set_reader_reviewed(book, [h["key"] for h in self.store.get_book(book)["data"]["highlights"]])
        runner = self.runner()
        self.assertIsNotNone(runner.auto_start(book))
        status = self.finish(runner, book)
        self.assertEqual([call["kind"] for call in self.ai.calls], ["ideas"])
        self.assertEqual(status["progress"]["ideas"]["state"], "done")

    def test_a_finished_book_with_no_companion_doesnt_start(self):
        book = self.add_book(2)
        self.store.set_reader_reviewed(book, [h["key"] for h in self.store.get_book(book)["data"]["highlights"]])
        self.assertIsNone(self.runner().auto_start(book))

    def test_a_finished_book_doesnt_start_with_key_ideas_turned_off(self):
        book = self.add_book(2, COMPANION)
        self.store.set_reader_reviewed(book, [h["key"] for h in self.store.get_book(book)["data"]["highlights"]])
        self.store.set_settings({"suggest_ideas": False})
        self.assertIsNone(self.runner().auto_start(book))


class ConnectionsTest(Base):
    def reply(self, *items):
        self.ai.reply("connections", json.dumps({"connections": list(items)}))

    def item(self, front, sources, title="T"):
        return {"title": title, "front": front, "back": "An answer.", "sources": sources}

    def test_sources_become_highlight_ids_and_weak_answers_are_dropped(self):
        book = self.add_book(4)
        self.reply(self.item("Good?", [1, 3]), self.item("One source?", [2]), self.item("Bad numbers?", [1, 99]),
                   self.item("Repeats?", [2, 2]), self.item("None?", []), self.item("Three?", [1, 2, 4]))
        status = self.run_all(self.runner(), book)
        stored = self.store.list_connections(book)
        self.assertEqual([c["front"] for c in stored], ["Good?", "Three?"])
        self.assertEqual(stored[0]["sources"], [self.hid(book, 1), self.hid(book, 3)])
        self.assertEqual(stored[1]["sources"], [self.hid(book, 1), self.hid(book, 2), self.hid(book, 4)])
        self.assertEqual(stored[0]["source"], "AI")
        self.assertEqual(status["progress"]["connections"]["found"], 2)

    def test_prompt_has_numbers_chapters_and_the_sliced_companion(self):
        book = self.add_book(2, COMPANION, percent_finished=0.5)
        self.run_all(self.runner(), book, {"ideas": False})
        call = next(c for c in self.ai.calls if c["kind"] == "connections")
        self.assertIn(f"1. [Chapter Two · p. 1] {self.text(1)}", call["prompt"])
        notes = section(call["prompt"], "book_notes")
        self.assertIn("First point.", notes)
        self.assertIn("Second point.", notes)
        self.assertNotIn("A spoiler point.", notes)

    def test_reviewed_and_skipped_highlights_are_left_out_of_a_run(self):
        book = self.add_book(4)
        self.store.set_skipped(book, self.hid(book, 1))
        key = self.store.get_book(book)["data"]["highlights"][1]["key"]
        self.store.set_reader_reviewed(book, {key})
        self.run_all(self.runner(), book)
        call = next(c for c in self.ai.calls if c["kind"] == "connections")
        self.assertNotIn(self.text(1), section(call["prompt"], "highlights"))
        self.assertNotIn(self.text(2), section(call["prompt"], "highlights"))
        self.assertIn(self.text(3), section(call["prompt"], "highlights"))

    def test_find_connections_scope_chapters_count_and_hint(self):
        book = self.add_book(4)
        data = self.store.get_book(book)["data"]
        data["highlights"][3].update(chapter="Chapter Nine", chapter_card="Chapter Nine", chapter_name="Chapter Nine")
        self.store.upsert_book({"device_path": DEVICE_PATH, "data": data})
        self.store.set_reader_reviewed(book, {data["highlights"][0]["key"]})
        runner = self.runner()
        self.reply(self.item("One?", [1, 2]), self.item("Two?", [2, 1]))

        result = runner.find_connections(book, {"count": 1, "model": "m", "hint": "about tea", "chapters": ["Chapter Two"]})
        self.assertEqual(result["error"], None)
        self.assertEqual(len(result["connections"]), 1)
        prompt = self.ai.calls[-1]["prompt"]
        self.assertEqual(self.ai.calls[-1]["model"], "m")
        self.assertIn("Find up to 1 connections", prompt)
        self.assertEqual(section(prompt, "reader_request"), "about tea")
        self.assertNotIn(self.text(1), section(prompt, "highlights"))
        self.assertNotIn(self.text(4), section(prompt, "highlights"))
        self.assertEqual(result["connections"][0]["sources"], [self.hid(book, 2), self.hid(book, 3)])

        runner.find_connections(book, {"scope": "all"})
        prompt = self.ai.calls[-1]["prompt"]
        self.assertIn(self.text(1), section(prompt, "highlights"))
        self.assertIn("One?", section(prompt, "existing_cards"))

    def test_find_connections_failures_are_plain(self):
        book = self.add_book(2)
        runner = self.runner()
        result = runner.find_connections(book, {"chapters": ["Nothing"]})
        self.assertEqual(result["error"], "Connections need at least two highlights to read.")
        self.ai.reply("connections", ai.AIError("It failed."))
        self.assertEqual(runner.find_connections(book)["error"], "It failed.")
        self.assertEqual(self.store.list_connections(book), [])

    def test_removed_connections_are_passed_as_rejected(self):
        book = self.add_book(2)
        gone = self.store.add_connection(book, "T", "Gone connection?", "A", [])
        self.store.remove_connection(gone["id"])
        self.runner().find_connections(book)
        self.assertIn("Gone connection?", section(self.ai.calls[-1]["prompt"], "removed_cards"))


class IdeasTest(Base):
    GOOD_QUOTE = "Words after 2."

    def reply(self, ideas, chapters=()):
        self.ai.reply("ideas", json.dumps({"chapters": list(chapters), "ideas": list(ideas)}))

    def idea(self, chapter, front, quote=""):
        return {"title": front, "chapter": chapter, "note": "Why.", "quote": quote, "front": front, "back": "An answer."}

    def test_ideas_past_the_readers_progress_are_dropped_and_links_follow_ids(self):
        book = self.add_book(2, COMPANION, percent_finished=0.5)
        ideas = [
            self.idea("Chapter Three", "A spoiler?"),
            self.idea("Chapter One", "Early?"),
            self.idea("Chapter Two", "Middle?"),
        ]
        chapters = [
            {"chapter": "Chapter Two", "big": "Big two.", "points": [
                {"text": "Links to the spoiler.", "covered": False, "idea": 0},
                {"text": "Links to middle.", "covered": False, "idea": 2},
                {"text": "Covered one.", "covered": True},
            ]},
            {"chapter": "Chapter Three", "big": "A spoiler.", "points": []},
        ]
        self.reply(ideas, chapters)
        status = self.run_all(self.runner(), book, {"connections": False})
        stored = self.store.list_ideas(book)
        self.assertEqual([i["front"] for i in stored], ["Early?", "Middle?"])
        self.assertEqual(status["progress"]["ideas"]["found"], 2)
        notes = self.store.get_chapter_notes(book)
        self.assertEqual(list(notes), ["Chapter Two"])
        points = notes["Chapter Two"]["points"]
        self.assertEqual(points[0], {"text": "Links to the spoiler.", "covered": False})
        self.assertEqual(points[1]["idea"], stored[1]["id"])
        self.assertEqual(points[2], {"text": "Covered one.", "covered": True})
        self.assertEqual(stored[0]["source"], "From the Book Companion")
        prompt = next(c for c in self.ai.calls if c["kind"] == "ideas")["prompt"]
        self.assertNotIn("A spoiler point.", section(prompt, "book_notes"))
        self.assertIn(self.text(1), section(prompt, "highlights"))

    def test_quote_is_checked_with_cached_passages_when_unplugged(self):
        book = self.add_book(2, COMPANION)
        self.reply([self.idea("Chapter Two", "Real?", self.GOOD_QUOTE),
                    self.idea("Chapter Two", "Invented?", "Words no book ever printed here.")])
        self.run_all(self.runner(), book, {"connections": False})
        stored = {i["front"]: i for i in self.store.list_ideas(book)}
        self.assertEqual(stored["Real?"]["quote"], self.GOOD_QUOTE)
        self.assertEqual(stored["Invented?"]["quote"], "")

    def test_every_highlight_is_sent_including_reviewed_ones(self):
        book = self.add_book(3, COMPANION)
        key = self.store.get_book(book)["data"]["highlights"][0]["key"]
        self.store.set_reader_reviewed(book, {key})
        self.run_all(self.runner(), book, {"connections": False})
        prompt = next(c for c in self.ai.calls if c["kind"] == "ideas")["prompt"]
        self.assertIn(self.text(1), section(prompt, "highlights"))
        self.assertEqual(sorted(self.ai.texts()), [self.text(2), self.text(3)])

    def test_stop_during_ideas_stores_nothing(self):
        book = self.add_book(1, COMPANION)
        self.ai.hold("ideas")
        runner = self.runner()
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: "ideas" in self.ai.kinds()))
        runner.stop(book)
        status = self.finish(runner, book)
        self.assertEqual(self.store.list_ideas(book), [])
        self.assertNotIn("ideas", status["progress"]["finished"])

    def test_a_companion_that_vanished_means_no_call(self):
        book = self.add_book(1, COMPANION)
        gate = self.ai.hold(self.text(1))
        runner = self.runner()
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 1))
        (self.home / "companions" / companion.file_name(DEVICE_PATH)).unlink()
        gate.set()
        status = self.finish(runner, book)
        self.assertEqual(status["progress"]["ideas"]["state"], "skipped")
        self.assertNotIn("ideas", self.ai.kinds())


class EventTest(Base):
    def test_every_result_fires_an_event_after_the_store_has_it(self):
        book = self.add_book(3, COMPANION)
        store = self.store
        seen = []

        def on_event(event):
            if event["type"] == "highlight" and event["state"] == "done":
                seen.append(len(store.list_cards(book, event["hid"])))
            self.events.append(event)

        self.run_all(self.runner(on_event=on_event), book)
        self.assertEqual(seen, [2, 2, 2])
        self.assertTrue(all(e["topic"] == f"book:{book}" and e["book_id"] == book for e in self.events))
        done = [e for e in self.events if e["type"] == "highlight" and e["state"] == "done"]
        self.assertEqual(sorted(e["hid"] for e in done), sorted(self.hid(book, n) for n in (1, 2, 3)))
        self.assertEqual(sum(e["type"] == "highlight" and e["state"] == "running" for e in self.events), 3)
        self.assertEqual(self.events[0]["type"], "run")
        self.assertEqual(self.events[-1], {"topic": f"book:{book}", "type": "run", "book_id": book, "status": "done",
                                           "progress": self.store.get_run(book)["progress"]})
        for name in ("connections", "ideas"):
            states = [e["state"] for e in self.events if e["type"] == name]
            self.assertEqual(states, ["running", "done"])
        ideas = [e for e in self.events if e["type"] == "ideas"][-1]
        self.assertEqual(ideas["found"], 2)

    def test_a_broken_callback_doesnt_stop_the_run(self):
        book = self.add_book(2)

        def broken(_event):
            raise ValueError("no")

        with self.assertLogs("pbanki_desktop.runs", "ERROR"):
            status = self.run_all(self.runner(on_event=broken), book)
        self.assertEqual(status["status"], "done")
        self.assertEqual(len(self.cards_of(book, 1)), 2)

    def test_no_callback_is_fine(self):
        book = self.add_book(1)
        self.assertEqual(self.run_all(self.runner(on_event=None), book, {"connections": False})["status"], "done")


class AutoStartTest(Base):
    def test_starts_when_on_and_nothing_has_run(self):
        book = self.add_book(2)
        runner = self.runner()
        self.assertEqual(runner.auto_start(book)["status"], "running")
        self.finish(runner, book)
        self.ai.calls.clear()
        self.assertIsNone(runner.auto_start(book))
        self.assertEqual(self.ai.calls, [])

    def test_stays_off_when_the_setting_is_off(self):
        book = self.add_book(2)
        self.store.set_setting("auto_suggest", False)
        self.assertIsNone(self.runner().auto_start(book))
        self.assertIsNone(self.store.get_run(book))

    def test_stays_off_for_an_interrupted_run(self):
        book = self.add_book(2)
        self.store.set_run(book, "running", {"count": 2})
        runner = self.runner()
        self.assertIsNone(runner.auto_start(book))
        self.assertEqual(runner.status(book)["status"], "interrupted")
        self.assertEqual(self.ai.calls, [])

    def test_stays_off_with_nothing_waiting(self):
        book = self.add_book(1)
        self.store.set_skipped(book, self.hid(book, 1))
        self.assertIsNone(self.runner().auto_start(book))
        self.assertIsNone(self.runner().auto_start("no-such-book"))

    def test_a_book_not_cached_is_a_plain_error(self):
        with self.assertRaisesRegex(runs.RunError, "isn't on this computer"):
            self.runner().start("no-such-book")


class FakeCommandTest(Base):
    """The real ai.query running the fake claude and codex commands, on a fake reader."""

    def cache_reader_book(self, fake, name):
        book = reader.load_book(str(fake.mount), fake.books[name])
        return self.store.upsert_book({
            "device_path": book["device_path"], "title": book["title"], "author": book["author"],
            "stem": book["stem"], "ext": book["ext"], "data": book,
        })

    def test_a_whole_run_leaves_the_reader_untouched(self):
        fake = build_reader(self.mount)
        before = fake.snapshot()
        book = self.cache_reader_book(fake, "pride")
        log = self.tmp / "fake.log"
        with mock.patch.dict(os.environ, {"FAKE_AI_LOG": str(log)}):
            runner = self.runner(query=None)
            status = self.run_all(runner, book)
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["progress"]["highlights"]["total"], 7)
        self.assertEqual(status["progress"]["highlights"]["failed"], 0)
        highlights = self.store.get_book(book)["data"]["highlights"]
        for h in highlights:
            self.assertGreaterEqual(len(self.store.list_cards(book, hid_for(h["key"]))), 1)
        self.assertEqual(self.store.list_ideas(book), [])
        connections = self.store.list_connections(book)
        self.assertEqual([c["title"] for c in connections], ["Fake connection"])
        self.assertEqual(connections[0]["sources"], [hid_for(highlights[0]["key"]), hid_for(highlights[1]["key"])])
        self.assertEqual(fake.snapshot(), before)
        rows = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row["tool"] == "claude" for row in rows))
        self.assertTrue(all("--tools" in row["argv"] for row in rows))

    def test_the_ai_sees_more_with_the_reader_plugged_in(self):
        fake = build_reader(self.mount)
        book = self.cache_reader_book(fake, "pride")
        self.run_all(self.runner(), book, {"connections": False})
        plugged = {call["text"]: section(call["prompt"], "passage") for call in self.ai.calls}
        self.assertEqual(len(plugged), 7)

        unplugged_book = self.store.upsert_book({
            **{k: self.store.get_book(book)[k] for k in ("title", "author", "stem", "ext", "data")},
            "device_path": "/mnt/ext1/English/Elsewhere.epub",
        })
        self.ai.calls.clear()
        self.run_all(self.runner(), unplugged_book, {"connections": False})
        cached = {call["text"]: section(call["prompt"], "passage") for call in self.ai.calls}
        for text, passage in plugged.items():
            self.assertGreater(len(passage), len(cached[text]))

    def test_unplugged_run_works_from_the_cache_alone(self):
        fake = build_reader(self.mount)
        book = self.cache_reader_book(fake, "pride")
        first = self.store.get_book(book)["data"]["highlights"][0]
        shutil.rmtree(self.mount)
        status = self.run_all(self.runner(query=None), book)
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["progress"]["highlights"]["failed"], 0)
        self.assertEqual(status["progress"]["highlights"]["total"], 7)
        self.assertEqual(len(self.store.list_connections(book)), 1)
        self.assertFalse(self.mount.exists())
        # Cached passage reaches the AI with nothing on disk to read
        self.run_cached_prompt(book, first)

    def run_cached_prompt(self, book, h):
        runner = self.runner()
        runner.suggest(book, hid_for(h["key"]), {"count": 1})
        self.assertIn(h["passage"]["after"], section(self.ai.calls[-1]["prompt"], "passage"))

    def test_the_ai_reads_choice_holds_with_the_reader_unplugged(self):
        fake = build_reader(self.mount)
        book = self.cache_reader_book(fake, "pride")
        runner = self.runner()
        self.assertTrue(runner.cache_book_text(book))
        self.assertTrue(runner.has_book_copy(book))
        h = self.store.get_book(book)["data"]["highlights"][5]
        hid = hid_for(h["key"])

        def words_read(sees):
            self.ai.calls.clear()
            runner.suggest(book, hid, {"count": 1, "sees": sees})
            return len(section(self.ai.calls[-1]["prompt"], "passage").split())

        plugged = {sees: words_read(sees) for sees in ("600 words each side", "The chapter", "The whole book")}
        shutil.rmtree(self.mount)
        unplugged = {sees: words_read(sees) for sees in plugged}
        self.assertEqual(unplugged, plugged)
        self.assertGreater(unplugged["The whole book"], unplugged["600 words each side"])

    def test_the_kept_copy_follows_a_changed_book(self):
        fake = build_reader(self.mount)
        book = self.cache_reader_book(fake, "pride")
        runner = self.runner()
        runner.cache_book_text(book)
        kept = runner._book_copy_path(book)
        local = fake.local("pride")
        self.assertEqual(kept.read_bytes(), local.read_bytes())
        with local.open("ab") as file:
            file.write(b"\0")
        before = fake.snapshot()
        runner.cache_book_text(book)
        self.assertEqual(kept.read_bytes(), local.read_bytes())
        self.assertEqual(fake.snapshot(), before)

    def test_a_reader_pulled_out_mid_run_changes_nothing_for_the_run(self):
        fake = build_reader(self.mount)
        book = self.cache_reader_book(fake, "pride")
        self.ai.gate.clear()
        runner = self.runner()
        runner.start(book, {"connections": False})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 3))
        shutil.rmtree(self.mount)
        self.ai.gate.set()
        status = self.finish(runner, book)
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["progress"]["highlights"]["failed"], 0)

    def test_key_ideas_check_quotes_against_the_kept_book_text_unplugged(self):
        fake = build_reader(self.mount)
        book = self.cache_reader_book(fake, "pride")
        data = self.store.get_book(book)["data"]
        (self.home / "companions").mkdir()
        (self.home / "companions" / companion.file_name(fake.books["pride"])).write_text(
            "# Pride\n\n## 1. Overview\n\nA novel.\n\n### Chapter I.\n\n- Fortune.\n", "utf-8"
        )
        # Far from any highlight, so only the whole book's text has it
        opened = runs.epub.open_epub(fake.local("pride"))
        far = next(line for line in opened.doc(len(opened.spine) - 1).text.split("\n") if len(line.split()) > 12)
        far_quote = " ".join(far.split()[:7])
        cached_text = " ".join(
            " ".join(str(v) for v in (h["passage"] or {}).values() if isinstance(v, str)) for h in data["highlights"]
        )
        self.assertNotIn(far_quote, cached_text)
        reply = {"chapters": [], "ideas": [
            {"title": "T", "chapter": "Chapter I.", "note": "N", "quote": far_quote, "front": "F?", "back": "B."},
        ]}
        self.ai.reply("ideas", json.dumps(reply))
        runner = self.runner()
        self.assertTrue(runner.cache_book_text(book))
        shutil.rmtree(self.mount)
        self.run_all(runner, book, {"connections": False})
        self.assertEqual(self.store.list_ideas(book)[0]["quote"], far_quote)

        # Without the kept text, the same quote can't be confirmed and is dropped
        self.store.remove_idea(self.store.list_ideas(book)[0]["id"])
        for path in (self.home / "book-text").iterdir():
            path.unlink()
        other = self.store.upsert_book({
            **{k: self.store.get_book(book)[k] for k in ("title", "author", "stem", "ext", "data")},
            "device_path": "/mnt/ext1/English/Copy.epub",
        })
        (self.home / "companions" / companion.file_name("/mnt/ext1/English/Copy.epub")).write_text(
            "# Pride\n\n## 1. Overview\n\nA novel.\n\n### Chapter I.\n\n- Fortune.\n", "utf-8"
        )
        self.run_all(runner, other, {"connections": False})
        self.assertEqual(self.store.list_ideas(other)[0]["quote"], "")

    def test_a_book_with_a_companion_gets_key_ideas(self):
        fake = build_reader(self.mount)
        before = fake.snapshot()
        book = self.cache_reader_book(fake, "mostly_done")
        status = self.run_all(self.runner(query=None), book)
        self.assertEqual(status["progress"]["ideas"]["state"], "done", status["progress"])
        self.assertEqual(status["progress"]["connections"]["state"], "skipped")
        stored = self.store.list_ideas(book)
        self.assertEqual(status["progress"]["highlights"]["total"], 1)
        self.assertEqual(fake.snapshot(), before)
        # The Companion only has "The daily miracle", so ideas for other chapters don't survive
        highlights = self.store.get_book(book)["data"]["highlights"]
        self.assertEqual(highlights[0]["chapter"], "The daily miracle")
        self.assertEqual({i["chapter"] for i in stored}, {highlights[0]["chapter_card"]})
        by_front = {i["front"]: i for i in stored}
        self.assertEqual(by_front["What is the fake idea?"]["quote"], highlights[0]["text"])
        self.assertEqual(by_front["What is the second fake idea?"]["quote"], "")

    def test_codex_model(self):
        book = self.add_book(1)
        log = self.tmp / "fake.log"
        with mock.patch.dict(os.environ, {"FAKE_AI_LOG": str(log)}):
            status = self.run_all(self.runner(query=None), book, {"model": "gpt-6.1-sol", "connections": False})
        self.assertEqual(status["progress"]["highlights"]["failed"], 0)
        self.assertEqual(json.loads(log.read_text("utf-8").splitlines()[0])["tool"], "codex")
        self.assertEqual(len(self.cards_of(book, 1)), 2)

    def test_one_malformed_reply_by_marker(self):
        book = self.add_book(3)
        data = self.store.get_book(book)["data"]
        data["highlights"][1]["note"] = "[[fake:malformed]]"
        self.store.upsert_book({"device_path": DEVICE_PATH, "data": data})
        status = self.run_all(self.runner(query=None), book, {"connections": False})
        self.assertEqual(self.store.get_highlight(book, self.hid(book, 2))["error"], "The AI reply could not be read.")
        self.assertEqual(status["progress"]["highlights"]["failed"], 1)
        self.assertEqual(len(self.cards_of(book, 1)), 2)
        self.assertEqual(len(self.cards_of(book, 3)), 2)

    def test_command_failures_are_plain_sentences(self):
        book = self.add_book(4)
        data = self.store.get_book(book)["data"]
        for index, mode in enumerate(("is_error", "not_logged_in", "exit", "no_output")):
            data["highlights"][index]["note"] = f"[[fake:{mode}]]"
        self.store.upsert_book({"device_path": DEVICE_PATH, "data": data})
        status = self.run_all(self.runner(query=None), book, {"connections": False})
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["progress"]["highlights"]["failed"], 4)
        errors = [self.store.get_highlight(book, self.hid(book, n))["error"] for n in range(1, 5)]
        self.assertEqual(errors, [
            "Claude Code couldn't answer: API Error: the fake model is overloaded",
            "Claude Code isn't signed in. Open Terminal, run claude and sign in, then try again.",
            "Claude Code couldn't answer: fake claude went wrong",
            "Claude Code's reply could not be read.",
        ])

    def test_a_missing_command_is_a_plain_sentence(self):
        book = self.add_book(1)
        with mock.patch.dict(os.environ, {"PBANKI_CLAUDE_BIN": str(self.tmp / "none")}):
            self.run_all(self.runner(query=None), book, {"connections": False})
        self.assertIn("isn't installed", self.store.get_highlight(book, self.hid(book, 1))["error"])

    def test_really_three_commands_at_a_time(self):
        book = self.add_book(7)
        log = self.tmp / "fake.log"
        with mock.patch.dict(os.environ, {"FAKE_AI_LOG": str(log), "FAKE_AI_MODE": "slow", "FAKE_AI_SLOW_SECONDS": "0.4"}):
            runner = self.runner(query=None)
            runner.start(book, {"connections": False})
            time.sleep(0.25)
            self.assertLessEqual(len(log.read_text("utf-8").splitlines()) if log.exists() else 0, 3)
            status = self.finish(runner, book)
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["progress"]["highlights"]["failed"], 0)
        self.assertEqual(len(log.read_text("utf-8").splitlines()), 7)

    def test_a_slow_command_past_its_timeout_is_stored(self):
        book = self.add_book(1)

        def short(model, messages, cancel=None):
            return ai.query(model, messages, timeout=0.2, cancel=cancel)

        with mock.patch.dict(os.environ, {"FAKE_AI_MODE": "slow", "FAKE_AI_SLOW_SECONDS": "30"}):
            self.run_all(self.runner(query=short), book, {"connections": False})
        self.assertIn("took longer than", self.store.get_highlight(book, self.hid(book, 1))["error"])

    def test_stop_kills_the_running_command(self):
        book = self.add_book(1)
        pids = self.tmp / "pids"
        with mock.patch.dict(os.environ, {"FAKE_AI_MODE": "slow", "FAKE_AI_SLOW_SECONDS": "30", "FAKE_AI_PIDS": str(pids)}):
            runner = self.runner(query=None)
            started = time.monotonic()
            runner.start(book, {"connections": False})
            self.assertTrue(wait_until(lambda: pids.exists() and pids.read_text().strip()))
            own, child = [int(x) for x in pids.read_text().split()]
            runner.stop(book)
            self.assertTrue(runner.wait(book, 15))
        self.assertLess(time.monotonic() - started, 15)

        def gone(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False

        self.assertTrue(wait_until(lambda: gone(own) and gone(child), 5))
        self.assertEqual(self.cards_of(book, 1), [])
        self.assertIsNone(self.store.get_highlight(book, self.hid(book, 1))["error"])
        self.assertEqual(runner.status(book)["status"], "stopped")

    def test_shutdown_kills_the_running_command(self):
        book = self.add_book(1)
        pids = self.tmp / "pids"
        with mock.patch.dict(os.environ, {"FAKE_AI_MODE": "slow", "FAKE_AI_SLOW_SECONDS": "30", "FAKE_AI_PIDS": str(pids)}):
            runner = self.runner(query=None)
            runner.start(book, {"connections": False})
            self.assertTrue(wait_until(lambda: pids.exists() and pids.read_text().strip()))
            own, _child = [int(x) for x in pids.read_text().split()]
            runner.shutdown(wait=15)
        with self.assertRaises(ProcessLookupError):
            os.kill(own, 0)
        self.assertEqual(self.store.get_run(book)["status"], "interrupted")


class ChapterNameTest(Base):
    CHAPTERS = [{"number": 2, "title": "Chapter Two", "name": "Chapter Two", "card": "Chapter Two", "doc": 2},
                {"number": 4, "title": "The garden", "name": "4 · The garden", "card": "Chapter 4: The garden", "doc": 4}]

    def stored(self, chapter):
        book = self.add_book(1, chapters=self.CHAPTERS)
        runner = self.runner()
        return runner._add_ideas(self.store.get_book(book), {"chapters": [], "ideas": [
            {"title": f"T {len(self.store.list_ideas(book))}", "chapter": chapter, "note": "", "quote": "", "front": "F?", "back": "B"},
        ]})[0]["chapter"]

    def test_every_form_of_the_name_becomes_the_card_form(self):
        for chapter in ("The garden", "4 · The garden", "Chapter 4: The garden", "the  Garden"):
            self.assertEqual(self.stored(chapter), "Chapter 4: The garden", chapter)

    def test_a_title_already_numbered_stays_as_it_is(self):
        self.assertEqual(self.stored("Chapter Two"), "Chapter Two")

    def test_a_chapter_the_book_does_not_list_stays_as_the_ai_wrote_it(self):
        self.assertEqual(self.stored("Something else entirely"), "Something else entirely")


if __name__ == "__main__":
    unittest.main()
