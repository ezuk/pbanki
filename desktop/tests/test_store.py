import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["PBANKI_SYNC_DELAY"] = "0"

from pbanki_desktop import paths, store as store_module
from pbanki_desktop.store import NotEditable, NotFound, Store, StoreError

BOOK = {"device_path": "/mnt/ext1/English/X.epub", "title": "X", "author": "Y"}
REPO = Path(__file__).resolve().parents[2]


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "deep" / "pbanki.db"
        self.store = Store(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.book_id = self.store.upsert_book(BOOK)
        self.hid = self.store.ensure_highlights(self.book_id, ["k1"])["k1"]


class SchemaTests(StoreCase):
    def test_a_new_database_is_at_the_latest_version_in_wal_mode(self):
        self.assertEqual(self.store.schema_version(), len(store_module.MIGRATIONS))
        mode = self.store._conn().execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")
        self.assertGreater(self.store._conn().execute("PRAGMA busy_timeout").fetchone()[0], 1000)

    def test_it_lives_at_the_app_home_by_default(self):
        with mock.patch.dict(os.environ, {"PBANKI_DESKTOP_HOME": self.tmp.name}):
            store = Store()
            self.assertEqual(store.path, Path(self.tmp.name) / "pbanki.db")
            store.close()

    def test_reopening_keeps_data_and_runs_no_migration_twice(self):
        card = self.store.add_card(self.book_id, self.hid, front="F")
        self.store.close()
        again = Store(self.path)
        self.addCleanup(again.close)
        self.assertEqual(again.get_card(card["id"])["front"], "F")
        self.assertEqual(again.schema_version(), len(store_module.MIGRATIONS))

    def test_a_later_migration_upgrades_in_place(self):
        card = self.store.add_card(self.book_id, self.hid, front="F")
        self.store.close()
        steps = store_module.MIGRATIONS + [("ALTER TABLE books ADD COLUMN extra TEXT DEFAULT 'x'",)]
        with mock.patch.object(store_module, "MIGRATIONS", steps):
            again = Store(self.path)
            self.addCleanup(again.close)
            self.assertEqual(again.schema_version(), len(store_module.MIGRATIONS))
            self.assertEqual(again.get_book(self.book_id)["extra"], "x")
            self.assertEqual(again.get_card(card["id"])["front"], "F")

    def test_a_failed_migration_changes_nothing(self):
        self.store.close()
        steps = store_module.MIGRATIONS + [("CREATE TABLE half(a)", "THIS IS NOT SQL")]
        with mock.patch.object(store_module, "MIGRATIONS", steps):
            with self.assertRaises(sqlite3.Error):
                Store(self.path)
        conn = sqlite3.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(store_module.MIGRATIONS))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'half'").fetchone()[0], 0)

    def test_a_database_from_a_newer_app_is_refused_in_plain_words(self):
        self.store._conn().execute("PRAGMA user_version=99")
        self.store.close()
        with self.assertRaisesRegex(StoreError, "newer version"):
            Store(self.path)


class BookTests(StoreCase):
    def test_ids_are_the_first_16_hex_digits_of_sha1(self):
        path = BOOK["device_path"]
        self.assertEqual(self.book_id, hashlib.sha1(path.encode()).hexdigest()[:16])
        self.assertEqual(self.hid, hashlib.sha1(b"k1").hexdigest()[:16])

    def test_stem_and_extension_come_from_the_path(self):
        book = self.store.get_book(self.book_id)
        self.assertEqual((book["stem"], book["ext"]), ("X", "epub"))

    def test_a_refresh_without_data_keeps_the_cached_highlights(self):
        self.store.upsert_book({**BOOK, "data": {"highlights": [1]}})
        self.store.upsert_book({"device_path": BOOK["device_path"], "title": "New"})
        book = self.store.get_book(self.book_id)
        self.assertEqual(book["data"], {"highlights": [1]})
        self.assertEqual(book["title"], "New")
        self.assertEqual(book["author"], "Y")

    def test_a_refresh_keeps_the_books_cards(self):
        card = self.store.add_card(self.book_id, self.hid, front="F")
        self.store.upsert_book({**BOOK, "data": {}})
        self.assertIsNotNone(self.store.get_card(card["id"]))

    def test_hostile_text_is_stored_as_text(self):
        title = "'); DROP TABLE books; --"
        self.store.upsert_book({**BOOK, "title": title})
        self.assertEqual(self.store.get_book(self.book_id)["title"], title)
        card = self.store.add_card(self.book_id, self.hid, front=title, back='"; DELETE FROM cards; --')
        self.assertEqual(self.store.get_card(card["id"])["front"], title)
        self.assertEqual(self.store.list_books()[0]["id"], self.book_id)
        self.assertEqual(self.store.list_cards(self.book_id, states=(title,)), [])


