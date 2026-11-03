"""ai.py against fake claude and codex commands."""

import http.server
import json
import os
import shutil
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pbanki_desktop import ai, cardgen  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_ai"
MESSAGES = [
    {"role": "system", "content": "NOTES", "cache": True},
    {"role": "system", "content": cardgen.SYSTEM_PROMPT},
    {"role": "user", "content": "<highlight>\nThe book's “words” é\n</highlight>\n\nWrite 2 cards, each asking something different."},
]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers; ps shows whether it really runs
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
    return bool(state) and not state.startswith("Z")


class FakeAITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "log.jsonl"
        self.env = {
            "PBANKI_CLAUDE_BIN": str(FAKE / "claude"),
            "PBANKI_CODEX_BIN": str(FAKE / "codex"),
            "FAKE_AI_LOG": str(self.log),
            "FAKE_AI_SLOW_SECONDS": "30",
        }
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("FAKE_AI_MODE", "FAKE_AI_PIDS"):
            os.environ.pop(name, None)

    def runs(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


class CommandTest(FakeAITest):
    def test_claude_command_is_exactly_the_verified_one(self):
        answer = ai.query("claude-sonnet-5-5", MESSAGES)
        self.assertEqual(json.loads(answer)["cards"][0]["front"], "Fake question 0 about a i?")
        run = self.runs()[0]
        system = "NOTES\n\n" + cardgen.SYSTEM_PROMPT
        self.assertEqual(run["argv"], [
            "-p", "--model", "claude-sonnet-5-5", "--setting-sources", "", "--strict-mcp-config",
            "--disable-slash-commands", "--tools", "", "--no-session-persistence",
            "--output-format", "json", "--system-prompt", system,
        ])
        self.assertNotIn("--bare", run["argv"])
        self.assertEqual(run["stdin"], MESSAGES[2]["content"], "the user message goes on stdin")

    def test_codex_command_is_exactly_the_verified_one(self):
        answer = ai.query("gpt-6.1-sol", MESSAGES)
        self.assertIn("Fake question", answer)
        run = self.runs()[0]
        self.assertEqual(run["argv"][:7], ["exec", "-m", "gpt-6.1-sol", "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral"])
        self.assertEqual(run["argv"][-3], "-o")
        self.assertEqual(run["argv"][-1], "-")
        for option in ("--ignore-user-config", "--ignore-rules"):
            self.assertIn(option, run["argv"])
        turned_off = {run["argv"][i + 1] for i, arg in enumerate(run["argv"]) if arg == "--disable"}
        for feature in ("shell_tool", "unified_exec", "code_mode_host", "view_image", "hooks", "plugins", "apps"):
            self.assertIn(feature, turned_off)
        self.assertIn("mcp_servers={}", run["argv"])
        self.assertEqual(run["stdin"], "NOTES\n\n" + cardgen.SYSTEM_PROMPT + "\n\n" + MESSAGES[2]["content"])

    def test_runs_in_an_empty_temporary_folder(self):
        for model in ("claude-haiku-5-5", "gpt-6.1-sol"):
            ai.query(model, MESSAGES)
        for run in self.runs():
            self.assertIn("pbanki-ai-", run["cwd"])
            self.assertEqual(Path(run["cwd"]).name, "work")
            self.assertFalse(Path(run["cwd"]).exists(), "removed afterwards")

    def test_codex_output_file_is_outside_the_working_folder(self):
        ai.query("gpt-6.1-sol", MESSAGES)
        run = self.runs()[0]
        self.assertNotEqual(Path(run["argv"][-2]).parent, Path(run["cwd"]))

    def test_utf8_passes_through_both_commands(self):
        for model in ("claude-opus-5-5", "gpt-6.1-sol"):
            ai.query(model, MESSAGES)
        for run in self.runs():
            self.assertIn("The book's “words” é", run["stdin"])

    def test_no_system_message(self):
        ai.query("gpt-6.1-sol", [{"role": "user", "content": "only user"}])
        self.assertEqual(self.runs()[0]["stdin"], "only user")

    def test_unknown_model(self):
        with self.assertRaises(ai.AIError) as caught:
            ai.query("gpt-unknown", MESSAGES)
        self.assertEqual(str(caught.exception), "That AI model isn't one pbAnki knows.")


class FailureTest(FakeAITest):
    def assert_fails(self, model, mode, sentence):
        with mock.patch.dict(os.environ, {"FAKE_AI_MODE": mode}):
            with self.assertRaises(ai.AIError) as caught:
                ai.query(model, MESSAGES)
        self.assertEqual(str(caught.exception), sentence)
        self.assertNotIn("Traceback", str(caught.exception))

    def test_claude_is_error(self):
        self.assert_fails("claude-sonnet-5-5", "is_error", "Claude Code couldn't answer: API Error: the fake model is overloaded")

    def test_claude_not_logged_in(self):
        self.assert_fails("claude-sonnet-5-5", "not_logged_in", "Claude Code isn't signed in. Open Terminal, run claude and sign in, then try again.")

    def test_claude_exit(self):
        self.assert_fails("claude-sonnet-5-5", "exit", "Claude Code couldn't answer: fake claude went wrong")

    def test_claude_empty(self):
        self.assert_fails("claude-sonnet-5-5", "empty", "Claude Code gave an empty reply.")

    def test_claude_no_output(self):
        self.assert_fails("claude-sonnet-5-5", "no_output", "Claude Code's reply could not be read.")

    def test_codex_is_error(self):
        self.assert_fails("gpt-6.1-sol", "is_error", "Codex couldn't answer: the fake model is overloaded")

    def test_codex_not_logged_in(self):
        self.assert_fails("gpt-6.1-sol", "not_logged_in", "Codex isn't signed in. Open Terminal, run codex and sign in, then try again.")

    def test_codex_exit(self):
        self.assert_fails("gpt-6.1-sol", "exit", "Codex couldn't answer: fake codex went wrong")

    def test_codex_no_output(self):
        self.assert_fails("gpt-6.1-sol", "no_output", "Codex finished without writing an answer.")

    def test_codex_empty(self):
        self.assert_fails("gpt-6.1-sol", "empty", "Codex gave an empty reply.")

    def test_malformed_text_is_returned_for_the_parser(self):
        # The command worked; the parser reports the bad reply in its own words
        with mock.patch.dict(os.environ, {"FAKE_AI_MODE": "malformed"}):
            for model in ("claude-sonnet-5-5", "gpt-6.1-sol"):
                answer = ai.query(model, MESSAGES)
                self.assertEqual(cardgen.parse_cards(answer, 2), (None, "The AI reply could not be read."))

    def test_marker_in_the_prompt_steers_one_request(self):
        messages = [*MESSAGES[:2], {"role": "user", "content": "[[fake:is_error]] hint"}]
        with self.assertRaises(ai.AIError):
            ai.query("claude-sonnet-5-5", messages)
        self.assertIn("Fake question", ai.query("claude-sonnet-5-5", MESSAGES))

    def test_stack_trace_is_never_shown(self):
        script = Path(self.temp.name) / "crash"
        script.write_text('#!/bin/sh\necho \'Traceback (most recent call last):\' >&2\necho \'  File "x.py", line 1\' >&2\necho \'ValueError: boom\' >&2\nexit 1\n', encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        with mock.patch.dict(os.environ, {"PBANKI_CLAUDE_BIN": str(script)}):
            with self.assertRaises(ai.AIError) as caught:
                ai.query("claude-sonnet-5-5", MESSAGES)
        self.assertEqual(str(caught.exception), "Claude Code couldn't answer: boom")

    def test_missing_command(self):
        with mock.patch.dict(os.environ, {"PBANKI_CLAUDE_BIN": "/no/such/claude"}):
            with self.assertRaises(ai.AIError) as caught:
                ai.query("claude-sonnet-5-5", MESSAGES)
        self.assertEqual(str(caught.exception), "Claude Code isn't installed, or pbAnki can't find it. Check Settings.")

    def test_huge_system_prompt_fails_in_a_plain_sentence(self):
        # Over the Mac's limit for command arguments
        big = [{"role": "system", "content": "x" * 8_000_000}, {"role": "user", "content": "u"}]
        with self.assertRaises(ai.AIError) as caught:
            ai.query("claude-sonnet-5-5", big)
        self.assertIn("could not be started", str(caught.exception))


class TimeoutAndCancelTest(FakeAITest):
    def wait_for_pids(self, path):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if path.exists() and len(path.read_text().split()) == 2:
                return [int(p) for p in path.read_text().split()]
            time.sleep(0.02)
        self.fail("the fake command did not start")

    def assert_gone(self, pids):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(alive(p) for p in pids):
            time.sleep(0.05)
        self.assertEqual([p for p in pids if alive(p)], [], "the whole process group is gone")

    def run_cancelled(self, model):
        pids_file = Path(self.temp.name) / f"pids-{model}"
        token = ai.CancelToken()
        results = []

        def work():
            try:
                results.append(ai.query(model, MESSAGES, cancel=token))
            except ai.AIError as e:
                results.append(e)

        with mock.patch.dict(os.environ, {"FAKE_AI_MODE": "slow", "FAKE_AI_PIDS": str(pids_file)}):
            thread = threading.Thread(target=work)
            thread.start()
            pids = self.wait_for_pids(pids_file)
            self.assertTrue(all(alive(p) for p in pids))
            started = time.monotonic()
            token.cancel()
            thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 10)
        self.assertIsInstance(results[0], ai.AICancelled)
        self.assert_gone(pids)

    def test_kill_running_ends_commands_and_removes_their_folders(self):
        pids_file = Path(self.temp.name) / "pids-kill"
        results = []
        folders = Path(self.temp.name) / "temps"
        folders.mkdir()

        def work():
            try:
                results.append(ai.query("claude-sonnet-5-5", MESSAGES))
            except ai.AIError as e:
                results.append(e)

        with mock.patch.dict(os.environ, {"FAKE_AI_MODE": "slow", "FAKE_AI_PIDS": str(pids_file)}), \
                mock.patch.object(tempfile, "tempdir", str(folders)):
            thread = threading.Thread(target=work)
            thread.start()
            pids = self.wait_for_pids(pids_file)
            self.assertEqual(len(list(folders.glob("pbanki-ai-*"))), 1)
            ai.kill_running()
            self.assert_gone(pids)
            self.assertEqual(list(folders.glob("pbanki-ai-*")), [])
            thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(results[0], ai.AIError)
        self.assertEqual(ai._running, {})

    def test_cancel_kills_the_process_group_claude(self):
        self.run_cancelled("claude-sonnet-5-5")

    def test_cancel_kills_the_process_group_codex(self):
        self.run_cancelled("gpt-6.1-sol")

    def test_cancel_before_start(self):
        token = ai.CancelToken()
        token.cancel()
        with self.assertRaises(ai.AICancelled):
            ai.query("claude-sonnet-5-5", MESSAGES, cancel=token)
        self.assertFalse(self.log.exists())

    def test_timeout_kills_the_process_group(self):
        pids_file = Path(self.temp.name) / "pids"
        with mock.patch.dict(os.environ, {"FAKE_AI_MODE": "slow", "FAKE_AI_PIDS": str(pids_file)}):
            with self.assertRaises(ai.AIError) as caught:
                ai.query("gpt-6.1-sol", MESSAGES, timeout=1.5)
        self.assertEqual(str(caught.exception), "Codex took longer than 2 seconds and was stopped.")
        self.assertNotIsInstance(caught.exception, ai.AICancelled)
        self.assert_gone([int(p) for p in pids_file.read_text().split()])

    def test_term_ignoring_command_is_still_killed(self):
        script = Path(self.temp.name) / "stubborn"
        script.write_text("#!/bin/bash\ntrap '' TERM\nsleep 30 &\nwait\nsleep 30\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"PBANKI_CLAUDE_BIN": str(script)}):
            with self.assertRaises(ai.AIError):
                ai.query("claude-sonnet-5-5", MESSAGES, timeout=0.5)
        self.assertLess(time.monotonic() - started, 10)


class DetectTest(FakeAITest):
    def test_reports_path_and_version(self):
        found = {row["id"]: row for row in ai.detect()}
        self.assertEqual(found["claude"]["name"], "Claude Code")
        self.assertEqual(found["claude"]["version"], "2.1.317")
        self.assertEqual(found["claude"]["path"], str(FAKE / "claude"))
        self.assertEqual(found["codex"]["version"], "0.160.1")
        self.assertTrue(found["codex"]["display"].endswith("codex · version 0.160.1"))

    def test_missing_command(self):
        with mock.patch.dict(os.environ, {"PBANKI_CODEX_BIN": "/no/such/codex"}):
            row = next(r for r in ai.detect() if r["id"] == "codex")
        self.assertEqual((row["found"], row["path"], row["version"], row["display"]), (False, None, None, None))

    def test_finds_commands_with_a_poor_path(self):
        # As when the app is started from Finder
        fake_home = Path(self.temp.name)
        local_bin = fake_home / ".local" / "bin"
        local_bin.mkdir(parents=True)
        command = local_bin / "claude"
        command.write_text("#!/bin/sh\necho 1.2.3\n", encoding="utf-8")
        command.chmod(0o755)
        os.environ.pop("PBANKI_CLAUDE_BIN")
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "HOME": str(fake_home)}):
            self.assertEqual(ai.find_binary("claude"), str(command))
            env = ai.child_env()
        self.assertIn(str(local_bin), env["PATH"].split(":"))
        self.assertIn("/opt/homebrew/bin", env["PATH"].split(":"))
        self.assertTrue(env["PATH"].startswith("/usr/bin:/bin"))
        os.environ["PBANKI_CLAUDE_BIN"] = self.env["PBANKI_CLAUDE_BIN"]

    def test_override_must_be_executable(self):
        plain = Path(self.temp.name) / "plain"
        plain.write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"PBANKI_CLAUDE_BIN": str(plain)}):
            self.assertIsNone(ai.find_binary("claude"))


