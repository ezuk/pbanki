"""prompts.py: normalize parity with prompts.lua, seeding, listing and the watcher."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import lua_parity  # noqa: E402
from pbanki_desktop import cardgen, paths, prompts  # noqa: E402

NORMALIZE_CASES = [
    "﻿One  \r\nline\r\n\r\n\r\n\r\nTwo\n",
    "Intro\n## Rules\n- a\n- b",
    "# Title\n\nText",
    "Use #tags\n",
    "",
    "   ",
    "\n\n\n",
    "﻿",
    "a\rb\r\nc\n\rd",
    "trailing tabs\t\t\nnext  \t",
    "### Three\n### Three again\n\n\n# One\nbody",
    "#\tTab heading\nbody",
    "#NoSpace\n####### Seven\n",
    "  ## Indented heading\nbody",
    "## Heading   \nbody\n",
    "é — “curly” text  \n\n\n\nñ\n",
    " nbsp stays \n",
    "\x0bvertical\x0c\n",
    "a\n\n\n\n\n\n\nb",
    "## A\n\n\n\n## B\n",
    "text\n#\n# \n## x",
]


@unittest.skipUnless(lua_parity.interpreters(), "lua is not installed")
class NormalizeParityTest(unittest.TestCase):
    def cases(self):
        real = [path.read_text(encoding="utf-8") for path in sorted(paths.repo_root().glob("koreader/pbanki.koplugin/prompts/**/*.md"))]
        real += [path.read_text(encoding="utf-8") for path in sorted(paths.repo_root().glob("prompts/*/*.md"))]
        real += [path.read_text(encoding="utf-8") for path in sorted(paths.desktop_root().glob("prompts/Standard/*.md"))]
        self.assertGreaterEqual(len(real), 8)
        return NORMALIZE_CASES + real + [text.replace("\n", "\r\n") + "  \n\n\n" for text in real]

    def test_normalize_matches_lua(self):
        cases = self.cases()
        for lua in lua_parity.interpreters():
            results = lua_parity.run_cases([{"fn": "normalize", "args": [text]} for text in cases], lua)
            for text, result in zip(cases, results):
                self.assertTrue(result["ok"], result.get("error"))
                self.assertEqual(prompts.normalize(text), result["value"], (lua, text[:60]))

    def test_names_and_choose_match_lua(self):
        folders = ["zeta", "Standard", "Concise", "alpha", "Beta"]
        cases = [{"fn": "promptNames", "args": [f]} for f in (folders, [], ["Standard"])]
        choices = [(folders, "zeta", "Concise"), (folders, None, "Concise"), (folders, "gone", "Concise"),
                   (folders, "gone", "also gone"), (folders, None, None)]
        cases += [{"fn": "promptChoose", "args": list(c)} for c in choices]
        results = lua_parity.run_cases(cases)
        for f, result in zip((folders, [], ["Standard"]), results[:3]):
            self.assertEqual(prompts.names(f), result["value"])
        for c, result in zip(choices, results[3:]):
            self.assertEqual(prompts.choose(*c), result["value"])

    def test_read_matches_lua_on_a_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "question.md").write_text("Ask briefly.\r\n", encoding="utf-8")
            Path(folder, "cloze.md").write_text("  \n", encoding="utf-8")
            Path(folder, "definition.md").write_text("## Define\nit", encoding="utf-8")
            result = lua_parity.run_cases([{"fn": "promptRead", "args": [folder]}])[0]
            own = prompts.read(folder)
            self.assertEqual(own, result["value"])
            self.assertEqual(sorted(own), ["definition", "questions"])

    def test_builtin_prompts_match_lua(self):
        # The plugin's instructions reach the AI unchanged from the desktop app
        result = lua_parity.run_cases([{"fn": "systemPrompt", "args": [None, kind]} for kind in ("questions", "cloze", "definition")])
        self.assertEqual(result[0]["value"], cardgen.SYSTEM_PROMPT)
        self.assertEqual(result[1]["value"], cardgen.CLOZE_PROMPT)
        self.assertEqual(result[2]["value"], cardgen.DEFINITION_PROMPT)


class FolderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {"PBANKI_DESKTOP_HOME": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.root = Path(self.temp.name) / "prompts"
        prompts.seed()

    def test_seed_writes_every_standard_file_and_a_readme(self):
        names = sorted(p.name for p in (self.root / "Standard").iterdir())
        self.assertEqual(names, ["cloze.md", "connections.md", "definitions.md", "ideas.md", "questions.md", "rewrite.md"])
        readme = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("edits made there are undone", readme)
        self.assertEqual(prompts.read(self.root / "Standard"), prompts.builtin())

    def test_seed_undoes_edits_to_standard(self):
        target = self.root / "Standard" / "questions.md"
        target.write_text("Edited", encoding="utf-8")
        prompts.seed()
        self.assertNotEqual(target.read_text(encoding="utf-8"), "Edited")

    def test_repo_prompts_copied_once_and_never_overwritten(self):
        target = self.root / "Self Help" / "questions.md"
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), (paths.repo_root() / "prompts" / "Self Help" / "questions.md").read_bytes())
        target.write_text("Mine", encoding="utf-8")
        prompts.seed()
        self.assertEqual(target.read_text(encoding="utf-8"), "Mine")

    def test_listing_marks_own_and_standard_files(self):
        listed = {row["name"]: row for row in prompts.list_folders()}
        self.assertEqual(list(listed)[0], "Standard")
        self.assertIn("Self Help", listed)
        files = {f["file"]: f["status"] for f in listed["Self Help"]["files"]}
        self.assertEqual(files["questions.md"], "this prompt")
        self.assertEqual(files["cloze.md"], "uses Standard")
        self.assertEqual(files["connections.md"], "uses Standard")
        self.assertTrue(all(f["status"] == "this prompt" for f in listed["Standard"]["files"]))
        cloze = next(f for f in listed["Self Help"]["files"] if f["file"] == "cloze.md")
        self.assertEqual(cloze["path"], str(self.root / "Standard" / "cloze.md"))

    def test_singular_file_names_and_blank_files(self):
        own = self.root / "Mine"
        own.mkdir()
        (own / "question.md").write_text("Ask briefly.", encoding="utf-8")
        (own / "cloze.md").write_text(" \n", encoding="utf-8")
        (own / "connections.md").write_text("Connect.", encoding="utf-8")
        files = {f["kind"]: f for row in prompts.list_folders() if row["name"] == "Mine" for f in row["files"]}
        self.assertEqual((files["questions"]["file"], files["questions"]["status"]), ("question.md", "this prompt"))
        self.assertEqual(files["cloze"]["status"], "uses Standard")
        self.assertEqual(files["connections"]["status"], "this prompt")
        prompt = prompts.effective("Mine")
        self.assertEqual(prompt["questions"], "Ask briefly.")
        self.assertEqual(prompt["connections"], "Connect.")
        self.assertEqual(prompt["cloze"], prompts.builtin()["cloze"])
        self.assertEqual(prompt["ideas"], prompts.builtin()["ideas"])

    def test_edits_apply_on_the_next_read(self):
        own = self.root / "Mine"
        own.mkdir()
        (own / "questions.md").write_text("First.", encoding="utf-8")
        self.assertEqual(prompts.effective("Mine")["questions"], "First.")
        (own / "questions.md").write_text("Second.", encoding="utf-8")
        self.assertEqual(prompts.effective("Mine")["questions"], "Second.")

    def test_unknown_prompt_falls_back_to_standard(self):
        self.assertEqual(prompts.effective("Gone"), prompts.effective(prompts.STANDARD))

    def test_default_for_book(self):
        self.assertEqual(prompts.default_for_book("Self Help", None), "Self Help")
        self.assertEqual(prompts.default_for_book("Gone", "Self Help"), "Self Help")
        self.assertEqual(prompts.default_for_book("Gone", "Also gone"), "Standard")
        self.assertEqual(prompts.default_for_book(None, None), "Standard")

    def test_read_one(self):
        row = prompts.read_one("Self Help", "cloze.md")
        self.assertEqual(row["status"], "uses Standard")
        self.assertIn("Write cloze cards", row["text"])
        self.assertEqual(prompts.read_one("Self Help", "questions")["file"], "questions.md")

    def test_names_cannot_escape_the_prompts_folder(self):
        secret = Path(self.temp.name) / "secret.md"
        secret.write_text("secret", encoding="utf-8")
        for name in ("..", "../..", "Standard/..", "/etc", "", None, ".hidden"):
            with self.assertRaises(prompts.PromptNotFound, msg=name):
                prompts.read_one(name, "questions.md")
        for file_name in ("../secret.md", "../../secret.md", "secret.md", "/etc/passwd", "README.md", None):
            with self.assertRaises(prompts.PromptNotFound, msg=file_name):
                prompts.read_one("Standard", file_name)

    def test_open_and_reveal_use_the_listed_path_only(self):
        with mock.patch.object(prompts, "_launch") as launch:
            prompts.open_in_editor("Self Help", "questions.md")
            prompts.reveal("Self Help", "cloze.md")
            with self.assertRaises(prompts.PromptNotFound):
                prompts.open_in_editor("..", "questions.md")
            with self.assertRaises(prompts.PromptNotFound):
                prompts.reveal("Standard", "../README.md")
        self.assertEqual(launch.call_args_list[0].args[0], ["open", "-t", str(self.root / "Self Help" / "questions.md")])
        self.assertEqual(launch.call_args_list[1].args[0], ["open", "-R", str(self.root / "Standard" / "cloze.md")])
        self.assertEqual(launch.call_count, 2)


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {"PBANKI_DESKTOP_HOME": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        prompts.seed()
        self.target = Path(self.temp.name) / "prompts" / "Standard" / "questions.md"

    def touch(self):
        stat = self.target.stat()
        os.utime(self.target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    def test_change_is_reported_once_and_resets_the_clock(self):
        seen = []
        watcher = prompts.PromptWatcher(on_change=seen.append)
        self.assertEqual(watcher.check(), [])
        watcher.last_reload -= 100
        self.assertGreaterEqual(watcher.seconds_since_reload(), 100)
        self.touch()
        self.assertEqual(watcher.check(), [str(self.target)])
        self.assertEqual(seen, [[str(self.target)]])
        self.assertLess(watcher.seconds_since_reload(), 5)
        self.assertEqual(watcher.check(), [])

    def test_new_and_removed_files_are_reported(self):
        watcher = prompts.PromptWatcher()
        new = self.target.parent / "extra.md"
        new.write_text("x", encoding="utf-8")
        self.assertEqual(watcher.check(), [str(new)])
        new.unlink()
        self.assertEqual(watcher.check(), [str(new)])

    def test_background_thread(self):
        seen = []
        watcher = prompts.PromptWatcher(on_change=seen.append)
        watcher.start(interval=0.02)
        self.addCleanup(watcher.stop)
        self.touch()
        deadline = time.monotonic() + 3
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(seen, [[str(self.target)]])


class OwnCopyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = mock.patch.dict(os.environ, {"PBANKI_DESKTOP_HOME": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        prompts.seed()
        self.folder = paths.prompts_dir() / "Concise"
        self.folder.mkdir()

    def test_a_row_names_the_file_in_its_own_folder(self):
        row = prompts.read_one("Concise", "cloze.md")
        self.assertEqual(row["status"], "uses Standard")
        self.assertEqual(row["own_path"], str(self.folder / "cloze.md"))
        self.assertEqual(row["path"], str(paths.prompts_dir() / "Standard" / "cloze.md"))

    def test_make_own_copies_standard_once(self):
        row = prompts.make_own("Concise", "cloze.md")
        self.assertEqual((row["status"], row["path"]), ("this prompt", str(self.folder / "cloze.md")))
        (self.folder / "cloze.md").write_text("Edited\n", encoding="utf-8")
        self.assertEqual(prompts.make_own("Concise", "cloze.md")["path"], str(self.folder / "cloze.md"))
        self.assertEqual((self.folder / "cloze.md").read_text("utf-8"), "Edited\n")

    def test_make_own_fills_a_blank_file_under_the_name_it_has(self):
        (self.folder / "question.md").write_text("  \n", encoding="utf-8")
        row = prompts.make_own("Concise", "questions.md")
        self.assertEqual(row["status"], "this prompt")
        self.assertEqual(Path(row["path"]).name, "questions.md")

    def test_make_own_leaves_standard_as_it_is(self):
        before = (paths.prompts_dir() / "Standard" / "cloze.md").read_bytes()
        self.assertEqual(prompts.make_own("Standard", "cloze.md")["status"], "this prompt")
        self.assertEqual((paths.prompts_dir() / "Standard" / "cloze.md").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