class HighlightTests(StoreCase):
    def test_flags_start_off_and_change_one_at_a_time(self):
        self.assertEqual(
            self.store.get_highlight(self.book_id, self.hid),
            {
                "book_id": self.book_id,
                "hid": self.hid,
                "key": "k1",
                "opened": False,
                "skipped": False,
                "error": None,
                "later_research": None,
                "chapter_notes_seen": False,
                "redo": False,
            },
        )
        self.store.set_opened(self.book_id, self.hid)
        self.store.set_skipped(self.book_id, self.hid)
        self.store.set_highlight_error(self.book_id, self.hid, "The AI failed.")
        self.store.set_later_research(self.book_id, self.hid, "Since publication...")
        self.store.set_chapter_notes_seen(self.book_id, self.hid)
        state = self.store.list_highlights(self.book_id)[self.hid]
        self.assertTrue(state["opened"] and state["skipped"] and state["chapter_notes_seen"])
        self.assertEqual(state["error"], "The AI failed.")
        self.assertEqual(state["later_research"], "Since publication...")
        self.store.set_skipped(self.book_id, self.hid, False)
        self.assertFalse(self.store.get_highlight(self.book_id, self.hid)["skipped"])

    def test_ensuring_again_keeps_the_flags(self):
        self.store.set_opened(self.book_id, self.hid)
        self.store.ensure_highlights(self.book_id, ["k1", "k2"])
        self.assertTrue(self.store.get_highlight(self.book_id, self.hid)["opened"])
        self.assertEqual(len(self.store.list_highlights(self.book_id)), 2)

    def test_changing_an_unknown_highlight_is_an_error(self):
        with self.assertRaises(NotFound):
            self.store.set_opened(self.book_id, "nope")