class ModelsTest(unittest.TestCase):
    def test_list_matches_the_plan(self):
        self.assertEqual([(m["id"], m["label"], m["assistant"]) for m in ai.MODELS], [
            ("claude-haiku-5-5", "Haiku 5.5", "claude"),
            ("claude-sonnet-5-5", "Sonnet 5.5", "claude"),
            ("claude-opus-5-5", "Opus 5.5", "claude"),
            ("claude-fable-5-1", "Fable 5.1", "claude"),
            ("gpt-6.1-sol", "GPT-6.1 Sol (Codex)", "codex"),
        ])
        self.assertEqual(ai.DEFAULT_MODEL, "claude-sonnet-5-5")
        self.assertEqual(ai.DEFAULT_META_MODEL, "claude-opus-5-5")

    def test_menus_list_only_enabled_assistants(self):
        self.assertEqual([m["id"] for m in ai.enabled_models({"codex": True, "claude": False})], ["gpt-6.1-sol"])
        self.assertEqual(len(ai.enabled_models({"codex": True, "claude": True})), 5)

    def test_at_least_one_stays_on(self):
        both = {"claude": True, "codex": True}
        one = ai.set_enabled(both, "codex", False)
        self.assertEqual(one, {"claude": True, "codex": False})
        self.assertEqual(ai.set_enabled(one, "claude", False), one)
        self.assertEqual(ai.set_enabled(one, "codex", True), both)


