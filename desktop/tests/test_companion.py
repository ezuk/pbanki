"""companion.py: slicer parity with Lua, real Companions, and the Create flow on fake folders."""

import importlib.util
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import lua_parity  # noqa: E402
from pbanki_desktop import companion, device  # noqa: E402

REAL = lua_parity.ASSISTANT / "companions"
WILLPOWER = REAL / "The Willpower Instinct - Kelly McGonigal.companion.md"
ANNE = REAL / "Anne of the Island - L. M. Montgomery.companion.md"
SKILL_SCRIPTS = lua_parity.ASSISTANT / ".claude" / "skills" / "book-companion" / "scripts"
FAKE_AI = Path(__file__).resolve().parent / "fixtures" / "fake_ai"
has_real = unittest.skipUnless(WILLPOWER.is_file() and ANNE.is_file(), "the assistant repo's Companions aren't here")

SYNTHETIC = """# Title
intro line

## Section

### Chapter One <!-- pos: 0.1000 -->
- one bullet
_Notes:_
- one note

#### Deep heading <!-- pos: 0.9000 -->
deep body

### Chapter Two <!-- pos: 0.5000 -->
- two bullet
↳ _Arc (spoiler, Ch. 3):_ she wins. <!-- pos: 0.8000 -->
_Notes:_
- two note

### Unmarked block
kept body

### Bad marker <!-- pos: 1.5000 -->
- bad bullet

### Hex marker <!-- pos: 0x1 -->
- hex bullet

### Chapter Three <!-- pos: 0.8000 -->
- three bullet

## Later section
**Alice** identity. <!-- pos: 0.3000 -->
**Bob** arc. <!-- pos: 0.9000 -->
"""