class CardTests(StoreCase):
    def ai_card(self, front="Q", back="A", kind="qa"):
        return self.store.add_card(
            self.book_id, self.hid, kind=kind, front=front, back=back, suggested={"front": front, "back": back}
        )

    def test_a_card_written_by_hand_is_yours(self):
        card = self.store.add_card(self.book_id, self.hid)
        self.assertEqual(card["source"], "You")
        self.assertIsNone(card["suggested"])
        self.assertEqual(card["state"], "draft")
        edited = self.store.update_card(card["id"], front="F")
        self.assertEqual(edited["source"], "You")

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.add_card(self.book_id, self.hid, kind="poem")

    def test_positions_follow_the_order_added_within_a_highlight(self):
        ids = [self.store.add_card(self.book_id, self.hid, front=str(i))["id"] for i in range(3)]
        self.assertEqual([c["id"] for c in self.store.list_cards(self.book_id, self.hid)], ids)
        self.assertEqual([c["position"] for c in self.store.list_cards(self.book_id, self.hid)], [0, 1, 2])

    def test_editing_an_ai_card_marks_it_and_undoing_the_edit_unmarks_it(self):
        card = self.ai_card()
        self.assertEqual(card["source"], "AI")
        self.assertEqual(self.store.update_card(card["id"], front="Q2")["source"], "AI, edited")
        self.assertEqual(self.store.update_card(card["id"], front="Q")["source"], "AI")

    def test_saving_the_same_text_is_not_an_edit(self):
        card = self.ai_card()
        self.assertEqual(self.store.update_card(card["id"], front="Q", back="A")["source"], "AI")

    def test_answering_a_question_the_ai_left_blank_is_not_an_edit(self):
        card = self.ai_card(back="", kind="qonly")
        updated = self.store.update_card(card["id"], back="my words")
        self.assertEqual(updated["source"], "AI")
        self.assertEqual(updated["back"], "my words")

    def test_filling_the_blank_back_of_an_ai_cloze_is_an_edit(self):
        card = self.ai_card(front="The {{c1::Seine}}", back="", kind="cloze")
        self.assertEqual(self.store.update_card(card["id"], back="extra")["source"], "AI, edited")

    def test_a_rewrite_is_the_new_text_later_edits_compare_against(self):
        card = self.ai_card()
        rewritten = self.store.rewrite_card(card["id"], "Q3", "A3")
        self.assertEqual(rewritten["source"], "AI, rewritten")
        self.assertEqual(rewritten["suggested"], {"front": "Q3", "back": "A3"})
        self.assertEqual(self.store.update_card(card["id"], back="mine")["source"], "AI, edited")

    def test_was_edited_matches_the_plugins_lua(self):
        if not shutil.which("lua"):
            self.skipTest("lua is not installed")
        cases = [
            {"front": "a", "back": "b"},
            {"front": "a", "back": "b", "suggested": {"front": "a", "back": "b"}},
            {"front": "x", "back": "b", "suggested": {"front": "a", "back": "b"}},
            {"front": "a", "back": "x", "suggested": {"front": "a", "back": "b"}},
            {"front": "a", "back": "x", "suggested": {"front": "a", "back": ""}},
            {"front": "{{c1::a}}", "back": "x", "suggested": {"front": "{{c1::a}}", "back": ""}},
            {"front": "{{c::a}}", "back": "x", "suggested": {"front": "{{c::a}}", "back": ""}},
            {"front": "{{c12::a}} b", "back": "", "suggested": {"front": "{{c12::a}} b", "back": ""}},
        ]

        def lua_table(card):
            quoted = lambda v: json.dumps(v)
            fields = [f"front={quoted(card['front'])}", f"back={quoted(card['back'])}"]
            if "suggested" in card:
                s = card["suggested"]
                fields.append(f"suggested={{front={quoted(s['front'])},back={quoted(s['back'])}}}")
            return "{" + ",".join(fields) + "}"

        script = (
            'package.path = "koreader/pbanki.koplugin/?.lua;" .. package.path\n'
            'local cardgen = require("cardgen")\n'
            "for _i, c in ipairs({" + ",".join(lua_table(c) for c in cases) + "}) do "
            "print(cardgen.wasEdited(c)) end\n"
        )
        out = subprocess.run(["lua", "-"], input=script, text=True, capture_output=True, cwd=REPO, check=True)
        self.assertEqual(
            out.stdout.split(),
            ["true" if store_module.was_edited(c) else "false" for c in cases],
        )

    def test_removed_ai_cards_feed_rejected_with_the_ais_own_text_until_undone(self):
        card = self.ai_card("Q1", "A1")
        mine = self.store.add_card(self.book_id, self.hid, front="mine")
        self.store.update_card(card["id"], front="changed")
        self.store.remove_card(card["id"])
        self.store.remove_card(mine["id"])
        self.assertEqual(self.store.rejected_cards(self.book_id, self.hid), [{"front": "Q1", "back": "A1"}])
        self.assertEqual(self.store.list_cards(self.book_id, self.hid), [])
        restored = self.store.restore_card(card["id"])
        self.assertEqual(restored["state"], "draft")
        self.assertEqual(restored["front"], "changed")
        self.assertEqual(self.store.rejected_cards(self.book_id, self.hid), [])

    def test_existing_has_the_shape_cardgen_wants(self):
        ai = self.ai_card("Q1", "A1")
        self.store.update_card(ai["id"], back="edited")
        self.store.add_card(self.book_id, self.hid, front="mine", back="b")
        gone = self.store.add_card(self.book_id, self.hid, front="gone")
        self.store.remove_card(gone["id"])
        self.assertEqual(
            self.store.existing_cards(self.book_id, self.hid),
            [
                {"front": "Q1", "back": "edited", "suggested": {"front": "Q1", "back": "A1"}},
                {"front": "mine", "back": "b"},
            ],
        )

    def test_a_saved_card_cannot_be_changed_or_removed(self):
        card = self.ai_card()
        self.store.record_batch("b1", self.book_id, [], [], {}, card_ids=[card["id"]])
        for change in (
            lambda: self.store.update_card(card["id"], front="late edit"),
            lambda: self.store.rewrite_card(card["id"], "x", "y"),
            lambda: self.store.remove_card(card["id"]),
            lambda: self.store.restore_card(card["id"]),
        ):
            with self.assertRaises(NotEditable):
                change()
        self.assertEqual(self.store.get_card(card["id"])["front"], "Q")

    def test_a_removed_card_cannot_be_edited(self):
        card = self.ai_card()
        self.store.remove_card(card["id"])
        with self.assertRaises(NotEditable):
            self.store.update_card(card["id"], front="x")

    def test_changing_an_unknown_card_is_an_error(self):
        for change in (self.store.update_card, self.store.remove_card, self.store.restore_card):
            with self.assertRaises(NotFound):
                change("nope")

    def test_draft_counts_leave_out_blank_cards(self):
        self.store.add_card(self.book_id, self.hid, front="F")
        self.store.add_card(self.book_id, self.hid, front="  ")
        self.assertEqual(self.store.draft_counts(), {self.book_id: 1})