class FakeRepliesTest(FakeAITest):
    """The fakes' valid replies go through the real parsers, so other tests can rely on them."""

    H = {"text": "Be content with quite a little.", "before": "Beware of too much.", "after": "Allow for accidents.", "book": "B"}

    def ask(self, messages):
        return ai.query("claude-sonnet-5-5", messages)

    def test_questions(self):
        request = {"max": 3}
        cards, err = cardgen.parse_reply(self.ask(cardgen.build_messages(self.H, request)), request, self.H)
        self.assertIsNone(err)
        self.assertEqual(len(cards), 2)

    def test_questions_only(self):
        request = {"count": 2, "questions_only": True}
        cards, _err = cardgen.parse_reply(self.ask(cardgen.build_messages(self.H, request)), request, self.H)
        self.assertEqual([c["back"] for c in cards], ["", ""])

    def test_cloze(self):
        request = {"count": 2, "cloze": True}
        cards, err = cardgen.parse_reply(self.ask(cardgen.build_messages(self.H, request)), request, self.H)
        self.assertIsNone(err)
        self.assertEqual(cards[0]["front"], "{{c1::Be}} content with quite a little.")

    def test_definition(self):
        reply = self.ask(cardgen.build_definition("ardour", self.H))
        self.assertEqual(cardgen.definition_cards(reply)[0][0], {"front": "Define ardour", "back": "A fake meaning for tests."})

    def test_connections(self):
        rows = [{"text": "One.", "chapter": "Chapter 1"}, {"text": "Two.", "chapter": "Chapter 2"}]
        found, err = cardgen.parse_connections(self.ask(cardgen.build_connections(rows, {"count": 2})), 2)
        self.assertIsNone(err)
        self.assertEqual(len(found), 1, "the single-source answer is dropped")
        self.assertEqual(found[0]["sources"], [1, 2])

    def test_ideas(self):
        rows = [{"text": "Allow for accidents.", "chapter": "Chapter 1"}]
        found, err = cardgen.parse_ideas(self.ask(cardgen.build_ideas(rows, {}, "NOTES")), "Then allow for accidents, always.")
        self.assertIsNone(err)
        self.assertEqual(found["ideas"][0]["quote"], "Allow for accidents.")
        self.assertEqual(found["ideas"][1]["quote"], "")
        self.assertEqual(found["chapters"][0]["points"][1]["idea"], 0)

    def test_rewrite(self):
        reply = self.ask(cardgen.build_rewrite({"front": "Q", "back": "A"}, self.H, "Shorter"))
        self.assertEqual(cardgen.parse_rewrite(reply)[0]["front"], "A rewritten fake question?")

    def test_later_research(self):
        request = {"count": 1, "later_research": True}
        with mock.patch.dict(os.environ, {"FAKE_AI_MODE": "later"}):
            reply = self.ask(cardgen.build_messages(self.H, request, "NOTES"))
        self.assertEqual(cardgen.parse_later_research(reply), "Later studies questioned this fake claim.")

    def test_fenced(self):
        with mock.patch.dict(os.environ, {"FAKE_AI_MODE": "fenced"}):
            reply = self.ask(cardgen.build_messages(self.H, {"count": 1}))
        self.assertEqual(len(cardgen.parse_cards(reply, 1)[0]), 1)