def slice_cases(text, path=None):
    positions = [i * 3.7 for i in range(28)] + [99, 99.5, 100, 0.001]
    markers = [float(m) for m in re.findall(r"<!-- pos: ([0-9.]+) -->", text)]
    for marker in markers[:: max(1, len(markers) // 6)]:
        positions += [marker * 100, marker * 100 - 0.0001, marker * 100 + 0.0001, (marker + 0.02) * 100, (marker - 0.02) * 100]
    return positions


@unittest.skipUnless(lua_parity.interpreters(), "lua is not installed")
class SlicerParityTest(unittest.TestCase):
    def compare(self, source, make_case, text):
        positions = slice_cases(text)
        cases = [make_case(p, keep) for p in positions for keep in (False, True)]
        for lua in lua_parity.interpreters():
            results = lua_parity.run_cases(cases, lua)
            for case, result in zip(cases, results):
                self.assertTrue(result["ok"], (source, result.get("error")))
                percent, keep = case["args"][1], case["args"][2]
                got_text, got_info = companion.slice_text(text, percent, keep_current_synopsis=keep)
                self.assertEqual(got_text, result["value"]["text"], (lua, source, percent, keep))
                self.assertEqual(got_info, result["value"]["info"], (lua, source, percent, keep))
        return len(cases)

    def test_synthetic_edge_cases(self):
        made = lambda p, keep: {"fn": "slice", "args": [SYNTHETIC, p, keep]}
        self.assertGreater(self.compare("synthetic", made, SYNTHETIC), 50)

    def test_no_markers_and_empty(self):
        for text in ("", "plain text\n## x\n", "<!-- pos: nope -->", "### a <!-- pos: nan -->\nb"):
            result = lua_parity.run_cases([{"fn": "slice", "args": [text, 50, False]}])[0]["value"]
            got_text, got_info = companion.slice_text(text, 50)
            self.assertEqual(got_text, result["text"])
            self.assertEqual(got_info, result["info"])

    @has_real
    def test_every_real_companion_at_many_positions(self):
        total = 0
        paths = sorted(REAL.glob("*.companion.md")) + [lua_parity.ASSISTANT / "Middlemarch_Reference_Companion.md"]
        self.assertGreaterEqual(len(paths), 8)
        for path in paths:
            text = path.read_bytes().decode("utf-8")
            made = lambda p, keep, path=path: {"fn": "sliceFile", "args": [str(path), p, keep]}
            total += self.compare(path.name, made, text)
        self.assertGreater(total, 500)
        print(f"\nslicer: {total} real slices identical under {', '.join(lua_parity.interpreters())}", file=sys.stderr)

    def test_position_note_matches_lua(self):
        infos = [None, {}, {"chapter_fraction": 0}, {"chapter_fraction": 0.05}, {"chapter_fraction": 0.0501},
                 {"chapter_fraction": 0.5}, {"chapter_fraction": 0.125}, {"chapter_fraction": 1}, {"chapter_fraction": 0.995}]
        results = lua_parity.run_cases([{"fn": "positionNote", "args": [info]} for info in infos])
        for info, result in zip(infos, results):
            self.assertEqual(companion.position_note(info), result.get("value"), info)


@has_real
class RealCompanionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.willpower = WILLPOWER.read_text(encoding="utf-8")
        cls.anne = ANNE.read_text(encoding="utf-8")

    def test_willpower_chapters_use_the_ereaders_real_names(self):
        # Real chapter names from the reader, as found in an exported pbAnki deck
        cases = {
            "Introduction": "Introduction: Welcome to Willpower 101",
            "ONE - I Will, I Won’t, I Want: What Willpower Is, and Why It Matters":
                "Chapter 1. I Will, I Won’t, I Want: What Willpower Is, and Why It Matters",
            "TWO - The Willpower Instinct: Your Body Was Born to Resist Cheesecake":
                "Chapter 2. The Willpower Instinct: Your Body Was Born to Resist Cheesecake",
            "Chapter 3": "Chapter 3. Too Tired to Resist: Why Self-Control Is Like a Muscle",
        }
        for chapter, heading in cases.items():
            block = companion.chapter_block(self.willpower, chapter)
            self.assertTrue(block["found"], chapter)
            self.assertEqual(block["heading"], heading)
            self.assertTrue(block["body"].startswith("**Big idea.**"), chapter)
            self.assertNotIn("\n### ", block["text"])

    def test_chapter_ten_is_not_chapter_one(self):
        self.assertEqual(companion.chapter_block(self.willpower, "Chapter 10")["heading"], "Chapter 10. Final Thoughts")
        self.assertEqual(companion.chapter_block(self.willpower, "Chapter 1")["heading"][:10], "Chapter 1.")

    def test_anne_with_markers(self):
        for chapter in ("Chapter I", "CHAPTER I.", "I", "I: The Shadow of Change", "The Shadow of Change",
                        "Chapter I — The Shadow of Change"):
            block = companion.chapter_block(self.anne, chapter)
            self.assertEqual(block.get("heading"), "Chapter I — The Shadow of Change", chapter)
            self.assertNotIn("<!--", block["text"])
            self.assertIn("_Notes:_", block["body"])
            self.assertTrue(block["text"].endswith("portraits were standard pious-and-\n  patriotic parlor decoration.") or "### Chapter II" not in block["text"])
        self.assertEqual(companion.chapter_block(self.anne, "A note here. CHAPTER II.")["heading"], "Chapter II — Garlands of Autumn")
        self.assertEqual(companion.chapter_block(self.anne, "XLI")["heading"], "Chapter XLI — Love Takes Up the Glass of Time")

    def test_unmatched_chapter(self):
        for chapter in ("Colophon", "Chapter 99", "", None):
            self.assertEqual(companion.chapter_block(self.anne, chapter), {"found": False})
        self.assertEqual(companion.chapter_block(self.willpower, "Notes"), {"found": False})

    def test_sections_that_are_not_chapters_never_match(self):
        jane = (REAL / "Jane Eyre_ An Autobiography - Charlotte Bronte.companion.md").read_text(encoding="utf-8")
        for chapter in ("Thornfield", "Latin", "Dialect"):
            self.assertEqual(companion.chapter_block(jane, chapter), {"found": False}, chapter)
        self.assertEqual(companion.chapter_block(jane, "Chapter II")["heading"], "Chapter II")
        ulysses = (REAL / "Ulysses - James Joyce.companion.md").read_text(encoding="utf-8")
        self.assertEqual(companion.chapter_block(ulysses, "French"), {"found": False})

    def test_polostan_counts_chapters_by_the_books_own_numbers(self):
        polostan = (REAL / "Polostan - Neal Stephenson.companion.md").read_text(encoding="utf-8")
        for chapter in ("Chapter 1", "1", "1 Magnitogorsk, Soviet Union (December 1933)"):
            self.assertEqual(companion.chapter_block(polostan, chapter)["heading"],
                             "Chapter 5 — 1 Magnitogorsk, Soviet Union (December 1933)", chapter)
        self.assertEqual(companion.chapter_block(polostan, "Prologue")["heading"], "Chapter 4 — Prologue: The Golden Gate (October 1933)")

    def test_several_matches_mean_no_match(self):
        noble = (REAL / "The Noble Eightfold Path - Gil Fronsdal.companion.md").read_text(encoding="utf-8")
        self.assertEqual(companion.chapter_block(noble, "One"), {"found": False})
        self.assertEqual(companion.chapter_block(noble, "One · October 28, 2020")["heading"], "One · October 28, 2020")

    def test_overview_picks_the_leading_notes_and_section_one(self):
        expected = {
            "Anne of the Island - L. M. Montgomery": ["## A note on explaining to this reader", "## 1. Quick Orientation"],
            "Jane Eyre_ An Autobiography - Charlotte Bronte": ["## A note on explaining to this reader", "## 1. Quick Orientation"],
            "Middlemarch_ A Study of Provincial Life - George Eliot": ["## A note on explaining to this reader", "## 1. Quick Orientation"],
            "Polostan - Neal Stephenson": ["# Book Companion — *Polostan* (Neal Stephenson)", "## A note on explaining to this reader", "## 1. Quick Orientation"],
            "The Noble Eightfold Path - Gil Fronsdal": ["## A note on explaining to this reader", "## 1. Thesis Roadmap"],
            "The Willpower Instinct - Kelly McGonigal": ["## How to use these notes", "## The reader's goals", "## 1. The book on one page"],
            "Ulysses - James Joyce": ["## A note on explaining to this reader", "# Ulysses — Reference Companion", "## 1. Quick Orientation"],
        }
        for name, headings in expected.items():
            text = (REAL / (name + ".companion.md")).read_text(encoding="utf-8")
            picked = companion.overview(text)
            found = [line for line in picked.split("\n") if re.match(r"#{1,2} ", line)]
            self.assertEqual(found, headings, name)
            self.assertLess(len(picked), len(text) / 5, name)
            self.assertNotIn("### Chapter", picked, name)

    def test_overview_without_numbered_sections(self):
        self.assertEqual(companion.overview("lead\n## First\nbody\n## Second\nmore"), "lead\n## First\nbody")
        self.assertEqual(companion.overview("just text\n"), "just text")

    def test_notes_panel_practical(self):
        panel = companion.notes_panel(self.willpower, "Introduction")
        self.assertEqual(panel["big"], "The best way to improve self-control is to understand how you lose it.")
        self.assertEqual(len(panel["points"]), 6)
        self.assertTrue(panel["points"][0].startswith('Willpower is "the ability'))
        self.assertIn("Restraint bias: people who think", panel["points"][2])
        self.assertFalse(any("Choose your willpower challenge" in p for p in panel["points"]), "only key points")

    def test_notes_panel_with_markers_joins_wrapped_lines(self):
        panel = companion.notes_panel(self.anne, "Chapter I")
        self.assertTrue(panel["big"].startswith("Anne spends her last week at Green Gables"))
        self.assertTrue(panel["big"].endswith("."))
        self.assertNotIn("\n", panel["big"])
        self.assertIn("feeling change everywhere", panel["big"])
        self.assertTrue(all("\n" not in point for point in panel["points"]))
        self.assertEqual(panel["points"][0][:30], '"Harvest is ended and summer i')
        self.assertTrue(any(p.startswith('"manse" — the house') for p in panel["points"]))

    def test_notes_panel_unmatched(self):
        self.assertEqual(companion.notes_panel(self.anne, "Colophon"), {"found": False, "big": "", "points": []})

    def test_notes_panel_for_a_chapter_past_the_reader(self):
        self.assertTrue(companion.notes_panel(self.anne, "Chapter XX")["found"])
        self.assertFalse(companion.notes_panel(self.anne, "Chapter XX", percent=10)["found"])
        self.assertTrue(companion.notes_panel(self.anne, "Chapter I", percent=10)["found"])

    def test_every_real_chapter_gives_a_panel(self):
        for path in sorted(REAL.glob("*.companion.md")):
            text = path.read_text(encoding="utf-8")
            if "<!-- pos:" not in text and "Chapter by chapter" not in text:
                continue
            headings = [h for _i, h in companion._chapter_headings(text.split("\n"))]
            self.assertGreater(len(headings), 5, path.name)
            for heading in headings:
                title = companion._clean_heading(heading)
                block = companion.chapter_block(text, title)
                self.assertEqual(block.get("heading"), title, path.name)
                self.assertTrue(companion.notes_panel(text, title)["big"], (path.name, title))

    def test_context_setting_off(self):
        self.assertIsNone(companion.context_for_highlight(self.anne, companion.OFF, "Chapter I", 5))

    def test_context_setting_whole(self):
        sent = companion.context_for_highlight(self.anne, companion.WHOLE, "Chapter I", 5)
        expected, _info = companion.slice_text(self.anne, 5, keep_current_synopsis=True)
        self.assertEqual(sent, expected)
        self.assertNotIn("Chapter XX", sent)

    def test_context_overview_and_chapter(self):
        sent = companion.context_for_highlight(self.anne, companion.OVERVIEW_AND_CHAPTER, "Chapter I", 2)
        self.assertIn("## A note on explaining to this reader", sent)
        self.assertIn("## 1. Quick Orientation", sent)
        self.assertIn("### Chapter I — The Shadow of Change", sent)
        self.assertIn("Anne spends her last week", sent, "the current chapter's synopsis is kept")
        self.assertNotIn("## 2. Character Dictionary", sent)
        self.assertNotIn("Garlands of Autumn", sent)
        self.assertLess(len(sent), 8000)

    def test_context_overview_when_the_chapter_is_past_the_cutoff_or_unmatched(self):
        overview_only = companion.context_for_highlight(self.anne, companion.OVERVIEW_AND_CHAPTER, "Chapter XX", 2)
        self.assertNotIn("Gilbert Speaks", overview_only)
        self.assertIn("## 1. Quick Orientation", overview_only)
        self.assertNotIn("### ", companion.context_for_highlight(self.anne, companion.OVERVIEW_AND_CHAPTER, "Colophon", 2))

    def test_context_on_a_practical_book_keeps_the_science_check_out_of_overview(self):
        sent = companion.context_for_highlight(self.willpower, companion.OVERVIEW_AND_CHAPTER, "TWO - The Willpower Instinct: Your Body Was Born to Resist Cheesecake", 30)
        self.assertIn("## How to use these notes", sent)
        self.assertIn("### Chapter 2. The Willpower Instinct", sent)
        self.assertNotIn("## 6. Science check", sent)

    def test_context_for_book(self):
        text = companion.context_for_book(self.anne, 0.1)
        self.assertEqual(text, companion.slice_text(self.anne, 10, keep_current_synopsis=True)[0])
        self.assertIn("April's Lady", text)
        self.assertNotIn("Gilbert Speaks", text)
        self.assertEqual(companion.context_for_book(self.willpower, 0.5), self.willpower, "no markers, all of it")


class SentenceTest(unittest.TestCase):
    def test_first_sentence(self):
        cases = {
            "One here. Two there.": "One here.",
            "Mr. Bennet left. Then more.": "Mr. Bennet left.",
            "At 2 a.m. Workers came. Then more.": "At 2 a.m. Workers came.",
            '"Quoted whole." Then more.': '"Quoted whole."',
            '"Part of a quote. And the rest."': "Part of a quote.",
            "Why? Because.": "Why?",
            "No end": "No end",
            "Ends with e.g. this one. Next.": "Ends with e.g. this one.",
            "A claim. (Since publication: wrong.)": "A claim.",
        }
        for text, want in cases.items():
            self.assertEqual(companion.first_sentence(text), want)

    def test_bold_terms_stay_but_labels_go(self):
        body = "**Big idea.** Think small. Then act.\n\n**Key points**\n- *First* point\n- **Bold** second\n\n**Apply it**\n- Not a key point"
        text = "## 1. Chapter by chapter\n### Chapter 1\n" + body
        panel = companion.notes_panel(text, "Chapter 1")
        self.assertEqual(panel, {"found": True, "big": "Think small.", "points": ["First point", "Bold second"]})
        text = "## 1. Chapter by chapter\n### Chapter 1\n**Alice** went home. Then more.\n- A\n- B"
        self.assertEqual(companion.notes_panel(text, "Chapter 1"), {"found": True, "big": "Alice went home.", "points": ["A", "B"]})


class StemRuleTest(unittest.TestCase):
    NAMES = ["Willpower Instinct, The - Kelly McGonigal.epub", "a.b.c.epub", "Don't Panic.epub", "book.fb2.zip", "noext",
             "Über Bücher.epub", "with space .epub", 'q"uote.epub', "/mnt/ext1/Dir.name/book.pdf"]

    @unittest.skipUnless(SKILL_SCRIPTS.is_dir(), "the skill isn't here")
    def test_same_as_the_skills_common_py(self):
        spec = importlib.util.spec_from_file_location("skill_common", SKILL_SCRIPTS / "common.py")
        common = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(common)
        for name in self.NAMES:
            self.assertEqual(companion.stem(name), common.companion_stem(name), name)
            self.assertEqual(companion.file_name(name), common.companion_filename(name), name)

    @unittest.skipUnless(lua_parity.interpreters(), "lua is not installed")
    def test_same_as_assistant_companion_lua(self):
        # The plugin looks for the sidecar with this rule on the reader
        code = 'package.path = "' + str(lua_parity.ASSISTANT) + '/?.lua;" .. package.path; local c = require("assistant_companion"); for line in io.lines() do print(c.getCompanionPath(line)) end'
        names = [n for n in self.NAMES if "/" not in n]
        done = subprocess.run(["lua", "-e", code], input=("\n".join("/mnt/ext1/English/" + n for n in names) + "\n").encode(), capture_output=True, check=True)
        got = done.stdout.decode().splitlines()
        self.assertEqual(got, ["/mnt/ext1/English/" + companion.file_name(n) for n in names])


class Folders(unittest.TestCase):
    """A fake reader, home and assistant folder in a temp directory, and fake commands."""

    DEVICE = "/mnt/ext1/English/Book.epub"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.mount = base / "reader"
        (self.mount / "English").mkdir(parents=True)
        self.home = base / "home"
        self.assistant = base / "assistant folder"
        skill = self.assistant / ".claude" / "skills" / "book-companion"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("skill", encoding="utf-8")
        self.launched = base / "launched.txt"
        hook = base / "terminal-hook"
        hook.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> {shlex.quote(str(self.launched))}\n', encoding="utf-8")
        hook.chmod(0o755)
        self.log = base / "claude.log"
        patcher = mock.patch.dict(os.environ, {
            "PBANKI_DESKTOP_HOME": str(self.home),
            "PBANKI_MOUNT": str(self.mount),
            "PBANKI_ASSISTANT_DIR": str(self.assistant),
            "PBANKI_TERMINAL_CMD": shlex.quote(str(hook)),
            "PBANKI_CLAUDE_BIN": str(FAKE_AI / "claude"),
            "PBANKI_SYNC_DELAY": "0",
            "FAKE_AI_LOG": str(self.log),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        settle = mock.patch.object(companion, "_SETTLE_SECONDS", 0)
        settle.start()
        self.addCleanup(settle.stop)

    def book(self, name="Book.epub", data=b"epub bytes"):
        path = self.mount / "English" / name
        path.write_bytes(data)
        return "/mnt/ext1/English/" + name

    def snapshot(self):
        found = {}
        for path in sorted(self.mount.rglob("*")):
            if path.is_file():
                found[str(path.relative_to(self.mount))] = path.read_bytes()
        return found


class FindTest(Folders):
    def test_absent(self):
        found = companion.find(self.DEVICE, self.mount)
        self.assertEqual((found["found"], found["where"], found["label"], found["path"]), (False, None, None, None))
        self.assertEqual(found["file_name"], "Book.companion.md")
        self.assertIsNone(companion.read_text(found))

    def test_beside_the_book_first(self):
        beside = self.mount / "English" / "Book.companion.md"
        beside.write_text("from the reader", encoding="utf-8")
        local = self.home / "companions" / "Book.companion.md"
        local.parent.mkdir(parents=True)
        local.write_text("from the computer", encoding="utf-8")
        found = companion.find(self.DEVICE, self.mount)
        self.assertEqual((found["where"], found["label"]), ("reader", "on the reader"))
        self.assertEqual(companion.read_text(found), "from the reader")

    def test_then_on_this_computer(self):
        local = self.home / "companions" / "Book.companion.md"
        local.parent.mkdir(parents=True)
        local.write_text("from the computer", encoding="utf-8")
        found = companion.find(self.DEVICE, self.mount)
        self.assertEqual((found["where"], found["label"]), ("computer", "on this computer"))
        self.assertEqual(companion.read_text(found), "from the computer")

    def test_empty_file_counts_as_absent(self):
        (self.mount / "English" / "Book.companion.md").write_bytes(b"")
        self.assertFalse(companion.find(self.DEVICE, self.mount)["found"])

    def test_memory_card_books_look_only_on_this_computer(self):
        self.assertFalse(companion.find("/mnt/ext2/Book.epub", self.mount)["found"])

    def test_reader_copy_stays_usable_unplugged(self):
        (self.mount / "English" / "Book.companion.md").write_text("from the reader", encoding="utf-8")
        companion.find(self.DEVICE, self.mount)
        unplugged = self.mount.parent / "gone"
        found = companion.find(self.DEVICE, unplugged)
        self.assertEqual((found["found"], found["label"], found["cached"]), (True, "on the reader", True))
        self.assertEqual(companion.read_text(found), "from the reader")

    def test_cache_is_not_used_once_the_file_left_the_plugged_in_reader(self):
        beside = self.mount / "English" / "Book.companion.md"
        beside.write_text("from the reader", encoding="utf-8")
        companion.find(self.DEVICE, self.mount)
        beside.unlink()
        self.assertFalse(companion.find(self.DEVICE, self.mount)["found"])

    def test_nothing_is_written_to_the_reader_by_finding(self):
        (self.mount / "English" / "Book.companion.md").write_text("x", encoding="utf-8")
        before = self.snapshot()
        companion.find(self.DEVICE, self.mount)
        self.assertEqual(self.snapshot(), before)


class CreateTest(Folders):
    def test_terminal_failing_to_open_leaves_nothing_running(self):
        device_path = self.book()
        with mock.patch.dict(os.environ, {"PBANKI_TERMINAL_CMD": "/usr/bin/false"}):
            with self.assertRaises(companion.CompanionError) as caught:
                companion.create(device_path, self.mount)
        self.assertEqual(str(caught.exception), "Terminal could not be opened.")
        self.assertEqual(companion.creation_status(device_path), {"stage": "none", "path": None})

    def test_skill_status(self):
        self.assertTrue(companion.skill_status()["ok"])
        with mock.patch.dict(os.environ, {"PBANKI_ASSISTANT_DIR": str(self.mount.parent / "nope")}):
            status = companion.skill_status()
            self.assertFalse(status["ok"])
            self.assertIn("assistant folder isn't at", status["problem"])
        (self.assistant / ".claude" / "skills" / "book-companion" / "SKILL.md").unlink()
        status = companion.skill_status()
        self.assertFalse(status["ok"])
        self.assertIn("skill isn't in", status["problem"])

    def test_refuses_without_the_skill_and_starts_nothing(self):
        self.book()
        with mock.patch.dict(os.environ, {"PBANKI_ASSISTANT_DIR": str(self.mount.parent / "nope")}):
            with self.assertRaises(companion.CompanionError):
                companion.create(self.DEVICE, self.mount)
        self.assertFalse(self.launched.exists())

    def test_refuses_with_the_reader_unplugged(self):
        with self.assertRaises(companion.CompanionError) as caught:
            companion.create(self.DEVICE, self.mount)
        self.assertIn("Plug in the reader", str(caught.exception))
        self.assertFalse(self.launched.exists())

    def test_copies_to_the_mac_and_opens_terminal(self):
        device_path = self.book()
        before = self.snapshot()
        made = companion.create(device_path, self.mount)
        work = self.home / "companion-work" / "Book"
        self.assertEqual(made["work_dir"], str(work))
        self.assertEqual(made["epub"], str(work / "Book.epub"))
        self.assertEqual((work / "Book.epub").read_bytes(), b"epub bytes")
        self.assertEqual(made["watching"], "Book.companion.md")
        self.assertEqual(self.launched.read_text().strip(), made["script"])
        self.assertTrue(made["script"].endswith(".command"))
        self.assertTrue(os.stat(made["script"]).st_mode & stat.S_IXUSR)
        self.assertEqual(self.snapshot(), before, "nothing changes on the reader")

    def test_default_launch_is_open_a_terminal(self):
        device_path = self.book()
        env = {k: v for k, v in os.environ.items() if k != "PBANKI_TERMINAL_CMD"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(companion.subprocess, "run") as run:
                run.return_value = mock.Mock(returncode=0)
                made = companion.create(device_path, self.mount)
        self.assertEqual(run.call_args.args[0], ["open", "-a", "Terminal", made["script"]])

    def run_generated(self, name):
        device_path = self.book(name)
        made = companion.create(device_path, self.mount)
        checked = subprocess.run(["bash", "-n", made["script"]], capture_output=True, check=False)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        lines = Path(made["script"]).read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[0], "#!/bin/bash")
        self.assertEqual(shlex.split(lines[1]), ["script=" + made["script"]])
        self.assertEqual(lines[2], "trap 'rm -f \"$script\"' EXIT")
        parsed_cd = shlex.split(lines[3])
        self.assertEqual(parsed_cd[:2], ["cd", str(self.assistant)])
        parsed = shlex.split(lines[4])
        self.assertEqual(parsed[0], str(FAKE_AI / "claude"))
        self.assertEqual(len(parsed), 2)
        # Really run it, against the fake claude
        self.log.unlink(missing_ok=True)
        run = subprocess.run(["bash", made["script"]], capture_output=True, check=False, stdin=subprocess.DEVNULL)
        self.assertEqual(run.returncode, 0, run.stderr)
        logged = json.loads(self.log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(logged["cwd"], os.path.realpath(self.assistant))
        self.assertEqual(logged["argv"], [parsed[1]])
        # A shell reading the prompt gets the path back whole
        words = subprocess.run(["bash", "-c", 'eval "set -- $1"; printf "%s\\0" "$@"', "_", logged["argv"][0]],
                               capture_output=True, check=True).stdout.decode("utf-8").split("\0")[:-1]
        self.assertEqual(words, ["/book-companion", made["epub"]])
        return logged["argv"][0], made

    def test_plain_name_reads_as_in_the_plan(self):
        prompt, made = self.run_generated("How to Live on 24 Hours a Day.epub")
        self.assertEqual(prompt, "/book-companion '" + made["epub"] + "'")

    def test_apostrophe_in_the_name(self):
        prompt, made = self.run_generated("Don't Panic.epub")
        self.assertEqual(prompt, '/book-companion "' + made["epub"] + '"')

    def test_awkward_names(self):
        for name in ('Two "double" quotes.epub', "Dollar $HOME and `tick`.epub", "Back\\slash.epub",
                     "Both ' and \".epub", "Semi; amp & (parens) #1.epub", "Über Bücher — ñ.epub", "It's $5.epub"):
            self.run_generated(name)

    def test_preview_reads_as_in_the_mock(self):
        with mock.patch.dict(os.environ, {"PBANKI_ASSISTANT_DIR": str(Path.home() / "Documents" / "GitHub" / "assistant.koplugin")}):
            preview = companion.create_preview("/mnt/ext1/English/How to Live on 24 Hours a Day.epub")
        self.assertEqual(preview["command"], "cd ~/Documents/GitHub/assistant.koplugin\nclaude \"/book-companion 'How to Live on 24 Hours a Day.epub'\"")
        self.assertEqual(preview["watching"], "How to Live on 24 Hours a Day.companion.md")

    def test_preview_with_an_apostrophe(self):
        preview = companion.create_preview("/mnt/ext1/English/Don't Panic.epub")
        last = preview["command"].split("\n")[1]
        self.assertEqual(shlex.split(last), ["claude", '/book-companion "Don\'t Panic.epub"'])

    def test_bad_stems_are_refused(self):
        for bad in ("/mnt/ext1/..", "/mnt/ext1/."):
            with self.assertRaises(companion.CompanionError):
                companion.work_dir(bad)

    def test_stages(self):
        device_path = self.book()
        self.assertEqual(companion.creation_status(device_path)["stage"], "none")
        companion.create(device_path, self.mount)
        self.assertEqual(companion.creation_status(device_path)["stage"], "running")
        output = self.home / "companion-work" / "Book" / "Book.companion.md"
        output.write_bytes(b"")
        self.assertEqual(companion.creation_status(device_path)["stage"], "running", "empty counts as absent")
        output.write_text("notes", encoding="utf-8")
        status = companion.creation_status(device_path)
        self.assertEqual((status["stage"], status["path"]), ("found", str(output)))
        companion.keep_local(device_path)
        self.assertEqual(companion.creation_status(device_path)["stage"], "done")

    def test_a_file_still_being_written_does_not_count_yet(self):
        device_path = self.book()
        companion.create(device_path, self.mount)
        output = self.home / "companion-work" / "Book" / "Book.companion.md"
        output.write_text("notes", encoding="utf-8")
        with mock.patch.object(companion, "_SETTLE_SECONDS", 60):
            self.assertEqual(companion.creation_status(device_path)["stage"], "running")

    def test_forget_a_closed_terminal(self):
        device_path = self.book()
        companion.create(device_path, self.mount)
        companion.forget_creation(device_path)
        self.assertEqual(companion.creation_status(device_path)["stage"], "none")

    def test_creating_again_sets_the_old_file_aside(self):
        device_path = self.book()
        companion.create(device_path, self.mount)
        output = self.home / "companion-work" / "Book" / "Book.companion.md"
        output.write_text("first", encoding="utf-8")
        companion.keep_local(device_path)
        companion.create(device_path, self.mount)
        self.assertEqual(companion.creation_status(device_path)["stage"], "running")
        self.assertEqual((output.parent / "previous-Book.companion.md").read_text(encoding="utf-8"), "first")

    def test_watch_notices_the_output(self):
        device_path = self.book()
        companion.create(device_path, self.mount)
        seen = []
        thread, _stop = companion.watch(device_path, seen.append, interval=0.02)
        time.sleep(0.1)
        self.assertEqual(seen, [])
        output = self.home / "companion-work" / "Book" / "Book.companion.md"
        output.write_text("notes", encoding="utf-8")
        thread.join(timeout=5)
        self.assertEqual(seen, [str(output)])

    def test_watch_can_stop(self):
        device_path = self.book()
        companion.create(device_path, self.mount)
        seen = []
        thread, stop = companion.watch(device_path, seen.append, interval=0.02)
        stop.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(seen, [])

    def finished(self, name="Book.epub", content="notes “é”"):
        device_path = self.book(name)
        companion.create(device_path, self.mount)
        output = self.home / "companion-work" / companion.stem(name) / companion.file_name(name)
        output.write_text(content, encoding="utf-8")
        return device_path, output

    def test_copy_to_reader_writes_only_the_sidecar(self):
        device_path, _output = self.finished()
        before = self.snapshot()
        target = companion.copy_to_reader(device_path, self.mount)
        self.assertEqual(target, str(self.mount / "English" / "Book.companion.md"))
        after = self.snapshot()
        self.assertEqual(after.pop("English/Book.companion.md"), "notes “é”".encode("utf-8"))
        self.assertEqual(after, before, "nothing else changed")
        found = companion.find(device_path, self.mount)
        self.assertEqual(found["label"], "on the reader")

    def test_copy_goes_through_the_proven_write(self):
        device_path, _output = self.finished()
        with mock.patch.object(device, "write_file") as write_file:
            companion.copy_to_reader(device_path, self.mount)
        args = write_file.call_args.args
        self.assertEqual(Path(args[1]).name, "Book.companion.md")
        self.assertEqual(args[0], self.mount)

    def test_failed_copy_keeps_the_prompt(self):
        device_path, _output = self.finished()
        with mock.patch.object(device, "write_file", side_effect=device.DeviceWriteError("no")):
            with self.assertRaises(device.DeviceWriteError):
                companion.copy_to_reader(device_path, self.mount)
        self.assertEqual(companion.creation_status(device_path)["stage"], "found")

    def test_keep_on_this_computer(self):
        device_path, _output = self.finished()
        before = self.snapshot()
        target = companion.keep_local(device_path)
        self.assertEqual(target, str(self.home / "companions" / "Book.companion.md"))
        self.assertEqual(Path(target).read_text(encoding="utf-8"), "notes “é”")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(companion.find(device_path, self.mount)["label"], "on this computer")

    def test_copy_or_keep_without_the_file(self):
        device_path = self.book()
        for action in (lambda: companion.copy_to_reader(device_path, self.mount), lambda: companion.keep_local(device_path)):
            with self.assertRaises(companion.CompanionError):
                action()

    def test_memory_card_book_cannot_be_copied(self):
        device_path, output = self.finished()
        card = "/mnt/ext2/Book.epub"
        with self.assertRaises(companion.CompanionError):
            companion.copy_to_reader(card, self.mount)

    @unittest.skipUnless(SKILL_SCRIPTS.is_dir(), "the skill isn't here")
    def test_the_skills_real_writer_lands_where_we_watch(self):
        for name in ("Book.epub", "Don't Panic.v2.epub", "Über Bücher.epub"):
            device_path = self.book(name)
            made = companion.create(device_path, self.mount)
            content = self.mount.parent / "draft.md"
            content.write_text("real notes", encoding="utf-8")
            repo = self.mount.parent / "repo-companions"
            done = subprocess.run(
                [sys.executable, str(SKILL_SCRIPTS / "write_companion.py"), "--content", str(content),
                 "--source", made["epub"], "--repo-companions", str(repo), "--book-folder", made["work_dir"]],
                capture_output=True, text=True, check=False, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            status = companion.creation_status(device_path)
            self.assertEqual(status["stage"], "found", name)
            self.assertIn(status["path"], done.stdout)
            self.assertEqual(Path(status["path"]).read_text(encoding="utf-8"), "real notes")

    def test_no_real_reader_is_touched(self):
        self.assertFalse(str(self.mount).startswith("/Volumes"))


class OddTitleTest(unittest.TestCase):
    def test_a_dotless_i_isnt_a_roman_numeral(self):
        self.assertEqual(companion.chapter_block("## Chapters\n### Chapter 1: A\nbody", "Mı"), {"found": False})
        self.assertEqual(companion.chapter_block("## Chapters\n### Mı\nbody", "Chapter 1"), {"found": False})
        self.assertEqual(companion.chapter_block("## Chapters\n### Mı\nbody", "Mı")["heading"], "Mı")
        self.assertEqual(companion.chapter_block("## Chapters\n### Chapter 1: A\nbody", "Chapter ı"), {"found": False})
        self.assertEqual(companion.chapter_block("## Chapters\n### Chapter 1: A\nbody", "Part ı: x"), {"found": False})

    def test_a_bare_roman_i_doesnt_take_notes_from_a_title_starting_with_the_word(self):
        text = "## 3. Chapters\n### I Love You\nnotes one\n### Chapter 2: Two\nnotes two\n"
        self.assertEqual(companion.chapter_block(text, "I"), {"found": False})
        self.assertEqual(companion.chapter_block("## Chapters\n### I\nnotes\n", "I Love You"), {"found": False})

    def test_the_numbered_heading_still_matches_a_bare_roman_i(self):
        text = "## 3. Chapters\n### I Love You\nnotes one\n### Chapter 1: One\nnotes two\n"
        self.assertEqual(companion.chapter_block(text, "I")["body"], "notes two")


class CutOffCopyTest(Folders):
    def finished(self):
        device_path = self.book()
        companion.create(device_path, self.mount)
        output = companion._work_output(device_path)
        output.write_text("# Notes\n\n" + "### Chapter\n\n- Point.\n\n" * 50, encoding="utf-8")
        return device_path, output

    def cut_off(self, device_path):
        real_flush = device._flush_to_disk

        def flush(path, data):
            real_flush(path, data[:20])
            raise OSError(5, "Input/output error")

        with mock.patch.object(device, "_flush_to_disk", flush):
            with self.assertRaises(device.DeviceWriteError):
                companion.copy_to_reader(device_path, self.mount)

    def test_a_cut_off_copy_does_not_count_as_the_companion(self):
        device_path, _ = self.finished()
        self.cut_off(device_path)
        self.assertEqual((self.mount / "English" / "Book.companion.md").stat().st_size, 20)
        self.assertFalse(companion.find(device_path, self.mount)["found"])
        self.assertTrue(companion.copy_cut_off(device_path))
        self.assertEqual(companion.creation_status(device_path)["stage"], "found")

    def test_a_copy_killed_part_way_does_not_count_either(self):
        device_path, _ = self.finished()
        real_flush = device._flush_to_disk

        def flush(path, data):
            real_flush(path, data[:20])
            raise KeyboardInterrupt

        with mock.patch.object(device, "_flush_to_disk", flush):
            with self.assertRaises(KeyboardInterrupt):
                companion.copy_to_reader(device_path, self.mount)
        self.assertFalse(companion.find(device_path, self.mount)["found"])

    def test_copying_again_repairs_it(self):
        device_path, output = self.finished()
        self.cut_off(device_path)
        companion.copy_to_reader(device_path, self.mount)
        found = companion.find(device_path, self.mount)
        self.assertEqual(found["where"], "reader")
        self.assertEqual(companion.read_text(found), output.read_text("utf-8"))
        self.assertFalse(companion.copy_cut_off(device_path))

    def test_keeping_it_after_a_cut_off_copy_uses_the_whole_file(self):
        device_path, output = self.finished()
        self.cut_off(device_path)
        companion.keep_local(device_path)
        found = companion.find(device_path, self.mount)
        self.assertEqual(found["where"], "computer")
        self.assertEqual(companion.read_text(found), output.read_text("utf-8"))


class ScriptEndsTest(Folders):
    def started(self, body):
        """The real .command script under bash, with a stand-in for claude."""
        device_path = self.book()
        companion.create(device_path, self.mount)
        script = companion.work_dir(device_path) / companion._SCRIPT_NAME
        claude = self.mount.parent / "stand-in"
        claude.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        claude.chmod(0o755)
        epub = companion.work_dir(device_path) / "Book.epub"
        script.write_text(companion.script_text(epub, claude=claude), encoding="utf-8")
        proc = subprocess.Popen(["/bin/bash", str(script)], start_new_session=True)
        self.addCleanup(proc.wait)
        return device_path, script, proc

    def test_a_finished_session_stops_counting_as_running(self):
        device_path, script, proc = self.started("exit 0")
        self.assertEqual(proc.wait(timeout=20), 0)
        self.assertFalse(script.exists())
        self.assertEqual(companion.creation_status(device_path)["stage"], "none")

    def test_a_closed_terminal_window_stops_counting_as_running(self):
        ready = self.mount.parent / "ready"
        device_path, script, proc = self.started(f'touch "{ready}"\nexec sleep 60')
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists())
        self.assertEqual(companion.creation_status(device_path)["stage"], "running")
        os.killpg(proc.pid, signal.SIGHUP)
        proc.wait(timeout=20)
        self.assertFalse(script.exists())
        self.assertEqual(companion.creation_status(device_path)["stage"], "none")

    def test_the_finished_file_is_still_found_after_the_session_ends(self):
        device_path, script, proc = self.started("exit 0")
        proc.wait(timeout=20)
        companion._work_output(device_path).write_text("notes", encoding="utf-8")
        self.assertEqual(companion.creation_status(device_path)["stage"], "found")


if __name__ == "__main__":
    unittest.main()