class ConnectionAndIdeaTests(StoreCase):
    def test_connections_keep_their_sources_and_mark_edits(self):
        item = self.store.add_connection(self.book_id, "T", "F", "B", ["h1", "h2"])
        self.assertEqual(item["sources"], ["h1", "h2"])
        self.assertEqual(item["source"], "AI")
        self.assertFalse(item["opened"])
        self.assertEqual(self.store.update_connection(item["id"], front="F")["source"], "AI")
        self.assertEqual(self.store.update_connection(item["id"], front="F2")["source"], "AI, edited")
        self.store.open_connection(item["id"])
        self.assertTrue(self.store.get_connection(item["id"])["opened"])

    def test_connections_remove_and_restore(self):
        item = self.store.add_connection(self.book_id, "T", "F", "B", [])
        self.store.remove_connection(item["id"])
        self.assertEqual(self.store.list_connections(self.book_id), [])
        self.assertEqual(len(self.store.list_connections(self.book_id, ("removed",))), 1)
        self.store.restore_connection(item["id"])
        self.assertEqual(len(self.store.list_connections(self.book_id)), 1)

    def test_ideas_mark_edits_in_the_mocks_wording(self):
        item = self.store.add_idea(self.book_id, "Chapter 1", "T", "note", "quote", "F", "B")
        self.assertEqual(item["source"], "From the Book Companion")
        self.assertEqual(self.store.update_idea(item["id"], back="B")["source"], "From the Book Companion")
        self.assertEqual(self.store.update_idea(item["id"], back="B2")["source"], "From the Book Companion, edited")
        self.store.open_idea(item["id"])
        self.assertTrue(self.store.list_ideas(self.book_id)[0]["opened"])
        self.store.remove_idea(item["id"])
        self.assertEqual(self.store.list_ideas(self.book_id), [])
        self.store.restore_idea(item["id"])
        self.assertEqual(len(self.store.list_ideas(self.book_id)), 1)

    def test_saved_connections_and_ideas_cannot_change(self):
        connection = self.store.add_connection(self.book_id, "T", "F", "B", [])
        idea = self.store.add_idea(self.book_id, "C", "T", "", "", "F", "B")
        self.store.record_batch(
            "b1", self.book_id, [], [], {}, connection_ids=[connection["id"]], idea_ids=[idea["id"]]
        )
        for change in (
            lambda: self.store.update_connection(connection["id"], front="x"),
            lambda: self.store.remove_connection(connection["id"]),
            lambda: self.store.update_idea(idea["id"], front="x"),
            lambda: self.store.remove_idea(idea["id"]),
        ):
            with self.assertRaises(NotEditable):
                change()

    def test_unknown_ids_are_errors(self):
        for change in (
            self.store.open_connection,
            self.store.remove_connection,
            self.store.update_connection,
            self.store.open_idea,
            self.store.remove_idea,
            self.store.update_idea,
        ):
            with self.assertRaises(NotFound):
                change("nope")

    def test_chapter_notes_replace_by_chapter(self):
        self.store.set_chapter_notes(self.book_id, "C1", "big", [{"text": "p", "covered": True, "idea": 0}])
        self.store.set_chapter_notes(self.book_id, "C1", "bigger", [])
        self.store.set_chapter_notes(self.book_id, "C2", "other", [])
        notes = self.store.get_chapter_notes(self.book_id)
        self.assertEqual(notes["C1"], {"big": "bigger", "points": []})
        self.assertEqual(sorted(notes), ["C1", "C2"])
        self.store.clear_chapter_notes(self.book_id)
        self.assertEqual(self.store.get_chapter_notes(self.book_id), {})


