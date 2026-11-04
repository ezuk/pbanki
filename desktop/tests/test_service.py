import json
import os
import shutil
import sqlite3
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

from fixtures.make_reader import build_reader, lua_file
from pbanki_desktop import ai, companion, device, lua, paths, prompts, reader, runs, save, service
from pbanki_desktop.service import Service, ServiceError
from pbanki_desktop.store import Store, book_id_for, hid_for
from test_runs import FAKE_DIR, FakeAI, wait_until

LONG = 30
ALLOWED_WRITES = {"applications/pbanki/inbox.jsonl", "applications/pbanki/reviewed.jsonl"}
HEADINGS_COMPANION = """# Pride

## 1. Overview

A book about manners.

## 2. The chapters

### Chapter 1 <!-- pos: 0.1000 -->

- First point.
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pbanki-service-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.fake = build_reader(self.tmp / "reader", companion=True)
        self.mount = self.fake.mount
        self.away = self.tmp / "away"
        self.assistant = self.tmp / "assistant"
        (self.assistant / ".claude" / "skills" / "book-companion").mkdir(parents=True)
        (self.assistant / ".claude" / "skills" / "book-companion" / "SKILL.md").write_text("skill", encoding="utf-8")
        env = {
            "PBANKI_MOUNT": str(self.mount),
            "PBANKI_DESKTOP_HOME": str(self.home),
            "PBANKI_ASSISTANT_DIR": str(self.assistant),
            "PBANKI_SYNC_DELAY": "0",
            "PBANKI_CLAUDE_BIN": str(FAKE_DIR / "claude"),
            "PBANKI_CODEX_BIN": str(FAKE_DIR / "codex"),
            "PBANKI_TERMINAL_CMD": "/usr/bin/true",
            "PBANKI_OPEN_CMD": "/usr/bin/true",
            "FAKE_AI_MODE": "valid",
            "FAKE_AI_SLOW_SECONDS": "30",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ai = FakeAI()
        self.addCleanup(self.ai.gate.set)
        self.launched = []
        self.ejected = []
        self.services = []
        self.addCleanup(self._stop_services)
        self.svc = self.make_service()
        self.events = self.svc.subscribe()
        self.addCleanup(self.events.close)
        self.pride = book_id_for(self.fake.books["pride"])
        self.pending = book_id_for(self.fake.books["pending"])
        self.mostly = book_id_for(self.fake.books["mostly_done"])

    def _stop_services(self):
        for made in self.services:
            made.stop(wait=LONG)

    def make_service(self, **kwargs):
        kwargs.setdefault("query", self.ai)
        kwargs.setdefault("launch", self._launch)
        kwargs.setdefault("eject", self.ejected.append)
        made = Service(store=Store(self.home / "pbanki.db"), **kwargs)
        self.services.append(made)
        return made

    def _launch(self, args):
        self.launched.append(args)
        return True

    def unplug(self):
        self.mount.rename(self.away)

    def replug(self):
        self.away.rename(self.mount)

    def wait_jobs(self, svc=None):
        svc = svc or self.svc
        self.assertTrue(wait_until(lambda: not svc._jobs, LONG))

    def wait_run(self, book_id, svc=None):
        svc = svc or self.svc
        self.assertTrue(svc.runner.wait(book_id, LONG))

    def drain(self):
        found = []
        while True:
            event = self.events.get(timeout=0)
            if event is None:
                return found
            found.append(event)

    def next_event(self, kind, topic=None):
        deadline = time.monotonic() + LONG
        while time.monotonic() < deadline:
            event = self.events.get(timeout=0.2)
            if event and event.get("type") == kind and (topic is None or event["topic"] == topic):
                return event
        self.fail(f"no {kind} event")

    def assert_refused(self, status, work, text=None):
        with self.assertRaises(ServiceError) as caught:
            work()
        self.assertEqual(caught.exception.status, status, caught.exception.sentence)
        self.assertEqual(caught.exception.payload(), {"error": caught.exception.sentence})
        if text:
            self.assertIn(text, caught.exception.sentence)

    def opened(self, book_id=None):
        return self.svc.open_book(book_id or self.pride)

    def first_hid(self, view, index=0):
        return view["highlights"][index]["hid"]

    def assert_only_inbox_changed(self, before):
        after = self.fake.snapshot()
        changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
        self.assertLessEqual(changed, ALLOWED_WRITES)
        return changed


class Reading(Base):
    def test_state_carries_reader_settings_assistants_models_and_prompts(self):
        state = self.svc.state()
        json.dumps(state, allow_nan=False)
        self.assertTrue(state["reader"]["connected"])
        self.assertEqual(state["reader"]["label"], "reader connected")
        self.assertEqual(state["reader"]["dot"], "dot")
        self.assertTrue(state["reader"]["rows"])
        for row in state["reader"]["rows"]:
            self.assertIn(row["cls"], ("chk", "chk warn", "chk bad"))
            self.assertIn(row["glyph"], ("✓", "!", "✕"))
        self.assertEqual([a["id"] for a in state["assistants"]], ["claude", "codex"])
        self.assertEqual({a["status"] for a in state["assistants"]}, {"Found"})
        self.assertTrue(state["models"])
        self.assertIn("claude -p --model", state["models"][0]["cmd"])
        self.assertIn("Standard", state["prompts"]["names"])
        self.assertEqual([k["id"] for k in state["kinds"]], ["qa", "cloze", "qonly"])
        self.assertIsNone(state["resume"])
        self.assertTrue(state["skill"]["ok"])

    def test_reader_check_rows_show_a_problem(self):
        shutil.rmtree(self.tmp / "reader")
        self.fake = build_reader(self.tmp / "reader", koreader=False)
        reader_view = self.svc.state()["reader"]
        self.assertEqual(reader_view["title"], "Something is missing on the reader")
        self.assertTrue(any(row["cls"] == "chk bad" and row["glyph"] == "✕" for row in reader_view["rows"]))

    def test_reader_check_rows_offer_update_plugin_as_a_warning(self):
        shutil.rmtree(self.tmp / "reader")
        self.fake = build_reader(self.tmp / "reader", plugin="old")
        reader_view = self.svc.state()["reader"]
        self.assertEqual(reader_view["title"], "Your reader is ready")
        row = next(row for row in reader_view["rows"] if row.get("action") == "update_plugin")
        self.assertEqual((row["cls"], row["glyph"]), ("chk warn", "!"))

    def test_books_rows_for_pick_a_book(self):
        result = self.svc.books()
        json.dumps(result, allow_nan=False)
        self.assertTrue(result["connected"])
        rows = {row["id"]: row for row in result["books"]}
        pride = rows[self.pride]
        self.assertEqual(pride["hl"], "7 to do of 7")
        self.assertEqual(pride["action"], "Open")
        self.assertFalse(pride["none"])
        self.assertEqual(rows[self.pending]["counts"]["all"], 5)
        walden = rows[book_id_for(self.fake.books["walden"])]
        self.assertEqual(walden["hl"], "No highlights")
        self.assertTrue(walden["none"])
        self.assertEqual(walden["action"], "")
        card = rows[book_id_for(self.fake.books["card"])]
        self.assertTrue(card["on_memory_card"])
        self.assertTrue(card["note"])

    def test_lede_with_one_pending_highlight(self):
        view = self.opened(self.mostly)
        self.assertEqual(view["suggest"]["pending"], 1)
        self.assertEqual(view["suggest"]["lede"], "The AI writes cards for your 1 pending highlight.")
        self.assertEqual(view["suggest"]["formLede"], view["suggest"]["lede"])
        self.assertEqual(view["suggest"]["sugBtn"], "Suggest cards for 1 highlight")

    def test_book_is_a_full_working_state(self):
        view = self.opened()
        json.dumps(view, allow_nan=False)
        self.assertEqual(view["id"], self.pride)
        self.assertEqual(len(view["highlights"]), 7)
        self.assertEqual(view["filters"][0], {"id": "all", "label": "All 7"})
        self.assertEqual(view["run"]["phase"], "idle")
        self.assertIn("7 pending highlights", view["suggest"]["lede"])
        self.assertEqual(view["suggest"]["sugBtn"], "Suggest cards for 7 highlights")
        self.assertEqual(view["save"]["saveLabel"], "Save 0 cards")
        self.assertTrue(view["save"]["noCards"])
        first = view["highlights"][0]
        for name in ("before", "pre", "mark", "post", "after", "curPos", "chapterShort", "glyph", "state", "canEdit"):
            self.assertIn(name, first)
        self.assertEqual(first["state"], "todo")
        self.assertEqual(first["curPos"], "1 of 7")
        self.assertTrue(first["found"])
        self.assertEqual(view["options"]["count"], self.svc.state()["settings"]["count"])

    def test_reviewed_highlights_show_their_saved_cards_from_history(self):
        view = self.opened(self.mostly)
        self.assertEqual(view["filters"][2]["label"], "Reviewed 3")
        reviewed = [h for h in view["highlights"] if h["status"] == "reviewed"]
        self.assertEqual(len(reviewed), 3)
        self.assertTrue(all(h["state"] == "rev" and h["cardsTitle"] == "Saved cards" for h in reviewed))
        self.assertTrue(all(not h["canEdit"] for h in reviewed))
        with_cards = [h for h in reviewed if h["saved"]]
        self.assertTrue(with_cards)
        self.assertIn("These are the cards you saved", with_cards[0]["banner"])

    def test_reviewed_on_the_reader_counts_from_jsonl(self):
        view = self.opened(self.pending)
        self.assertEqual(view["filters"][2]["label"], "Reviewed 2")
        self.assertEqual(view["filters"][1]["label"], "To do 3")

    def test_every_fixture_book_opens_or_is_refused_plainly_plugged_and_unplugged(self):
        self.svc.tick()
        self.assertTrue(self.svc.wait_for_refresh(LONG))
        names = sorted(self.fake.books)
        refused = set()
        for name in names:
            book_id = book_id_for(self.fake.books[name])
            try:
                view = self.svc.open_book(book_id)
                json.dumps(view, allow_nan=False)
            except ServiceError as e:
                self.assertEqual(e.status, 409, name)
                refused.add(name)
        self.assertEqual(refused, {"card"})
        scan = self.svc.book(book_id_for(self.fake.books["scan"]))
        self.assertFalse(scan["is_epub"])
        self.assertTrue(all(h["passage_note"] for h in scan["highlights"]))
        self.assertEqual(self.svc.open_book(book_id_for(self.fake.books["walden"]))["run"]["phase"], "idle")
        self.unplug()
        self.svc.tick()
        for name in names:
            book_id = book_id_for(self.fake.books[name])
            if name not in refused:
                json.dumps(self.svc.open_book(book_id), allow_nan=False)
                json.dumps(self.svc.book(book_id), allow_nan=False)
                json.dumps(self.svc.save_preview(book_id), allow_nan=False)
        self.assertEqual(len(self.svc.books()["books"]), len(names))

    def test_unknown_book_is_refused_plainly(self):
        self.assert_refused(404, lambda: self.svc.book("nope"))
        self.assert_refused(404, lambda: self.svc.open_book("nope"))

    def test_book_on_the_memory_card_cannot_open(self):
        self.svc.books()
        self.assert_refused(409, lambda: self.svc.open_book(book_id_for(self.fake.books["card"])), "memory card")

    def test_a_book_listed_but_not_read_yet_says_so_unplugged(self):
        self.svc.books()
        self.unplug()
        self.assert_refused(409, lambda: self.svc.open_book(self.pride), "Plug in")

    def test_passage_widens_with_the_reader_and_stays_cached_without_it(self):
        view = self.opened()
        hid = self.first_hid(view)
        base = self.svc.passage(self.pride, hid)
        wider = self.svc.passage(self.pride, hid, more=2)
        self.assertTrue(wider["found"])
        self.assertFalse(wider["limited"])
        self.assertGreaterEqual(len(wider["before"]) + len(wider["after"]), len(base["before"]) + len(base["after"]))
        self.unplug()
        cached = self.svc.passage(self.pride, hid)
        self.assertEqual(cached["mark"], base["mark"])
        limited = self.svc.passage(self.pride, hid, more=2)
        self.assertTrue(limited["limited"])
        self.assertIn("Plug in", limited["limit_note"])
        self.assert_refused(400, lambda: self.svc.passage(self.pride, hid, more="lots"))
        self.assert_refused(404, lambda: self.svc.passage(self.pride, "nope"))


class Highlights(Base):
    def test_open_marks_opened_and_publishes(self):
        hid = self.first_hid(self.opened())
        self.drain()
        result = self.svc.open_highlight(self.pride, hid)
        self.assertTrue(result["highlight"]["opened"])
        self.assertEqual(self.next_event("opened", f"book:{self.pride}")["hid"], hid)
        self.assert_refused(404, lambda: self.svc.open_highlight(self.pride, "nope"))

    def test_skip_and_undo(self):
        hid = self.first_hid(self.opened())
        result = self.svc.skip_highlight(self.pride, hid, True)
        self.assertEqual(result["highlight"]["state"], "skip")
        self.assertEqual(result["highlight"]["glyph"], "–")
        self.assertIn("Skipped", result["highlight"]["banner"])
        self.assertEqual(result["counts"]["todo"], 6)
        self.assert_refused(409, lambda: self.svc.add_card(self.pride, hid, "qa"), "skipped")
        self.assertEqual(self.svc.skip_highlight(self.pride, hid, False)["highlight"]["state"], "todo")
        self.assert_refused(400, lambda: self.svc.skip_highlight(self.pride, hid, "yes"))

    def test_reviewed_highlight_cannot_be_skipped_or_given_cards(self):
        view = self.opened(self.pending)
        reviewed = next(h for h in view["highlights"] if h["status"] == "reviewed")
        self.assert_refused(409, lambda: self.svc.skip_highlight(self.pending, reviewed["hid"], True))
        self.assert_refused(409, lambda: self.svc.add_card(self.pending, reviewed["hid"], "qa"))
        self.assert_refused(409, lambda: self.svc.suggest(self.pending, reviewed["hid"], {}))

    def test_add_card_kinds(self):
        view = self.opened()
        hid = self.first_hid(view)
        qa = self.svc.add_card(self.pride, hid, "qa")
        self.assertEqual(qa["card"]["kindLabel"], "Question and answer")
        self.assertEqual(qa["card"]["src"], "You")
        self.assertEqual(qa["card"]["front"], "")
        cloze = self.svc.add_card(self.pride, hid, "cloze")
        self.assertTrue(cloze["card"]["isCloze"])
        marked = self.svc.update_card(cloze["card"]["id"], front="The {{c1::answer::a hint}} is here.")["card"]
        self.assertEqual(marked["frontPlain"], "The [...] is here.")
        self.assertEqual(marked["backPlain"], "The answer is here.")
        self.assertEqual(marked["parts"], [{"text": "The "}, {"gap": True}, {"text": " is here."}])
        self.assertEqual(cloze["card"]["front"], view["highlights"][0]["mark"])
        qonly = self.svc.add_card(self.pride, hid, "qonly")
        self.assertEqual(qonly["card"]["kindLabel"], "Question only")
        self.assertEqual(qonly["card"]["backPh"], "Write the answer yourself")
        self.assertTrue(qonly["highlight"]["opened"])
        self.assertEqual(qonly["highlight"]["state"], "cards")
        self.assert_refused(400, lambda: self.svc.add_card(self.pride, hid, "nonsense"))

    def test_suggest_returns_at_once_and_delivers_through_store_and_event(self):
        view = self.opened()
        hid = self.first_hid(view)
        gate = self.ai.hold(view["highlights"][0]["text"])
        self.drain()
        result = self.svc.suggest(self.pride, hid, {"count": 3, "kind": "qa", "hint": "be brief"})
        self.assertTrue(result["working"])
        self.assertTrue(result["highlight"]["busy"])
        self.assertIn("claude -p", result["highlight"]["busyCmd"])
        self.assert_refused(409, lambda: self.svc.suggest(self.pride, hid, {}))
        self.assert_refused(409, lambda: self.svc.define(self.pride, hid, "manners"))
        gate.set()
        event = self.next_event("suggest", f"book:{self.pride}")
        self.assertEqual(event["state"], runs.FINISHED)
        self.assertEqual(event["hid"], hid)
        after = self.svc.book(self.pride)["highlights"][0]
        self.assertFalse(after["busy"])
        self.assertEqual(after["state"], "sugg")
        self.assertEqual(after["meta"].split(" · ")[0], f"{len(after['cards'])} suggested")
        self.assertEqual(after["cards"][0]["src"], "AI")
        self.assertIn("be brief", self.ai.calls[-1]["prompt"])
        self.assertEqual(self.svc._store.get_setting("count"), 3)

    def test_suggest_cloze_asks_for_one_and_keeps_the_remembered_count(self):
        hid = self.first_hid(self.opened())
        self.svc.suggest(self.pride, hid, {"count": 4, "kind": "cloze"})
        self.wait_jobs()
        cards = self.svc.book(self.pride)["highlights"][0]["cards"]
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]["isCloze"])
        self.assertEqual(self.svc._store.get_setting("count"), 4)
        self.assertEqual(self.svc._store.get_setting("kind"), "cloze")

    def test_suggest_failure_lands_on_the_highlight(self):
        view = self.opened()
        self.ai.reply(view["highlights"][0]["text"], ai.AIError("The AI is out of reach."))
        self.drain()
        self.svc.suggest(self.pride, self.first_hid(view), {})
        event = self.next_event("suggest")
        self.assertEqual(event["state"], runs.FAILED)
        self.assertIn("out of reach", event["error"])
        after = self.svc.book(self.pride)["highlights"][0]
        self.assertIn("out of reach", after["error"])
        self.assertIn("Failed", after["meta"])

    def test_suggest_options_are_checked(self):
        hid = self.first_hid(self.opened())
        for bad in ({"count": "two"}, {"count": True}, {"kind": "x"}, {"sees": "x"}, {"companion": "x"}, {"hint": 3}):
            self.assert_refused(400, lambda: self.svc.suggest(self.pride, hid, bad))
        self.assert_refused(404, lambda: self.svc.suggest(self.pride, hid, {"prompt": "../../etc"}))
        self.assert_refused(400, lambda: self.svc.suggest(self.pride, hid, {"model": "gpt-1"}))
        self.svc.update_settings({"assistants": {"codex": False}})
        self.assert_refused(409, lambda: self.svc.suggest(self.pride, hid, {"model": "gpt-6.1-sol"}), "turned off")

    def test_define_adds_a_pair_on_a_thread(self):
        view = self.opened(self.pending)
        hid = next(h["hid"] for h in view["highlights"] if h["status"] != "reviewed")
        self.assert_refused(400, lambda: self.svc.define(self.pending, hid, "one two three four"), "1 to 3 words")
        self.assert_refused(400, lambda: self.svc.define(self.pending, hid, "   "))
        self.drain()
        result = self.svc.define(self.pending, hid, "  ardour  ")
        self.assertTrue(result["working"])
        event = self.next_event("define", f"book:{self.pending}")
        self.assertEqual(event["state"], runs.FINISHED)
        self.assertEqual(event["cards"], 2)
        h = next(row for row in self.svc.book(self.pending)["highlights"] if row["hid"] == hid)
        self.assertEqual([c["kind"] for c in h["cards"]], ["def", "def"])
        self.assertTrue(h["cards"][0]["front"].startswith("Define "))
        self.assertEqual(h["cards"][0]["src"], "AI")
        self.assertEqual(h["cards"][0]["kindLabel"], "Definition")

    def test_define_with_a_companion_says_so(self):
        view = self.opened(self.mostly)
        hid = next(h["hid"] for h in view["highlights"] if h["status"] != "reviewed")
        self.svc.update_settings({"companion_send": companion.WHOLE})
        self.svc.define(self.mostly, hid, "habit")
        self.wait_jobs()
        h = next(row for row in self.svc.book(self.mostly)["highlights"] if row["hid"] == hid)
        self.assertEqual(h["cards"][0]["src"], "AI, using the Book Companion")

    def test_define_failure_is_stored(self):
        view = self.opened()
        self.ai.reply("highlight", "not json")
        self.ai.replies[view["highlights"][0]["text"]] = "not json"
        self.drain()
        self.svc.define(self.pride, self.first_hid(view), "manners")
        event = self.next_event("define")
        self.assertEqual(event["state"], runs.FAILED)
        self.assertTrue(event["error"])


class Cards(Base):
    def setUp(self):
        super().setUp()
        self.view = self.opened()
        self.hid = self.first_hid(self.view)
        self.svc.suggest(self.pride, self.hid, {"count": 2})
        self.wait_jobs()
        self.cards = self.svc.book(self.pride)["highlights"][0]["cards"]

    def test_edit_follows_was_edited_and_back_again(self):
        card = self.cards[0]
        edited = self.svc.update_card(card["id"], front="A changed front?")
        self.assertEqual(edited["card"]["src"], "AI, edited")
        self.assertEqual(edited["card"]["front"], "A changed front?")
        restored = self.svc.update_card(card["id"], front=card["front"])
        self.assertEqual(restored["card"]["src"], "AI")
        self.assert_refused(400, lambda: self.svc.update_card(card["id"], front=3))
        self.assert_refused(404, lambda: self.svc.update_card("nope", front="x"))

    def test_checks_warn_about_a_duplicate_and_a_card_that_leans_on_the_book(self):
        card = self.cards[0]
        other = self.cards[1]
        result = self.svc.update_card(other["id"], front=card["front"], back=card["back"])
        warned = [c["warn"] for c in result["cards"]]
        self.assertTrue(any(warned), warned)
        leaning = self.svc.update_card(other["id"], front="What does this mean?", back="x")
        self.assertTrue(leaning["card"]["warn"])

    def test_remove_feeds_the_rejected_list_and_restore_brings_it_back(self):
        card = self.cards[0]
        removed = self.svc.remove_card(card["id"])
        self.assertEqual(removed["removed"]["state"], "removed")
        self.assertIn("won't suggest it again", removed["undo"])
        self.assertEqual(len(removed["cards"]), 1)
        self.svc.suggest(self.pride, self.hid, {"count": 1})
        self.wait_jobs()
        self.assertIn(card["front"], self.ai.calls[-1]["prompt"])
        back = self.svc.restore_card(card["id"])
        self.assertEqual(back["card"]["state"], "draft")
        self.assert_refused(404, lambda: self.svc.remove_card("nope"))
        self.assert_refused(404, lambda: self.svc.restore_card("nope"))

    def test_edits_are_refused_after_removal(self):
        self.svc.remove_card(self.cards[0]["id"])
        self.assert_refused(409, lambda: self.svc.update_card(self.cards[0]["id"], front="x"))
        self.assert_refused(409, lambda: self.svc.rewrite_card(self.cards[0]["id"], None))

    def test_rewrite_on_a_thread(self):
        card = self.cards[0]
        self.ai.gate.clear()
        self.drain()
        result = self.svc.rewrite_card(card["id"], "shorter")
        self.assertTrue(result["card"]["rewriting"])
        self.assertEqual(result["card"]["rwBtn"], "Rewriting...")
        self.assert_refused(409, lambda: self.svc.rewrite_card(card["id"], "again"))
        self.ai.gate.set()
        event = self.next_event("rewrite", f"book:{self.pride}")
        self.assertEqual(event["state"], runs.FINISHED)
        self.assertEqual(event["card_id"], card["id"])
        after = self.svc.book(self.pride)["highlights"][0]["cards"][0]
        self.assertEqual(after["src"], "AI, rewritten")
        self.assertNotEqual(after["front"], card["front"])
        self.assertFalse(after["rewriting"])
        self.assertIn("shorter", self.ai.calls[-1]["prompt"])

    def test_rewrite_keeps_an_edit_made_meanwhile(self):
        card = self.cards[0]
        self.ai.gate.clear()
        self.drain()
        asked = len(self.ai.calls)
        self.svc.rewrite_card(card["id"])
        self.assertTrue(wait_until(lambda: len(self.ai.calls) > asked, LONG))
        self.svc.update_card(card["id"], front="My own words?")
        self.ai.gate.set()
        event = self.next_event("rewrite")
        self.assertEqual(event["state"], runs.FAILED)
        self.assertIn("your version was kept", event["error"])
        self.assertEqual(self.svc.book(self.pride)["highlights"][0]["cards"][0]["front"], "My own words?")

    def test_rewrite_of_a_card_removed_meanwhile_drops_the_rewrite(self):
        card = self.cards[0]
        self.ai.gate.clear()
        self.drain()
        asked = len(self.ai.calls)
        self.svc.rewrite_card(card["id"])
        self.assertTrue(wait_until(lambda: len(self.ai.calls) > asked, LONG))
        self.svc.remove_card(card["id"])
        self.ai.gate.set()
        self.assertEqual(self.next_event("rewrite")["state"], runs.FAILED)
        self.assertEqual(self.svc._store.get_card(card["id"])["state"], "removed")

    def test_card_context_gives_what_a_one_card_request_needs(self):
        about = self.svc.runner.card_context(self.pride, self.hid)
        self.assertEqual(about["about"]["text"], self.view["highlights"][0]["text"])
        self.assertIn("prompt", about["request"])
        self.assertEqual(about["options"]["model"], self.svc._store.get_setting("model"))
        with self.assertRaises(runs.RunError):
            self.svc.runner.card_context(self.pride, "nope")


class Runs(Base):
    def test_open_never_starts_a_run(self):
        view = self.svc.open_book(self.pride)
        self.assertNotIn("auto_started", view)
        self.assertEqual(view["run"]["phase"], "idle")
        self.assertIsNone(self.svc._store.get_run(self.pride))
        self.assertEqual(self.svc.open_book(self.pride)["run"]["phase"], "idle")
        self.assertIsNone(self.svc._store.get_run(self.pride))

    def test_a_started_run_shows_as_busy_then_done(self):
        gate = self.ai.hold("connections")
        self.svc.open_book(self.pride)
        self.svc.run(self.pride, "start")
        view = self.svc.book(self.pride)
        self.assertTrue(view["run"]["active"])
        self.assertEqual(view["run"]["phase"], "busy")
        self.assertTrue(view["run"]["bg"]["show"])
        self.assertIn("Writing suggestions", view["run"]["bg"]["label"])
        gate.set()
        self.wait_run(self.pride)
        again = self.svc.open_book(self.pride)
        self.assertEqual(again["run"]["phase"], "done")
        self.assertEqual(again["run"]["rows"][0]["path"], "Done · 7 of 7 highlights")
        self.assertIn("Done · ", again["run"]["rows"][1]["path"])
        self.assertTrue(again["connections"])
        self.assertEqual(again["suggest"]["due"], 0)

    def test_suggest_again_counts_and_replaces_what_is_not_opened(self):
        self.opened()
        self.svc.run(self.pride, "start", {"connections": False})
        self.wait_run(self.pride)
        view = self.svc.book(self.pride)
        self.assertEqual((view["suggest"]["due"], view["suggest"]["redo"]), (0, 7))
        hid = self.first_hid(view)
        self.svc.open_highlight(self.pride, hid)
        view = self.svc.book(self.pride)
        self.assertEqual(view["suggest"]["redo"], 6)
        self.assertEqual(view["suggest"]["againBtn"], "Suggest again for 6 highlights")
        kept = [card["id"] for card in view["highlights"][0]["cards"]]
        self.svc.run(self.pride, "start", {"connections": False, "fresh": True})
        self.wait_run(self.pride)
        after = self.svc.book(self.pride)
        self.assertEqual([card["id"] for card in after["highlights"][0]["cards"]], kept)
        self.assertEqual(after["run"]["rows"][0]["path"], "Done · 6 of 6 highlights")

    def test_start_pause_resume_stop(self):
        self.opened()
        gate = self.ai.hold("connections")
        self.ai.gate.clear()
        started = self.svc.run(self.pride, "start", {"count": 1, "connections": True})
        self.assertTrue(started["run"]["active"])
        self.assertEqual(self.svc._store.get_setting("count"), 1)
        paused = self.svc.run(self.pride, "pause")
        self.assertEqual(paused["run"]["bg"]["btn"], "Resume")
        self.assertEqual(paused["run"]["bg"]["label"], "Suggestions paused")
        resumed = self.svc.run(self.pride, "resume")
        self.assertEqual(resumed["run"]["bg"]["btn"], "Pause")
        self.assertEqual(self.svc.run(self.pride, "start")["run"]["bg"]["btn"], "Pause")
        stopped = self.svc.run(self.pride, "stop")
        self.ai.gate.set()
        gate.set()
        self.wait_run(self.pride)
        self.assertEqual(self.svc.book(self.pride)["run"]["status"], stopped["run"]["status"])
        self.assert_refused(400, lambda: self.svc.run(self.pride, "dance"))
        self.assert_refused(404, lambda: self.svc.run("nope", "start"))
        self.assert_refused(400, lambda: self.svc.run(self.pride, "start", "not a dict"))

    def test_run_writes_key_ideas_with_a_companion_and_covered_points(self):
        view = self.svc.open_book(self.mostly)
        self.assertTrue(view["companion"]["have"])
        self.svc.run(self.mostly, "start", {"ideas": True, "connections": False})
        self.wait_run(self.mostly)
        view = self.svc.book(self.mostly)
        titles = [row["title"] for row in view["run"]["rows"]]
        self.assertIn("Key ideas you didn't highlight", titles)
        self.assertEqual(view["counts"]["ideaCount"], len(view["ideas"]))

    def test_ai_chapter_notes_show_covered_and_uncovered_points(self):
        view = self.opened(self.mostly)
        h = next(row for row in view["highlights"] if row["ch"])
        store = self.svc._store
        idea = store.add_idea(self.mostly, h["ch"], "An idea", "Why", "A quote", "F?", "B")
        store.set_chapter_notes(self.mostly, h["ch"], "The big idea", [
            {"text": "Covered point", "covered": True},
            {"text": "Open point", "covered": False, "idea": idea["id"]},
        ])
        comp = next(row for row in self.svc.book(self.mostly)["highlights"] if row["hid"] == h["hid"])["comp"]
        self.assertTrue(comp["tagged"])
        self.assertEqual(comp["big"], "The big idea")
        self.assertEqual([p["glyph"] for p in comp["pts"]], ["✓", "○"])
        self.assertTrue(comp["pts"][1]["idea"])

    def test_reader_notes_panel_without_ai_notes_is_untagged(self):
        view = self.opened(self.mostly)
        chapter = next(h["ch"] for h in view["highlights"] if h["ch"])
        file = self.fake.local("mostly_done").with_name("Time - Arnold Bennett.companion.md")
        file.write_text(f"# Time\n\n## 2. Chapters\n\n### {chapter} <!-- pos: 0.0100 -->\n\nThe big idea.\n\n- A bullet.\n", encoding="utf-8")
        panels = [h["comp"] for h in self.svc.book(self.mostly)["highlights"] if h["ch"] == chapter]
        self.assertTrue(panels[0]["found"])
        self.assertFalse(panels[0]["tagged"])
        self.assertEqual(panels[0]["big"], "The big idea.")
        self.assertEqual(panels[0]["pts"], [{"t": "A bullet.", "glyph": "", "covered": None, "idea": None}])

    def test_no_companion_means_no_notes_panel_and_no_ideas(self):
        view = self.opened(self.pending)
        self.assertTrue(view["companion"]["none"])
        self.assertTrue(view["ideasNeedComp"])
        self.assertEqual(view["ideaEmptyText"], "Nothing to compare against yet.")
        self.assertTrue(all(h["comp"] is None for h in view["highlights"]))

    def test_later_research_warning_shows_only_with_a_companion(self):
        view = self.opened(self.mostly)
        h = next(row for row in view["highlights"] if row["status"] != "reviewed")
        self.svc._store.set_later_research(self.mostly, h["hid"], "Later studies questioned this.")
        after = next(row for row in self.svc.book(self.mostly)["highlights"] if row["hid"] == h["hid"])
        self.assertEqual(after["later"], "Later studies questioned this.")
        self.assertIn("Later research", after["meta"])

    def test_interrupted_run_is_offered_again_after_a_restart(self):
        self.opened()
        self.ai.gate.clear()
        self.svc.run(self.pride, "start", {})
        self.svc.stop(wait=LONG)
        self.ai.gate.set()
        again = self.make_service()
        view = again.book(self.pride)
        self.assertTrue(view["run"]["offer_again"])
        self.assertIn("Carry on?", view["run"]["offer"])


class Connections(Base):
    def setUp(self):
        super().setUp()
        self.view = self.opened()

    def find(self, **options):
        self.drain()
        result = self.svc.find_connections(self.pride, options)
        self.assertTrue(result["working"])
        return self.next_event("find", f"book:{self.pride}")

    def test_find_from_all_highlights_with_a_chapter_filter(self):
        picks = self.svc.book(self.pride)["find"]["picks"]
        self.assertTrue(picks)
        self.assertTrue(all("pending" in p and "all" in p for p in picks))
        event = self.find(chapters=[p["id"] for p in picks], scope=runs.SCOPE_ALL, count=2, hint="across chapters")
        self.assertEqual(event["state"], runs.FINISHED)
        self.assertEqual(event["found"], len(event["connections"]))
        view = self.svc.book(self.pride)
        self.assertEqual(view["counts"]["metaCount"], len(view["connections"]))
        item = view["connections"][0]
        self.assertEqual(item["src"], "AI")
        self.assertFalse(item["opened"])
        self.assertIn("Not opened", item["meta"])
        self.assertTrue(item["sources"][0]["mark"])
        self.assertIn("across chapters", self.ai.calls[-1]["prompt"])

    def test_find_shows_busy_and_refuses_a_second_one(self):
        gate = self.ai.hold("connections")
        result = self.svc.find_connections(self.pride, {})
        self.assertTrue(result["find"]["busy"])
        self.assertIn("claude -p", result["find"]["busyCmd"])
        self.assert_refused(409, lambda: self.svc.find_connections(self.pride, {}))
        gate.set()
        self.wait_jobs()
        self.assertFalse(self.svc.book(self.pride)["find"]["busy"])

    def test_find_failure_shows_on_the_screen(self):
        self.ai.reply("connections", ai.AIError("Too busy."))
        event = self.find()
        self.assertEqual(event["state"], runs.FAILED)
        self.assertIn("Too busy", self.svc.book(self.pride)["find"]["error"])

    def test_find_options_are_checked(self):
        for bad in ({"scope": "x"}, {"chapters": "one"}, {"count": "x"}, {"hint": 3}, {"model": "nope"}):
            self.assert_refused(400, lambda: self.svc.find_connections(self.pride, bad))
        self.assert_refused(404, lambda: self.svc.find_connections(self.pride, {"prompt": "../x"}))

    def test_open_edit_and_remove_a_connection(self):
        self.find()
        item = self.svc.book(self.pride)["connections"][0]
        self.assertTrue(self.svc.open_connection(item["id"])["connection"]["opened"])
        edited = self.svc.update_connection(item["id"], front="New question?")
        self.assertEqual(edited["connection"]["src"], "AI, edited")
        self.assertEqual(edited["connection"]["front"], "New question?")
        removed = self.svc.remove_connection(item["id"])
        self.assertEqual(removed["connection"]["state"], "removed")
        self.assertEqual(removed["connections"], [row for row in removed["connections"] if row["id"] != item["id"]])
        for call in (self.svc.open_connection, self.svc.remove_connection):
            self.assert_refused(404, lambda: call("nope"))
        self.assert_refused(404, lambda: self.svc.update_connection("nope", front="x"))
        self.assert_refused(409, lambda: self.svc.update_connection(item["id"], front="x"))


class Ideas(Base):
    def test_open_edit_and_remove_a_key_idea(self):
        view = self.opened(self.mostly)
        self.assertFalse(view["ideas"])
        idea = self.svc._store.add_idea(self.mostly, "Chapter 1", "An idea", "Why it matters", "Quote", "Front?", "Back")
        idea_id = idea["id"]
        listed = self.svc.book(self.mostly)["ideas"][0]
        self.assertEqual(listed["src"], "From the Book Companion")
        self.assertIn("Not opened", listed["meta"])
        self.assertTrue(self.svc.open_idea(idea_id)["idea"]["opened"])
        edited = self.svc.update_idea(idea_id, back="New back")
        self.assertEqual(edited["idea"]["src"], "From the Book Companion, edited")
        self.assertEqual(self.svc.remove_idea(idea_id)["ideas"], [])
        self.assert_refused(404, lambda: self.svc.open_idea("nope"))
        self.assert_refused(404, lambda: self.svc.update_idea("nope", front="x"))
        self.assert_refused(404, lambda: self.svc.remove_idea("nope"))


class Saving(Base):
    def write_card(self, book_id, hid, front="Why this?", back="Because."):
        card = self.svc.add_card(book_id, hid, "qa")["card"]
        return self.svc.update_card(card["id"], front=front, back=back)["card"]

    def test_whole_flow_from_open_to_verified_save_changes_only_the_inbox_files(self):
        before = self.fake.snapshot()
        self.svc.start()
        self.svc.open_book(self.pride)
        self.svc.run(self.pride, "start")
        self.wait_run(self.pride)
        view = self.svc.book(self.pride)
        hid = self.first_hid(view)
        self.svc.open_highlight(self.pride, hid)
        card = view["highlights"][0]["cards"][0]
        self.svc.update_card(card["id"], front="An edited question?")
        self.svc.remove_card(view["highlights"][0]["cards"][1]["id"])
        self.write_card(self.pride, self.first_hid(view, 1))
        self.svc.skip_highlight(self.pride, self.first_hid(view, 2), True)
        connection = view["connections"][0]
        self.svc.open_connection(connection["id"])

        preview = self.svc.save_preview(self.pride)
        json.dumps(preview, allow_nan=False)
        expected = len(view["highlights"][0]["cards"]) - 1 + len(view["highlights"][1]["cards"]) + 1 + 1
        self.assertEqual(preview["saveCount"], expected)
        self.assertEqual(preview["saveBtn"], "Save to reader")
        self.assertEqual(len(preview["steps"]), 4)
        self.assertEqual(preview["steps"][3]["title"], "Read back from the reader to check")
        self.assertTrue(preview["groups"])
        self.assertTrue(preview["markText"])
        self.drain()
        result = self.svc.save_book(self.pride, mark_connection_sources=True)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["summary"]["cards"], expected)
        self.assertEqual(result["done"]["doneTitle"], f"{expected} cards on your reader")
        self.assertIn("deck", result["done"]["lede"])
        self.assertEqual(result["done"]["doneRows"][3]["t"], "Read back from the reader")
        self.assertEqual(result["save"]["saveLabel"], "Save 0 cards")
        statuses = [e.get("status") for e in self.drain() if e.get("type") == "save"]
        self.assertEqual(statuses, ["saving", "verified"])

        self.assertEqual(self.assert_only_inbox_changed(before), ALLOWED_WRITES)
        inbox = (self.mount / paths.INBOX_FILE).read_text(encoding="utf-8")
        self.assertIn("An edited question?", inbox)

        after = self.svc.book(self.pride)
        first = after["highlights"][0]
        self.assertEqual(first["state"], "rev")
        self.assertTrue(first["saved"])
        self.assertEqual(first["saved"][0]["front"], "An edited question?")
        self.assertFalse(after["connections"])

        self.assertEqual(self.svc.reader_eject(), {"ejected": True, "ejectLabel": "Safe to unplug"})
        self.assertEqual(self.ejected, [str(self.mount)])

    def test_preview_says_what_is_blocked_and_what_stays(self):
        view = self.opened()
        hid = self.first_hid(view)
        card = self.svc.add_card(self.pride, hid, "qonly")["card"]
        self.svc.update_card(card["id"], front="A question with no answer?")
        preview = self.svc.save_preview(self.pride)
        self.assertTrue(preview["saveBlocked"])
        self.assertEqual(preview["needCount"], 1)
        self.assertTrue(preview["needText"])
        self.assertTrue(preview["leftText"])
        self.assert_refused(409, lambda: self.svc.save_book(self.pride))
        self.svc.update_card(card["id"], back="The answer.")
        self.assertFalse(self.svc.save_preview(self.pride)["saveBlocked"])

    def test_mark_connection_sources_is_kept(self):
        self.opened()
        self.assertTrue(self.svc.save_preview(self.pride)["markMeta"])
        self.assertFalse(self.svc.save_preview(self.pride, False)["markMeta"])
        self.assertTrue(self.svc.save_preview(self.pride)["markMeta"])
        self.write_card(self.pride, self.first_hid(self.svc.book(self.pride)))
        self.svc.save_book(self.pride, mark_connection_sources=False)
        self.assertFalse(self.svc.save_preview(self.pride)["markMeta"])
        self.assert_refused(400, lambda: self.svc.save_book(self.pride, mark_connection_sources="no"))
        self.assert_refused(400, lambda: self.svc.save_preview(self.pride, "no"))

    def test_second_save_during_a_save_is_refused(self):
        view = self.opened()
        self.write_card(self.pride, self.first_hid(view))
        self.assertTrue(self.svc._save_lock.acquire(blocking=False))
        try:
            self.assert_refused(409, lambda: self.svc.save_book(self.pride), "already under way")
            self.assert_refused(409, lambda: self.svc.save_retry(self.pride), "nothing waiting")
        finally:
            self.svc._save_lock.release()
        self.assertEqual(self.svc.save_book(self.pride)["status"], "verified")

    def test_real_double_click_saves_only_once(self):
        view = self.opened()
        self.write_card(self.pride, self.first_hid(view))
        started, release = threading.Event(), threading.Event()
        real = save.save

        def held(*args, **kwargs):
            started.set()
            release.wait(LONG)
            return real(*args, **kwargs)

        outcome = []

        def first():
            try:
                outcome.append(self.svc.save_book(self.pride)["status"])
            finally:
                self.svc.release_thread()

        with mock.patch.object(save, "save", held):
            thread = threading.Thread(target=first)
            thread.start()
            self.assertTrue(started.wait(LONG))
            self.assert_refused(409, lambda: self.svc.save_book(self.pride), "already under way")
            self.assertTrue(self.svc.save_preview(self.pride)["saveBlocked"])
            release.set()
            thread.join(LONG)
        self.assertEqual(outcome, ["verified"])
        self.assertEqual(len(self.svc._store.list_batches(self.pride)), 1)

    def test_unplugged_save_waits_and_finishes_when_the_reader_appears(self):
        before = self.fake.snapshot()
        view = self.opened()
        hid = self.first_hid(view)
        self.write_card(self.pride, hid)
        self.svc.tick()
        self.unplug()
        self.svc.tick()
        preview = self.svc.save_preview(self.pride)
        self.assertFalse(preview["connected"])
        self.assertEqual(preview["saveBtn"], "Back up and wait for reader")
        self.assertTrue(preview["saveUnplugNote"])
        result = self.svc.save_book(self.pride)
        self.assertEqual(result["status"], "waiting")
        self.assertIsNone(result["done"])
        self.assertTrue(self.svc.save_preview(self.pride)["saveWaiting"])
        self.assertIn(str(self.mount), self.svc.save_preview(self.pride)["waitText"])
        self.assertEqual(self.svc.state()["pending_saves"][0]["status"], "waiting")
        self.assertEqual(self.svc.book(self.pride)["highlights"][0]["status"], "reviewed")
        self.assertEqual(self.svc.save_retry(self.pride)["status"], "waiting")

        self.replug()
        self.drain()
        self.svc.tick()
        self.assertTrue(self.svc.wait_for_refresh(LONG))
        events = [e for e in self.drain() if e.get("type") == "save"]
        self.assertEqual(events[0]["status"], "verified")
        self.assertEqual(events[0]["done"]["saved"], 1)
        self.assertEqual(self.svc.state()["pending_saves"], [])
        self.assertEqual(self.svc.book(self.pride)["highlights"][0]["status"], "reviewed")
        self.assertEqual(self.assert_only_inbox_changed(before), ALLOWED_WRITES)
        self.assert_refused(409, lambda: self.svc.save_retry(self.pride), "nothing waiting")

    def test_waiting_save_finishes_at_startup(self):
        view = self.opened()
        self.write_card(self.pride, self.first_hid(view))
        self.unplug()
        self.assertEqual(self.svc.save_book(self.pride)["status"], "waiting")
        self.svc.stop(wait=LONG)
        self.replug()
        again = self.make_service()
        listener = again.subscribe()
        self.addCleanup(listener.close)
        again.start()
        self.assertTrue(wait_until(lambda: not again._store.unverified_batches(), LONG))
        self.assertEqual(again.book(self.pride)["highlights"][0]["status"], "reviewed")

    def test_failed_write_is_shown_and_retried(self):
        view = self.opened()
        self.write_card(self.pride, self.first_hid(view))
        with mock.patch.object(device, "write_files", side_effect=device.DeviceWriteError("The reader lost it.")):
            result = self.svc.save_book(self.pride)
        self.assertEqual(result["status"], "failed")
        self.assertIn("lost it", result["error"])
        preview = self.svc.save_preview(self.pride)
        self.assertEqual(preview["failed"][0]["error"], result["error"])
        self.assertTrue(self.svc.book(self.pride)["save"]["failed"])
        retried = self.svc.save_retry(self.pride)
        self.assertEqual(retried["status"], "verified")
        self.assertEqual(retried["done"]["saved"], 1)

    def test_eject_failure_is_a_plain_sentence(self):
        def refuse(_mount):
            raise save.EjectError("Something on this Mac is still using the reader. Close it and try again.")

        svc = self.make_service(eject=refuse)
        self.assert_refused(409, svc.reader_eject, "still using")

    def test_eject_with_nothing_there_is_fine(self):
        self.unplug()
        self.assertEqual(self.svc.reader_eject()["ejectLabel"], "Safe to unplug")
        self.assertEqual(self.ejected, [])

    def test_default_eject_goes_through_save_eject(self):
        with mock.patch.object(device, "_run_diskutil") as diskutil:
            svc = Service(store=Store(self.home / "pbanki.db"), query=self.ai)
            self.services.append(svc)
            svc.reader_eject()
        diskutil.assert_not_called()

    def test_plugin_update_is_a_proven_write_on_the_button_only(self):
        shutil.rmtree(self.tmp / "reader")
        self.fake = build_reader(self.tmp / "reader", plugin="old")
        before = self.fake.snapshot()
        self.assertFalse(self.svc.state()["reader"]["plugin_ok"])
        self.assertEqual(self.fake.snapshot(), before)
        with mock.patch.object(device, "write_files", wraps=device.write_files) as writes:
            result = self.svc.reader_plugin_update()
        writes.assert_called_once()
        self.assertTrue(result["copied"])
        self.assertTrue(result["reader"]["plugin_ok"])
        changed = {p for p in before.keys() | self.fake.snapshot().keys() if before.get(p) != self.fake.snapshot().get(p)}
        self.assertTrue(all(path.startswith(paths.PLUGIN_DIR) for path in changed), changed)
        self.unplug()
        self.assert_refused(409, self.svc.reader_plugin_update, "Plug in")

    def test_plugin_write_failure_is_503(self):
        with mock.patch.object(save, "plugin_update", side_effect=device.DeviceWriteError("It didn't land.")):
            self.assert_refused(503, self.svc.reader_plugin_update, "didn't land")


class Unplugged(Base):
    def setUp(self):
        super().setUp()
        self.view = self.opened()
        self.hid = self.first_hid(self.view)
        self.svc.tick()
        self.assertTrue(self.svc.wait_for_refresh(LONG))
        self.unplug()
        self.svc.tick()

    def test_everything_works_from_the_cache(self):
        state = self.svc.state()
        self.assertFalse(state["reader"]["connected"])
        self.assertEqual(state["reader"]["label"], "Reader unplugged")
        self.assertEqual(state["reader"]["dot"], "dot off")
        rows = self.svc.books()
        self.assertFalse(rows["connected"])
        self.assertIn(self.pride, [row["id"] for row in rows["books"]])
        opened = self.svc.open_book(self.pride)
        self.assertEqual(len(opened["highlights"]), 7)
        self.assertFalse(opened["reader"]["connected"])
        self.assertEqual(opened["highlights"][0]["mark"], self.view["highlights"][0]["mark"])

        card = self.svc.add_card(self.pride, self.hid, "qa")["card"]
        self.svc.update_card(card["id"], front="Unplugged?", back="Yes.")
        self.svc.open_highlight(self.pride, self.hid)
        self.svc.skip_highlight(self.pride, self.first_hid(self.view, 1), True)
        self.svc.suggest(self.pride, self.first_hid(self.view, 2), {})
        self.svc.define(self.pride, self.first_hid(self.view, 3), "manners")
        self.svc.rewrite_card(card["id"], "shorter")
        self.wait_jobs()
        self.svc.find_connections(self.pride, {})
        self.wait_jobs()
        connections = self.svc.book(self.pride)["connections"]
        self.assertTrue(connections)
        self.svc.open_connection(connections[0]["id"])
        self.svc.update_connection(connections[0]["id"], back="Unplugged too.")
        idea = self.svc._store.add_idea(self.pride, "Chapter", "T", "N", "Q", "F?", "B")
        self.svc.open_idea(idea["id"])
        self.svc.update_idea(idea["id"], back="Unplugged too.")
        self.svc.remove_idea(idea["id"])
        spare = self.svc.add_card(self.pride, self.hid, "qa")["card"]
        self.svc.remove_card(spare["id"])
        self.svc.restore_card(spare["id"])
        self.svc.remove_card(spare["id"])
        self.svc.run(self.pride, "start", {"connections": False})
        self.wait_run(self.pride)
        self.assertEqual(self.svc.save_preview(self.pride)["saveBtn"], "Back up and wait for reader")
        self.assertEqual(self.svc.save_book(self.pride)["status"], "waiting")
        self.assertEqual(self.svc.save_retry(self.pride)["status"], "waiting")
        self.assertIsNotNone(self.svc.update_settings({"suggest_ideas": False})["settings"])
        self.assertTrue(self.svc.prompt("Standard")["files"])
        self.svc.open_prompt("Standard", "questions.md")
        self.svc.reveal_prompt("Standard", "questions.md")
        self.assertEqual(len(self.launched), 2)

        # Plugged back in, the waiting save lands and every reader action works again
        self.replug()
        self.drain()
        self.svc.tick()
        self.assertTrue(self.svc.wait_for_refresh(LONG))
        self.assertEqual([e["status"] for e in self.drain() if e["type"] == "save"], ["verified"])
        self.assertEqual(self.svc.book(self.pride)["highlights"][0]["status"], "reviewed")
        self.assertTrue(self.svc.save_preview(self.pride)["connected"])
        self.assertEqual(self.svc.passage(self.pride, self.hid, more=2)["limited"], False)
        self.svc.reader_plugin_update()
        self.assertEqual(self.svc.reader_eject()["ejectLabel"], "Safe to unplug")
        self.assertTrue(self.svc.companion_create(self.pride)["companion"]["running"])

    def test_resume_offers_the_book_with_unsaved_cards(self):
        card = self.svc.add_card(self.pride, self.hid, "qa")["card"]
        self.svc.update_card(card["id"], front="Unplugged?", back="Yes.")
        resume = self.svc.state()["resume"]
        self.assertEqual(resume["book_id"], self.pride)
        self.assertEqual(resume["text"], "1 unsaved card is safe on this computer")
        self.assertIn("plug in when you save", resume["detail"])
        row = next(row for row in self.svc.books()["books"] if row["id"] == self.pride)
        self.assertEqual(row["draft"], "1 unsaved card")
        self.assertEqual(row["action"], "Continue")

    def test_reviewed_status_comes_from_save_while_unplugged(self):
        self.replug()
        self.svc.tick()
        self.svc.open_book(self.pending)
        self.unplug()
        self.svc.tick()
        with mock.patch.object(save, "reviewed_keys", wraps=save.reviewed_keys) as reviewed:
            view = self.svc.book(self.pending)
        self.assertTrue(reviewed.called)
        self.assertTrue(all(call.args[3] is None for call in reviewed.call_args_list))
        self.assertEqual(view["filters"][2]["label"], "Reviewed 2")

    def test_companion_actions_ask_for_the_reader(self):
        view = self.svc.book(self.pride)
        self.assertFalse(view["companion"]["can_copy"])
        self.assertFalse(view["companion"]["can_create"])
        self.assertIn("Plug in", view["companion"]["problem"])
        self.assert_refused(409, lambda: self.svc.companion_create(self.pride), "Plug in")

    def test_replugged_reader_refreshes_the_cache(self):
        self.replug()
        self.drain()
        self.svc.tick()
        self.assertTrue(self.svc.wait_for_refresh(LONG))
        events = self.drain()
        self.assertTrue(any(e["topic"] == "reader" and e["type"] == "changed" and e["connected"] for e in events))
        self.assertTrue(any(e["topic"] == "reader" and e["type"] == "books" for e in events))
        self.assertTrue(self.svc.state()["reader"]["connected"])
        row = next(row for row in self.svc.books()["books"] if row["id"] == self.pending)
        self.assertEqual(row["hl"], "3 to do of 5")


class Watching(Base):
    def test_events_on_unplug_and_replug(self):
        self.svc.tick()
        self.assertTrue(self.svc.wait_for_refresh(LONG))
        self.drain()
        self.unplug()
        self.svc.tick()
        changed = self.next_event("changed", "reader")
        self.assertFalse(changed["connected"])
        self.svc.tick()
        self.assertEqual(self.next_event("checked", "reader")["connected"], False)
        self.replug()
        self.svc.tick()
        self.assertTrue(self.next_event("changed", "reader")["connected"])
        self.assertTrue(self.svc.wait_for_refresh(LONG))

    def test_remount_is_not_shown_as_unplugged(self):
        self.svc.tick()
        self.assertTrue(self.svc.wait_for_refresh(LONG))
        self.svc.state()
        self.drain()
        thread_done = threading.Event()

        def tick():
            try:
                self.svc.tick()
            finally:
                self.svc.release_thread()
                thread_done.set()

        with device.remount_lock:
            self.unplug()
            thread = threading.Thread(target=tick)
            thread.start()
            self.assertFalse(thread_done.wait(0.3))
            self.assertEqual(self.drain(), [])
            self.assertTrue(self.svc.state()["reader"]["connected"])
            self.assertTrue(self.svc._reader_small()["connected"])
            self.replug()
        thread.join(LONG)
        self.assertEqual([e for e in self.drain() if e["topic"] == "reader"], [])

    def test_read_is_dropped_when_the_reader_leaves_during_it(self):
        self.opened()

        def leaves(_mount):
            self.unplug()
            return ["half a read"]

        self.assertIsNone(self.svc._read_reader(leaves))
        self.replug()

    def test_watcher_thread_polls_and_stops(self):
        svc = self.make_service(poll_seconds=0.02)
        listener = svc.subscribe({"reader"})
        self.addCleanup(listener.close)
        svc.start()
        svc.start()
        self.assertTrue(listener.get(timeout=LONG)["connected"])
        self.unplug()
        deadline = time.monotonic() + LONG
        seen = None
        while time.monotonic() < deadline:
            event = listener.get(timeout=0.2)
            if event and event["type"] == "changed":
                seen = event
                break
        self.assertFalse(seen["connected"])
        self.replug()
        svc.stop(wait=LONG)
        self.assertFalse(any(t.name == "pbanki-watcher" and t.is_alive() for t in threading.enumerate()))

    def test_prompts_watcher_pushes_a_change(self):
        self.svc.tick()
        self.drain()
        file = paths.prompts_dir() / "Standard" / "questions.md"
        file.write_text("New words\n", encoding="utf-8")
        os.utime(file, (time.time() + 5, time.time() + 5))
        self.svc.tick()
        event = self.next_event("changed", "prompts")
        self.assertTrue(any("questions.md" in path for path in event["paths"]))
        self.assertEqual(self.svc.prompt("Standard")["reloadAgo"], 0)

    def test_subscribe_filters_topics_and_drops_oldest_when_full(self):
        listener = self.svc.subscribe({"settings"})
        self.addCleanup(listener.close)
        self.svc.update_settings({"suggest_ideas": False})
        self.assertEqual(listener.get(timeout=LONG)["topic"], "settings")
        with self.svc.subscribe() as full:
            for n in range(service.EVENT_BACKLOG + 3):
                self.svc._hub.publish({"topic": "reader", "n": n})
            self.assertEqual(full.get(timeout=0)["n"], 3)
        self.svc._hub.publish({"topic": "reader", "n": 0})
        self.assertIsNone(full.get(timeout=0) if full._queue.empty() else None)

    def test_refresh_reads_a_book_again_only_when_it_changed(self):
        self.opened()
        with mock.patch.object(service.reader, "load_book", wraps=service.reader.load_book) as load:
            self.svc.tick()
            self.assertTrue(self.svc.wait_for_refresh(LONG))
            self.assertNotIn(self.fake.books["pride"], [call.args[1] for call in load.call_args_list])
            load.reset_mock()
            sidecar = next(self.fake.local("pride").with_suffix(".sdr").glob("metadata.*.lua"))
            os.utime(sidecar, (time.time() + 9, time.time() + 9))
            self.unplug()
            self.svc.tick()
            self.replug()
            self.svc.tick()
            self.assertTrue(self.svc.wait_for_refresh(LONG))
            self.assertIn(self.fake.books["pride"], [call.args[1] for call in load.call_args_list])

    def test_a_save_finished_by_the_watcher_just_before_a_retry_is_reported(self):
        view = self.opened()
        card = self.svc.add_card(self.pride, self.first_hid(view), "qa")["card"]
        self.svc.update_card(card["id"], front="Why?", back="Because.")
        self.unplug()
        self.assertEqual(self.svc.save_book(self.pride)["status"], "waiting")
        self.replug()
        with mock.patch.object(self.svc, "_finish_saves", side_effect=lambda: (save.finish_pending(self.svc._store, self.mount), [])[1]):
            self.assertEqual(self.svc.save_retry(self.pride)["status"], "verified")

    def test_a_failed_read_keeps_the_highlights_already_here(self):
        view = self.opened()
        sidecar = self.fake.local("pride").with_suffix(".sdr") / "metadata.epub.lua"
        self.assertTrue(sidecar.exists())
        sidecar.write_text("not lua at all {{{", encoding="utf-8")
        again = self.svc.open_book(self.pride)
        self.assertEqual(len(again["highlights"]), len(view["highlights"]))
        self.assertTrue(again["error"])

    def test_a_missing_book_keeps_passages_and_chapters(self):
        view = self.opened()
        self.fake.local("pride").unlink()
        again = self.svc.open_book(self.pride)
        self.assertTrue(again["missing"])
        self.assertEqual(again["highlights"][0]["mark"], view["highlights"][0]["mark"])
        self.assertEqual(again["highlights"][0]["ch"], view["highlights"][0]["ch"])

    def test_highlight_removed_on_reader_stays_while_it_has_cards(self):
        view = self.opened()
        h = view["highlights"][0]
        store = self.svc._store
        card = self.svc.add_card(self.pride, h["hid"], "qa")["card"]
        self.svc.update_card(card["id"], front="Keep me?", back="Yes.")
        book = store.get_book(self.pride)
        record = dict(book["data"])
        record["highlights"] = record["highlights"][1:]
        merged = self.svc._merged_record(self.pride, record, book["data"])
        kept = next(row for row in merged["highlights"] if row["key"] == h["key"])
        self.assertTrue(kept["removed_on_reader"])


class Settings(Base):
    def test_settings_change_and_persist(self):
        self.drain()
        state = self.svc.update_settings({
            "model": "claude-opus-5-5", "meta_model": "claude-haiku-5-5",
            "companion_send": companion.OFF, "count": 3, "kind": "cloze", "sees": "The chapter",
            "suggest_connections": False, "suggest_ideas": False, "mark_connection_sources": False,
            "last_prompt": "Standard",
        })
        self.assertNotIn("auto_suggest", state["settings"])
        self.assertEqual(state["settings"]["model"], "claude-opus-5-5")
        self.assertEqual(self.next_event("changed", "settings")["topic"], "settings")
        self.svc.stop(wait=LONG)
        self.assertEqual(self.make_service().state()["settings"]["companion_send"], companion.OFF)

    def test_at_least_one_assistant_stays_on_and_models_follow(self):
        self.assert_refused(400, lambda: self.svc.update_settings({"assistants": {"claude": False, "codex": False}}), "At least one")
        state = self.svc.update_settings({"assistants": {"claude": False}})
        self.assertEqual(state["settings"]["assistants"], {"claude": False, "codex": True})
        self.assertEqual(state["settings"]["model"], "gpt-6.1-sol")
        self.assertEqual(state["settings"]["meta_model"], "gpt-6.1-sol")
        self.assertEqual({m["a"] for m in state["models"]}, {"codex"})
        self.assert_refused(400, lambda: self.svc.update_settings({"assistants": {"codex": False}}), "At least one")
        self.assert_refused(409, lambda: self.svc.update_settings({"model": "claude-opus-5-5"}), "turned off")

    def test_bad_settings_are_refused(self):
        for bad in (
            {"mystery": 1}, {"mount": ""}, {"mount": "relative/path"}, {"mount": 3}, {"auto_suggest": True}, {"suggest_ideas": "yes"},
            {"model": "nope"}, {"companion_send": "x"}, {"sees": "x"}, {"kind": "x"}, {"count": "x"},
            {"assistants": "x"}, {"assistants": {"gemini": True}}, {"assistants": {"claude": 1}},
        ):
            self.assert_refused(400, lambda: self.svc.update_settings(bad))
        self.assert_refused(404, lambda: self.svc.update_settings({"last_prompt": "../x"}))
        self.assert_refused(400, lambda: self.svc.update_settings("x"))

    def test_changing_the_mount_changes_where_the_reader_is_looked_for(self):
        self.svc.tick()
        self.drain()
        state = self.svc.update_settings({"mount": str(self.tmp / "elsewhere")})
        self.assertFalse(state["reader"]["connected"])
        self.svc.tick()
        self.assertFalse(self.next_event("changed", "reader")["connected"])
        self.assertTrue(self.svc.update_settings({"mount": "~/"})["settings"]["mount"].startswith("/"))

    def test_choices_are_remembered_after_a_suggest(self):
        hid = self.first_hid(self.opened())
        self.svc.suggest(self.pride, hid, {"count": 3, "sees": "The chapter", "companion": companion.OFF, "prompt": "Standard"})
        self.wait_jobs()
        settings = self.svc.state()["settings"]
        self.assertEqual(settings["sees"], "The chapter")
        self.assertEqual(settings["companion_send"], companion.OFF)
        self.assertEqual(settings["last_prompt"], "Standard")
        self.assertEqual(self.svc.book(self.pride)["options"]["count"], 3)


class Prompts(Base):
    def test_prompt_lists_files_and_text(self):
        result = self.svc.prompt("Standard")
        json.dumps(result, allow_nan=False)
        self.assertEqual(result["name"], "Standard")
        self.assertIn("questions.md", [row["file"] for row in result["files"]])
        self.assertTrue(all(row["text"] for row in result["files"]))
        self.assertTrue(all(row["undone_on_start"] for row in result["files"]))
        self.assertIsInstance(result["reloadAgo"], int)

    def test_a_folder_of_ones_own_shows_its_own_file_and_the_standard_ones(self):
        folder = paths.prompts_dir() / "Mine"
        folder.mkdir()
        (folder / "questions.md").write_text("Mine\n", encoding="utf-8")
        result = self.svc.prompt("Mine")
        rows = {row["file"]: row for row in result["files"]}
        self.assertEqual(rows["questions.md"]["text"].strip(), "Mine")
        self.assertFalse(rows["questions.md"]["undone_on_start"])
        self.assertFalse(rows["cloze.md"]["undone_on_start"])

    def test_open_and_reveal_use_the_replaceable_command(self):
        opened = self.svc.open_prompt("Standard", "cloze.md")
        self.assertEqual(self.launched[-1], ["open", "-t", opened["opened"]])
        revealed = self.svc.reveal_prompt("Standard", "questions")
        self.assertEqual(self.launched[-1][:2], ["open", "-R"])
        self.assertTrue(revealed["opened"].endswith("questions.md"))

    def test_names_cannot_escape_the_folder(self):
        for name, file_name in (("../..", "questions.md"), ("Standard", "../../secret"), ("/etc", "passwd"),
                                ("Standard", "/etc/passwd"), (None, None), ("Standard/..", "cloze.md")):
            before = len(self.launched)
            self.assert_refused(404, lambda: self.svc.open_prompt(name, file_name))
            self.assert_refused(404, lambda: self.svc.reveal_prompt(name, file_name))
            self.assertEqual(len(self.launched), before)
        self.assert_refused(404, lambda: self.svc.prompt("../.."))

    def test_a_failed_launch_says_so(self):
        svc = self.make_service(launch=lambda _args: False)
        self.assert_refused(500, lambda: svc.open_prompt("Standard", "cloze.md"), "couldn't be opened")

    def test_default_launch_runs_the_test_command(self):
        self.assertTrue(service._open_command(["open", "-t", "/nowhere"]))
        with mock.patch.dict(os.environ, {"PBANKI_OPEN_CMD": "/nonexistent/command"}):
            self.assertFalse(service._open_command(["open", "-t", "/nowhere"]))


class Companions(Base):
    def setUp(self):
        super().setUp()
        self.view = self.opened()
        self.device_path = self.fake.books["pride"]
        self.output = companion._work_output(self.device_path)

    def finish_output(self):
        self.output.write_text(HEADINGS_COMPANION, encoding="utf-8")
        past = time.time() - 60
        os.utime(self.output, (past, past))

    def test_companion_view_when_there_is_none_and_when_there_is(self):
        none = self.view["companion"]
        self.assertTrue(none["none"])
        self.assertEqual(none["status"], "None yet")
        self.assertEqual(none["dest"], f"Beside the book: {companion.file_name(self.device_path)}")
        self.assertIn("claude", none["command"])
        self.assertTrue(none["can_create"])
        have = self.svc.open_book(self.mostly)["companion"]
        self.assertTrue(have["have"])
        self.assertEqual(have["status"], "Book Companion on the reader")

    def test_never_created_automatically(self):
        with mock.patch.object(companion, "create") as create:
            self.svc.open_book(self.pride)
            self.svc.run(self.pride, "start")
            self.wait_run(self.pride)
            self.svc.tick()
        create.assert_not_called()

    def test_create_then_copy_to_the_reader_is_a_proven_write(self):
        before = self.fake.snapshot()
        self.drain()
        created = self.svc.companion_create(self.pride)["companion"]
        self.assertTrue(created["running"])
        self.assertEqual(created["status"], "Being created")
        self.assertEqual(self.next_event("companion", f"book:{self.pride}")["stage"], "running")
        self.assertEqual(self.svc.books()["books"][0]["comp"], "Creating")
        self.assert_refused(409, lambda: self.svc.companion_copy(self.pride), "isn't ready")
        self.assert_refused(409, lambda: self.svc.companion_keep(self.pride), "isn't ready")

        self.finish_output()
        self.svc.tick()
        self.assertEqual(self.next_event("companion", f"book:{self.pride}")["stage"], "found")
        ready = self.svc.book(self.pride)["companion"]
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["busyText"], "Your Book Companion is ready to copy to the reader.")

        with mock.patch.object(device, "write_files", wraps=device.write_files) as writes:
            copied = self.svc.companion_copy(self.pride)["companion"]
        writes.assert_called_once()
        self.assertTrue(copied["have"])
        self.assertEqual(copied["status"], "Book Companion on the reader")
        self.assertEqual(self.next_event("companion", f"book:{self.pride}")["stage"], "have")
        after = self.fake.snapshot()
        changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
        self.assertEqual(changed, {str(Path(self.device_path[len("/mnt/ext1/"):]).with_name(companion.file_name(self.device_path)))})
        self.assert_refused(409, lambda: self.svc.companion_create(self.pride), "already has")

    def test_keep_stays_on_this_computer_and_works_unplugged(self):
        self.svc.companion_create(self.pride)
        self.finish_output()
        self.unplug()
        self.assert_refused(409, lambda: self.svc.companion_copy(self.pride), "Plug in")
        kept = self.svc.companion_keep(self.pride)["companion"]
        self.assertTrue(kept["have"])
        self.assertEqual(kept["status"], "Book Companion on this computer")
        self.replug()

    def test_a_created_companion_is_tracked_across_a_restart(self):
        self.svc.companion_create(self.pride)
        self.svc.stop(wait=LONG)
        again = self.make_service()
        listener = again.subscribe({f"book:{self.pride}"})
        self.addCleanup(listener.close)
        again.start()
        self.finish_output()
        deadline = time.monotonic() + LONG
        again.tick()
        while time.monotonic() < deadline:
            event = listener.get(timeout=0.2)
            if event and event["type"] == "companion":
                self.assertEqual(event["stage"], "found")
                return
            again.tick()
        self.fail("no companion event")

    def test_create_says_why_it_cannot(self):
        shutil.rmtree(self.assistant)
        self.assert_refused(409, lambda: self.svc.companion_create(self.pride), "assistant folder")
        self.assertFalse(self.svc.book(self.pride)["companion"]["can_create"])


class Restart(Base):
    def test_restart_loses_nothing(self):
        view = self.opened()
        hid = self.first_hid(view)
        store = self.svc._store
        card = self.svc.add_card(self.pride, hid, "qa")["card"]
        self.svc.update_card(card["id"], front="Kept?", back="Kept.")
        removed = self.svc.add_card(self.pride, hid, "qa")["card"]
        self.svc.update_card(removed["id"], front="Gone?", back="Gone.")
        self.svc.remove_card(removed["id"])
        self.svc.skip_highlight(self.pride, self.first_hid(view, 1), True)
        self.svc.open_highlight(self.pride, hid)
        self.svc.update_settings({"count": 4})
        self.svc.define(self.pride, self.first_hid(view, 2), "manners")
        self.wait_jobs()
        self.svc.find_connections(self.pride, {})
        self.wait_jobs()
        connection = self.svc.book(self.pride)["connections"][0]
        self.svc.open_connection(connection["id"])
        idea = store.add_idea(self.pride, "Chapter 1", "T", "N", "Q", "F?", "B")
        before = self.svc.book(self.pride)
        self.svc.stop(wait=LONG)

        again = self.make_service()
        after = again.book(self.pride)
        self.assertEqual(json.dumps(after["highlights"], sort_keys=True), json.dumps(before["highlights"], sort_keys=True))
        self.assertEqual(after["connections"], before["connections"])
        self.assertEqual(after["ideas"], before["ideas"])
        self.assertEqual(after["counts"], before["counts"])
        self.assertEqual(again.state()["settings"]["count"], 4)
        self.assertEqual(again.state()["current_book"], self.pride)
        self.assertEqual(again._store.get_card(removed["id"])["state"], "removed")
        self.assertEqual(after["ideas"][0]["id"], idea["id"])
        self.unplug()
        self.assertEqual(len(again.book(self.pride)["highlights"]), 7)

    def test_stop_cancels_jobs_and_refuses_new_ones(self):
        view = self.opened()
        gate = self.ai.hold(view["highlights"][0]["text"])
        self.svc.suggest(self.pride, self.first_hid(view), {})
        self.assertTrue(wait_until(lambda: self.ai.calls, LONG))
        self.svc.stop(wait=LONG)
        gate.set()
        self.assertFalse(self.svc._jobs)
        self.assert_refused(503, lambda: self.svc.suggest(self.pride, self.first_hid(view), {}), "closing")

    def test_reader_only_changes_in_the_allowed_paths_with_all_reads(self):
        before = self.fake.snapshot()
        self.svc.tick()
        self.assertTrue(self.svc.wait_for_refresh(LONG))
        self.svc.state()
        self.svc.books()
        for book_id in (self.pride, self.pending, self.mostly):
            self.svc.open_book(book_id)
            self.svc.save_preview(book_id)
        self.assertEqual(self.fake.snapshot(), before)


class Errors(Base):
    def test_database_failure_is_a_plain_500(self):
        import sqlite3

        with mock.patch.object(self.svc._store, "get_book", side_effect=sqlite3.OperationalError("disk I/O error")):
            self.assert_refused(500, lambda: self.svc.book(self.pride), "may not have been stored")

    def test_a_forbidden_write_is_a_plain_500(self):
        with mock.patch.object(save, "plugin_update", side_effect=device.ForbiddenPath("/somewhere")):
            self.assert_refused(500, self.svc.reader_plugin_update, "safe on this computer")

    def test_hid_matches_the_store(self):
        view = self.opened()
        self.assertEqual(view["highlights"][0]["hid"], hid_for(view["highlights"][0]["key"]))

    def test_open_command_override_does_not_run_real_open(self):
        with mock.patch.object(service.subprocess, "run") as run:
            run.return_value.returncode = 0
            service._open_command(["open", "-R", "/x"])
        self.assertEqual(run.call_args.args[0], ["/usr/bin/true", "-R", "/x"])

    def test_prompts_module_still_refuses_unknown_names(self):
        with self.assertRaises(prompts.PromptNotFound):
            prompts.check_name("../..")


class Findings(Base):
    """Defects independent checkers found, each reproduced before its fix."""

    def sidecar(self, name):
        return self.fake.local(name).with_suffix(".sdr") / "metadata.epub.lua"

    def drop_annotation(self, name, key):
        path = self.sidecar(name)
        data = lua.loads(path.read_text("utf-8"))
        data["annotations"] = [a for a in lua.sequence(data["annotations"]) if reader.highlight_key(a) != key]
        path.write_text(lua_file("x", data), "utf-8")

    def write_card(self, book_id, hid, front="Why this?", back="Because."):
        card = self.svc.add_card(book_id, hid, "qa")["card"]
        return self.svc.update_card(card["id"], front=front, back=back)["card"]

    def pending_hids(self, book_id):
        view = self.opened(book_id)
        return [h["hid"] for h in view["highlights"] if h["status"] != "reviewed"]

    def inbox_lines(self):
        path = self.mount / paths.INBOX_FILE
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()] if path.exists() else []

    # Key idea chapter names

    def test_a_key_idea_shows_its_chapter_the_way_cards_do(self):
        self.ai.reply("ideas", json.dumps({"chapters": [], "ideas": [
            {"title": "Idea A", "chapter": "The daily miracle", "note": "Why.", "quote": "", "front": "QA?", "back": "A."},
        ]}))
        self.opened(self.mostly)
        self.svc.run(self.mostly, "start", {"connections": False, "ideas": True})
        self.wait_run(self.mostly)
        view = self.svc.book(self.mostly)
        idea = view["ideas"][0]
        self.assertEqual(idea["chapter"], "Chapter 1: The daily miracle")
        self.assertEqual(idea["meta"], "Chapter 1: The daily miracle · Not opened")
        self.svc.open_idea(idea["id"])
        group = next(g for g in self.svc.save_preview(self.mostly)["groups"] if g["type"] == "idea")
        self.assertEqual(group["where"], "Chapter 1: The daily miracle")
        self.assertEqual(self.svc.save_book(self.mostly)["status"], "verified")
        line = next(item for item in self.inbox_lines() if item["front"] == "QA?")
        self.assertEqual(line["chapter"], "Chapter 1: The daily miracle")

    # The save screen while a save waits

    def test_a_waiting_save_keeps_showing_its_cards(self):
        hid = self.pending_hids(self.pending)[0]
        self.write_card(self.pending, hid)
        before = self.svc.save_preview(self.pending)
        self.assertEqual(before["heldCount"], 0)
        self.unplug()
        self.assertEqual(self.svc.save_book(self.pending)["status"], "waiting")
        after = self.svc.save_preview(self.pending)
        self.assertTrue(after["saveWaiting"])
        for field in ("saveCount", "saveNoun", "markText", "groups", "count", "noun", "mark_count"):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after["steps"][2]["title"], before["steps"][2]["title"])
        self.assertEqual(after["heldCount"], 1)
        self.assertTrue(after["saveBlocked"])
        small = self.svc.book(self.pending)["save"]
        self.assertEqual((small["count"], small["saveLabel"], small["noCards"], small["waiting"]),
                         (1, "Save 1 card", False, True))
        self.replug()
        self.svc._finish_saves()
        done = self.svc.save_preview(self.pending)
        self.assertEqual((done["saveCount"], done["groups"], done["heldCount"]), (0, [], 0))

    def test_a_failed_save_keeps_showing_its_cards_and_new_cards_add_up(self):
        first, second = self.pending_hids(self.pending)[:2]
        self.write_card(self.pending, first, front="First?")
        with mock.patch.object(device, "write_files", side_effect=device.DeviceWriteError("It failed.")):
            self.assertEqual(self.svc.save_book(self.pending)["status"], "failed")
        failed = self.svc.save_preview(self.pending)
        self.assertEqual(failed["failed"][0]["error"], "It failed.")
        self.assertEqual((failed["saveCount"], failed["markText"]), (1, "Mark 1 highlight as reviewed"))
        self.assertEqual(self.svc.book(self.pending)["save"]["count"], 1)
        self.write_card(self.pending, second, front="Second?")
        both = self.svc.save_preview(self.pending)
        self.assertEqual((both["saveCount"], both["heldCount"], both["markText"]), (2, 1, "Mark 2 highlights as reviewed"))
        self.assertEqual([g["rows"][0]["front"] for g in both["groups"]], ["First?", "Second?"])
        self.assertFalse(both["saveBlocked"])

    # Resuming work

    def test_resume_counts_an_opened_connection_and_key_idea(self):
        hids = self.pending_hids(self.pride)
        item = self.svc._store.add_connection(self.pride, "T", "F?", "B", hids[:2])
        self.assertIsNone(self.svc.state()["resume"])
        self.svc.open_connection(item["id"])
        resume = self.svc.state()["resume"]
        self.assertEqual((resume["book_id"], resume["count"]), (self.pride, 1))
        self.assertEqual(resume["text"], "1 unsaved card is safe on this computer")
        self.unplug()
        self.assertEqual(self.svc.state()["resume"]["count"], 1)
        self.replug()
        idea = self.svc._store.add_idea(self.pride, "Chapter", "T", "", "", "I?", "B")
        self.svc.open_idea(idea["id"])
        self.assertEqual(self.svc.state()["resume"]["count"], 2)

    def test_resume_still_counts_suggestions_not_opened_yet(self):
        hid = self.pending_hids(self.pride)[0]
        self.svc._store.add_card(self.pride, hid, "qa", "Q?", "A", suggested={"front": "Q?", "back": "A"})
        self.assertEqual(self.svc.state()["resume"]["count"], 1)

    # Cards that no save can include

    def test_a_card_on_a_skipped_highlight_is_named_then_retired(self):
        first, second = self.pending_hids(self.pending)[:2]
        typed = self.write_card(self.pending, first, front="Typed then skipped")
        self.svc.skip_highlight(self.pending, first, True)
        self.assertIsNone(self.svc.state()["resume"])
        self.write_card(self.pending, second)
        preview = self.svc.save_preview(self.pending)
        self.assertEqual(preview["saveCount"], 1)
        self.assertIn("1 card on a skipped highlight will not be saved. Undo the skip to keep it.", preview["leftText"])
        self.assertEqual(self.svc.save_book(self.pending)["status"], "verified")
        self.assertEqual(self.svc._store.get_card(typed["id"])["state"], "removed")
        self.assertIsNone(self.svc.state()["resume"])
        self.assertEqual(self.svc.book(self.pending)["counts"]["cards"], 0)
        self.assertFalse([line for line in self.inbox_lines() if line["front"] == "Typed then skipped"])

    def test_undoing_a_skip_brings_the_card_back_into_the_save(self):
        hid = self.pending_hids(self.pending)[0]
        self.write_card(self.pending, hid)
        self.svc.skip_highlight(self.pending, hid, True)
        self.svc.skip_highlight(self.pending, hid, False)
        preview = self.svc.save_preview(self.pending)
        self.assertEqual(preview["saveCount"], 1)
        self.assertNotIn("skipped", preview["leftText"])

    def test_a_suggestion_finishing_after_the_save_adds_nothing(self):
        hid = self.pending_hids(self.pride)[0]
        self.write_card(self.pride, hid)
        self.ai.gate.clear()
        self.svc.suggest(self.pride, hid, {"count": 1, "kind": "qa"})
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 1, LONG))
        self.assertEqual(self.svc.save_book(self.pride)["status"], "verified")
        self.ai.gate.set()
        self.wait_jobs()
        self.assertEqual(self.svc._store.list_cards(self.pride, hid), [])
        self.assertIsNone(self.svc.state()["resume"])

    def test_a_definition_finishing_after_the_save_adds_nothing(self):
        hid = self.pending_hids(self.pride)[0]
        self.write_card(self.pride, hid)
        self.ai.gate.clear()
        self.svc.define(self.pride, hid, "manners")
        self.assertTrue(wait_until(lambda: self.ai.in_flight == 1, LONG))
        self.assertEqual(self.svc.save_book(self.pride)["status"], "verified")
        self.ai.gate.set()
        self.wait_jobs()
        self.assertEqual(self.svc._store.list_cards(self.pride, hid), [])

    def test_a_highlight_deleted_during_a_run_gets_no_hidden_card(self):
        self.opened(self.pride)
        self.ai.gate.clear()
        self.svc.run(self.pride, "start", {"connections": False, "ideas": False, "count": 1})
        self.assertTrue(wait_until(lambda: self.svc.runner.status(self.pride)["progress"].get("writing"), LONG))
        writing = self.svc.runner.status(self.pride)["progress"]["writing"][0]
        view = self.svc.book(self.pride)
        victim = next(h for h in view["highlights"] if h["hid"] == writing)
        self.drop_annotation("pride", victim["key"])
        view = self.svc.open_book(self.pride)
        self.assertNotIn(writing, [h["hid"] for h in view["highlights"]])
        self.ai.gate.set()
        self.wait_run(self.pride)
        view = self.svc.book(self.pride)
        visible = {h["hid"] for h in view["highlights"]}
        self.assertEqual([c["front"] for c in self.svc._store.list_cards(self.pride) if c["hid"] not in visible], [])
        cards = sum(len(h["cards"]) for h in view["highlights"])
        self.assertEqual(view["counts"]["cards"], cards)
        self.assertEqual(self.svc.state()["resume"]["count"], cards)

    def test_undo_is_refused_cleanly_when_the_highlight_has_left_the_reader(self):
        view = self.opened(self.pride)
        h = view["highlights"][2]
        card = self.write_card(self.pride, h["hid"])
        self.svc.remove_card(card["id"])
        self.drop_annotation("pride", h["key"])
        self.svc.open_book(self.pride)
        self.assert_refused(409, lambda: self.svc.restore_card(card["id"]), "isn't on the reader any more")
        self.assertEqual(self.svc._store.get_card(card["id"])["state"], "removed")
        self.assertIsNone(self.svc.state()["resume"])
        self.assertEqual(self.svc.book(self.pride)["counts"]["cards"], 0)

    # Refreshing the cache

    def test_a_connection_keeps_its_sources_when_the_sidecar_reads_as_empty(self):
        hids = self.pending_hids(self.pending)[:2]
        texts = [h["text"] for h in self.svc.book(self.pending)["highlights"] if h["hid"] in hids]
        item = self.svc._store.add_connection(self.pending, "T", "F?", "B", hids)
        self.svc.open_connection(item["id"])
        self.sidecar("pending").write_bytes(b"")
        view = self.svc.open_book(self.pending)
        self.assertEqual([h["hid"] for h in view["highlights"]], hids)
        self.assertTrue(all(h["removed_on_reader"] for h in view["highlights"]))
        self.assertEqual(len(view["connections"][0]["sources"]), 2)
        self.assertEqual(self.svc.save_book(self.pending)["status"], "verified")
        line = next(item for item in self.inbox_lines() if item["front"] == "F?")
        self.assertTrue(all(text in line["quote"] for text in texts))
        self.assertTrue(line["chapter"])

    def test_an_unreadable_sidecar_keeps_the_title_and_deck(self):
        path = self.sidecar("pending")
        data = lua.loads(path.read_text("utf-8"))
        data.setdefault("doc_props", {})["title"] = "My Edited Title"
        path.write_text(lua_file("x", data), "utf-8")
        view = self.opened(self.pending)
        self.assertEqual((view["title"], view["deck"]), ("My Edited Title", "KOReader::My Edited Title"))
        path.write_text("return {{{ bad", "utf-8")
        view = self.svc.open_book(self.pending)
        self.assertTrue(view["error"])
        self.assertEqual((view["title"], view["deck"]), ("My Edited Title", "KOReader::My Edited Title"))
        self.svc.books()
        self.assertEqual(self.svc.book(self.pending)["title"], "My Edited Title")
        hid = next(h["hid"] for h in view["highlights"] if h["status"] != "reviewed")
        self.write_card(self.pending, hid, front="Deck check?")
        self.assertEqual(self.svc.save_book(self.pending)["status"], "verified")
        line = next(item for item in self.inbox_lines() if item["front"] == "Deck check?")
        self.assertEqual(line["book"], "My Edited Title")

    def test_an_unreadable_reviewed_file_changes_nothing(self):
        hid = self.pending_hids(self.pride)[0]
        self.write_card(self.pride, hid)
        self.assertEqual(self.svc.save_book(self.pride)["status"], "verified")
        path = self.mount / paths.REVIEWED_FILE
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o644)
        self.svc.save_preview(self.pride)
        h = next(row for row in self.svc.book(self.pride)["highlights"] if row["hid"] == hid)
        self.assertEqual(h["status"], "reviewed")
        runner = self.svc.runner
        self.assertFalse(runner._due(self.pride, {**h}, runner._reviewed(self.pride)))

    # Run rows

    def test_the_first_run_row_tells_the_truth_about_failures(self):
        self.ai.reply("highlight", "This is not JSON.")
        self.opened(self.pride)
        self.svc.run(self.pride, "start", {"connections": False, "ideas": False})
        self.wait_run(self.pride)
        row = self.svc.book(self.pride)["run"]["rows"][0]
        self.assertEqual((row["path"], row["ok"], row["glyph"], row["cls"]),
                         ("7 of 7 highlights failed", False, "✕", "chk bad"))

    def test_the_first_run_row_counts_a_partial_failure(self):
        view = self.opened(self.pride)
        self.ai.reply(view["highlights"][0]["text"], "This is not JSON.")
        self.svc.run(self.pride, "start", {"connections": False, "ideas": False})
        self.wait_run(self.pride)
        row = self.svc.book(self.pride)["run"]["rows"][0]
        self.assertEqual((row["path"], row["ok"], row["glyph"]), ("1 of 7 highlights failed", False, "✕"))

    def test_the_first_run_row_says_done_when_all_went_well(self):
        self.opened(self.pride)
        self.svc.run(self.pride, "start", {"connections": False, "ideas": False})
        self.wait_run(self.pride)
        row = self.svc.book(self.pride)["run"]["rows"][0]
        self.assertEqual((row["path"], row["ok"], row["glyph"]), ("Done · 7 of 7 highlights", True, "✓"))

    # Database trouble

    def test_a_database_failure_does_not_claim_the_change_is_safe(self):
        hid = self.pending_hids(self.pride)[0]
        card = self.write_card(self.pride, hid)
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(self.svc._store, "update_card", side_effect=locked):
            with self.assertRaises(ServiceError) as caught:
                self.svc.update_card(card["id"], front="Held")
        self.assertEqual(caught.exception.status, 500)
        self.assertNotIn("safe", caught.exception.sentence)
        self.assertIn("may not have been stored", caught.exception.sentence)

    # The plugin check

    def plugin_row(self):
        return self.svc.state()["reader"]["rows"][-1]

    def test_a_cut_off_plugin_update_is_reported_and_can_be_repaired(self):
        target = self.mount / paths.PLUGIN_DIR / "main.lua"
        whole = (paths.repo_root() / "koreader" / "pbanki.koplugin" / "main.lua").read_bytes()
        target.write_bytes(b"-- old, before desktop saves\n")
        real_flush = device._flush_to_disk

        def cut_off(path, data):
            real_flush(path, data[:len(data) // 2] if path.name == "main.lua" else data)
            if path.name == "main.lua":
                raise OSError(5, "Input/output error")

        with mock.patch.object(device, "_flush_to_disk", cut_off):
            self.assert_refused(503, self.svc.reader_plugin_update)
        self.assertIn(b"reviewed.jsonl", target.read_bytes())
        row = self.plugin_row()
        self.assertEqual((row["ok"], row["level"], row["action"]), (False, "warning", "update_plugin"))
        self.assertIn("didn't finish", row["detail"])
        self.svc.reader_plugin_update()
        self.assertEqual(target.read_bytes(), whole)
        self.assertEqual(self.plugin_row()["level"], "ok")

    def test_a_cut_off_update_survives_a_restart(self):
        (self.mount / paths.PLUGIN_DIR / "main.lua").write_bytes(b"-- reads reviewed.jsonl, an older version\n")
        with mock.patch.object(device, "_flush_to_disk", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.svc.reader_plugin_update()
        self.assertEqual(self.make_service().state()["reader"]["rows"][-1]["level"], "warning")


class CompanionFindings(Base):
    def setUp(self):
        super().setUp()
        self.opened()
        self.device_path = self.fake.books["pride"]
        self.output = companion._work_output(self.device_path)

    def finish_output(self):
        self.output.write_text(HEADINGS_COMPANION, encoding="utf-8")
        past = time.time() - 60
        os.utime(self.output, (past, past))

    def launches(self):
        return mock.patch.object(companion, "_launch_terminal", wraps=companion._launch_terminal)

    def test_create_is_refused_while_one_is_running(self):
        with self.launches() as launch:
            self.svc.companion_create(self.pride)
            self.assert_refused(409, lambda: self.svc.companion_create(self.pride), "already being created")
        self.assertEqual(launch.call_count, 1)

    def test_create_is_refused_when_a_finished_file_waits_and_leaves_it_there(self):
        self.svc.companion_create(self.pride)
        self.finish_output()
        with self.launches() as launch:
            self.assert_refused(409, lambda: self.svc.companion_create(self.pride), "ready")
        self.assertEqual(launch.call_count, 0)
        self.assertEqual(self.output.read_text("utf-8"), HEADINGS_COMPANION)

    def test_simultaneous_creates_open_one_terminal(self):
        answers = []

        def click():
            try:
                self.svc.companion_create(self.pride)
                answers.append(200)
            except ServiceError as e:
                answers.append(e.status)
            finally:
                self.svc.release_thread()

        with self.launches() as launch:
            threads = [threading.Thread(target=click) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(LONG)
        self.assertEqual(sorted(answers), [200, 409, 409, 409, 409])
        self.assertEqual(launch.call_count, 1)

    def test_create_can_run_again_once_its_terminal_session_has_ended(self):
        self.svc.companion_create(self.pride)
        (companion.work_dir(self.device_path) / companion._SCRIPT_NAME).unlink()
        self.assertEqual(self.svc.companion_create(self.pride)["companion"]["stage"], "running")

    def cut_off_copy(self):
        real_flush = device._flush_to_disk

        def flush(path, data):
            real_flush(path, data[:20])
            raise OSError(5, "Input/output error")

        with mock.patch.object(device, "_flush_to_disk", flush):
            self.assert_refused(503, lambda: self.svc.companion_copy(self.pride), "Input/output error")

    def test_a_cut_off_copy_is_not_used_and_copy_again_repairs_it(self):
        self.svc.companion_create(self.pride)
        self.finish_output()
        self.cut_off_copy()
        self.assertTrue(self.fake.local("pride").with_name(companion.file_name(self.device_path)).exists())
        book = self.svc._store.get_book(self.pride)
        self.assertIsNone(self.svc.runner._companion_text(book))
        view = self.svc.book(self.pride)["companion"]
        self.assertEqual(view["stage"], "found")
        self.assertTrue(view["cutOff"])
        done = self.svc.companion_copy(self.pride)["companion"]
        self.assertEqual((done["have"], done["where"], done["cutOff"]), (True, "reader", False))
        self.assertEqual(self.svc.runner._companion_text(book), HEADINGS_COMPANION)

    def test_keep_after_a_cut_off_copy_uses_the_whole_file_and_copy_still_repairs(self):
        self.svc.companion_create(self.pride)
        self.finish_output()
        self.cut_off_copy()
        kept = self.svc.companion_keep(self.pride)["companion"]
        self.assertEqual((kept["have"], kept["where"], kept["cutOff"]), (True, "computer", True))
        book = self.svc._store.get_book(self.pride)
        self.assertEqual(self.svc.runner._companion_text(book), HEADINGS_COMPANION)
        done = self.svc.companion_copy(self.pride)["companion"]
        self.assertEqual((done["where"], done["cutOff"]), ("reader", False))
        target = self.fake.local("pride").with_name(companion.file_name(self.device_path))
        self.assertEqual(target.read_text("utf-8"), HEADINGS_COMPANION)


class PromptFindings(Base):
    def setUp(self):
        super().setUp()
        self.folder = paths.prompts_dir() / "Concise"
        self.folder.mkdir()
        (self.folder / "questions.md").write_text("Mine\n", encoding="utf-8")
        (self.folder / "cloze.md").write_text("  \n", encoding="utf-8")

    def test_a_standard_file_in_another_folder_shows_that_folders_path(self):
        rows = {row["file"]: row for row in self.svc.prompt("Concise")["files"]}
        self.assertEqual(rows["cloze.md"]["status"], "uses Standard")
        self.assertTrue(rows["cloze.md"]["display"].endswith("/prompts/Concise/cloze.md"))
        self.assertFalse(rows["cloze.md"]["undone_on_start"])
        listed = next(f for f in self.svc.state()["prompts"]["folders"] if f["name"] == "Concise")
        row = next(r for r in listed["files"] if r["file"] == "cloze.md")
        self.assertTrue(row["display"].endswith("/prompts/Concise/cloze.md"))

    def test_opening_it_gives_the_folder_its_own_copy_and_edits_stay(self):
        standard = (paths.prompts_dir() / "Standard" / "cloze.md").read_bytes()
        opened = self.svc.open_prompt("Concise", "cloze.md")
        own = self.folder / "cloze.md"
        self.assertEqual(self.launched[-1], ["open", "-t", str(own)])
        self.assertEqual((opened["opened"], opened["status"]), (str(own), "this prompt"))
        self.assertEqual(own.read_bytes(), standard)
        own.write_text("Edited\n", encoding="utf-8")
        prompts.seed()
        self.assertEqual(own.read_text("utf-8"), "Edited\n")
        self.assertEqual(prompts.effective("Concise")["cloze"], "Edited")
        self.assertEqual((paths.prompts_dir() / "Standard" / "cloze.md").read_bytes(), standard)

    def test_opening_a_file_of_its_own_leaves_it_alone(self):
        self.svc.open_prompt("Concise", "questions.md")
        self.assertEqual((self.folder / "questions.md").read_text("utf-8"), "Mine\n")

    def test_reveal_changes_nothing_and_shows_the_folder_when_the_file_is_missing(self):
        revealed = self.svc.reveal_prompt("Concise", "connections.md")
        self.assertEqual(self.launched[-1], ["open", "-R", str(self.folder)])
        self.assertEqual(revealed["status"], "uses Standard")
        self.assertFalse((self.folder / "connections.md").exists())
        self.svc.reveal_prompt("Concise", "cloze.md")
        self.assertEqual(self.launched[-1], ["open", "-R", str(self.folder / "cloze.md")])
        self.assertEqual((self.folder / "cloze.md").read_text("utf-8"), "  \n")

    def test_standard_still_opens_its_own_file(self):
        opened = self.svc.open_prompt("Standard", "cloze.md")
        self.assertEqual(opened["opened"], str(paths.prompts_dir() / "Standard" / "cloze.md"))
        self.assertTrue(all(row["undone_on_start"] for row in self.svc.prompt("Standard")["files"]))


if __name__ == "__main__":
    unittest.main()


class RegressionFindings(Base):
    def all_reviewed(self, book_id):
        book = self.svc.open_book(book_id)
        self.svc._store.set_reader_reviewed(book_id, [h["key"] for h in book["highlights"]])

    def test_a_finished_book_can_start_key_ideas_alone(self):
        self.all_reviewed(self.mostly)
        book = self.svc.book(self.mostly)
        self.assertEqual(book["suggest"]["due"], 0)
        self.assertFalse(book["suggest"]["canConnect"])
        self.assertEqual(book["ideaEmptyText"], "pbAnki hasn't compared the Book Companion with your highlights yet.")
        self.assertTrue(book["ideaLook"]["show"])
        self.svc.run(self.mostly, "start", {"highlights": False, "connections": False, "ideas": True})
        self.wait_run(self.mostly)
        book = self.svc.book(self.mostly)
        self.assertEqual([call["kind"] for call in self.ai.calls], ["ideas"])
        self.assertEqual([row["title"] for row in book["run"]["rows"]], ["Key ideas you didn't highlight"])
        self.assertEqual(self.svc._store.get_settings()["suggest_connections"], True)

    def test_key_ideas_say_nothing_is_left_only_after_looking(self):
        book = self.svc.open_book(self.mostly)
        self.assertNotIn("No key ideas left", book["ideaEmptyText"])
        self.svc.run(self.mostly, "start", {"connections": False})
        self.wait_run(self.mostly)
        self.svc.run(self.mostly, "start", {"connections": False, "ideas": False})
        self.wait_run(self.mostly)
        self.assertIn("No key ideas left", self.svc.book(self.mostly)["ideaEmptyText"])

    def test_a_save_finishes_when_the_reader_was_gone_only_between_checks(self):
        self.svc.tick()
        book = self.svc.open_book(self.mostly)
        hid = next(h for h in book["highlights"] if h["canEdit"])["id"]
        self.svc.open_highlight(self.mostly, hid)
        card = self.svc.add_card(self.mostly, hid, "qa")["card"]
        self.svc.update_card(card["id"], front="Q?", back="A")
        self.unplug()
        self.assertEqual(self.svc.save_book(self.mostly)["status"], "waiting")
        self.replug()
        self.svc.tick()
        self.assertTrue(self.svc.save_preview(self.mostly)["last_saved"]["done"])

    def test_reading_further_is_noticed(self):
        self.svc.open_book(self.mostly)
        self.svc.run(self.mostly, "start", {"connections": False})
        self.wait_run(self.mostly)
        self.assertEqual(self.svc.book(self.mostly)["ideaLook"]["note"], "")
        record = self.svc._store.get_book(self.mostly)
        looked = runs.ideas_looked(self.svc.runner.status(self.mostly)["progress"])
        self.svc._store.upsert_book({**record, "data": {**record["data"], "percent_finished": looked + 0.2}})
        self.assertIn("read further", self.svc.book(self.mostly)["ideaLook"]["note"])

    def test_the_due_count_matches_what_a_run_asks_for(self):
        book = self.svc.open_book(self.pride)
        hids = [h["id"] for h in book["highlights"]]
        pending = book["counts"]["todo"]
        self.assertEqual(book["suggest"]["due"], pending)
        card = self.svc.add_card(self.pride, hids[0], "qa")["card"]
        self.svc.skip_highlight(self.pride, hids[1], True)
        rejected = self.svc._store.add_card(self.pride, hids[2], "qa", "F", "B", suggested={"front": "F", "back": "B"})
        self.svc.remove_card(rejected["id"])
        self.assertTrue(card["id"])
        book = self.svc.book(self.pride)
        self.assertEqual(book["suggest"]["due"], pending - 3)
        self.assertEqual(book["suggest"]["sugBtn"], f"Suggest cards for {pending - 3} highlights")
        self.assertIn("with none yet", book["suggest"]["formLede"])
        self.svc.run(self.pride, "start", {"connections": False, "ideas": False})
        self.wait_run(self.pride)
        self.assertEqual(self.svc.runner.status(self.pride)["progress"]["highlights"]["total"], pending - 3)

    def test_a_run_with_nothing_waiting_says_so(self):
        self.svc.open_book(self.pride)
        self.svc.run(self.pride, "start", {"connections": False, "ideas": False})
        self.wait_run(self.pride)
        self.svc.run(self.pride, "start", {"connections": False, "ideas": False})
        self.wait_run(self.pride)
        row = self.svc.book(self.pride)["run"]["rows"][0]
        self.assertEqual(row["path"], "No highlights were waiting for cards")
        self.assertTrue(row["ok"])

    def test_a_prompt_picked_for_a_book_beats_the_one_on_the_reader(self):
        record = self.svc._store.get_book(self.svc.open_book(self.mostly)["id"])
        self.svc._store.upsert_book({**record, "data": {**record["data"], "pbanki_prompt": "Self Help"}})
        self.assertEqual(self.svc.book(self.mostly)["options"]["prompt"], "Self Help")
        answer = self.svc.choose_prompt(self.mostly, "Standard")
        self.assertEqual(answer["options"]["prompt"], "Standard")
        self.assertEqual(self.svc.book(self.mostly)["options"]["prompt"], "Standard")
        self.assertEqual(self.svc.book(self.pride)["options"]["prompt"], "Standard")
        with self.assertRaises(ServiceError):
            self.svc.choose_prompt(self.mostly, "Nothing Called This")

    def test_a_picked_prompt_that_was_deleted_falls_back(self):
        record = self.svc._store.get_book(self.svc.open_book(self.mostly)["id"])
        self.svc._store.upsert_book({**record, "data": {**record["data"], "pbanki_prompt": "Standard"}})
        self.svc.choose_prompt(self.mostly, "Self Help")
        shutil.rmtree(paths.prompts_dir() / "Self Help")
        self.assertEqual(self.svc.book(self.mostly)["options"]["prompt"], "Standard")

    def test_models_move_off_an_assistant_that_isnt_installed(self):
        with mock.patch.dict(os.environ, {"PBANKI_CLAUDE_BIN": str(self.tmp / "missing")}):
            settings = self.svc.state()["settings"]
            self.assertEqual(settings["model"], "gpt-6.1-sol")
            self.assertEqual(settings["meta_model"], "gpt-6.1-sol")
            self.svc.open_book(self.pride)
            self.svc.run(self.pride, "start")
            self.wait_run(self.pride)

    def test_models_stay_when_no_assistant_is_there(self):
        before = self.svc._store.get_settings()["model"]
        with mock.patch.dict(os.environ, {"PBANKI_CLAUDE_BIN": str(self.tmp / "no"), "PBANKI_CODEX_BIN": str(self.tmp / "no")}):
            self.assertEqual(self.svc.state()["settings"]["model"], before)

    def test_create_on_a_book_gone_from_the_reader_gives_the_real_reason(self):
        gone = book_id_for(self.fake.books["gone"])
        view = self.svc.open_book(gone)["companion"]
        self.assertFalse(view["can_create"])
        self.assertEqual(view["problem"], companion.NOT_ON_READER)
        with self.assertRaises(ServiceError) as caught:
            self.svc.companion_create(gone)
        self.assertEqual(caught.exception.sentence, companion.NOT_ON_READER)

    def test_the_ai_note_shows_only_when_the_choice_cant_be_honoured(self):
        book = self.svc.open_book(self.pride)
        self.assertEqual(book["aiNote"], "")
        self.unplug()
        self.assertEqual(self.svc.book(self.pride)["aiNote"], "")
        copy = self.svc.runner._book_copy_path(self.pride)
        copy.unlink()
        self.assertIn("only the passage", self.svc.book(self.pride)["aiNote"])
        self.replug()

    def test_a_waiting_save_says_what_it_leaves_out(self):
        book = self.svc.open_book(self.pride)
        hid = book["highlights"][0]["id"]
        self.svc.open_highlight(self.pride, hid)
        card = self.svc.add_card(self.pride, hid, "qa")["card"]
        self.svc.update_card(card["id"], front="Q1", back="A1")
        self.unplug()
        self.svc.save_book(self.pride)
        self.assertEqual(self.svc.save_preview(self.pride)["heldText"], "")
        other = book["highlights"][1]["id"]
        self.svc.open_highlight(self.pride, other)
        card = self.svc.add_card(self.pride, other, "qa")["card"]
        self.svc.update_card(card["id"], front="Q2", back="A2")
        preview = self.svc.save_preview(self.pride)
        self.assertEqual(preview["saveCount"], 2)
        self.assertEqual(preview["freshCount"], 1)
        self.assertIn("Step 1 is done for 1 card.", preview["waitText"])
        self.assertEqual(preview["heldText"], "1 newer card isn't part of that save. Save again afterwards to send them.")
        self.replug()

    def test_need_link_goes_to_the_first_thing_lacking(self):
        book = self.svc.open_book(self.pride)
        hid = book["highlights"][0]["id"]
        self.svc.open_highlight(self.pride, hid)
        card = self.svc.add_card(self.pride, hid, "qa")["card"]
        self.svc.update_card(card["id"], front="Question?")
        preview = self.svc.save_preview(self.pride)
        self.assertEqual(preview["needLink"], {"screen": "highlights", "id": hid})
        self.assertEqual(preview["needCount"], 1)
        self.assertTrue(preview["saveBlocked"])
        self.assertTrue(self.svc.book(self.pride)["highlights"][0]["cards"][0]["need"])