class RealFailureWordingTest(FakeAITest):
    """Lines recorded from the real commands, and crashes of the kind they can produce."""

    def assert_says(self, stderr, sentence, assistant="codex", model="gpt-6.1-sol"):
        script = Path(self.temp.name) / "crash"
        output = Path(self.temp.name) / "stderr.txt"
        output.write_bytes(stderr)
        script.write_text(f'#!/bin/sh\ncat "{output}" >&2\nexit 1\n', encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        variable = "PBANKI_CODEX_BIN" if assistant == "codex" else "PBANKI_CLAUDE_BIN"
        with mock.patch.dict(os.environ, {variable: str(script)}):
            with self.assertRaises(ai.AIError) as caught:
                ai.query(model, MESSAGES)
        self.assertEqual(str(caught.exception), sentence)

    def test_a_signed_out_codex_is_told_to_sign_in(self):
        self.assert_says(
            b"OpenAI Codex v0.160.1\n--------\nuser\nsome book text\n"
            b"ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header, "
            b"url: https://api.openai.com/v1/responses, cf-ray: a54cbb99efb73a53-YYZ, request id: req_39294e3912414c1e8c5\n",
            "Codex isn't signed in. Open Terminal, run codex and sign in, then try again.",
        )

    def test_the_servers_own_message_replaces_its_json(self):
        self.assert_says(
            b'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error",'
            b'"message":"The \'no-such-model-xyz\' model is not supported when using Codex with a ChatGPT account.",'
            b'"code":"model_not_found"}}\n',
            "Codex couldn't answer: The 'no-such-model-xyz' model is not supported when using Codex with a ChatGPT account.",
        )

    def test_urls_and_ids_are_left_out(self):
        self.assert_says(
            b"ERROR: unexpected status 500 Internal Server Error: Upstream failed, url: https://x.example/v1, "
            b"cf-ray: abc, request id: req_1\n",
            "Codex couldn't answer: Upstream failed",
        )

    def test_book_text_echoed_before_the_error_cant_look_like_signed_out(self):
        self.assert_says(
            b"user\nHe said he was not logged in.\nERROR: the server is busy\n",
            "Codex couldn't answer: the server is busy",
        )

    def test_log_lines_and_warnings_are_not_the_reason(self):
        self.assert_says(
            b"warning: Code Mode is unavailable\n2026-11-03T16:57:48.406452Z ERROR codex_core::tools::router: error=x\n",
            "Codex couldn't answer, and gave no reason.",
        )

    def test_a_node_crash_gives_its_error_and_not_its_version(self):
        self.assert_says(
            b"node:internal/modules/cjs/loader:1228\n  throw err;\n  ^\n\nError: Cannot find module '/x/y.js'\n"
            b"    at Module._resolveFilename (node:internal/modules/cjs/loader:1225:15)\n"
            b"  code: 'MODULE_NOT_FOUND',\n  requireStack: []\n}\n\nNode.js v22.11.0\n",
            "Claude Code couldn't answer: Cannot find module '/x/y.js'",
            "claude",
            "claude-sonnet-5-5",
        )

    def test_colour_codes_are_removed(self):
        self.assert_says(
            b"\x1b[31mError:\x1b[0m something failed\n",
            "Claude Code couldn't answer: something failed",
            "claude",
            "claude-sonnet-5-5",
        )


class ProcessEdgeTest(FakeAITest):
    def script(self, body):
        script = Path(self.temp.name) / "fake"
        script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return mock.patch.dict(os.environ, {"PBANKI_CLAUDE_BIN": str(script)})

    def test_a_child_holding_the_output_open_doesnt_lose_the_answer(self):
        body = (
            "sleep 30 &\ncat >/dev/null\n"
            """echo '{"type":"result","is_error":false,"result":"THE ANSWER"}'\nexit 0\n"""
        )
        with self.script(body):
            started = time.monotonic()
            self.assertEqual(ai.query("claude-sonnet-5-5", MESSAGES), "THE ANSWER")
        self.assertLess(time.monotonic() - started, 2)

    def test_a_zero_byte_in_the_system_text_is_removed(self):
        ai.query("claude-sonnet-5-5", [{"role": "system", "content": "a\x00b"}, {"role": "user", "content": "u\x00v"}])
        run = self.runs()[0]
        self.assertEqual(run["argv"][run["argv"].index("--system-prompt") + 1], "ab")
        self.assertEqual(run["stdin"], "uv")

    def test_a_command_that_cant_start_gets_a_sentence(self):
        with mock.patch.object(subprocess, "Popen", side_effect=ValueError("embedded null byte")):
            with self.assertRaises(ai.AIError) as caught:
                ai.query("claude-sonnet-5-5", MESSAGES)
        self.assertEqual(str(caught.exception), "Claude Code could not be started: embedded null byte.")


@unittest.skipUnless(shutil.which("codex"), "codex is not installed")
class RealCodexToolsTest(unittest.TestCase):
    """What the real codex command offers the model, caught by a local stand-in for the API.
    Nothing leaves this Mac, and this fails if a new codex adds a tool the command doesn't turn off."""

    SHELL_TOOLS = {"shell", "local_shell", "exec_command", "write_stdin", "unified_exec", "view_image", "web_search"}

    def request_body(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.rfile.read(int(self.headers.get("content-length") or 0)))
                self.send_response(500)
                self.end_headers()

            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        provider = [
            'model_provider="stub"', f'model_providers.stub.base_url="http://127.0.0.1:{port}/v1"',
            'model_providers.stub.name="stub"', 'model_providers.stub.wire_api="responses"',
            "model_providers.stub.request_max_retries=0", "model_providers.stub.stream_max_retries=0",
            "model_providers.stub.requires_openai_auth=false",
        ]
        with tempfile.TemporaryDirectory() as folder:
            argv = ai.codex_command(shutil.which("codex"), "gpt-6.1-sol", Path(folder) / "answer.txt")
            at = argv.index("-o")
            argv[at:at] = [arg for value in provider for arg in ("-c", value)]
            done = subprocess.run(argv, input=b"hello", cwd=folder, capture_output=True, timeout=120, check=False)
        self.assertTrue(seen, done.stderr.decode("utf-8", "replace")[-500:])
        return json.loads(seen[0]), done.stderr.decode("utf-8", "replace")

    def test_nothing_offered_can_run_commands_or_use_the_users_setup(self):
        body, stderr = self.request_body()
        names = []

        def walk(value):
            if isinstance(value, dict):
                if value.get("type") in ("function", "custom", "namespace", "web_search", "local_shell", "image_generation"):
                    names.append(value.get("name") or value["type"])
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(body.get("tools"))
        walk([item for item in body["input"] if item.get("type") != "message"])
        self.assertFalse(self.SHELL_TOOLS & set(names), names)
        self.assertFalse([name for name in names if name.startswith("mcp__")], names)
        self.assertNotIn("skills_instructions", json.dumps(body))
        self.assertIn("Code Mode is unavailable", stderr, "exec has to fail closed")
        self.assertNotIn("hook:", stderr)
        self.assertNotIn("rmcp", stderr)


if __name__ == "__main__":
    unittest.main()