class SettingsAndRunTests(StoreCase):
    def test_defaults_until_set(self):
        self.assertEqual(self.store.get_setting("model"), "claude-sonnet-5-5")
        self.assertEqual(self.store.get_setting("unknown"), None)
        self.assertEqual(self.store.get_setting("unknown", "fallback"), "fallback")
        self.store.set_setting("model", "claude-opus-5-5")
        self.assertEqual(self.store.get_setting("model"), "claude-opus-5-5")
        self.store.set_setting("suggest_ideas", False)
        self.assertIs(self.store.get_setting("suggest_ideas", True), False)

    def test_all_settings_merge_stored_values_over_defaults(self):
        with mock.patch.dict(os.environ, {"PBANKI_MOUNT": "/somewhere/else"}):
            self.store.set_settings({"count": 5, "extra": {"a": 1}})
            settings = self.store.get_settings()
        self.assertEqual(settings["count"], 5)
        self.assertEqual(settings["extra"], {"a": 1})
        self.assertEqual(settings["meta_model"], "claude-opus-5-5")
        self.assertEqual(settings["mount"], "/somewhere/else")

    def test_changing_a_returned_default_does_not_change_the_defaults(self):
        self.store.get_settings()["assistants"]["claude"] = False
        self.store.get_setting("assistants")["codex"] = False
        self.assertEqual(self.store.get_setting("assistants"), {"claude": True, "codex": True})

    def test_a_run_keeps_what_an_update_leaves_out(self):
        self.assertIsNone(self.store.get_run(self.book_id))
        self.store.set_run(self.book_id, "running", options={"model": "m"}, progress={"done": 1})
        self.store.set_run(self.book_id, "paused", progress={"done": 2})
        run = self.store.get_run(self.book_id)
        self.assertEqual(run["status"], "paused")
        self.assertEqual(run["options"], {"model": "m"})
        self.assertEqual(run["progress"], {"done": 2})
        self.assertEqual(len(self.store.list_runs()), 1)


