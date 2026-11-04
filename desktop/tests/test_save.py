import errno
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["PBANKI_SYNC_DELAY"] = "0"

from pbanki_desktop import device, epub, paths, reader, save
from pbanki_desktop import store as store_module
from pbanki_desktop.store import Store

REPO = Path(__file__).resolve().parents[2]
FILE = "/mnt/ext1/English/book.epub"
BOOK = {"device_path": FILE, "title": "Pride and Prejudice", "author": "Jane Austen\nAnn Other"}
OLD_LINE = b'{"front":"from the plugin","back":"b"}'


class Crash(BaseException):
    """Stands in for the process dying: nothing in the app catches it."""


def rust_lines():
    """The literal lines in the Rust test in common/src/inbox.rs."""
    source = (REPO / "common/src/inbox.rs").read_text("utf-8")
    block = source[source.index("const DESKTOP_SAVE_LINES") :]
    block = block[: block.index("];")]
    return re.findall(r'r#"(.*?)"#', block)


def fake_reader(root):
    """A reader's folders, with files the app must never touch."""
    for rel, data in {
        "system/config/desktop/view.json": b"{}",
        "applications/koreader/history.lua": b"return {}",
        "applications/koreader/settings.reader.lua": b'return { ["document_metadata_folder"] = "doc" }',
        "applications/koreader/plugins/other.koplugin/main.lua": b"other",
        "applications/pbanki/config.toml": b"x = 1",
        "applications/pbanki/prompts/Standard/questions.md": b"edited by the plugin",
        "applications/pbanki/prompts/Mine/questions.md": b"my prompt",
        "English/book.epub": b"epub bytes",
        "English/book.sdr/metadata.epub.lua": b"return {}",
        "English/book.companion.md": b"notes",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def snapshot(root):
    """Every file's digest and every folder under `root`."""
    found = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        found[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "folder"
    return found


def highlight(key, text="Text", **fields):
    return {"key": key, "hid": store_module.hid_for(key), "text": text, **fields}


class SaveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.db = base / "home" / "pbanki.db"
        self.mount = base / "reader"
        self.mount.mkdir()
        fake_reader(self.mount)
        self.inbox = self.mount / paths.INBOX_FILE
        self.reviewed = self.mount / paths.REVIEWED_FILE
        env = {"PBANKI_DESKTOP_HOME": str(base / "home"), "PBANKI_MOUNT": str(self.mount)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = Store(self.db)
        self.addCleanup(lambda: self.store.close())
        self.book_id = self.store.upsert_book(BOOK)
        self.book = {**BOOK, "id": self.book_id}

    def fresh_store(self):
        """A process restart: a new Store on the same file."""
        self.store.close()
        self.store = Store(self.db)
        return self.store

    def add_highlight(self, key="k1", opened=True, cards=(("qa", "Q", "A"),), **fields):
        h = highlight(key, **fields)
        self.store.ensure_highlights(self.book_id, [key])
        if opened:
            self.store.set_opened(self.book_id, h["hid"])
        for kind, front, back in cards:
            self.store.add_card(self.book_id, h["hid"], kind=kind, front=front, back=back)
        return h

    def work(self):
        """Two highlights with cards, a connection and a key idea, all opened."""
        a = self.add_highlight("k1", cards=(("qa", "Q1", "A1"), ("cloze", "{{c1::x}} y", "")), pageno=5, chapter="C")
        b = self.add_highlight("k2", cards=(("qa", "Q2", "A2"),), pageno=6, chapter="D")
        c = self.store.add_connection(self.book_id, "T", "CF", "CB", [a["hid"], b["hid"]])
        self.store.open_connection(c["id"])
        i = self.store.add_idea(self.book_id, "E", "IT", "", "q", "IF", "IB")
        self.store.open_idea(i["id"])
        return [a, b]

    def batch_lines(self, batch_id):
        batch = self.store.get_batch(batch_id)
        return batch["inbox_lines"], batch["reviewed_lines"]

    def assert_each_once(self, path, lines, extra=()):
        """The file holds exactly `extra` then `lines`, each once, nothing cut off."""
        got = path.read_bytes().splitlines()
        self.assertEqual(got, [*extra, *(line.encode() for line in lines)])
        self.assertTrue(path.read_bytes().endswith(b"\n"))

    def only_batch(self):
        batches = self.store.list_batches(self.book_id)
        self.assertEqual(len(batches), 1)
        return batches[0]


class NothingToAnswerTests(SaveCase):
    def test_a_question_with_no_answer_blocks_the_save(self):
        h = self.add_highlight("k1", cards=(("qa", "Why?", "  "),))
        p = save.preview(self.store, self.book, [h])
        self.assertTrue(p["blocked"])
        self.assertEqual(p["needs"], [h["hid"]])
        self.assertEqual(p["need_text"], "1 question still needs an answer. Answer or remove before saving.")
        self.assertEqual(p["groups"][0]["rows"][0]["needLabel"], "No answer yet")
        with self.assertRaises(save.SaveBlocked):
            save.back_up(self.store, self.book, [h])

    def test_a_cloze_hiding_nothing_blocks_the_save(self):
        for front in ("A plain sentence.", "Empty {{c1::  }} gap.", "{{{{c2::c1::Senten}}ce}} broken."):
            with self.subTest(front=front):
                key = f"key {front}"
                h = self.add_highlight(key, cards=(("cloze", front, ""),))
                p = save.preview(self.store, self.book, [h])
                self.assertTrue(p["blocked"])
                self.assertEqual(p["groups"][0]["rows"][0]["needLabel"], "No words hidden")
                self.assertIn("1 cloze card still needs words hidden correctly.", p["need_text"])
                self.store.remove_card(self.store.list_cards(self.book_id, h["hid"])[0]["id"])

    def test_good_cards_and_blank_ones_dont_block(self):
        h = self.add_highlight("k1", cards=(("qa", "Q", "A"), ("cloze", "{{c1::x}} y {{c2::z}}", ""), ("qa", "", ""), ("cloze", "  ", "")))
        p = save.preview(self.store, self.book, [h])
        self.assertFalse(p["blocked"])
        self.assertEqual(p["count"], 2)

    def test_a_question_written_as_a_cloze_needs_no_back(self):
        h = self.add_highlight("k1", cards=(("qa", "{{c1::x}} y", ""),))
        self.assertFalse(save.preview(self.store, self.book, [h])["blocked"])

    def test_a_connection_or_idea_with_no_answer_blocks_the_save(self):
        h = self.add_highlight("k1")
        connection = self.store.add_connection(self.book_id, "T", "F?", "", [h["hid"]])
        self.store.open_connection(connection["id"])
        idea = self.store.add_idea(self.book_id, "E", "IT", "", "", "I?", " ")
        self.store.open_idea(idea["id"])
        p = save.preview(self.store, self.book, [h])
        self.assertTrue(p["blocked"])
        self.assertEqual(p["need_connections"], [connection["id"]])
        self.assertEqual(p["need_ideas"], [idea["id"]])
        self.assertEqual(p["need_text"], "2 questions still need answers. Answer or remove before saving.")


class PreviewTests(SaveCase):
    def test_only_what_was_opened_is_included(self):
        opened = self.add_highlight("k1", cards=(("qa", "Q1", "A1"),), pageno=5, chapter="C", chapter_number=3)
        unopened = self.add_highlight("k2", opened=False, cards=(("qa", "Q2", "A2"),))
        untouched = self.add_highlight("k3", opened=False, cards=())
        c1 = self.store.add_connection(self.book_id, "Open", "CF", "CB", [opened["hid"]])
        self.store.open_connection(c1["id"])
        self.store.add_connection(self.book_id, "Closed", "F", "B", [])
        i1 = self.store.add_idea(self.book_id, "Ch", "OpenIdea", "", "", "IF", "IB")
        self.store.open_idea(i1["id"])
        self.store.add_idea(self.book_id, "Ch", "ClosedIdea", "", "", "F", "B")

        p = save.preview(self.store, self.book, [opened, unopened, untouched])

        self.assertEqual(p["count"], 3)
        self.assertEqual(p["noun"], "cards ready")
        self.assertEqual(
            p["groups"],
            [
                {
                    "type": "highlight",
                    "id": opened["hid"],
                    "head": "Text",
                    "where": "Ch. 3 · p. 5",
                    "rows": [{"front": "Q1", "back": "A1", "need": False}],
                },
                {
                    "type": "connection",
                    "id": c1["id"],
                    "head": "Connection: Open",
                    "where": "Chapter 3: C",
                    "rows": [{"front": "CF", "back": "CB", "need": False}],
                },
                {
                    "type": "idea",
                    "id": i1["id"],
                    "head": "Key idea: OpenIdea",
                    "where": "Ch",
                    "rows": [{"front": "IF", "back": "IB", "need": False}],
                },
            ],
        )
        self.assertEqual(p["mark_text"], "Mark 1 highlight as reviewed")
        self.assertEqual(p["left"], 2)
        self.assertEqual(
            p["left_text"],
            "2 highlights will stay to do. 1 highlight with suggestions you haven't opened is not included. "
            "Its suggestions stay here for later. 1 connection you haven't opened also stays here. "
            "1 key idea you haven't opened also stays here.",
        )
        self.assertFalse(p["blocked"])

    def test_every_highlight_covered_reads_as_the_mock_does(self):
        h = self.add_highlight("k1")
        p = save.preview(self.store, self.book, [h])
        self.assertEqual(p["noun"], "card ready")
        self.assertEqual(p["left_text"], "Every pending highlight is covered.")

    def test_a_question_without_an_answer_blocks_the_save(self):
        h = self.add_highlight("k1", cards=(("qonly", "Q", " "), ("qonly", "Q2", "")))
        p = save.preview(self.store, self.book, [h])
        self.assertTrue(p["blocked"])
        self.assertEqual(p["needs"], [h["hid"], h["hid"]])
        self.assertEqual(p["need_text"], "2 questions still need answers. Answer or remove before saving.")
        self.assertTrue(p["groups"][0]["rows"][0]["need"])
        with self.assertRaisesRegex(save.SaveBlocked, "still need answers"):
            save.save(self.store, self.book, [h], self.mount)
        self.assertEqual(self.store.list_batches(), [])
        self.assertFalse(self.inbox.exists())

    def test_an_unanswered_question_on_an_unopened_highlight_does_not_block(self):
        a = self.add_highlight("k1")
        b = self.add_highlight("k2", opened=False, cards=(("qonly", "Q", ""),))
        self.assertFalse(save.preview(self.store, self.book, [a, b])["blocked"])

    def test_a_blank_front_is_not_saved_or_counted(self):
        h = self.add_highlight("k1", cards=(("qa", "  ", "A"), ("qa", "Q", "A")))
        blank = self.add_highlight("k2", cards=(("qa", "", ""),))
        p = save.preview(self.store, self.book, [h, blank])
        self.assertEqual(p["count"], 1)
        self.assertEqual(p["marked"]["with_cards"], [h["hid"]])

    def test_cloze_rows_show_the_gap_as_the_mock_does(self):
        h = self.add_highlight("k1", cards=(("cloze", "The {{c1::Seine}} and {{c2::Loire}}", ""),))
        row = save.preview(self.store, self.book, [h])["groups"][0]["rows"][0]
        self.assertEqual(row, {"front": "The [...] and [...]", "back": "Cloze", "need": False})

    def test_nothing_to_save_is_blocked(self):
        h = self.add_highlight("k1", opened=False, cards=())
        self.store.set_skipped(self.book_id, h["hid"])
        p = save.preview(self.store, self.book, [h])
        self.assertTrue(p["blocked"])
        self.assertEqual(p["count"], 0)
        with self.assertRaises(save.SaveBlocked):
            save.back_up(self.store, self.book, [h])

    def test_connection_only_sources_follow_the_checkbox(self):
        with_cards = self.add_highlight("k1")
        only = self.add_highlight("k2", opened=False, cards=())
        unopened_cards = self.add_highlight("k3", opened=False)
        c = self.store.add_connection(
            self.book_id, "T", "F", "B", [with_cards["hid"], only["hid"], unopened_cards["hid"], "gone"]
        )
        self.store.open_connection(c["id"])
        all_hl = [with_cards, only, unopened_cards]

        on = save.preview(self.store, self.book, all_hl, mark_connection_sources=True)
        self.assertEqual(on["marked"]["connection_only"], [only["hid"]])
        self.assertEqual(on["mark_count"], 2)
        self.assertEqual(on["left"], 1)

        off = save.preview(self.store, self.book, all_hl, mark_connection_sources=False)
        self.assertEqual(off["marked"]["connection_only"], [])
        self.assertEqual(off["connection_only"], [only["hid"]])
        self.assertEqual(off["mark_count"], 1)
        self.assertEqual(off["left"], 2)

    def test_skipped_highlights_are_marked_and_their_cards_stay_out(self):
        a = self.add_highlight("k1")
        b = self.add_highlight("k2", cards=(("qa", "skip me", "x"),))
        self.store.set_skipped(self.book_id, b["hid"])
        p = save.preview(self.store, self.book, [a, b])
        self.assertEqual(p["count"], 1)
        self.assertEqual(p["marked"]["skipped"], [b["hid"]])
        self.assertEqual(p["mark_count"], 2)
        self.assertEqual(p["left"], 0)

    def test_reviewed_highlights_stay_out_however_the_status_arrives(self):
        for fields in ({"reviewed": True}, {"status": "reviewed"}):
            key = "k" + str(sorted(fields))
            h = self.add_highlight(key, **fields)
            self.assertEqual(save.preview(self.store, self.book, [h])["count"], 0)


class LineShapeTests(SaveCase):
    def pipeline_highlights(self):
        quote = (
            "IT is a truth universally acknowledged, that a single man in possession of a good fortune "
            "must be in want of a wife."
        )
        first = self.add_highlight(
            "2026-11-03 15:50:37|/body/DocFragment[2]/body/p[19]/span/text().0",
            cards=(
                ("qa", "What is every rich single man said to need?", "A wife"),
                ("cloze", "A single man of fortune must be in want of a {{c1::wife}}.", ""),
            ),
            text=quote,
            chapter="Chapter I.",
            chapter_number=1,
            pageno=35,
            # An annotation's `page` is an xpointer, as in the real KOReader fixture
            page="/body/DocFragment[2]/body/p[19]/span/text().0",
            pos0="/body/DocFragment[2]/body/p[19]/span/text().0",
        )
        a = self.add_highlight(
            "k2", opened=False, cards=(), text="“Oh, single, my dear, to be sure!”", chapter="Chapter I.", pageno=36
        )
        b = self.add_highlight(
            "k3",
            opened=False,
            cards=(),
            text="Mrs. Bennet deigned not to make any reply;\n“Don’t keep coughing so, Kitty”",
            chapter="Her nerves",
            chapter_number=2,
            pageno=41,
        )
        connection = self.store.add_connection(
            self.book_id,
            "T",
            "How does Mrs. Bennet react to a rich neighbour and to a cough?",
            "Delight, then nerves",
            [a["hid"], b["hid"]],
        )
        self.store.open_connection(connection["id"])
        idea = self.store.add_idea(
            self.book_id,
            "CHAPTER IV.",
            "Candour",
            "note",
            "a quote that stays off the card",
            "What does candour without design mean?",
            "Saying nothing of the bad",
        )
        self.store.open_idea(idea["id"])
        return [first, a, b]

    def test_the_inbox_holds_exactly_the_lines_the_rust_test_imports(self):
        highlights = self.pipeline_highlights()
        with mock.patch.object(store_module, "new_id", return_value="0123456789abcdef"):
            result = save.save(self.store, self.book, highlights, self.mount)
        self.assertEqual(result["status"], "verified")
        expected = rust_lines()
        self.assertEqual(len(expected), 4)
        self.assertEqual(self.inbox.read_text("utf-8"), "\n".join(expected) + "\n")

    def test_every_line_type_avoids_null_and_keeps_page_a_number(self):
        highlights = self.pipeline_highlights()
        highlights.append(self.add_highlight("k4", text="", note=None, chapter=None))
        save.save(self.store, {**self.book, "author": None}, highlights, self.mount)
        for line in self.inbox.read_text("utf-8").splitlines():
            card = json.loads(line)
            for field in ("front", "back", "quote", "book", "author", "chapter", "file", "position", "batch"):
                self.assertIsInstance(card[field], str, field)
            self.assertIsInstance(card.get("page", 0), int)
            self.assertNotIn(":null", line)
        for line in self.reviewed.read_text("utf-8").splitlines():
            self.assertNotIn("null", line)

    def test_page_is_a_number_or_absent(self):
        for fields, want in (
            ({"pageno": 35, "page": "/body/x"}, 35),
            ({"page": 12}, 12),
            ({"page": 12.0}, 12),
            ({"page": "12"}, 12),
            ({"page": "IV"}, None),
            ({"page": "/body/x"}, None),
            ({"page": True}, None),
            ({"page": -1}, None),
            ({"page": 2**32}, None),
            ({"page": 1.5}, None),
            ({"page": None}, None),
            ({}, None),
        ):
            self.assertEqual(save.page_number(fields), want, fields)
            line = json.loads(
                save.highlight_card_line(self.book, highlight("k", **fields), {"front": "f", "back": "b"}, "b1")
            )
            if want is None:
                self.assertNotIn("page", line)
            else:
                self.assertEqual(line["page"], want)

    def test_position_is_the_xpointer_or_the_page_as_text(self):
        self.assertEqual(save.position_of({"pos0": "/body/p[1]/text().0"}), "/body/p[1]/text().0")
        self.assertEqual(save.position_of({"pos0": {"page": 12, "x": 1}}), "12")
        self.assertEqual(save.position_of({"position": "7", "pos0": {"page": 12}}), "7")
        self.assertEqual(save.position_of({}), "")

    def test_chapter_names_follow_the_plan(self):
        self.assertEqual(save.chapter_name({"chapter": "Her nerves", "chapter_number": 2}), "Chapter 2: Her nerves")
        self.assertEqual(save.chapter_name({"chapter": "Chapter I.", "chapter_number": 1}), "Chapter I.")
        self.assertEqual(save.chapter_name({"chapter": "CHAPTER IV.", "chapter_number": 4}), "CHAPTER IV.")
        self.assertEqual(save.chapter_name({"chapter": "12 Rules", "chapter_number": 12}), "12 Rules")
        self.assertEqual(save.chapter_name({"chapter": "Chapters of life", "chapter_number": 3}), "Chapter 3: Chapters of life")
        self.assertEqual(save.chapter_name({"chapter": "No number"}), "No number")
        self.assertEqual(save.chapter_name({}), "")

    def test_a_connection_quote_has_one_line_per_source(self):
        a = highlight("a", text="one\ntwo   three", chapter="T", chapter_number=1, pageno=19)
        b = highlight("b", text="no page", chapter="T", chapter_number=1)
        c = highlight("c", text="no chapter", pageno=3)
        d = highlight("d", text="nothing")
        connection = {"front": "f", "back": "b", "sources": [h["hid"] for h in (a, b, c, d)] + ["gone"]}
        line = json.loads(save.connection_line(self.book, connection, {h["hid"]: h for h in (a, b, c, d)}, "b1"))
        self.assertEqual(
            line["quote"],
            'Chapter 1: T · p. 19: "one two three"\nChapter 1: T: "no page"\np. 3: "no chapter"\n"nothing"',
        )
        self.assertEqual(line["chapter"], "Chapter 1: T")
        self.assertNotIn("page", line)
        self.assertEqual(line["position"], "")

    def test_reviewed_lines_match_the_plan_exactly(self):
        h = highlight("2026|/x", text="the text", note="my note")
        cards = [
            {"kind": "qa", "front": "F", "back": "B", "suggested": {"front": "F0", "back": "B0"}},
            {"kind": "qa", "front": "G", "back": "H", "suggested": None},
            {"kind": "def", "front": "Define x", "back": "y", "suggested": {"front": "a", "back": "b"}},
        ]
        self.assertEqual(
            save.reviewed_line(self.book, h, cards, "b1"),
            '{"v":1,"batch":"b1","file":"/mnt/ext1/English/book.epub","key":"2026|/x","cards":['
            '{"front":"F","back":"B","suggested":{"front":"F0","back":"B0"},"quote":"the text","note":"my note"},'
            '{"front":"G","back":"H","quote":"the text","note":"my note"}]}',
        )
        self.assertEqual(
            save.reviewed_line(self.book, highlight("k"), [], "b1"),
            '{"v":1,"batch":"b1","file":"/mnt/ext1/English/book.epub","key":"k","cards":[]}',
        )

    def test_a_batch_marks_cards_skips_and_sources(self):
        a = self.add_highlight("k1", text="text a", note="n", cards=(("qa", "Q", "A"), ("def", "Define x", "y")))
        skipped = self.add_highlight("k2", opened=False, cards=(("qa", "not saved", "x"),))
        self.store.set_skipped(self.book_id, skipped["hid"])
        only = self.add_highlight("k3", opened=False, cards=())
        c = self.store.add_connection(self.book_id, "T", "F", "B", [only["hid"]])
        self.store.open_connection(c["id"])
        result = save.save(self.store, self.book, [a, skipped, only], self.mount)
        self.assertEqual(result["status"], "verified")
        lines = [json.loads(l) for l in self.reviewed.read_text("utf-8").splitlines()]
        self.assertEqual([l["key"] for l in lines], ["k1", "k2", "k3"])
        self.assertEqual(lines[0]["cards"], [{"front": "Q", "back": "A", "quote": "text a", "note": "n"}])
        self.assertEqual(lines[1]["cards"], [])
        self.assertEqual(lines[2]["cards"], [])
        self.assertEqual(
            {key: value for key, value in result["summary"].items() if key != "groups"},
            {
                "cards": 3,
                "highlight_cards": 2,
                "connections": 1,
                "ideas": 0,
                "marked": 3,
                "skipped": 1,
                "title": "Pride and Prejudice",
                "deck": "KOReader::Pride and Prejudice",
                "file": FILE,
            },
        )

    def test_a_line_is_always_one_valid_utf8_line(self):
        h = highlight("k", text="line one\nline two \ud83d")
        line = save.highlight_card_line(self.book, h, {"front": "a\nb", "back": ""}, "b1")
        self.assertNotIn("\n", line)
        line.encode("utf-8")
        self.assertEqual(json.loads(line)["front"], "a\nb")

    def test_deck_names_match_the_rust_function(self):
        self.assertEqual(save.deck_name("Pride and Prejudice"), "KOReader::Pride and Prejudice")
        self.assertEqual(save.deck_name("  Dune:: Part  One\n"), "KOReader::Dune: Part One")
        self.assertEqual(save.deck_name("  "), "KOReader")


def lua_literal(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "{" + ",".join(lua_literal(v) for v in value) + "}"
    if isinstance(value, dict):
        return "{" + ",".join(f"[{lua_literal(k)}]={lua_literal(v)}" for k, v in value.items()) + "}"
    return str(value)


class PluginAndReaderTests(SaveCase):
    """Against the real plugin code and the sidecar real KOReader wrote."""

    @unittest.skipUnless(shutil.which("lua"), "lua is not installed")
    def test_the_plugins_own_parser_takes_in_the_reviewed_lines(self):
        cards = (("qa", 'Q "1"', "A\nB"), ("qonly", "Q2", "typed"), ("def", "Define x", "y"))
        a = self.add_highlight("k1", text="the text", note="my note", cards=cards)
        self.store.update_card(self.store.list_cards(self.book_id, a["hid"])[0]["id"], front='Q "1"')
        b = self.add_highlight("k2", opened=False, cards=())
        self.store.set_skipped(self.book_id, b["hid"])
        ai = self.store.add_card(self.book_id, a["hid"], front="F", back="B", suggested={"front": "F0", "back": "B0"})
        self.assertEqual(ai["source"], "AI")
        save.save(self.store, self.book, [a, b], self.mount)
        text = self.reviewed.read_text("utf-8")
        decoded = {line: json.loads(line) for line in text.splitlines()}
        script = (
            'package.path = "koreader/pbanki.koplugin/?.lua;" .. package.path\n'
            'local cardgen = require("cardgen")\n'
            f"local decoded = {lua_literal(decoded)}\n"
            'local function decode(s) return decoded[s] or error("bad") end\n'
            f"local saves, remaining, malformed = cardgen.parseDesktopSaves({lua_literal(text)}, {lua_literal(FILE)}, decode)\n"
            "print(#saves, #remaining, #malformed)\n"
            "for _i, save in ipairs(saves) do\n"
            "  print(save.key, #save.cards)\n"
            "  for _j, c in ipairs(save.cards) do\n"
            '    print(c.front, c.quote, c.note, c.suggested and c.suggested.front or "-")\n'
            "  end\n"
            "end\n"
            "print(#cardgen.newHistoryEntries({}, saves))\n"
        )
        done = subprocess.run(["lua", "-"], input=script, text=True, capture_output=True, cwd=REPO)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            [line.split("\t") for line in done.stdout.splitlines()],
            [
                ["2", "0", "0"],
                ["k1", "3"],
                ['Q "1"', "the text", "my note", "-"],
                ["Q2", "the text", "my note", "-"],
                ["F", "the text", "my note", "F0"],
                ["k2", "0"],
                ["3"],
            ],
        )

    def test_the_real_koreader_sidecar_goes_through_to_the_right_lines(self):
        fixture = Path(__file__).parent / "fixtures" / "real_sidecar"
        (self.mount / "English" / "book.sdr").mkdir(parents=True, exist_ok=True)
        shutil.copy(fixture / "book.epub", self.mount / "English" / "book.epub")
        shutil.copy(fixture / "metadata.epub.lua", self.mount / "English" / "book.sdr" / "metadata.epub.lua")
        shutil.copy(fixture / "history.lua", self.mount / paths.KOREADER_DIR / "history.lua")
        before = snapshot(self.mount)

        loaded = reader.load_book(self.mount, FILE)
        highlights = loaded["highlights"]
        self.assertEqual(len(highlights), 7)
        book = {
            "id": self.store.upsert_book({k: loaded[k] for k in ("device_path", "title", "author", "stem", "ext")}),
            **loaded,
        }
        self.store.ensure_highlights(book["id"], [h["key"] for h in highlights])
        with_note = next(h for h in highlights if h["note"])
        two_paragraphs = next(h for h in highlights if "\n" in h["text"])
        for h in (with_note, two_paragraphs):
            hid = store_module.hid_for(h["key"])
            self.store.set_opened(book["id"], hid)
            self.store.add_card(book["id"], hid, front="F " + h["key"], back="B")
        connection = self.store.add_connection(
            book["id"], "T", "CF", "CB", [store_module.hid_for(h["key"]) for h in (with_note, two_paragraphs)]
        )
        self.store.open_connection(connection["id"])

        result = save.save(self.store, book, highlights, self.mount)
        self.assertEqual(result["status"], "verified")

        lines = [json.loads(l) for l in self.inbox.read_text("utf-8").splitlines()]
        self.assertEqual(len(lines), 3)
        by_front = {line["front"]: line for line in lines}
        for h in (with_note, two_paragraphs):
            line = by_front["F " + h["key"]]
            self.assertEqual(line["quote"], h["text"])
            self.assertIsInstance(line["page"], int)
            self.assertEqual(line["page"], h["page"])
            self.assertEqual(line["position"], h["pos0"])
            self.assertTrue(line["position"].startswith("/body/DocFragment["))
            self.assertEqual(line["chapter"], epub.chapter_card(h["chapter_number"], h["chapter"]))
            self.assertEqual(line["file"], FILE)
            self.assertEqual(line["book"], loaded["title"])
        self.assertEqual(by_front["CF"]["quote"].count("\n"), 1)
        self.assertNotIn("page", by_front["CF"])

        marks = [json.loads(l) for l in self.reviewed.read_text("utf-8").splitlines()]
        self.assertEqual([m["key"] for m in marks], [with_note["key"], two_paragraphs["key"]])
        self.assertEqual(marks[0]["cards"][0]["note"], with_note["note"])
        self.assertNotIn("note", marks[1]["cards"][0])

        # What the reader layer reads back counts them as reviewed, and nothing else moved
        again = reader.load_book(self.mount, FILE)["highlights"]
        reviewed = [h["key"] for h in again if h["status"] == "reviewed"]
        self.assertEqual(reviewed, [with_note["key"], two_paragraphs["key"]])
        after = snapshot(self.mount)
        del after[paths.INBOX_FILE], after[paths.REVIEWED_FILE]
        self.assertEqual(after, before)


class PipelineTests(SaveCase):
    def test_a_whole_save(self):
        highlights = self.work()
        result = save.save(self.store, self.book, highlights, self.mount)
        self.assertEqual(result["status"], "verified")
        self.assertIsNone(result["error"])
        batch = self.only_batch()
        self.assertEqual(batch["status"], "verified")
        self.assert_each_once(self.inbox, batch["inbox_lines"])
        self.assert_each_once(self.reviewed, batch["reviewed_lines"])
        self.assertEqual(len(batch["inbox_lines"]), 5)
        self.assertEqual(len(batch["reviewed_lines"]), 2)
        self.assertEqual(self.store.list_cards(self.book_id), [])
        self.assertEqual(self.store.list_connections(self.book_id), [])
        self.assertEqual(self.store.list_ideas(self.book_id), [])
        saved = self.store.list_saved_cards(self.book_id, highlights[0]["hid"])
        self.assertEqual([s["front"] for s in saved], ["Q1", "{{c1::x}} y"])
        self.assertEqual(save.pending_saves(self.store), [])

    def test_saved_cards_cannot_be_saved_twice(self):
        highlights = self.work()
        save.save(self.store, self.book, highlights, self.mount)
        with self.assertRaises(save.SaveBlocked):
            save.save(self.store, self.book, highlights, self.mount)
        self.assertEqual(len(self.store.list_batches()), 1)

    def test_nothing_else_on_the_reader_changes_in_a_whole_session(self):
        before = snapshot(self.mount)
        highlights = self.work()
        save.preview(self.store, self.book, highlights)
        save.save(self.store, self.book, highlights, self.mount)
        save.finish_pending(self.store, self.mount)
        more = self.add_highlight("k9")
        save.save(self.store, self.book, [*highlights, more], self.mount)
        save.reviewed_keys(self.store, self.book, {}, self.mount)
        after = snapshot(self.mount)
        for rel in (paths.INBOX_FILE, paths.REVIEWED_FILE):
            self.assertIn(rel, after)
            del after[rel]
        self.assertEqual(after, before)

    def test_a_failed_and_retried_session_changes_nothing_else_either(self):
        before = snapshot(self.mount)
        highlights = self.work()
        with mock.patch.object(device, "remount", side_effect=device.DeviceWriteError("busy")):
            save.save(self.store, self.book, highlights, self.mount)
        save.finish_pending(self.store, self.mount)
        after = snapshot(self.mount)
        del after[paths.INBOX_FILE], after[paths.REVIEWED_FILE]
        self.assertEqual(after, before)

    def test_cards_already_in_the_inbox_are_kept_and_a_missing_newline_mended(self):
        self.inbox.write_bytes(OLD_LINE)
        self.reviewed.write_bytes(b"other book line\n")
        h = self.add_highlight("k1")
        save.save(self.store, self.book, [h], self.mount)
        batch = self.only_batch()
        self.assert_each_once(self.inbox, batch["inbox_lines"], extra=[OLD_LINE])
        self.assert_each_once(self.reviewed, batch["reviewed_lines"], extra=[b"other book line"])

    def test_a_batch_with_no_marks_leaves_reviewed_alone(self):
        idea = self.store.add_idea(self.book_id, "C", "T", "", "", "F", "B")
        self.store.open_idea(idea["id"])
        result = save.save(self.store, self.book, [], self.mount)
        self.assertEqual(result["status"], "verified")
        self.assertTrue(self.inbox.exists())
        self.assertFalse(self.reviewed.exists())

    def test_two_save_clicks_at_once_make_one_batch(self):
        highlights = self.work()
        outcomes = []

        def click():
            try:
                outcomes.append(save.save(self.store, self.book, highlights, self.mount)["status"])
            except save.SaveBlocked:
                outcomes.append("blocked")
            finally:
                self.store.close()

        threads = [threading.Thread(target=click) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["blocked"] * 5 + ["verified"])
        batch = self.only_batch()
        self.assert_each_once(self.inbox, batch["inbox_lines"])

    def test_retry_and_the_mount_watcher_at_once_write_each_line_once(self):
        batch_id = save.back_up(self.store, self.book, self.work())
        results = []

        def finish():
            results.append(save.finish_batch(self.store, batch_id, self.mount)["status"])
            self.store.close()

        threads = [threading.Thread(target=finish) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, ["verified"] * 6)
        inbox_lines, reviewed_lines = self.batch_lines(batch_id)
        self.assert_each_once(self.inbox, inbox_lines)
        self.assert_each_once(self.reviewed, reviewed_lines)

    def test_extra_clicks_are_told_the_cards_are_being_or_have_been_saved(self):
        work = self.work()
        results = []

        def click():
            store = Store(self.db)
            try:
                results.append(save.save(store, self.book, work, self.mount)["status"])
            except save.SaveBlocked as e:
                results.append(str(e))
            finally:
                store.close()

        threads = [threading.Thread(target=click) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("verified"), 1, results)
        allowed = {"verified", "These cards are already being saved.", "These cards are already saved. There's nothing new to save."}
        self.assertLessEqual(set(results), allowed)
        self.assert_each_once(self.inbox, self.store.list_batches(self.book_id)[0]["inbox_lines"])

    def test_the_preview_shows_a_numbered_title_as_written(self):
        self.assertEqual(save._where({"chapter": "I", "chapter_number": 5, "page": 19}), "I · p. 19")
        self.assertEqual(save._where({"chapter": "Her nerves", "chapter_number": 5, "page": 19}), "Ch. 5 · p. 19")

    def test_a_late_edit_cannot_change_a_saved_card(self):
        h = self.add_highlight("k1")
        card = self.store.list_cards(self.book_id, h["hid"])[0]
        save.save(self.store, self.book, [h], self.mount)
        with self.assertRaises(store_module.NotEditable):
            self.store.update_card(card["id"], front="too late")

    def test_marked_highlights_lose_opened_and_skipped(self):
        a = self.add_highlight("k1")
        skipped = self.add_highlight("k2", opened=True, cards=())
        self.store.set_skipped(self.book_id, skipped["hid"])
        save.save(self.store, self.book, [a, skipped], self.mount)
        for h in (a, skipped):
            state = self.store.get_highlight(self.book_id, h["hid"])
            self.assertFalse(state["opened"] or state["skipped"], h["key"])

    def test_batches_of_several_books_finish_oldest_first(self):
        other = {"device_path": "/mnt/ext1/English/other.epub", "title": "Other", "author": ""}
        other_id = self.store.upsert_book(other)
        first = save.back_up(self.store, self.book, [self.add_highlight("k1")])
        h2 = highlight("k1b")
        self.store.ensure_highlights(other_id, ["k1b"])
        self.store.set_opened(other_id, h2["hid"])
        self.store.add_card(other_id, h2["hid"], front="other card", back="b")
        second = save.back_up(self.store, {**other, "id": other_id}, [h2])
        results = save.finish_pending(self.store, self.mount)
        self.assertEqual([r["batch"] for r in results], [first, second])
        self.assertEqual({r["status"] for r in results}, {"verified"})
        got = self.inbox.read_text("utf-8").splitlines()
        self.assertEqual(got, self.batch_lines(first)[0] + self.batch_lines(second)[0])


class UnpluggedTests(SaveCase):
    def setUp(self):
        super().setUp()
        self.away = self.mount.with_name("reader-away")

    def unplug(self):
        self.mount.rename(self.away)

    def replug(self):
        self.away.rename(self.mount)

    def test_unplugged_backs_up_and_waits_then_finishes_when_the_mount_appears(self):
        highlights = self.work()
        self.unplug()
        result = save.save(self.store, self.book, highlights, self.mount)
        self.assertEqual(result["status"], "waiting")
        self.assertIsNone(result["error"])
        self.assertFalse(self.mount.exists())
        batch = self.only_batch()
        self.assertEqual(batch["status"], "backed_up")
        self.assertEqual(self.store.list_cards(self.book_id), [])
        self.assertEqual(save.pending_saves(self.store)[0]["status"], "waiting")
        # Still waiting on another pass, and across a restart
        self.assertEqual(save.finish_pending(self.fresh_store(), self.mount)[0]["status"], "waiting")
        self.replug()
        results = save.finish_pending(self.fresh_store(), self.mount)
        self.assertEqual([r["status"] for r in results], ["verified"])
        self.assert_each_once(self.inbox, batch["inbox_lines"])
        self.assert_each_once(self.reviewed, batch["reviewed_lines"])
        self.assertEqual(save.finish_pending(self.store, self.mount), [])

    def test_no_mount_set_counts_as_unplugged(self):
        self.assertFalse(save.reader_connected(None))
        self.assertFalse(save.reader_connected(""))
        self.assertTrue(save.reader_connected(self.mount))

    def test_pulling_the_plug_mid_save_is_not_lost(self):
        highlights = self.work()
        away = self.away

        def pulled(_mount):
            self.mount.rename(away)
            raise device.DeviceWriteError("The reader disappeared while checking the files. Plug it in and try again.")

        with mock.patch.object(device, "remount", pulled):
            result = save.save(self.store, self.book, highlights, self.mount)
        self.assertEqual(result["status"], "failed")
        self.assertIn("disappeared", result["error"])
        # Unplugged, the sentence gives way to the waiting state
        self.assertEqual(save.finish_pending(self.store, self.mount)[0]["status"], "waiting")
        self.replug()
        self.assertEqual(save.finish_pending(self.store, self.mount)[0]["status"], "verified")
        batch = self.only_batch()
        self.assert_each_once(self.inbox, batch["inbox_lines"])

    def test_an_unverified_batch_counts_as_reviewed_until_it_is_on_the_reader(self):
        h = self.add_highlight("k1")
        self.unplug()
        save.save(self.store, self.book, [h], self.mount)
        self.assertEqual(save.reviewed_keys(self.store, self.book, {"side"}, self.mount), {"side", "k1"})
        # A new card on the same highlight isn't saved again while that batch is in flight
        self.store.set_opened(self.book_id, h["hid"])
        self.store.add_card(self.book_id, h["hid"], front="later", back="b")
        self.assertEqual(save.preview(self.store, self.book, [h])["count"], 0)

    def test_a_save_with_nothing_left_says_why(self):
        h = self.add_highlight("k1")
        self.unplug()
        save.save(self.store, self.book, [h], self.mount)
        with self.assertRaises(save.SaveBlocked) as caught:
            save.save(self.store, self.book, [h], self.mount)
        self.assertEqual(str(caught.exception), "These cards are already being saved.")

    def test_local_saved_cards_alone_do_not_count_as_reviewed(self):
        h = self.add_highlight("k1")
        save.save(self.store, self.book, [h], self.mount)
        self.assertEqual(save.reviewed_keys(self.store, self.book, {}, self.mount), {"k1"})
        # The plugin takes the line in, then "Review this book's highlights again" clears the sidecar
        self.reviewed.unlink()
        self.assertEqual(save.reviewed_keys(self.store, self.book, {}, self.mount), set())

    def test_reviewed_status_tolerates_a_bad_file_and_other_books(self):
        self.reviewed.write_bytes(
            b'not json\n\xff\xfe\n[1]\n{"v":1,"file":"/mnt/ext1/o.epub","key":"other"}\n'
            b'{"v":1,"file":"' + FILE.encode() + b'","key":"mine"}\r\n{"v":1,"file":"' + FILE.encode() + b'","key":3}\n'
        )
        self.assertEqual(save.reviewed_keys(self.store, self.book, {}, self.mount), {"mine"})
        self.assertEqual(save.reviewed_keys(self.store, self.book, {"side"}, None), {"side", "mine"})

    def unplug_only(self):
        away = self.mount.with_name("away")
        shutil.move(self.mount, away)
        self.addCleanup(lambda: shutil.move(away, self.mount) if away.exists() else None)
        return away

    def test_saved_highlights_stay_reviewed_after_the_reader_is_unplugged(self):
        h = self.add_highlight("k1")
        self.assertEqual(save.save(self.store, self.book, [h], self.mount)["status"], "verified")
        self.unplug_only()
        self.assertEqual(save.reviewed_keys(self.store, self.book, set(), self.mount), {"k1"})
        self.assertEqual(save.reviewed_keys(self.fresh_store(), self.book, set(), self.mount), {"k1"})
        self.assertEqual(save.reviewed_keys(self.store, self.book, set(), None), {"k1"})

    def test_what_reviewedjsonl_held_when_last_seen_counts_while_unplugged(self):
        self.reviewed.write_bytes(b'{"v":1,"file":"' + FILE.encode() + b'","key":"older"}\n')
        self.assertEqual(save.reviewed_keys(self.store, self.book, set(), self.mount), {"older"})
        self.unplug_only()
        self.assertEqual(save.reviewed_keys(self.store, self.book, set(), self.mount), {"older"})

    def test_reviewing_again_on_the_reader_makes_them_pending_here_and_stays_so_unplugged(self):
        h = self.add_highlight("k1")
        save.save(self.store, self.book, [h], self.mount)
        # The plugin takes the line in, then "Review this book's highlights again" clears the sidecar
        self.reviewed.unlink()
        self.assertEqual(save.reviewed_keys(self.store, self.book, set(), self.mount), set())
        self.unplug_only()
        self.assertEqual(save.reviewed_keys(self.store, self.book, set(), self.mount), set())

    def test_a_reader_gone_during_the_read_doesnt_clear_what_was_known(self):
        h = self.add_highlight("k1")
        save.save(self.store, self.book, [h], self.mount)
        with mock.patch.object(reader, "is_connected", side_effect=[True, False]), \
                mock.patch.object(reader, "reviewed_keys", return_value={}):
            self.assertEqual(save.reviewed_keys(self.store, self.book, set(), self.mount), {"k1"})
        self.assertEqual(self.store.reader_reviewed(self.book_id), {"k1"})


class CrashTests(SaveCase):
    """Every step can die. Afterwards no card may be lost or doubled and a retry must complete."""

    def setUp(self):
        super().setUp()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def assert_complete(self, batch_id, extra_inbox=()):
        batch = self.store.get_batch(batch_id)
        self.assertEqual(batch["status"], "verified")
        self.assertIsNone(batch["error"])
        self.assert_each_once(self.inbox, batch["inbox_lines"], extra=extra_inbox)
        self.assert_each_once(self.reviewed, batch["reviewed_lines"])

    def test_dying_during_the_backup_leaves_everything_as_it_was(self):
        for failure in (Crash(), RuntimeError("disk full")):
            with self.subTest(failure=type(failure).__name__):
                highlights = self.work() if not self.store.list_cards(self.book_id) else self.highlights
                self.highlights = highlights
                drafts = self.store.list_cards(self.book_id)
                real_write = self.store._write

                def dying_write(sql, args=()):
                    # The very last statement of the backup
                    if sql.startswith("UPDATE highlight_state SET opened = 0"):
                        raise failure
                    return real_write(sql, args)

                with mock.patch.object(self.store, "_write", dying_write):
                    with self.assertRaises(type(failure)):
                        save.back_up(self.store, self.book, highlights)
                self.fresh_store()
                self.assertEqual(self.store.list_batches(), [])
                self.assertEqual(self.store.list_cards(self.book_id), drafts)
                self.assertEqual(len(self.store.list_connections(self.book_id)), 1)
                self.assertEqual(len(self.store.list_ideas(self.book_id)), 1)
                self.assertFalse(self.inbox.exists())
        result = save.save(self.store, self.book, self.highlights, self.mount)
        self.assert_complete(result["batch"])

    def partial_flush(self, failure, on_file="inbox.jsonl"):
        """Writes half the file, truncating what was there, then fails."""
        real = device._flush_to_disk

        def flush(path, data):
            if path.name == on_file:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data[: len(data) // 2])
                raise failure
            real(path, data)

        return flush

    def test_dying_while_writing_keeps_older_cards_and_retry_completes(self):
        self.inbox.write_bytes(OLD_LINE + b"\n")
        batch_id = save.back_up(self.store, self.book, self.work())
        with mock.patch.object(device, "_flush_to_disk", self.partial_flush(Crash())):
            with self.assertRaises(Crash):
                save.finish_batch(self.store, batch_id, self.mount)
        self.assertEqual(self.store.get_batch(batch_id)["status"], "written")
        # The file really is cut short now
        self.assertLess(len(self.inbox.read_bytes().splitlines()), 6)
        store = self.fresh_store()
        self.assertEqual(save.finish_pending(store, self.mount)[0]["status"], "verified")
        self.assert_complete(batch_id, extra_inbox=[OLD_LINE])

    def test_a_write_cut_off_inside_the_older_lines_still_restores_them(self):
        older = [OLD_LINE, b'{"front":"second plugin card","back":"b"}', b'{"front":"third plugin card","back":"b"}']
        self.inbox.write_bytes(b"\n".join(older) + b"\n")
        batch_id = save.back_up(self.store, self.book, [self.add_highlight("k1")])
        real = device._flush_to_disk

        def flush(path, data):
            if path.name == "inbox.jsonl":
                path.write_bytes(data[:10])
                raise Crash()
            real(path, data)

        with mock.patch.object(device, "_flush_to_disk", flush):
            with self.assertRaises(Crash):
                save.finish_batch(self.store, batch_id, self.mount)
        self.assertEqual(self.inbox.read_bytes(), OLD_LINE[:10])
        save.finish_pending(self.fresh_store(), self.mount)
        self.assert_complete(batch_id, extra_inbox=older)

    def test_dying_with_an_emptied_file_restores_it(self):
        self.inbox.write_bytes(OLD_LINE + b"\n")
        batch_id = save.back_up(self.store, self.book, self.work())

        def flush(path, _data):
            path.write_bytes(b"")
            raise Crash()

        with mock.patch.object(device, "_flush_to_disk", flush):
            with self.assertRaises(Crash):
                save.finish_batch(self.store, batch_id, self.mount)
        save.finish_pending(self.fresh_store(), self.mount)
        self.assert_complete(batch_id, extra_inbox=[OLD_LINE])

    def test_dying_on_the_second_file_does_not_double_the_first(self):
        batch_id = save.back_up(self.store, self.book, self.work())
        with mock.patch.object(device, "_flush_to_disk", self.partial_flush(Crash(), on_file="reviewed.jsonl")):
            with self.assertRaises(Crash):
                save.finish_batch(self.store, batch_id, self.mount)
        self.assert_each_once(self.inbox, self.batch_lines(batch_id)[0])
        save.finish_pending(self.fresh_store(), self.mount)
        self.assert_complete(batch_id)

    def test_a_write_error_is_reported_and_try_again_works(self):
        self.inbox.write_bytes(OLD_LINE + b"\n")
        batch_id = save.back_up(self.store, self.book, self.work())
        with mock.patch.object(device, "_flush_to_disk", self.partial_flush(OSError(28, "No space left on device"))):
            result = save.finish_batch(self.store, batch_id, self.mount)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "Could not write to the reader: No space left on device")
        self.assertEqual(save.pending_saves(self.store)[0]["status"], "failed")
        self.assertEqual(save.finish_batch(self.store, batch_id, self.mount)["status"], "verified")
        self.assert_complete(batch_id, extra_inbox=[OLD_LINE])

    def test_dying_at_the_remount_does_not_double_anything(self):
        for failure in (Crash(), device.DeviceWriteError("Could not check the files."), RuntimeError("boom")):
            with self.subTest(failure=type(failure).__name__):
                book_path = f"/mnt/ext1/English/{type(failure).__name__}.epub"
                book = {"device_path": book_path, "title": "T", "author": ""}
                book["id"] = self.store.upsert_book(book)
                h = highlight("k-" + type(failure).__name__)
                self.store.ensure_highlights(book["id"], [h["key"]])
                self.store.set_opened(book["id"], h["hid"])
                self.store.add_card(book["id"], h["hid"], front="F", back="B")
                batch_id = save.back_up(self.store, book, [h])
                with mock.patch.object(device, "remount", side_effect=failure):
                    if isinstance(failure, Crash):
                        with self.assertRaises(Crash):
                            save.finish_batch(self.store, batch_id, self.mount)
                    else:
                        result = save.finish_batch(self.store, batch_id, self.mount)
                        self.assertEqual(result["status"], "failed")
                        self.assertTrue(result["error"])
                        self.assertNotIn("Traceback", result["error"])
                self.assertEqual(self.store.get_batch(batch_id)["status"], "written")
                before = self.inbox.read_bytes()
                store = self.fresh_store()
                self.assertEqual(save.finish_batch(store, batch_id, self.mount)["status"], "verified")
                self.assertEqual(self.inbox.read_bytes(), before)
                self.assertEqual(self.inbox.read_text("utf-8").count(f'"batch":"{batch_id}"'), 1)

    def test_a_write_that_never_lands_fails_loudly_and_keeps_the_batch(self):
        batch_id = save.back_up(self.store, self.book, self.work())
        with mock.patch.object(device, "_on_disk", return_value=b"something else"):
            result = save.finish_batch(self.store, batch_id, self.mount)
        self.assertEqual(result["status"], "failed")
        self.assertIn("did not land", result["error"])
        self.assertEqual(save.finish_batch(self.store, batch_id, self.mount)["status"], "verified")
        self.assert_complete(batch_id)

    def test_files_that_vanish_after_the_remount_are_written_again(self):
        batch_id = save.back_up(self.store, self.book, self.work())
        vanished = []

        def remount(_mount):
            if not vanished:
                vanished.append(True)
                self.inbox.unlink()

        with mock.patch.object(device, "remount", remount):
            self.assertEqual(save.finish_batch(self.store, batch_id, self.mount)["status"], "verified")
        self.assert_complete(batch_id)

    def test_lines_vanished_between_attempts_are_added_again(self):
        batch_id = save.back_up(self.store, self.book, self.work())
        with mock.patch.object(device, "remount", side_effect=Crash()):
            with self.assertRaises(Crash):
                save.finish_batch(self.store, batch_id, self.mount)
        # What the FAT disk did in the recorded incidents
        self.inbox.unlink()
        self.reviewed.unlink()
        save.finish_pending(self.fresh_store(), self.mount)
        self.assert_complete(batch_id)

    def test_lines_already_on_the_reader_are_not_added_again(self):
        batch_id = save.back_up(self.store, self.book, self.work())
        inbox_lines, reviewed_lines = self.batch_lines(batch_id)
        self.inbox.write_text(OLD_LINE.decode() + "\n" + "\n".join(inbox_lines) + "\n", "utf-8")
        self.reviewed.write_text("\n".join(reviewed_lines) + "\n", "utf-8")
        self.assertEqual(self.store.get_batch(batch_id)["status"], "backed_up")
        with mock.patch.object(device, "write_files", wraps=device.write_files) as write_files:
            result = save.finish_batch(self.store, batch_id, self.mount)
        self.assertEqual(result["status"], "verified")
        # Still proven: being there in this Mac's cache is no proof
        self.assertEqual(write_files.call_count, 1)
        self.assert_complete(batch_id, extra_inbox=[OLD_LINE])

    def test_only_the_lines_missing_from_a_batch_are_added(self):
        batch_id = save.back_up(self.store, self.book, self.work())
        inbox_lines, reviewed_lines = self.batch_lines(batch_id)
        self.inbox.write_text(inbox_lines[0] + "\n" + inbox_lines[1] + "\n", "utf-8")
        self.assertEqual(save.finish_batch(self.store, batch_id, self.mount)["status"], "verified")
        self.assert_complete(batch_id)

    def test_a_batch_with_two_identical_cards_keeps_both(self):
        h = self.add_highlight("k1", cards=(("qa", "Q", "A"), ("qa", "Q", "A")))
        batch_id = save.back_up(self.store, self.book, [h])
        lines = self.batch_lines(batch_id)[0]
        self.assertEqual(lines[0], lines[1])
        self.inbox.write_text(lines[0] + "\n", "utf-8")
        save.finish_batch(self.store, batch_id, self.mount)
        self.assert_each_once(self.inbox, lines)

    def test_lines_pbanki_is_importing_or_could_not_import_are_not_added_again(self):
        batch_id = save.back_up(self.store, self.book, self.work())
        inbox_lines = self.batch_lines(batch_id)[0]
        (self.mount / save.IMPORTING_FILE).write_text("\n".join(inbox_lines[:2]) + "\n", "utf-8")
        (self.mount / save.FAILED_FILE).write_text(inbox_lines[2] + "\n", "utf-8")
        self.assertEqual(save.finish_batch(self.store, batch_id, self.mount)["status"], "verified")
        self.assertEqual(self.inbox.read_text("utf-8").splitlines(), inbox_lines[3:])

    def test_new_lines_after_a_crash_are_kept_on_retry(self):
        batch_id = save.back_up(self.store, self.book, self.work())
        with mock.patch.object(device, "remount", side_effect=Crash()):
            with self.assertRaises(Crash):
                save.finish_batch(self.store, batch_id, self.mount)
        # The plugin added a card while the reader was away from this Mac
        with self.inbox.open("ab") as f:
            f.write(OLD_LINE + b"\n")
        save.finish_pending(self.fresh_store(), self.mount)
        batch = self.store.get_batch(batch_id)
        self.assertEqual(
            self.inbox.read_bytes().splitlines(), [*(l.encode() for l in batch["inbox_lines"]), OLD_LINE]
        )

    def test_the_plugin_taking_its_lines_between_attempts_is_harmless(self):
        batch_id = save.back_up(self.store, self.book, self.work())
        with mock.patch.object(device, "remount", side_effect=Crash()):
            with self.assertRaises(Crash):
                save.finish_batch(self.store, batch_id, self.mount)
        self.reviewed.unlink()
        save.finish_pending(self.fresh_store(), self.mount)
        self.assert_complete(batch_id)

    @unittest.skipIf(os.geteuid() == 0, "root can read anything")
    def test_an_unreadable_inbox_is_not_overwritten(self):
        self.inbox.write_bytes(OLD_LINE + b"\n")
        batch_id = save.back_up(self.store, self.book, self.work())
        self.inbox.chmod(0)
        try:
            result = save.finish_batch(self.store, batch_id, self.mount)
        finally:
            self.inbox.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Could not read the reader", result["error"])
        self.assertEqual(self.inbox.read_bytes(), OLD_LINE + b"\n")
        self.assertEqual(save.finish_batch(self.store, batch_id, self.mount)["status"], "verified")
        self.assert_complete(batch_id, extra_inbox=[OLD_LINE])

    def test_a_reader_without_pbanki_gets_no_new_folder(self):
        h = self.add_highlight("k1")
        (self.mount / paths.PBANKI_DIR / "config.toml").unlink()
        for path in sorted((self.mount / paths.PBANKI_DIR).rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        (self.mount / paths.PBANKI_DIR).rmdir()
        before = snapshot(self.mount)
        result = save.save(self.store, self.book, [h], self.mount)
        self.assertEqual(result["status"], "failed")
        self.assertIn("doesn't have pbAnki", result["error"])
        self.assertEqual(snapshot(self.mount), before)
        (self.mount / paths.PBANKI_DIR).mkdir()
        self.assertEqual(save.finish_pending(self.store, self.mount)[0]["status"], "verified")
        self.assert_each_once(self.inbox, self.only_batch()["inbox_lines"])

    def test_finishing_an_unknown_batch_is_an_error(self):
        with self.assertRaises(store_module.NotFound):
            save.finish_batch(self.store, "nope", self.mount)

    def test_finishing_a_verified_batch_writes_nothing(self):
        h = self.add_highlight("k1")
        result = save.save(self.store, self.book, [h], self.mount)
        with mock.patch.object(device, "write_files") as write_files:
            self.assertEqual(save.finish_batch(self.store, result["batch"], self.mount)["status"], "verified")
        write_files.assert_not_called()


class ProcessDeathTests(SaveCase):
    """The same crashes with a real process that exits on the spot, with no cleanup at all."""

    SCRIPT = textwrap.dedent(
        """
        import os, sys
        sys.path.insert(0, sys.argv[1])
        from pathlib import Path
        from pbanki_desktop import device, save
        from pbanki_desktop.store import Store

        where, db, mount, book_id, key = sys.argv[2:7]
        mount = Path(mount)
        store = Store(db)
        real_flush = device._flush_to_disk
        real_write = store._write

        def die():
            os._exit(9)

        if where == "backup":
            def dying_write(sql, args=()):
                if sql.startswith("UPDATE highlight_state SET opened = 0"):
                    die()
                return real_write(sql, args)
            store._write = dying_write
        elif where == "writing":
            def flush(path, data):
                path.write_bytes(data[: len(data) // 2])
                die()
            device._flush_to_disk = flush
        elif where == "second file":
            def flush(path, data):
                if path.name == "reviewed.jsonl":
                    die()
                real_flush(path, data)
            device._flush_to_disk = flush
        elif where == "remount":
            device.remount = lambda _mount: die()
        elif where == "after verifying":
            store.mark_batch_verified = lambda _id: die()

        book = {"id": book_id, "device_path": "/mnt/ext1/English/book.epub", "title": "T", "author": ""}
        save.save(store, book, [{"key": key, "text": "Text"}], mount)
        os._exit(0)
        """
    )

    def test_a_real_crash_at_each_step_loses_nothing_and_doubles_nothing(self):
        for where in ("backup", "writing", "second file", "remount", "after verifying"):
            with self.subTest(where=where):
                base = Path(tempfile.mkdtemp(dir=self.tmp.name))
                db = base / "pbanki.db"
                mount = base / "reader"
                (mount / paths.PBANKI_DIR).mkdir(parents=True)
                inbox = mount / paths.INBOX_FILE
                inbox.write_bytes(OLD_LINE + b"\n")
                store = Store(db)
                book_id = store.upsert_book(BOOK)
                book = {**BOOK, "id": book_id}
                key = "k1"
                hid = store.ensure_highlights(book_id, [key])[key]
                store.set_opened(book_id, hid)
                store.add_card(book_id, hid, front="Q1", back="A1")
                store.add_card(book_id, hid, front="Q2", back="A2")
                store.close()

                done = subprocess.run(
                    [sys.executable, "-c", self.SCRIPT, str(REPO / "desktop"), where, str(db), str(mount), book_id, key],
                    capture_output=True,
                    env={**os.environ, "PBANKI_SYNC_DELAY": "0"},
                )
                self.assertEqual(done.returncode, 9, done.stderr.decode())

                store = Store(db)
                self.addCleanup(store.close)
                if where == "backup":
                    self.assertEqual(store.list_batches(), [])
                    self.assertEqual(len(store.list_cards(book_id)), 2)
                    self.assertEqual(inbox.read_bytes(), OLD_LINE + b"\n")
                    result = save.save(store, book, [highlight(key, text="Text")], mount)
                else:
                    self.assertEqual(store.list_cards(book_id), [])
                    results = save.finish_pending(store, mount)
                    self.assertEqual(len(results), 1)
                    result = results[0]
                self.assertEqual(result["status"], "verified")
                batch = store.get_batch(result["batch"])
                self.assertEqual(batch["status"], "verified")
                expected = [OLD_LINE, *(l.encode() for l in batch["inbox_lines"])]
                self.assertEqual(inbox.read_bytes().splitlines(), expected)
                self.assertEqual(len(batch["inbox_lines"]), 2)
                reviewed = (mount / paths.REVIEWED_FILE).read_bytes().splitlines()
                self.assertEqual(reviewed, [l.encode() for l in batch["reviewed_lines"]])
                self.assertEqual(len(reviewed), 1)
                store.close()


class PluginUpdateTests(SaveCase):
    def setUp(self):
        super().setUp()
        self.source = REPO / "koreader" / "pbanki.koplugin"
        self.target = self.mount / paths.PLUGIN_DIR
        self.expected = sorted(
            p.relative_to(self.source).as_posix() for p in self.source.rglob("*") if p.is_file()
        )

    def test_copies_the_whole_plugin_with_its_built_in_prompts(self):
        before = snapshot(self.mount)
        result = save.plugin_update(self.mount)
        self.assertEqual(result, {"copied": self.expected, "unchanged": 0})
        self.assertIn("prompts/Standard/questions.md", result["copied"])
        for rel in self.expected:
            self.assertEqual((self.target / rel).read_bytes(), (self.source / rel).read_bytes(), rel)
        after = snapshot(self.mount)
        rest = {k: v for k, v in after.items() if not k.startswith(paths.PLUGIN_DIR)}
        self.assertEqual(rest, before)
        self.assertEqual(
            (self.mount / "applications/pbanki/prompts/Standard/questions.md").read_bytes(), b"edited by the plugin"
        )

    def test_files_that_already_match_are_skipped_and_all_go_in_one_write(self):
        save.plugin_update(self.mount)
        (self.target / "main.lua").write_bytes(b"old version")
        (self.target / "cardgen.lua").unlink()
        with mock.patch.object(device, "write_files", wraps=device.write_files) as write_files:
            result = save.plugin_update(self.mount)
        self.assertEqual(write_files.call_count, 1)
        self.assertEqual(result["copied"], ["cardgen.lua", "main.lua"])
        self.assertEqual(result["unchanged"], len(self.expected) - 2)
        self.assertEqual(set(write_files.call_args.args[1]), {self.target / "cardgen.lua", self.target / "main.lua"})
        self.assertEqual((self.target / "main.lua").read_bytes(), (self.source / "main.lua").read_bytes())

    def test_a_second_update_writes_nothing_at_all(self):
        save.plugin_update(self.mount)
        with mock.patch.object(device, "write_files") as write_files:
            result = save.plugin_update(self.mount)
        write_files.assert_not_called()
        self.assertEqual(result, {"copied": [], "unchanged": len(self.expected)})

    def test_new_files_that_vanish_after_the_remount_are_copied_again(self):
        gone = []

        def remount(_mount):
            if not gone:
                gone.append(True)
                (self.target / "prompts/Standard/cloze.md").unlink()
                (self.target / "main.lua").unlink()

        with mock.patch.object(device, "remount", remount):
            save.plugin_update(self.mount)
        for rel in self.expected:
            self.assertEqual((self.target / rel).read_bytes(), (self.source / rel).read_bytes(), rel)

    def test_files_on_the_reader_the_repo_doesnt_have_are_left_alone(self):
        self.target.mkdir(parents=True)
        (self.target / "extra.lua").write_bytes(b"mine")
        save.plugin_update(self.mount)
        self.assertEqual((self.target / "extra.lua").read_bytes(), b"mine")

    def test_system_junk_is_not_copied(self):
        source = Path(self.tmp.name) / "plugin"
        (source / "__pycache__").mkdir(parents=True)
        (source / "main.lua").write_bytes(b"x")
        (source / ".DS_Store").write_bytes(b"x")
        (source / "._main.lua").write_bytes(b"x")
        (source / "__pycache__" / "a.pyc").write_bytes(b"x")
        self.assertEqual(save.plugin_update(self.mount, source)["copied"], ["main.lua"])

    def test_fails_in_plain_words_without_a_reader_or_koreader(self):
        with self.assertRaisesRegex(device.DeviceWriteError, "Plug in"):
            save.plugin_update(self.mount / "missing")
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(device.DeviceWriteError, "KOReader isn't on this reader"):
            save.plugin_update(empty)
        self.assertEqual(list(empty.iterdir()), [])


class EjectTests(unittest.TestCase):
    MOUNT = "/Volumes/NotAReader"

    def test_a_fake_reader_folder_is_not_ejected(self):
        with mock.patch.object(device, "_run_diskutil") as diskutil:
            save.eject("/tmp/fake-reader")
        diskutil.assert_not_called()

    def test_a_real_disk_is_ejected(self):
        with mock.patch.object(device, "_run_diskutil") as diskutil:
            diskutil.return_value = subprocess.CompletedProcess([], 0)
            save.eject(self.MOUNT)
        diskutil.assert_called_once_with("eject", self.MOUNT)

    def test_a_busy_disk_is_reported_in_plain_words(self):
        for outcome in (
            {"return_value": subprocess.CompletedProcess([], 1)},
            {"side_effect": subprocess.TimeoutExpired("diskutil", 60)},
            {"side_effect": FileNotFoundError("diskutil")},
        ):
            with mock.patch.object(device, "_run_diskutil", **outcome):
                with self.assertRaisesRegex(save.EjectError, "still using the reader"):
                    save.eject(self.MOUNT)


def make_collection(path, field_rows):
    """A file with the notes table of pbAnki's collection.anki2, as far as this code reads it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, flds TEXT NOT NULL)")
    connection.executemany("INSERT INTO notes(flds) VALUES (?)", [(row,) for row in field_rows])
    connection.commit()
    connection.close()


class ImportedSinceTests(SaveCase):
    """pbAnki imports and clears the inbox while a batch waits for Try again."""

    def setUp(self):
        super().setUp()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.collection = self.mount / save.COLLECTION_FILE

    def failed_check(self, highlights=None):
        highlights = highlights or [self.add_highlight("k1")]
        error = device.DeviceWriteError("Could not check the files, because something on this Mac is using the reader.")
        with mock.patch.object(device, "remount", side_effect=error):
            result = save.save(self.store, self.book, highlights, self.mount)
        self.assertEqual(result["status"], "failed")
        return result["batch"]

    def import_lines(self, count=None):
        """What pbAnki does: adds the notes, then removes the inbox."""
        lines = self.inbox.read_text("utf-8").splitlines()
        make_collection(self.collection, [save._note_fields(line) for line in lines[:count]])
        self.inbox.unlink()
        return lines

    def test_cards_imported_after_a_failed_check_are_not_queued_again(self):
        batch_id = self.failed_check()
        self.import_lines()
        before = snapshot(self.mount / "applications" / "pbanki" / "collection")
        result = save.finish_batch(self.store, batch_id, self.mount)
        self.assertEqual(result["status"], "verified")
        self.assertFalse(self.inbox.exists())
        self.assertEqual(before, snapshot(self.mount / "applications" / "pbanki" / "collection"), "only read")

    def test_only_the_cards_not_imported_are_queued_again(self):
        batch_id = self.failed_check([self.add_highlight("k1", cards=(("qa", "Q1", "A"), ("qa", "Q2", "A"), ("qa", "Q3", "A")))])
        lines = self.import_lines(count=1)
        save.finish_batch(self.store, batch_id, self.mount)
        self.assertEqual(self.inbox.read_text("utf-8").splitlines(), lines[1:])

    def test_identical_cards_are_counted(self):
        batch_id = self.failed_check([self.add_highlight("k1", cards=(("qa", "Q", "A"), ("qa", "Q", "A")))])
        lines = self.import_lines(count=1)
        save.finish_batch(self.store, batch_id, self.mount)
        self.assertEqual(self.inbox.read_text("utf-8").splitlines(), lines[1:])

    def test_a_card_lost_by_the_disk_is_written_again(self):
        batch_id = self.failed_check()
        lines = self.inbox.read_text("utf-8").splitlines()
        make_collection(self.collection, ["other\x1fnote"])
        self.inbox.unlink()
        save.finish_batch(self.store, batch_id, self.mount)
        self.assertEqual(self.inbox.read_text("utf-8").splitlines(), lines)

    def test_no_collection_or_a_damaged_one_writes_the_cards_again(self):
        batch_id = self.failed_check()
        lines = self.inbox.read_text("utf-8").splitlines()
        self.inbox.unlink()
        self.assertEqual(save._in_collection(self.mount, lines), [])
        self.collection.parent.mkdir(parents=True)
        self.collection.write_bytes(b"not a database")
        self.assertEqual(save.finish_batch(self.store, batch_id, self.mount)["status"], "verified")
        self.assertEqual(self.inbox.read_text("utf-8").splitlines(), lines)

    def test_a_first_attempt_doesnt_look_in_the_collection(self):
        h = self.add_highlight("k1")
        with mock.patch.object(save, "_in_collection") as look:
            save.save(self.store, self.book, [h], self.mount)
        look.assert_not_called()

    def test_recent_notes_still_in_the_write_ahead_log_are_seen(self):
        batch_id = self.failed_check()
        lines = self.inbox.read_text("utf-8").splitlines()
        self.inbox.unlink()
        connection = sqlite3.connect(self.mount / "wal.db")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, flds TEXT NOT NULL)")
        connection.execute("INSERT INTO notes(flds) VALUES (?)", (save._note_fields(lines[0]),))
        connection.commit()
        self.collection.parent.mkdir(parents=True)
        shutil.copyfile(self.mount / "wal.db", self.collection)
        shutil.copyfile(self.mount / "wal.db-wal", f"{self.collection}-wal")
        connection.close()
        (self.mount / "wal.db").unlink()
        self.assertEqual(save.finish_batch(self.store, batch_id, self.mount)["status"], "verified")
        self.assertFalse(self.inbox.exists())

    def test_fields_match_the_rust_tests_expectations(self):
        # The values imports_the_lines_the_desktop_app_writes in common/src/inbox.rs asserts
        line = json.dumps({
            "front": "  A <b> & c\nd ", "back": "", "quote": "q\nr", "book": "B & C", "author": "A", "chapter": "Ch",
            "page": 35, "file": "/mnt/ext1/x.epub", "position": "/body/DocFragment[2]/text().0", "batch": "z",
        })
        self.assertEqual(save._note_fields(line).split("\x1f"), [
            "A &lt;b&gt; &amp; c<br>d", "", "q<br>r", "B &amp; C", "A", "Ch", "35", "/mnt/ext1/x.epub",
            "/body/DocFragment[2]/text().0",
        ])
        self.assertEqual(save._note_fields(json.dumps({"front": "e\u0301\u0001"})).split("\x1f")[0], "é")


class CutOffWriteTests(SaveCase):
    def setUp(self):
        super().setUp()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.older = OLD_LINE + b"\n" + b'{"front":"second plugin card","back":"b"}\n'

    def cut_after(self, count):
        """Real writes stopped after `count` more bytes, as with a full disk or a pulled cable."""
        real_write = os.fdopen

        def fdopen(fd, mode):
            f = real_write(fd, mode)

            class Short:
                def __getattr__(self, name):
                    return getattr(f, name)

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    f.close()

                def write(self, data):
                    f.write(data[:count])
                    f.flush()
                    raise OSError(errno.ENOSPC, "No space left on device")

            return Short()

        return mock.patch.object(device.os, "fdopen", fdopen)

    def test_older_cards_survive_a_write_cut_off_even_if_pbanki_imports_before_the_retry(self):
        self.inbox.write_bytes(self.older)
        h = self.add_highlight("k1")
        with self.cut_after(3):
            result = save.save(self.store, self.book, [h], self.mount)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(self.inbox.read_bytes().startswith(self.older), "older lines are never touched")
        lines = self.store.get_batch(result["batch"])["inbox_lines"]
        self.inbox.unlink()
        self.assertEqual(save.finish_pending(self.store, self.mount)[0]["status"], "verified")
        self.assertEqual(self.inbox.read_text("utf-8").splitlines(), lines)

    def test_a_retry_with_the_file_still_there_finishes_it(self):
        self.inbox.write_bytes(self.older)
        h = self.add_highlight("k1")
        with self.cut_after(3):
            result = save.save(self.store, self.book, [h], self.mount)
        save.finish_pending(self.store, self.mount)
        lines = self.store.get_batch(result["batch"])["inbox_lines"]
        self.assertEqual(self.inbox.read_bytes(), self.older + lines[0].encode("utf-8") + b"\n")

    def other_book(self):
        other = {"device_path": "/mnt/ext1/English/Other.epub", "title": "Other", "author": "A"}
        other_id = self.store.upsert_book(other)
        h = highlight("x1")
        self.store.ensure_highlights(other_id, ["x1"])
        self.store.set_opened(other_id, h["hid"])
        self.store.add_card(other_id, h["hid"], front="Other Q", back="A")
        return {**other, "id": other_id}, [h]

    def test_saving_another_book_finishes_the_older_batch_first(self):
        # A write that had already cut into the older lines, as the code before this fix could
        self.inbox.write_bytes(self.older)
        h = self.add_highlight("k1")
        real = device._flush_to_disk

        def cut(path, data):
            path.write_bytes(data[:len(self.older) // 2 + 3])
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(device, "_flush_to_disk", cut):
            first = save.save(self.store, self.book, [h], self.mount)
        self.assertEqual(first["status"], "failed")
        self.assertIs(device._flush_to_disk, real)
        book, highlights = self.other_book()
        self.assertEqual(save.save(self.store, book, highlights, self.mount)["status"], "verified")
        self.assertEqual(save.pending_saves(self.store), [])
        lines = self.inbox.read_bytes().splitlines()
        self.assertEqual(lines[:2], self.older.splitlines())
        self.assertEqual(len(lines), 4)
        for line in lines:
            json.loads(line)

    def test_a_save_after_a_write_that_fails_again_drops_the_broken_line_and_keeps_every_card(self):
        self.inbox.write_bytes(self.older)
        h = self.add_highlight("k1")
        with self.cut_after(3):
            first = save.save(self.store, self.book, [h], self.mount)
        book, highlights = self.other_book()
        attempts = []
        real = device.write_files

        def fail_first_then_work(mount, files):
            attempts.append(1)
            if len(attempts) == 1:
                raise device.DeviceWriteError("Could not write to the reader: No space left on device")
            real(mount, files)

        with mock.patch.object(device, "write_files", fail_first_then_work):
            second = save.save(self.store, book, highlights, self.mount)
        self.assertEqual(second["status"], "verified")
        self.assertEqual(save.batch_result(self.store.get_batch(first["batch"]))["status"], "failed")
        for line in self.inbox.read_bytes().splitlines():
            json.loads(line)
        self.assertEqual(save.finish_pending(self.store, self.mount)[0]["status"], "verified")
        lines = self.inbox.read_bytes().splitlines()
        self.assertEqual(lines[:2], self.older.splitlines())
        self.assertEqual(len(lines), 4)
        self.assertEqual(len(set(lines)), 4)


class FindingsTests(SaveCase):
    def test_cards_on_skipped_highlights_are_named_and_retired_by_the_save(self):
        a = self.add_highlight("k1", cards=(("qa", "Typed", ""), ("qa", "  ", "")))
        self.store.set_skipped(self.book_id, a["hid"])
        b = self.add_highlight("k2")
        preview = save.preview(self.store, self.book, [a, b])
        self.assertEqual(preview["count"], 1)
        self.assertIn("1 card on a skipped highlight will not be saved. Undo the skip to keep it.", preview["left_text"])
        c = self.add_highlight("k3", cards=(("qa", "One", ""), ("qa", "Two", "")))
        self.store.set_skipped(self.book_id, c["hid"])
        preview = save.preview(self.store, self.book, [a, b, c])
        self.assertIn("3 cards on skipped highlights will not be saved. Undo the skip to keep them.", preview["left_text"])
        self.assertEqual(save.save(self.store, self.book, [a, b, c], self.mount)["status"], save.VERIFIED)
        self.assertEqual(self.store.list_cards(self.book_id), [])
        self.assertEqual(self.store.draft_counts(), {})
        removed = self.store.list_cards(self.book_id, states=("removed",))
        self.assertEqual(sorted(card["front"] for card in removed), ["  ", "One", "Two", "Typed"])
        self.assertNotIn(b"Typed", self.inbox.read_bytes())

    def test_a_batch_remembers_what_the_preview_showed(self):
        highlights = self.work()
        preview = save.preview(self.store, self.book, highlights)
        result = save.save(self.store, self.book, highlights, self.mount)
        self.assertEqual(result["summary"]["groups"], preview["groups"])

    def test_with_held_adds_batches_to_a_preview(self):
        a = self.add_highlight("k1", cards=(("qa", "Q1", "A1"),))
        held = save.batch_result(self.store.get_batch(save.back_up(self.store, self.book, [a])))
        b = self.add_highlight("k2", cards=(("qa", "Q2", "A2"),))
        fresh = save.preview(self.store, self.book, [a, b])
        self.assertEqual((fresh["count"], fresh["mark_count"]), (1, 1))
        both = save.with_held(fresh, [held])
        self.assertEqual((both["count"], both["noun"], both["mark_count"]), (2, "cards ready", 2))
        self.assertEqual(both["mark_text"], "Mark 2 highlights as reviewed")
        self.assertEqual([g["head"] for g in both["groups"]], [g["head"] for g in held["summary"]["groups"] + fresh["groups"]])
        self.assertEqual(save.with_held(fresh, []), fresh)

    def test_an_older_batch_without_groups_still_counts(self):
        a = self.add_highlight("k1", cards=())
        fresh = save.preview(self.store, self.book, [a])
        both = save.with_held(fresh, [{"summary": {"cards": 2, "marked": 1}}])
        self.assertEqual((both["count"], both["mark_count"], both["groups"]), (2, 1, []))

    def test_an_unreadable_reviewed_file_keeps_the_last_seen_keys(self):
        self.reviewed.write_text(json.dumps({"v": 1, "file": FILE, "key": "k1", "cards": []}) + "\n", encoding="utf-8")
        self.assertEqual(save.reviewed_keys(self.store, self.book, (), self.mount), {"k1"})
        os.chmod(self.reviewed, 0)
        self.addCleanup(os.chmod, self.reviewed, 0o644)
        self.assertEqual(save.reviewed_keys(self.store, self.book, (), self.mount), {"k1"})
        self.assertEqual(self.store.reader_reviewed(self.book_id), {"k1"})

    def test_a_missing_reviewed_file_still_counts_as_none(self):
        self.reviewed.write_text(json.dumps({"v": 1, "file": FILE, "key": "k1", "cards": []}) + "\n", encoding="utf-8")
        save.reviewed_keys(self.store, self.book, (), self.mount)
        self.reviewed.unlink()
        self.assertEqual(save.reviewed_keys(self.store, self.book, (), self.mount), set())

    def test_the_plugin_update_mark_lasts_until_an_update_is_proven(self):
        self.assertFalse(save.plugin_update_cut_off())
        with mock.patch.object(device, "write_files", side_effect=device.DeviceWriteError("It failed.")):
            with self.assertRaises(device.DeviceWriteError):
                save.plugin_update(self.mount)
        self.assertTrue(save.plugin_update_cut_off())
        save.plugin_update(self.mount)
        self.assertFalse(save.plugin_update_cut_off())

    def test_an_update_with_nothing_to_copy_clears_the_mark(self):
        save.plugin_update(self.mount)
        with mock.patch.object(device, "write_files", side_effect=KeyboardInterrupt):
            (self.mount / paths.PLUGIN_DIR / "main.lua").write_bytes(b"cut")
            with self.assertRaises(KeyboardInterrupt):
                save.plugin_update(self.mount)
        self.assertTrue(save.plugin_update_cut_off())
        (self.mount / paths.PLUGIN_DIR / "main.lua").write_bytes((REPO / "koreader/pbanki.koplugin/main.lua").read_bytes())
        self.assertEqual(save.plugin_update(self.mount)["copied"], [])
        self.assertFalse(save.plugin_update_cut_off())


if __name__ == "__main__":
    unittest.main()