class BatchTests(StoreCase):
    def test_a_batch_moves_forward_and_records_its_lines(self):
        line = '{"v":1,"batch":"b1","file":"f","key":"k1","cards":[]}'
        self.store.record_batch("b1", self.book_id, ["i"], [line], {"cards": 1})
        batch = self.store.get_batch("b1")
        self.assertEqual(batch["status"], "backed_up")
        self.assertEqual((batch["inbox_lines"], batch["reviewed_lines"]), (["i"], [line]))
        self.assertEqual(batch["summary"], {"cards": 1})
        self.assertEqual(self.store.unverified_reviewed_keys(self.book_id), {"k1"})
        self.store.mark_batch_written("b1", b"i\n", None)
        self.assertEqual(self.store.get_batch("b1")["intended_inbox"], b"i\n")
        self.assertEqual(len(self.store.unverified_batches()), 1)
        self.store.mark_batch_verified("b1")
        self.assertEqual(self.store.get_batch("b1")["status"], "verified")
        self.assertIsNone(self.store.get_batch("b1")["intended_inbox"])
        self.assertEqual(self.store.unverified_batches(), [])
        self.assertEqual(self.store.unverified_reviewed_keys(self.book_id), set())
        self.store.mark_batch_written("b1", b"late", None)
        self.assertEqual(self.store.get_batch("b1")["status"], "verified")

    def test_recording_is_all_or_nothing(self):
        card = self.store.add_card(self.book_id, self.hid, front="F")
        self.store.set_opened(self.book_id, self.hid)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_batch(
                "b1",
                self.book_id,
                ["i"],
                [],
                {},
                saved_cards=[{"hid": self.hid, "kind": "qa", "front": None, "back": "", "source": "You"}],
                card_ids=[card["id"]],
            )
        self.assertIsNone(self.store.get_batch("b1"))
        self.assertEqual(self.store.get_card(card["id"])["state"], "draft")

    def test_recording_saves_drafts_and_clears_marked_highlights(self):
        card = self.store.add_card(self.book_id, self.hid, front="F", back="B")
        blank = self.store.add_card(self.book_id, self.hid, front=" ")
        self.store.set_opened(self.book_id, self.hid)
        self.store.set_skipped(self.book_id, self.hid)
        self.store.record_batch(
            "b1",
            self.book_id,
            ["i"],
            [],
            {},
            saved_cards=[{**card, "hid": self.hid}],
            card_ids=[card["id"]],
            blank_card_ids=[blank["id"]],
            marked_hids=[self.hid],
        )
        self.assertEqual(self.store.get_card(card["id"])["state"], "saved")
        self.assertEqual(self.store.get_card(blank["id"])["state"], "removed")
        state = self.store.get_highlight(self.book_id, self.hid)
        self.assertFalse(state["opened"] or state["skipped"])
        saved = self.store.list_saved_cards(self.book_id, self.hid)
        self.assertEqual([(s["front"], s["back"], s["kind"]) for s in saved], [("F", "B", "qa")])
        self.assertEqual(self.store.list_saved_cards(self.book_id, "other"), [])
        self.assertEqual(self.store.existing_cards(self.book_id, self.hid), [{"front": "F", "back": "B"}])

    def test_a_transaction_rolls_back_even_for_a_crash(self):
        class Crash(BaseException):
            pass

        with self.assertRaises(Crash):
            with self.store.transaction():
                self.store.add_card(self.book_id, self.hid, front="F")
                with self.store.transaction():
                    self.store.set_setting("a", 1)
                raise Crash()
        self.assertEqual(self.store.list_cards(self.book_id), [])
        self.assertEqual(self.store.get_setting("a"), None)
        self.store.set_setting("a", 2)
        self.assertEqual(self.store.get_setting("a"), 2)


class ThreadTests(StoreCase):
    THREADS = 8

    def run_threads(self, work):
        errors = []
        barrier = threading.Barrier(self.THREADS)

        def body(index):
            try:
                barrier.wait()
                work(index)
            except BaseException as e:
                errors.append(e)
            finally:
                self.store.close()

        threads = [threading.Thread(target=body, args=(i,)) for i in range(self.THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])

    def test_each_thread_has_its_own_connection(self):
        seen = {}

        def work(index):
            first = self.store._conn()
            self.assertIs(self.store._conn(), first)
            seen[index] = id(first)
            # Stay open until every thread has one, so the ids can't be reused
            self._hold.wait()

        self._hold = threading.Barrier(self.THREADS)
        self.run_threads(work)
        self.assertEqual(len(set(seen.values())), self.THREADS)
        self.assertNotIn(id(self.store._conn()), seen.values())

    def test_concurrent_writers_all_land_with_unique_positions(self):
        per_thread = 40

        def work(index):
            for i in range(per_thread):
                card = self.store.add_card(self.book_id, self.hid, front=f"{index}-{i}")
                self.store.update_card(card["id"], back="b")
                self.store.set_setting(f"s{index}", i)
                self.store.set_opened(self.book_id, self.hid)
                self.store.list_cards(self.book_id, self.hid)

        self.run_threads(work)
        cards = self.store.list_cards(self.book_id, self.hid)
        self.assertEqual(len(cards), self.THREADS * per_thread)
        self.assertEqual(len({c["front"] for c in cards}), len(cards))
        self.assertEqual(sorted(c["position"] for c in cards), list(range(len(cards))))
        self.assertTrue(all(c["back"] == "b" for c in cards))
        self.assertEqual(self.store.get_setting("s0"), per_thread - 1)

    def test_a_long_write_makes_others_wait_not_fail(self):
        started, release = threading.Event(), threading.Event()

        def work(index):
            if index == 0:
                with self.store.transaction():
                    self.store.set_setting("slow", 1)
                    started.set()
                    release.wait(1)
            else:
                started.wait()
                self.store.set_setting(f"quick{index}", 1)
                release.set()

        self.run_threads(work)
        self.assertEqual(self.store.get_setting("slow"), 1)
        self.assertEqual(self.store.get_setting("quick1"), 1)

    def test_many_stores_opening_a_new_file_at_once(self):
        path = Path(self.tmp.name) / "fresh.db"

        def work(_index):
            store = Store(path)
            store.set_setting("x", 1)
            store.close()

        self.run_threads(work)
        store = Store(path)
        self.addCleanup(store.close)
        self.assertEqual(store.schema_version(), len(store_module.MIGRATIONS))


class OddTextTests(StoreCase):
    def test_half_an_emoji_from_an_ai_reply_is_stored_not_raised(self):
        book_id = self.store.upsert_book({"device_path": "/mnt/ext1/a.epub"})
        hid = self.store.ensure_highlights(book_id, ["k"])["k"]
        card = self.store.add_card(book_id, hid, front="cut \ud83d emoji?", back="x",
                                   suggested={"front": "cut \ud83d emoji?", "back": "x"})
        self.assertEqual(card["front"], "cut ? emoji?")
        self.assertEqual(card["suggested"], {"front": "cut ? emoji?", "back": "x"})
        self.assertEqual(self.store.update_card(card["id"], back="half \ude00")["back"], "half ?")
        self.assertEqual(self.store.rewrite_card(card["id"], "a\ud83d", "b")["front"], "a?")
        connection = self.store.add_connection(book_id, "t\ud83d", "f\ud83d", "b", [hid])
        self.assertEqual(connection["title"], "t?")
        idea = self.store.add_idea(book_id, "c", "t", "n\ud83d", "q", "f", "b")
        self.assertEqual(idea["note"], "n?")
        self.store.set_highlight_error(book_id, hid, "e\ud83d")
        self.assertEqual(self.store.get_highlight(book_id, hid)["error"], "e?")


class ReaderReviewedTests(StoreCase):
    def test_keys_are_replaced_and_kept_per_book(self):
        self.store.set_reader_reviewed("b1", {"a", "b"})
        self.store.set_reader_reviewed("b2", {"c"})
        self.store.set_reader_reviewed("b1", {"b"})
        self.assertEqual(self.store.reader_reviewed("b1"), {"b"})
        self.assertEqual(self.store.reader_reviewed("b2"), {"c"})
        self.assertEqual(self.store.reader_reviewed("none"), set())

    def test_a_proven_batch_adds_its_keys(self):
        book_id = self.store.upsert_book({"device_path": "/mnt/ext1/a.epub"})
        self.store.set_reader_reviewed(book_id, {"older"})
        self.store.record_batch("b", book_id, [], [json.dumps({"key": "k1"}), json.dumps({"key": "k2"})], {})
        self.assertEqual(self.store.reader_reviewed(book_id), {"older"})
        self.store.mark_batch_verified("b")
        self.assertEqual(self.store.reader_reviewed(book_id), {"older", "k1", "k2"})


if __name__ == "__main__":
    unittest.main()
