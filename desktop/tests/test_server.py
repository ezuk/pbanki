"""The real server on a free port against the fake reader, called over real HTTP."""

import http.client
import io
import json
import os
import re
import select
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["PBANKI_SYNC_DELAY"] = "0"

from fixtures.make_reader import build_reader
from pbanki_desktop import companion, paths, save, server
from pbanki_desktop.service import Service
from pbanki_desktop.store import Store, book_id_for
from test_runs import FAKE_DIR, wait_until

LONG = 30
RUN_PY = paths.desktop_root() / "run.py"
PLAN = paths.desktop_root() / "PLAN.md"
NOTHING = object()
ALLOWED_WRITES = {"applications/pbanki/inbox.jsonl", "applications/pbanki/reviewed.jsonl"}
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()[:1] not in ("", "Z")


def changed_paths(before, after):
    return {path for path in set(before) | set(after) if before.get(path) != after.get(path)}


class Answer:
    def __init__(self, status, headers, data):
        self.status = status
        self.headers = headers
        self.data = data

    @property
    def json(self):
        return json.loads(self.data)


def fetch(url, method="GET", body=NOTHING, headers=None, data=None):
    if body is not NOTHING:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", **(headers or {})}
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with _opener.open(request, timeout=LONG) as response:
            return Answer(response.status, response.headers, response.read())
    except urllib.error.HTTPError as e:
        with e:
            return Answer(e.code, e.headers, e.read())


def raw_request(port, text):
    """Sends these exact bytes and reads to the end, with nothing tidied up on the way."""
    with socket.create_connection(("127.0.0.1", port), timeout=LONG) as sock:
        sock.sendall(text)
        found = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            found += chunk
    head, _, body = found.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, head.decode("latin-1"), body


class EventStream:
    """A real client of GET /api/events."""

    def __init__(self, base, query="", headers=None):
        request = urllib.request.Request(f"{base}/api/events{query}", headers=headers or {})
        self.response = _opener.open(request, timeout=LONG)
        self.comments = []

    def next(self, kind=None, topic=None):
        """The next event, past any that don't fit."""
        data = None
        while True:
            line = self.response.readline()
            if not line:
                raise EOFError("the stream ended")
            text = line.decode("utf-8").rstrip("\n")
            if text.startswith(":"):
                self.comments.append(text)
            elif text.startswith("data: "):
                data = json.loads(text[6:])
            elif not text and data is not None:
                found, data = data, None
                if (kind is None or found.get("type") == kind) and (topic is None or found.get("topic") == topic):
                    return found

    def next_comment(self):
        before = len(self.comments)
        while len(self.comments) == before:
            line = self.response.readline()
            if not line:
                raise EOFError("the stream ended")
            if line.startswith(b":"):
                self.comments.append(line.decode("utf-8").strip())
        return self.comments[-1]

    def ended(self):
        while True:
            line = self.response.readline()
            if not line:
                return True

    def close(self):
        self.response.close()


class Base(unittest.TestCase):
    POLL = 3600
    HEARTBEAT = server.HEARTBEAT_SECONDS

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pbanki-server-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.fake = build_reader(self.tmp / "reader", companion=True)
        self.mount = self.fake.mount
        self.away = self.tmp / "away"
        self.web = self.tmp / "web"
        self.assistant = self.tmp / "assistant"
        skill = self.assistant / ".claude" / "skills" / "book-companion"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("skill", encoding="utf-8")
        self.env = {
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
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.launched = []
        self.launch_works = True
        self.ejected = []
        self.eject_error = None
        self.svc = Service(store=Store(self.home / "pbanki.db"), launch=self._launch, eject=self._eject,
                           poll_seconds=self.POLL)
        self.http = server.Server(self.svc, 0, web_root=self.web, heartbeat=self.HEARTBEAT)
        self.addCleanup(self.http.stop, 5)
        self.http.start()
        self.assertTrue(wait_until(self._first_refresh_done, LONG))
        self.base = self.http.url
        self.port = self.http.port
        self.pride = book_id_for(self.fake.books["pride"])
        self.mostly = book_id_for(self.fake.books["mostly_done"])
        self.streams = []
        self.addCleanup(self._close_streams)

    def _first_refresh_done(self):
        thread = self.svc._refresh_thread
        return thread is not None and thread.ident is not None and not thread.is_alive()

    def _close_streams(self):
        for stream in self.streams:
            stream.close()

    def _launch(self, args):
        self.launched.append(args)
        return self.launch_works

    def _eject(self, mount):
        if self.eject_error:
            raise self.eject_error
        self.ejected.append(mount)

    def call(self, method, path, body=NOTHING, **kwargs):
        return fetch(self.base + path, method, body, **kwargs)

    def ok(self, method, path, body=NOTHING, **kwargs):
        answer = self.call(method, path, body, **kwargs)
        self.assertEqual(answer.status, 200, answer.data[:300])
        self.assertIn("application/json", answer.headers["Content-Type"])
        return answer.json

    def refused(self, status, method, path, body=NOTHING, text=None, **kwargs):
        answer = self.call(method, path, body, **kwargs)
        self.assertEqual(answer.status, status, answer.data[:300])
        self.assertIn("application/json", answer.headers["Content-Type"])
        payload = answer.json
        self.assertEqual(list(payload), ["error"])
        self.assertTrue(payload["error"].strip())
        self.assertNotIn("Traceback", payload["error"])
        if text:
            self.assertIn(text, payload["error"])
        return payload["error"]

    def open_book(self, book_id=None):
        return self.ok("POST", f"/api/books/{book_id or self.pride}/open")

    def hid(self, view, index=0):
        return view["highlights"][index]["hid"]

    def stream(self, query="", headers=None):
        made = EventStream(self.base, query, headers)
        self.streams.append(made)
        return made

    def unplug(self):
        self.mount.rename(self.away)

    def replug(self):
        self.away.rename(self.mount)

    def wait_idle(self):
        self.assertTrue(wait_until(lambda: not self.svc._jobs, LONG))

    def write_card(self, book_id, hid, front="Why this?", back="Because."):
        card = self.ok("POST", f"/api/books/{book_id}/highlights/{hid}/cards", {"kind": "qa"})["card"]
        return self.ok("PATCH", f"/api/cards/{card['id']}", {"front": front, "back": back})["card"]


class Routes(Base):
    """Every route in plan section 11, succeeding and failing."""

    def test_the_table_matches_section_11_of_the_plan(self):
        text = PLAN.read_text(encoding="utf-8")
        section = text[text.index("## 11. HTTP API"):text.index("## 12. Browser app")]
        planned = set()
        for method, address in re.findall(r"^(GET|POST|PATCH|DELETE)\s+(/api/\S+)", section, re.M):
            planned.add((method, re.sub(r"<\w+>", "*", address.split("?")[0])))
        self.assertGreater(len(planned), 30)
        self.assertEqual({(row[0], row[1]) for row in server._ROUTES}, planned)

    def test_state(self):
        state = self.ok("GET", "/api/state")
        self.assertTrue(state["reader"]["connected"])
        self.assertEqual(state["settings"]["mount"], str(self.mount))
        self.assertEqual(state["prompts"]["names"][0], "Standard")
        self.refused(405, "POST", "/api/state")

    def test_state_when_unplugged(self):
        self.unplug()
        self.assertFalse(self.ok("GET", "/api/state")["reader"]["connected"])

    def test_books(self):
        found = self.ok("GET", "/api/books")
        self.assertEqual(found["books"][0]["title"], "Pride and Prejudice")
        self.assertEqual(found["books"][0]["id"], self.pride)
        self.refused(405, "DELETE", "/api/books")

    def test_book(self):
        self.open_book()
        view = self.ok("GET", f"/api/books/{self.pride}")
        self.assertEqual(view["id"], self.pride)
        self.assertFalse(view["not_loaded"])
        self.assertEqual(len(view["highlights"]), 7)
        self.refused(404, "GET", "/api/books/nope", text="isn't on this computer")

    def test_book_open(self):
        view = self.open_book()
        self.assertEqual(view["id"], self.pride)
        self.assertEqual(view["run"]["phase"], "idle")
        self.assertEqual(self.ok("GET", "/api/state")["current_book"], self.pride)
        self.refused(404, "POST", "/api/books/nope/open")
        self.refused(405, "GET", f"/api/books/{self.pride}/open")

    def test_a_run_starts_only_when_asked(self):
        self.assertEqual(self.open_book()["run"]["phase"], "idle")
        self.assertFalse(self.ok("GET", f"/api/books/{self.pride}")["highlights"][0]["cards"])
        self.ok("POST", f"/api/books/{self.pride}/run", {"action": "start"})
        self.assertTrue(self.svc.runner.wait(self.pride, LONG))
        self.assertTrue(self.ok("GET", f"/api/books/{self.pride}")["highlights"][0]["cards"])

    def test_run_actions(self):
        self.open_book()
        with mock.patch.dict(os.environ, {"FAKE_AI_MODE": "slow"}):
            path = f"/api/books/{self.pride}/run"
            self.assertIn("run", self.ok("POST", path, {"action": "start", "options": {"count": 1, "kind": "qa"}}))
            self.ok("POST", path, {"action": "pause"})
            self.ok("POST", path, {"action": "resume"})
            self.ok("POST", path, {"action": "stop"})
            self.assertTrue(self.svc.runner.wait(self.pride, LONG))

    def test_run_refusals(self):
        self.open_book()
        path = f"/api/books/{self.pride}/run"
        self.refused(400, "POST", path, {"action": "fly"}, text="start, pause, resume or stop")
        self.refused(400, "POST", path, {}, text="start, pause, resume or stop")
        self.refused(400, "POST", path, {"action": 5}, text='"action" has to be text')
        self.refused(400, "POST", path, {"action": "start", "options": []}, text='"options"')
        self.refused(400, "POST", path, {"action": "start", "options": {"count": "two"}}, text='"count"')
        self.refused(400, "POST", path, {"action": "start", "options": {"count": True}}, text='"count"')
        self.refused(400, "POST", path, {"action": "start", "options": {"nope": 1}}, text="nope")
        self.refused(400, "POST", path, {"action": "start", "options": {"kind": "x"}}, text="kind of card")
        self.refused(404, "POST", path, {"action": "start", "options": {"prompt": "../.."}})
        self.refused(404, "POST", "/api/books/nope/run", {"action": "start"})

    def test_highlight_open(self):
        hid = self.hid(self.open_book())
        found = self.ok("POST", f"/api/books/{self.pride}/highlights/{hid}/open")
        self.assertTrue(found["highlight"]["opened"])
        self.refused(404, "POST", f"/api/books/{self.pride}/highlights/nope/open", text="isn't in this book")
        self.refused(404, "POST", f"/api/books/nope/highlights/{hid}/open")

    def test_highlight_skip(self):
        hid = self.hid(self.open_book())
        path = f"/api/books/{self.pride}/highlights/{hid}/skip"
        self.assertTrue(self.ok("POST", path, {"skipped": True})["highlight"]["isSkipped"])
        self.assertFalse(self.ok("POST", path, {"skipped": False})["highlight"]["isSkipped"])
        self.assertTrue(self.ok("POST", path)["highlight"]["isSkipped"])
        self.refused(400, "POST", path, {"skipped": "yes"}, text='"skipped"')
        self.refused(404, "POST", f"/api/books/{self.pride}/highlights/nope/skip", {"skipped": True})

    def test_add_card(self):
        hid = self.hid(self.open_book())
        path = f"/api/books/{self.pride}/highlights/{hid}/cards"
        self.assertEqual(self.ok("POST", path, {"kind": "qonly"})["card"]["kind"], "qonly")
        self.assertEqual(self.ok("POST", path)["card"]["kind"], "qa")
        self.refused(400, "POST", path, {"kind": "zz"}, text="kind of card")
        self.refused(400, "POST", path, {"kind": 1}, text='"kind"')
        self.refused(404, "POST", f"/api/books/{self.pride}/highlights/nope/cards", {"kind": "qa"})

    def test_suggest(self):
        hid = self.hid(self.open_book())
        path = f"/api/books/{self.pride}/highlights/{hid}/suggest"
        stream = self.stream(f"?topics=book:{self.pride}")
        self.assertTrue(self.ok("POST", path, {"count": 2, "kind": "qa", "sees": "The chapter"})["working"])
        self.assertEqual(stream.next("suggest")["state"], "done")
        self.assertTrue(self.ok("GET", f"/api/books/{self.pride}")["highlights"][0]["cards"])
        self.refused(400, "POST", path, {"count": "x"}, text='"count"')
        self.refused(400, "POST", path, {"sees": "x"}, text="how much the AI reads")
        self.refused(400, "POST", path, {"model": "nope"}, text="model")
        self.refused(404, "POST", f"/api/books/{self.pride}/highlights/nope/suggest", {"count": 1})

    def test_suggest_twice_at_once_is_refused(self):
        hid = self.hid(self.open_book())
        path = f"/api/books/{self.pride}/highlights/{hid}/suggest"
        with mock.patch.dict(os.environ, {"FAKE_AI_MODE": "slow"}):
            self.ok("POST", path, {"count": 1})
            self.refused(409, "POST", path, {"count": 1}, text="still working")

    def test_define(self):
        hid = self.hid(self.open_book())
        path = f"/api/books/{self.pride}/highlights/{hid}/define"
        stream = self.stream()
        self.assertTrue(self.ok("POST", path, {"term": "universally"})["working"])
        self.assertEqual(stream.next("define")["state"], "done")
        self.refused(400, "POST", path, {"term": "one two three four"}, text="1 to 3 words")
        self.refused(400, "POST", path, {}, text="1 to 3 words")
        self.refused(400, "POST", path, {"term": 5}, text='"term"')
        self.refused(404, "POST", f"/api/books/{self.pride}/highlights/nope/define", {"term": "x"})

    def test_passage(self):
        hid = self.hid(self.open_book())
        path = f"/api/books/{self.pride}/highlights/{hid}/passage"
        first = self.ok("GET", path)
        self.assertEqual(first["more"], 0)
        self.assertTrue(first["mark"])
        wider = self.ok("GET", f"{path}?more=2")
        self.assertEqual(wider["more"], 2)
        self.assertGreater(len(wider["after2"]), len(first["after2"]))
        self.refused(400, "GET", f"{path}?more=abc", text="amount of text")
        self.refused(404, "GET", f"/api/books/{self.pride}/highlights/nope/passage")
        self.unplug()
        self.assertTrue(self.ok("GET", f"{path}?more=2")["limited"])

    def test_card_edit_remove_restore(self):
        hid = self.hid(self.open_book())
        card = self.ok("POST", f"/api/books/{self.pride}/highlights/{hid}/cards", {"kind": "qa"})["card"]
        path = f"/api/cards/{card['id']}"
        self.assertEqual(self.ok("PATCH", path, {"front": "New?", "back": "Yes."})["card"]["front"], "New?")
        self.refused(400, "PATCH", path, {"front": 5}, text='"front"')
        self.refused(400, "PATCH", path, {"title": "x"}, text="title")
        self.assertEqual(self.ok("DELETE", path)["removed"]["id"], card["id"])
        self.refused(409, "PATCH", path, {"front": "Late?"})
        self.assertEqual(self.ok("POST", f"{path}/restore")["card"]["state"], "draft")
        self.refused(404, "PATCH", "/api/cards/nope", {"front": "x"})
        self.refused(404, "DELETE", "/api/cards/nope")
        self.refused(404, "POST", "/api/cards/nope/restore")

    def test_card_rewrite(self):
        hid = self.hid(self.open_book())
        card = self.write_card(self.pride, hid)
        path = f"/api/cards/{card['id']}/rewrite"
        stream = self.stream()
        self.ok("POST", path, {"instruction": "Shorter", "model": "claude-haiku-5-5"})
        self.assertEqual(stream.next("rewrite")["state"], "done")
        self.refused(400, "POST", path, {"instruction": 5}, text='"instruction"')
        self.refused(400, "POST", path, {"model": "nope"}, text="model")
        self.refused(404, "POST", "/api/cards/nope/rewrite", {})

    def test_connections(self):
        self.open_book()
        path = f"/api/books/{self.pride}/connections/find"
        stream = self.stream()
        self.ok("POST", path, {"scope": "all", "count": 2, "hint": "across chapters"})
        self.assertEqual(stream.next("find")["state"], "done")
        self.refused(400, "POST", path, {"scope": "x"}, text="pending or all")
        self.refused(400, "POST", path, {"chapters": [5]}, text="chapters")
        self.refused(400, "POST", path, {"chapters": "x"}, text='"chapters"')
        self.refused(404, "POST", "/api/books/nope/connections/find", {})

        item = self.ok("GET", f"/api/books/{self.pride}")["connections"][0]
        path = f"/api/connections/{item['id']}"
        self.assertTrue(self.ok("POST", f"{path}/open"))
        self.assertTrue(self.ok("PATCH", path, {"front": "Linked?"}))
        self.assertEqual(self.ok("GET", f"/api/books/{self.pride}")["connections"][0]["front"], "Linked?")
        self.refused(400, "PATCH", path, {"front": []}, text='"front"')
        self.assertTrue(self.ok("DELETE", path))
        self.assertFalse(self.ok("GET", f"/api/books/{self.pride}")["connections"])
        self.refused(404, "POST", "/api/connections/nope/open")
        self.refused(404, "PATCH", "/api/connections/nope", {"front": "x"})
        self.refused(404, "DELETE", "/api/connections/nope")

    def test_ideas(self):
        self.open_book()
        idea = self.svc._store.add_idea(self.pride, "Chapter", "T", "N", "Q", "F?", "B")
        path = f"/api/ideas/{idea['id']}"
        self.assertTrue(self.ok("POST", f"{path}/open"))
        self.assertTrue(self.ok("PATCH", path, {"back": "Better."}))
        self.assertEqual(self.ok("GET", f"/api/books/{self.pride}")["ideas"][0]["back"], "Better.")
        self.refused(400, "PATCH", path, {"back": 1.5}, text='"back"')
        self.assertTrue(self.ok("DELETE", path))
        self.assertFalse(self.ok("GET", f"/api/books/{self.pride}")["ideas"])
        self.refused(404, "POST", "/api/ideas/nope/open")
        self.refused(404, "PATCH", "/api/ideas/nope", {"front": "x"})
        self.refused(404, "DELETE", "/api/ideas/nope")

    def test_save_preview(self):
        hid = self.hid(self.open_book())
        self.write_card(self.pride, hid)
        preview = self.ok("GET", f"/api/books/{self.pride}/save")
        self.assertEqual(preview["saveCount"], 1)
        self.assertFalse(self.ok("GET", f"/api/books/{self.pride}/save?mark_connection_sources=false")["markMeta"])
        self.assertTrue(self.ok("GET", f"/api/books/{self.pride}/save?mark_connection_sources=1")["markMeta"])
        self.refused(400, "GET", f"/api/books/{self.pride}/save?mark_connection_sources=maybe")
        self.refused(404, "GET", "/api/books/nope/save")

    def test_save_writes_only_the_inbox_files_on_the_reader(self):
        view = self.open_book()
        before = self.fake.snapshot()
        self.write_card(self.pride, self.hid(view), front="Saved over HTTP?")
        answer = self.ok("POST", f"/api/books/{self.pride}/save", {"mark_connection_sources": True})
        self.assertEqual(answer["status"], "verified")
        self.assertEqual(answer["summary"]["cards"], 1)
        self.assertLessEqual(changed_paths(before, self.fake.snapshot()), ALLOWED_WRITES)
        self.assertIn("Saved over HTTP?", (self.mount / paths.INBOX_FILE).read_text(encoding="utf-8"))
        self.refused(409, "POST", f"/api/books/{self.pride}/save", {})

    def test_save_refusals(self):
        view = self.open_book()
        path = f"/api/books/{self.pride}/save"
        self.refused(400, "POST", path, {"mark_connection_sources": "yes"}, text='"mark_connection_sources"')
        card = self.ok("POST", f"/api/books/{self.pride}/highlights/{self.hid(view)}/cards", {"kind": "qonly"})["card"]
        self.ok("PATCH", f"/api/cards/{card['id']}", {"front": "Why?"})
        self.refused(409, "POST", path, {}, text="no cards to save")
        self.refused(404, "POST", "/api/books/nope/save", {})

    def test_save_retry_refusals(self):
        self.open_book()
        self.refused(409, "POST", f"/api/books/{self.pride}/save/retry", text="nothing waiting")
        self.refused(404, "POST", "/api/books/nope/save/retry")

    def test_eject(self):
        self.assertTrue(self.ok("POST", "/api/reader/eject")["ejected"])
        self.assertEqual(self.ejected, [str(self.mount)])
        self.eject_error = save.EjectError("Something on this Mac is still using the reader.")
        self.refused(409, "POST", "/api/reader/eject", text="still using")
        self.refused(405, "GET", "/api/reader/eject")

    def test_choose_folder(self):
        with mock.patch.dict(os.environ, {"PBANKI_CHOOSE_FOLDER_CMD": f"/bin/echo {self.tmp}/picked/"}):
            self.assertEqual(self.ok("POST", "/api/reader/choose-folder")["mount"], f"{self.tmp}/picked")
        self.assertEqual(self.ok("GET", "/api/state")["settings"]["mount"], f"{self.tmp}/picked")
        with mock.patch.dict(os.environ, {"PBANKI_CHOOSE_FOLDER_CMD": "/usr/bin/false"}):
            self.assertIsNone(self.ok("POST", "/api/reader/choose-folder")["mount"])
        self.assertEqual(self.ok("GET", "/api/state")["settings"]["mount"], f"{self.tmp}/picked")
        with mock.patch.dict(os.environ, {"PBANKI_CHOOSE_FOLDER_CMD": "/nonexistent/command"}):
            self.refused(500, "POST", "/api/reader/choose-folder", text="Type the folder")
        self.refused(405, "GET", "/api/reader/choose-folder")

    def test_plugin_update(self):
        before = self.fake.snapshot()
        found = self.ok("POST", "/api/reader/plugin/update")
        self.assertIn("copied", found)
        self.assertIn("unchanged", found)
        changed = changed_paths(before, self.fake.snapshot())
        self.assertTrue(all(path.startswith(paths.PLUGIN_DIR) for path in changed), changed)
        self.unplug()
        self.refused(409, "POST", "/api/reader/plugin/update", text="Plug in")

    def test_settings(self):
        self.assertEqual(self.ok("PATCH", "/api/settings", {"count": 5, "sees": "The chapter"})["settings"]["count"], 5)
        self.assertEqual(self.ok("GET", "/api/state")["settings"]["sees"], "The chapter")
        self.refused(400, "PATCH", "/api/settings", {"nope": 1}, text="nope")
        self.refused(400, "PATCH", "/api/settings", {"assistants": {"claude": False, "codex": False}}, text="stay on")
        self.refused(400, "PATCH", "/api/settings", {"mount": "relative/path"}, text="full path")
        self.refused(400, "PATCH", "/api/settings", {"model": "nope"})
        self.refused(404, "PATCH", "/api/settings", {"last_prompt": "../.."})

    def test_prompt(self):
        found = self.ok("GET", "/api/prompts/Standard")
        self.assertEqual(found["name"], "Standard")
        self.assertTrue(found["files"][0]["text"])
        self.assertEqual(self.ok("GET", "/api/prompts/Self%20Help")["name"], "Self Help")
        self.refused(404, "GET", "/api/prompts/nope", text="prompts folder")
        self.refused(404, "GET", "/api/prompts/..%2F..%2Fetc", text="prompts folder")
        self.refused(404, "GET", "/api/prompts/%ff")
        self.refused(404, "GET", "/api/prompts/%00")
        self.refused(404, "GET", "/api/books/%00")
        self.refused(404, "GET", "/api/books/%ff")

    def test_a_prompt_folder_named_like_an_action_can_be_read(self):
        (paths.prompts_dir() / "open").mkdir()
        self.assertEqual(self.ok("GET", "/api/prompts/open")["name"], "open")
        self.refused(400, "POST", "/api/prompts/open", {}, text='"name" is missing')

    def test_prompt_open_and_reveal(self):
        for action, flag in (("open", "-t"), ("reveal", "-R")):
            self.launched.clear()
            path = f"/api/prompts/{action}"
            found = self.ok("POST", path, {"name": "Standard", "file": "questions.md"})
            self.assertEqual(found["file"], "questions.md")
            self.assertEqual(self.launched, [["open", flag, found["opened"]]])
            self.refused(400, "POST", path, {"name": "Standard"}, text='"file" is missing')
            self.refused(400, "POST", path, {"file": "questions.md"}, text='"name" is missing')
            self.refused(400, "POST", path, {"name": 5, "file": "questions.md"}, text='"name"')
            self.refused(404, "POST", path, {"name": "../..", "file": "x"}, text="prompts folder")
            self.refused(404, "POST", path, {"name": "Standard", "file": "../../secret"})
            self.launch_works = False
            self.refused(500, "POST", path, {"name": "Standard", "file": "questions.md"}, text="couldn't be opened")
            self.launch_works = True

    def test_companion_create_copy(self):
        self.open_book()
        before = self.fake.snapshot()
        base = f"/api/books/{self.pride}/companion"
        self.refused(409, "POST", f"{base}/copy", text="isn't ready")
        self.refused(409, "POST", f"{base}/keep", text="isn't ready")
        self.assertTrue(self.ok("POST", f"{base}/create")["companion"]["running"])
        output = companion._work_output(self.fake.books["pride"])
        output.write_text("# Pride\n\n## 1. Overview\n\nManners.\n", encoding="utf-8")
        past = time.time() - 60
        os.utime(output, (past, past))
        self.assertTrue(wait_until(lambda: self.ok("GET", f"/api/books/{self.pride}")["companion"].get("ready"), LONG))
        self.assertTrue(self.ok("POST", f"{base}/copy")["companion"]["have"])
        changed = changed_paths(before, self.fake.snapshot())
        self.assertEqual(len(changed), 1)
        self.assertTrue(next(iter(changed)).endswith(".companion.md"))
        self.refused(409, "POST", f"{base}/create", text="already has")

    def test_companion_keep(self):
        self.open_book()
        before = self.fake.snapshot()
        base = f"/api/books/{self.pride}/companion"
        self.ok("POST", f"{base}/create")
        output = companion._work_output(self.fake.books["pride"])
        output.write_text("# Pride\n\n## 1. Overview\n\nManners.\n", encoding="utf-8")
        past = time.time() - 60
        os.utime(output, (past, past))
        self.assertTrue(self.ok("POST", f"{base}/keep")["companion"]["have"])
        self.assertEqual(self.fake.snapshot(), before)

    def test_last_found_assistant_stays_on(self):
        with mock.patch.dict(os.environ, {"PBANKI_CODEX_BIN": str(self.tmp / "no-codex")}):
            before = self.ok("GET", "/api/state")["settings"]["assistants"]
            self.refused(400, "PATCH", "/api/settings", {"assistants": {"claude": False}}, text="stay on")
            self.assertEqual(self.ok("GET", "/api/state")["settings"]["assistants"], before)

    def test_companion_forget(self):
        self.open_book()
        base = f"/api/books/{self.pride}/companion"
        self.refused(409, "POST", f"{base}/forget", text="isn't being created")
        self.assertTrue(self.ok("POST", f"{base}/create")["companion"]["running"])
        output = companion._work_output(self.fake.books["pride"])
        output.write_text("", encoding="utf-8")
        answer = self.ok("POST", f"{base}/forget")["companion"]
        self.assertTrue(answer["none"])
        self.assertEqual(answer["status"], "None yet")
        self.assertFalse(output.exists())
        self.assertTrue(self.ok("POST", f"{base}/create")["companion"]["running"])

    def test_companion_forget_still_notices_the_file(self):
        self.open_book()
        base = f"/api/books/{self.pride}/companion"
        self.ok("POST", f"{base}/create")
        self.ok("POST", f"{base}/forget")
        output = companion._work_output(self.fake.books["pride"])
        output.write_text("# Pride\n\n## 1. Overview\n\nManners.\n", encoding="utf-8")
        past = time.time() - 60
        os.utime(output, (past, past))
        self.svc._poll_creations()
        self.assertEqual(self.svc._creations[self.pride]["stage"], "found")

    def test_companion_forget_keeps_a_finished_file(self):
        self.open_book()
        base = f"/api/books/{self.pride}/companion"
        self.ok("POST", f"{base}/create")
        output = companion._work_output(self.fake.books["pride"])
        output.write_text("# Pride\n\n## 1. Overview\n\nManners.\n", encoding="utf-8")
        past = time.time() - 60
        os.utime(output, (past, past))
        self.refused(409, "POST", f"{base}/forget", text="isn't being created")
        self.assertTrue(output.exists())

    def test_companion_unknown_book(self):
        for action in ("create", "copy", "keep", "forget"):
            self.refused(404, "POST", f"/api/books/nope/companion/{action}")

    def test_unknown_addresses_are_json(self):
        for path in ("/api", "/api/", "/api/nope", "/api/books/", f"/api/books/{self.pride}/nope", "/api/state/"):
            self.refused(404, "GET", path)

    def test_wrong_method_lists_the_right_ones(self):
        answer = self.call("POST", "/api/cards/x")
        self.assertEqual(answer.status, 405)
        self.assertEqual(answer.headers["Allow"], "DELETE, PATCH")
        self.assertEqual(self.call("PUT", "/api/state").status, 405)


class Bodies(Base):
    def post(self, data, content_type="application/json", path="/api/settings", method="PATCH"):
        headers = {"Content-Type": content_type} if content_type else {}
        return self.call(method, path, data=data, headers=headers)

    def assert_settings_untouched(self):
        self.assertEqual(self.svc._store.get_settings()["count"], 3)

    def test_json_content_type_is_required_for_a_body(self):
        for kind in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x",
                     "text/json", "application/jsonp", None):
            answer = self.post(b'{"count": 9}', kind)
            self.assertEqual(answer.status, 415, kind)
            self.assertIn("JSON", answer.json["error"])
        self.assert_settings_untouched()

    def test_json_content_type_can_name_utf8_and_any_case(self):
        for kind in ("application/json", "application/json; charset=utf-8", "Application/JSON;charset=UTF-8"):
            self.assertEqual(self.post(b'{"count": 4}', kind).status, 200, kind)

    def test_another_charset_is_refused(self):
        self.assertEqual(self.post(b'{"count": 9}', "application/json; charset=latin-1").status, 415)
        self.assert_settings_untouched()

    def test_an_empty_post_from_a_form_is_refused(self):
        answer = self.call("POST", "/api/reader/eject", data=b"", headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(answer.status, 415)
        self.assertEqual(self.ejected, [])

    def test_an_empty_post_with_no_type_is_allowed(self):
        self.assertEqual(self.call("POST", "/api/reader/eject").status, 200)

    def test_not_json(self):
        for data in (b"{nope", b"\xff\xfe", b'{"count": NaN}', b'{"count": Infinity}', b'"\\ud83d"',
                     b'{"mount": "\\ud83d"}', b"[" * 200000):
            answer = self.post(data)
            self.assertEqual(answer.status, 400, data[:20])
            self.assertIn("error", answer.json)
        self.assert_settings_untouched()

    def test_an_empty_body_counts_as_no_fields(self):
        self.assertEqual(self.post(b"").status, 200)
        self.assert_settings_untouched()

    def test_a_body_has_to_be_an_object(self):
        for data in (b"[]", b"3", b'"x"', b"null", b"true"):
            answer = self.post(data)
            self.assertEqual(answer.status, 400, data)
            self.assertIn("object", answer.json["error"])

    def test_oversized_body_sent_in_full(self):
        body = json.dumps({"mount": "x" * (server.MAX_BODY + 1)}).encode("utf-8")
        answer = self.post(body)
        self.assertEqual(answer.status, 413)
        self.assertIn("too large", answer.json["error"])
        self.assertEqual(answer.headers["Connection"], "close")
        self.assert_settings_untouched()

    def test_a_body_just_under_the_limit_is_read(self):
        padding = "x" * (server.MAX_BODY - 200)
        card = self.svc._store.add_card(self.pride, self.hid(self.open_book()), "qa")
        answer = self.call("PATCH", f"/api/cards/{card['id']}", {"front": padding})
        self.assertEqual(answer.status, 200)
        self.assertEqual(self.svc._store.get_card(card["id"])["front"], padding)

    def test_oversized_claim_is_refused_without_waiting_for_the_bytes(self):
        started = time.monotonic()
        status, head, body = raw_request(
            self.port,
            f"PATCH /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {10 ** 12}\r\n\r\n{{".encode(),
        )
        self.assertEqual(status, 413)
        self.assertIn("too large", json.loads(body)["error"])
        self.assertLess(time.monotonic() - started, 5)

    def test_broken_lengths(self):
        common = f"Host: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n"
        for length in ("-1", "abc", "1_0", "+5", "9" * 40):
            status, _, body = raw_request(
                self.port, f"PATCH /api/settings HTTP/1.1\r\n{common}Content-Length: {length}\r\n\r\n".encode())
            self.assertEqual(status, 400, length)
            self.assertIn("error", json.loads(body))
        status, _, _ = raw_request(
            self.port,
            f"PATCH /api/settings HTTP/1.1\r\n{common}Content-Length: 2\r\nContent-Length: 3\r\n\r\n{{}}".encode())
        self.assertEqual(status, 400)

    def test_chunked_bodies_are_refused(self):
        status, _, body = raw_request(
            self.port,
            f"PATCH /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\n"
            'Transfer-Encoding: chunked\r\n\r\nc\r\n{"count": 9}\r\n0\r\n\r\n'.encode(),
        )
        self.assertEqual(status, 411)
        self.assertIn("error", json.loads(body))
        self.assert_settings_untouched()

    def test_a_body_that_stops_short_is_refused(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=LONG) as sock:
            sock.sendall(f"PATCH /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                         "Content-Type: application/json\r\nContent-Length: 50\r\n\r\n{".encode())
            sock.shutdown(socket.SHUT_WR)
            found = b""
            while chunk := sock.recv(4096):
                found += chunk
        self.assertIn(b" 400 ", found.split(b"\r\n")[0])

    def test_get_with_a_body_is_refused(self):
        self.assertEqual(self.call("GET", "/api/state", data=b"{}").status, 400)

    def test_every_field_with_the_wrong_type_is_a_plain_400(self):
        view = self.open_book()
        hid = self.hid(view)
        card = self.write_card(self.pride, hid)
        connection = self.svc._store.add_connection(self.pride, "T", "F?", "B", [])
        idea = self.svc._store.add_idea(self.pride, "Chapter", "T", "N", "Q", "F?", "B")
        ids = {"cards": card["id"], "connections": connection["id"], "ideas": idea["id"]}
        seen = 0
        for method, address, fields, call in server._ROUTES:
            if method == "GET":
                continue
            path = address
            for name, value in ids.items():
                path = path.replace(f"/{name}/*", f"/{name}/{value}")
            path = path.replace("/books/*", f"/books/{self.pride}").replace("/highlights/*", f"/highlights/{hid}")
            for name, kind in fields.items():
                wrong = [1.5] if kind is not list else "x"
                answer = self.call(method, path, {name: wrong})
                self.assertEqual(answer.status, 400, f"{method} {path} {name}: {answer.data[:200]}")
                seen += 1
            answer = self.call(method, path, {"surely_not_a_field": 1})
            self.assertEqual(answer.status, 400, f"{method} {path}")
            answer = self.call(method, path, [1])
            self.assertEqual(answer.status, 400, f"{method} {path}")
        self.assertGreater(seen, 30)

    def test_silly_values_of_the_right_type_never_cause_a_500(self):
        hid = self.hid(self.open_book())
        for changes in ({"assistants": {"x": 1}}, {"assistants": {"claude": 1}}, {"count": 10 ** 30}, {"mount": " "},
                        {"kind": "\0"}, {"sees": ""}, {"companion_send": "x" * 1000}):
            self.assertLess(self.call("PATCH", "/api/settings", changes).status, 500, changes)
        path = f"/api/books/{self.pride}/highlights/{hid}/suggest"
        for options in ({"count": 10 ** 30}, {"count": -5}, {"prompt": "\0"}, {"hint": "\0"}, {"model": ""}):
            self.assertLess(self.call("POST", path, options).status, 500, options)
            self.wait_idle()


class Security(Base):
    def test_binds_only_to_loopback(self):
        self.assertEqual(self.http.server_address[0], "127.0.0.1")
        self.assertEqual(self.http.socket.getsockname()[0], "127.0.0.1")

    def test_foreign_hosts_are_refused(self):
        bad = ["evil.example", f"evil.example:{self.port}", "127.0.0.1", "localhost", f"127.0.0.1:{self.port + 1}",
               f"[::1]:{self.port}", f"127.0.0.1.evil.example:{self.port}", f"0.0.0.0:{self.port}"]
        for host in bad:
            for path in ("/api/state", "/"):
                answer = self.call("GET", path, headers={"Host": host})
                self.assertEqual(answer.status, 403, host)
                self.assertIn("own address", answer.json["error"])
            with self.assertRaises(urllib.error.HTTPError) as caught:
                EventStream(self.base, headers={"Host": host})
            with caught.exception as refused:
                self.assertEqual(refused.code, 403, host)

    def test_own_hosts_are_answered(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"LOCALHOST:{self.port}"):
            self.assertEqual(self.call("GET", "/api/state", headers={"Host": host}).status, 200, host)

    def test_a_missing_or_doubled_host_is_refused(self):
        self.assertEqual(raw_request(self.port, b"GET /api/state HTTP/1.1\r\n\r\n")[0], 403)
        own = f"Host: 127.0.0.1:{self.port}\r\n"
        self.assertEqual(raw_request(self.port, f"GET /api/state HTTP/1.1\r\n{own}{own}\r\n".encode())[0], 403)
        self.assertEqual(raw_request(self.port, f"GET /api/state HTTP/1.1\r\n{own}Host: evil.example\r\n\r\n".encode())[0], 403)

    def test_a_foreign_host_on_a_change_does_nothing(self):
        self.refused(403, "POST", "/api/reader/eject", headers={"Host": "evil.example"})
        self.assertEqual(self.ejected, [])

    def test_foreign_origins_are_refused_on_every_change(self):
        bad = ["https://evil.example", "null", f"http://evil.example:{self.port}", f"https://127.0.0.1:{self.port}",
               "http://127.0.0.1", f"http://127.0.0.1:{self.port + 1}", f"http://[::1]:{self.port}"]
        for origin in bad:
            for method, path in (("POST", "/api/reader/eject"), ("PATCH", "/api/settings"), ("DELETE", "/api/cards/x")):
                answer = self.call(method, path, {}, headers={"Origin": origin})
                self.assertEqual(answer.status, 403, origin)
                self.assertIn("its own page", answer.json["error"])
        self.assertEqual(self.ejected, [])

    def test_own_origins_and_no_origin_are_accepted(self):
        for headers in ({}, {"Origin": self.base}, {"Origin": f"http://localhost:{self.port}"}):
            self.assertEqual(self.call("PATCH", "/api/settings", {"count": 4}, headers=headers).status, 200, headers)

    def test_a_foreign_origin_never_gets_cors_headers(self):
        for method, path in (("OPTIONS", "/api/settings"), ("GET", "/api/state"), ("OPTIONS", "/")):
            answer = self.call(method, path, headers={"Origin": "https://evil.example"})
            self.assertFalse([k for k in answer.headers if k.lower().startswith("access-control")])
        self.assertEqual(self.call("OPTIONS", "/api/settings").status, 405)

    def test_a_cross_site_form_cannot_change_anything(self):
        # What a browser sends for <form method=post enctype=text/plain>, with and without Origin
        for headers in ({"Content-Type": "text/plain", "Origin": "https://evil.example"},
                        {"Content-Type": "text/plain"}):
            answer = self.call("PATCH", "/api/settings", data=b'{"count": 9, "x": "=y"}', headers=headers)
            self.assertIn(answer.status, (403, 415))
        self.assertEqual(self.svc._store.get_settings()["count"], 3)

    def test_error_requests_get_json_not_html(self):
        status, head, body = raw_request(self.port, b"NONSENSE\r\n\r\n")
        self.assertEqual(status, 400)
        self.assertIn("application/json", head)
        self.assertIn("error", json.loads(body))
        status, head, body = raw_request(self.port, f"BREW /api/state HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode())
        self.assertEqual(status, 501)
        self.assertIn("error", json.loads(body))
        status, head, body = raw_request(self.port, b"GET /" + b"x" * 70000 + b" HTTP/1.1\r\n\r\n")
        self.assertEqual(status, 414)
        self.assertIn("error", json.loads(body))
        status, head, body = raw_request(
            self.port, f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Long: {'x' * 70000}\r\n\r\n".encode())
        self.assertEqual(status, 431)
        self.assertIn("error", json.loads(body))

    def test_an_absolute_address_is_refused(self):
        status, _, _ = raw_request(
            self.port, f"GET http://evil.example/api/state HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode())
        self.assertEqual(status, 400)

    def test_an_unexpected_failure_is_a_plain_500_with_the_details_on_stderr(self):
        secret = "/Users/someone/secret-path"
        with mock.patch.object(self.svc, "state", side_effect=RuntimeError(secret)), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            answer = self.call("GET", "/api/state")
        self.assertEqual(answer.status, 500)
        self.assertEqual(answer.json, {"error": "Something went wrong. Your work is safe on this computer."})
        self.assertNotIn(b"Traceback", answer.data)
        self.assertNotIn(secret.encode(), answer.data)
        self.assertIn("Traceback", stderr.getvalue())
        self.assertIn(secret, stderr.getvalue())

    def test_an_answer_that_cannot_be_json_is_a_plain_500(self):
        with mock.patch.object(self.svc, "state", return_value={"bad": object()}), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            answer = self.call("GET", "/api/state")
        self.assertEqual(answer.status, 500)
        self.assertIn("error", answer.json)

    def test_the_server_keeps_going_after_a_failure(self):
        with mock.patch.object(self.svc, "books", side_effect=KeyError("x")), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(self.call("GET", "/api/books").status, 500)
        self.assertEqual(self.call("GET", "/api/books").status, 200)

    def test_every_answer_says_no_store(self):
        self.web.mkdir()
        (self.web / "index.html").write_text("hi", encoding="utf-8")
        for path in ("/", "/api/state", "/api/nope", "/nope"):
            self.assertEqual(self.call("GET", path).headers["Cache-Control"], "no-store", path)


class Static(Base):
    def setUp(self):
        super().setUp()
        self.web.mkdir()
        (self.web / "index.html").write_text("<!doctype html><title>pbAnki</title>", encoding="utf-8")
        (self.web / "companion.css").write_text("body{}", encoding="utf-8")
        (self.web / "js").mkdir()
        (self.web / "js" / "app.js").write_text("export default 1", encoding="utf-8")
        (self.web / "js" / "with space.js").write_text("export default 2", encoding="utf-8")
        (self.web / "fonts").mkdir()
        (self.web / "fonts" / "a.woff2").write_bytes(b"wOF2\x00\xff")
        (self.web / "fonts" / "OFL.txt").write_text("licence", encoding="utf-8")
        (self.web / "icon.svg").write_text("<svg/>", encoding="utf-8")
        (self.web / "mystery.xyz").write_bytes(b"?")
        (self.web / ".hidden").write_text("no", encoding="utf-8")
        (self.tmp / "outside.txt").write_text("outside", encoding="utf-8")

    def raw_get(self, path):
        return raw_request(self.port, f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nConnection: close\r\n\r\n".encode())

    def test_files_have_the_right_types(self):
        expected = {
            "/": ("text/html; charset=utf-8", b"<!doctype html><title>pbAnki</title>"),
            "/index.html": ("text/html; charset=utf-8", b"<!doctype html><title>pbAnki</title>"),
            "/companion.css": ("text/css; charset=utf-8", b"body{}"),
            "/js/app.js": ("text/javascript; charset=utf-8", b"export default 1"),
            "/js/with%20space.js": ("text/javascript; charset=utf-8", b"export default 2"),
            "/fonts/a.woff2": ("font/woff2", b"wOF2\x00\xff"),
            "/fonts/OFL.txt": ("text/plain; charset=utf-8", b"licence"),
            "/icon.svg": ("image/svg+xml", b"<svg/>"),
            "/mystery.xyz": ("application/octet-stream", b"?"),
        }
        for path, (kind, data) in expected.items():
            answer = self.call("GET", path)
            self.assertEqual(answer.status, 200, path)
            self.assertEqual(answer.headers["Content-Type"], kind, path)
            self.assertEqual(answer.data, data, path)
            self.assertEqual(answer.headers["Cache-Control"], "no-store", path)
            self.assertEqual(answer.headers["Content-Length"], str(len(data)), path)
            self.assertEqual(answer.headers["X-Content-Type-Options"], "nosniff", path)

    def test_a_reload_gets_the_current_code(self):
        self.assertEqual(self.call("GET", "/js/app.js").data, b"export default 1")
        (self.web / "js" / "app.js").write_text("export default 3", encoding="utf-8")
        self.assertEqual(self.call("GET", "/js/app.js").data, b"export default 3")

    def test_path_traversal_is_refused_however_it_is_spelled(self):
        attempts = [
            "/../outside.txt", "/..%2foutside.txt", "/%2e%2e/outside.txt", "/%2e%2e%2foutside.txt",
            "/js/../../outside.txt", "/js/..%2f..%2foutside.txt", "/js/%2e%2e/%2e%2e/outside.txt",
            "/..%5coutside.txt", "/js/..\\..\\outside.txt", "/%252e%252e/outside.txt", "//../outside.txt",
            "/./index.html", "/js/./app.js", "/js//app.js", "/js/", "/js", "/.hidden", "/%2ehidden",
            "/index.html%00.js", "/%ff", "/" + "/".join([".."] * 12) + "/etc/passwd",
            "/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
        ]
        for path in attempts:
            status, head, body = self.raw_get(path)
            self.assertEqual(status, 404, path)
            self.assertIn("application/json", head, path)
            self.assertNotIn(b"outside", body, path)
            self.assertNotIn(b"root:", body, path)

    def test_an_absolute_file_path_stays_inside_the_folder(self):
        secret = self.tmp / "outside.txt"
        self.assertEqual(self.raw_get(f"/{secret}")[0], 404)
        self.assertEqual(self.raw_get(f"/%2F{str(secret)[1:]}")[0], 404)

    def test_the_design_folder_is_never_served(self):
        for path in ("/design/mock.dc.html", "/../design/mock.dc.html", "/%2e%2e/design/mock.dc.html",
                     "/design/mock-data.js", "/desktop/design/mock.dc.html", "/PLAN.md", "/../PLAN.md", "/run.py"):
            self.assertEqual(self.raw_get(path)[0], 404, path)

    def test_the_default_folder_is_desktop_web(self):
        self.assertEqual(server.WEB_ROOT, paths.desktop_root() / "web")
        self.assertEqual(server.WEB_ROOT.name, "web")

    def test_a_link_out_of_the_folder_is_refused(self):
        (self.web / "link.txt").symlink_to(self.tmp / "outside.txt")
        (self.web / "linked").symlink_to(self.tmp)
        self.assertEqual(self.raw_get("/link.txt")[0], 404)
        self.assertEqual(self.raw_get("/linked/outside.txt")[0], 404)

    def test_only_get_and_head_read_files(self):
        answer = self.call("HEAD", "/js/app.js")
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.data, b"")
        self.assertEqual(answer.headers["Content-Length"], "16")
        self.assertEqual(self.call("POST", "/js/app.js").status, 405)
        self.assertEqual(self.call("DELETE", "/js/app.js").status, 405)
        self.assertEqual(self.call("HEAD", "/api/state").status, 405)
        self.assertTrue((self.web / "js" / "app.js").exists())

    def test_a_file_that_cannot_be_read_is_not_found(self):
        file = self.web / "locked.js"
        file.write_text("x", encoding="utf-8")
        file.chmod(0)
        self.addCleanup(file.chmod, stat.S_IRUSR | stat.S_IWUSR)
        if os.geteuid() != 0:
            self.assertEqual(self.call("GET", "/locked.js").status, 404)


class MissingWeb(Base):
    def test_a_missing_folder_gives_a_plain_page(self):
        self.assertFalse(self.web.exists())
        for path in ("/", "/index.html", "/js/app.js"):
            answer = self.call("GET", path)
            self.assertEqual(answer.status, 503, path)
            self.assertIn("text/html", answer.headers["Content-Type"])
            self.assertIn(b"screens aren't here yet", answer.data)
            self.assertIn(str(self.web).encode(), answer.data)
        self.assertEqual(self.call("GET", "/api/state").status, 200)

    def test_a_folder_with_no_index_gives_a_plain_page(self):
        self.web.mkdir()
        answer = self.call("GET", "/")
        self.assertEqual(answer.status, 503)
        self.assertIn(b"index.html", answer.data)
        self.assertEqual(self.call("GET", "/nope.js").status, 404)

    def test_the_page_appears_once_the_folder_does(self):
        self.assertEqual(self.call("GET", "/").status, 503)
        self.web.mkdir()
        (self.web / "index.html").write_text("ready", encoding="utf-8")
        self.assertEqual(self.call("GET", "/").data, b"ready")


class Events(Base):
    HEARTBEAT = 0.1

    def listeners(self):
        return len(self.svc._hub._listeners)

    def test_the_answer_is_an_event_stream(self):
        stream = self.stream()
        self.assertEqual(stream.response.status, 200)
        self.assertEqual(stream.response.headers["Content-Type"], "text/event-stream; charset=utf-8")
        self.assertEqual(stream.response.headers["Cache-Control"], "no-store")
        self.assertEqual(stream.response.headers["Connection"], "close")
        self.assertEqual(stream.response.readline(), f"retry: {server.EVENTS_RETRY_MS}\n".encode())

    def test_an_action_over_http_arrives_as_an_event(self):
        hid = self.hid(self.open_book())
        stream = self.stream()
        self.ok("POST", f"/api/books/{self.pride}/highlights/{hid}/skip", {"skipped": True})
        found = stream.next("skipped")
        self.assertEqual(found["topic"], f"book:{self.pride}")
        self.assertEqual(found["hid"], hid)

    def test_settings_events_arrive(self):
        stream = self.stream()
        self.ok("PATCH", "/api/settings", {"count": 4})
        found = stream.next("changed", "settings")
        self.assertEqual(found["keys"], ["count"])

    def test_topics_filter_what_arrives(self):
        hid = self.hid(self.open_book())
        settings_only = self.stream("?topics=settings")
        book_only = self.stream(f"?topics=reader,book:{self.pride}")
        self.ok("POST", f"/api/books/{self.pride}/highlights/{hid}/skip", {"skipped": True})
        self.ok("PATCH", "/api/settings", {"count": 4})
        self.assertEqual(settings_only.next()["topic"], "settings")
        self.assertEqual(book_only.next("skipped")["topic"], f"book:{self.pride}")

    def test_unknown_topics_are_refused(self):
        for query in ("?topics=nope", "?topics=", "?topics=reader,nope", "?topics=book:"):
            self.refused(400, "GET", f"/api/events{query}")
        self.assertEqual(self.listeners(), 0)

    def test_a_refused_request_never_subscribes(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            EventStream(self.base, headers={"Host": "evil.example"})
        caught.exception.close()
        self.assertEqual(self.call("POST", "/api/events", {}).status, 405)
        self.assertEqual(self.listeners(), 0)

    def test_heartbeats_keep_arriving(self):
        stream = self.stream()
        for _ in range(3):
            self.assertEqual(stream.next_comment(), ": heartbeat")

    def test_a_disconnect_removes_the_listener_and_its_thread(self):
        stream = self.stream()
        self.assertEqual(self.listeners(), 1)
        self.assertEqual(self.http.open_connections(), 1)
        stream.close()
        self.assertTrue(wait_until(lambda: self.listeners() == 0, 5))
        self.assertTrue(wait_until(lambda: self.http.open_connections() == 0, 5))

    def test_reconnecting_gets_new_events(self):
        for count in range(4, 7):
            stream = self.stream()
            self.ok("PATCH", "/api/settings", {"count": count})
            self.assertEqual(stream.next("changed", "settings")["keys"], ["count"])
            stream.close()

    def test_many_reconnects_leak_no_threads_or_listeners(self):
        self.ok("GET", "/api/state")
        self.assertTrue(wait_until(lambda: self.http.open_connections() == 0, 5))
        threads = threading.active_count()
        for _ in range(60):
            stream = EventStream(self.base)
            stream.response.readline()
            stream.close()
        many = [EventStream(self.base) for _ in range(30)]
        self.assertGreaterEqual(self.listeners(), 30)
        for stream in many:
            stream.close()
        self.assertTrue(wait_until(lambda: self.listeners() == 0, 10), self.listeners())
        self.assertTrue(wait_until(lambda: self.http.open_connections() == 0, 10))
        self.assertTrue(wait_until(lambda: threading.active_count() <= threads, 10),
                        f"{threading.active_count()} threads, was {threads}")
        stream = self.stream()
        self.ok("PATCH", "/api/settings", {"count": 8})
        self.assertEqual(stream.next("changed", "settings")["keys"], ["count"])

    def test_a_listener_that_stops_reading_does_not_hold_up_the_others(self):
        idle = socket.create_connection(("127.0.0.1", self.port), timeout=LONG)
        self.addCleanup(idle.close)
        idle.sendall(f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode())
        self.assertTrue(wait_until(lambda: self.listeners() == 1, 5))
        stream = self.stream()
        self.ok("PATCH", "/api/settings", {"count": 4})
        self.assertEqual(stream.next("changed", "settings")["keys"], ["count"])

    def test_an_event_that_cannot_be_json_does_not_end_the_stream(self):
        stream = self.stream()
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.svc._hub.publish({"topic": "reader", "type": "bad", "value": object()})
            self.svc._hub.publish({"topic": "reader", "type": "good"})
            self.assertEqual(stream.next("good")["type"], "good")

    def test_stopping_ends_the_stream(self):
        stream = self.stream()
        self.http.stop(5)
        self.assertTrue(stream.ended())
        self.assertEqual(self.listeners(), 0)


class Watching(Base):
    POLL = 0.05

    def test_plugging_and_unplugging_arrives_as_reader_events(self):
        stream = self.stream("?topics=reader")
        self.unplug()
        while stream.next("changed", "reader")["connected"]:
            pass
        self.replug()
        self.assertTrue(stream.next("changed", "reader")["connected"])

    def test_prompt_edits_arrive_as_events(self):
        stream = self.stream("?topics=prompts")
        file = paths.prompts_dir() / "Self Help" / "questions.md"
        file.parent.mkdir(exist_ok=True)
        file.write_text("Ask well.", encoding="utf-8")
        future = time.time() + 5
        os.utime(file, (future, future))
        self.assertIn(str(file), stream.next("changed", "prompts")["paths"])


class Connections(Base):
    def test_keep_alive_reuses_one_connection_and_one_thread(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=LONG)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/state")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        json.loads(response.read())
        sock = conn.sock
        self.assertEqual(self.http.open_connections(), 1)
        conn.request("PATCH", "/api/settings", body=b'{"count": 4}', headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        conn.request("GET", "/api/nope")
        response = conn.getresponse()
        self.assertEqual(response.status, 404)
        response.read()
        self.assertIs(conn.sock, sock)
        self.assertEqual(self.http.open_connections(), 1)

    def test_an_idle_connection_is_closed(self):
        with mock.patch.object(server.Handler, "timeout", 0.1):
            with socket.create_connection(("127.0.0.1", self.port), timeout=LONG) as sock:
                sock.sendall(f"GET /api/books HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode())
                self.assertTrue(sock.recv(100).startswith(b"HTTP/1.1 200"))
                self.assertTrue(wait_until(lambda: self.http.open_connections() == 0, 5))

    def test_a_client_that_leaves_early_is_quiet(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            for _ in range(5):
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=LONG)
                sock.sendall(f"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode())
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                sock.close()
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=LONG)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                sock.close()
            self.assertTrue(wait_until(lambda: self.http.open_connections() == 0, 10))
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(self.call("GET", "/api/state").status, 200)

    def test_many_requests_leak_no_threads_or_database_connections(self):
        self.ok("GET", "/api/state")
        self.ok("GET", "/api/books")
        self.assertTrue(wait_until(lambda: self.http.open_connections() == 0, 5))
        time.sleep(0.2)
        threads = threading.active_count()
        files = len(os.listdir("/dev/fd"))
        for _ in range(40):
            self.ok("GET", "/api/books")
        self.assertTrue(wait_until(lambda: self.http.open_connections() == 0, 5))
        self.assertTrue(wait_until(lambda: threading.active_count() <= threads, 5))
        # One leaked database connection per request would be 40 files or more
        self.assertTrue(wait_until(lambda: len(os.listdir("/dev/fd")) <= files + 3, 5),
                        f"{len(os.listdir('/dev/fd'))} open files, was {files}")

    def test_a_disconnect_is_noticed_well_before_the_next_heartbeat(self):
        self.assertGreaterEqual(self.http.heartbeat, 15)
        stream = EventStream(self.base)
        stream.response.readline()
        self.assertEqual(len(self.svc._hub._listeners), 1)
        stream.close()
        self.assertTrue(wait_until(lambda: not self.svc._hub._listeners, 5))
        self.assertTrue(wait_until(lambda: self.http.open_connections() == 0, 5))

    def test_each_connection_gives_back_its_database_connection(self):
        with mock.patch.object(self.svc, "release_thread", wraps=self.svc.release_thread) as release:
            self.ok("GET", "/api/books")
            self.assertTrue(wait_until(lambda: release.call_count >= 1, 5))
            self.ok("GET", "/api/books")
            self.assertTrue(wait_until(lambda: release.call_count >= 2, 5))

    def test_requests_at_the_same_time_all_get_answers(self):
        hid = self.hid(self.open_book())
        results = []

        def one(index):
            if index % 2:
                results.append(self.call("GET", f"/api/books/{self.pride}").status)
            else:
                results.append(self.call("POST", f"/api/books/{self.pride}/highlights/{hid}/open").status)

        threads = [threading.Thread(target=one, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(LONG)
        self.assertEqual(results, [200] * 20)

    def test_two_saves_at_once_one_is_refused(self):
        view = self.open_book()
        self.write_card(self.pride, self.hid(view))
        original = save.save
        entered = threading.Event()
        release = threading.Event()

        def held(*args, **kwargs):
            entered.set()
            release.wait(LONG)
            return original(*args, **kwargs)

        results = {}
        with mock.patch.object(save, "save", side_effect=held):
            first = threading.Thread(target=lambda: results.update(first=self.call("POST", f"/api/books/{self.pride}/save", {})))
            first.start()
            self.assertTrue(entered.wait(LONG))
            self.refused(409, "POST", f"/api/books/{self.pride}/save", {}, text="already under way")
            self.assertTrue(self.ok("GET", "/api/state")["reader"]["saving"])
            release.set()
            first.join(LONG)
        self.assertEqual(results["first"].json["status"], "verified")


class Unplugged(Base):
    def test_a_save_waits_for_the_reader_then_try_again_finishes_it(self):
        view = self.open_book()
        self.write_card(self.pride, self.hid(view), front="Waited for?")
        self.unplug()
        path = f"/api/books/{self.pride}/save"
        self.assertEqual(self.ok("POST", path, {})["status"], "waiting")
        self.assertEqual(self.ok("GET", f"/api/books/{self.pride}")["id"], self.pride)
        self.assertEqual(self.ok("POST", f"{path}/retry")["status"], "waiting")
        self.replug()
        self.assertEqual(self.ok("POST", f"{path}/retry")["status"], "verified")
        self.assertIn("Waited for?", (self.mount / paths.INBOX_FILE).read_text(encoding="utf-8"))

    def test_everything_that_only_needs_the_computer_works_unplugged(self):
        view = self.open_book()
        hid = self.hid(view)
        self.unplug()
        book = f"/api/books/{self.pride}"
        self.ok("GET", "/api/state")
        self.ok("GET", "/api/books")
        self.assertEqual(len(self.ok("GET", book)["highlights"]), 7)
        self.ok("POST", f"{book}/highlights/{hid}/open")
        self.ok("POST", f"{book}/highlights/{hid}/skip", {"skipped": False})
        card = self.write_card(self.pride, hid)
        self.ok("DELETE", f"/api/cards/{card['id']}")
        self.ok("POST", f"/api/cards/{card['id']}/restore")
        self.ok("GET", f"{book}/save")
        self.ok("PATCH", "/api/settings", {"count": 4})
        self.ok("GET", "/api/prompts/Standard")
        self.assertTrue(self.ok("POST", "/api/reader/eject")["ejected"])


class Stopping(Base):
    def test_stop_closes_everything(self):
        stream = self.stream()
        self.open_book()
        idle = socket.create_connection(("127.0.0.1", self.port), timeout=LONG)
        self.addCleanup(idle.close)
        started = time.monotonic()
        self.http.stop(10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(stream.ended())
        self.assertEqual(self.http.open_connections(), 0)
        try:
            self.assertEqual(idle.recv(10), b"")
        except ConnectionResetError:
            pass
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=1)
        self.assertFalse(self.svc._thread)
        self.assertFalse(self.http._serving.is_alive())

    def test_stop_lets_a_request_under_way_finish(self):
        entered = threading.Event()
        original = self.svc.books

        def slow():
            entered.set()
            time.sleep(1.3)
            return original()

        results = {}
        told = []
        with mock.patch.object(self.svc, "books", side_effect=slow):
            client = threading.Thread(target=lambda: results.update(answer=self.call("GET", "/api/books")))
            client.start()
            self.assertTrue(entered.wait(LONG))
            self.http.stop(10, waiting=lambda: told.append(True))
            client.join(LONG)
        self.assertEqual(results["answer"].status, 200)
        self.assertEqual(results["answer"].headers["Connection"], "close")
        self.assertEqual(told, [True])

    def test_a_new_request_on_an_open_connection_is_told_it_is_closing(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=LONG)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/books")
        conn.getresponse().read()
        self.http.closing.set()
        conn.request("GET", "/api/books")
        response = conn.getresponse()
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.read()), {"error": "pbAnki is closing."})
        self.http.closing.clear()

    def test_stopping_twice_is_harmless(self):
        self.http.stop(5)
        self.http.stop(5)

    def test_stop_cancels_a_running_ai_request(self):
        view = self.open_book()
        pids = self.tmp / "pids"
        with mock.patch.dict(os.environ, {"FAKE_AI_MODE": "slow", "FAKE_AI_PIDS": str(pids)}):
            self.ok("POST", f"/api/books/{self.pride}/highlights/{self.hid(view)}/suggest", {"count": 1})
            self.assertTrue(wait_until(lambda: pids.exists() and pids.read_text().strip(), LONG))
        running = [int(pid) for pid in pids.read_text().split()]
        started = time.monotonic()
        self.http.stop(10)
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(self.svc._jobs)
        self.assertTrue(wait_until(lambda: not any(_alive(pid) for pid in running), 5), running)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RunPy(unittest.TestCase):
    """python3 desktop/run.py as a real program, from another folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pbanki-run-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.fake = build_reader(self.tmp / "reader", companion=True)
        self.cwd = self.tmp / "elsewhere"
        self.cwd.mkdir()
        self.opened = self.tmp / "opened.txt"
        self.open_cmd = self.tmp / "fake-open"
        self.open_cmd.write_text(f'#!/bin/sh\necho "$@" >> "{self.opened}"\n', encoding="utf-8")
        self.open_cmd.chmod(0o755)
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "PBANKI_MOUNT": str(self.fake.mount),
            "PBANKI_DESKTOP_HOME": str(self.home),
            "PBANKI_ASSISTANT_DIR": str(self.tmp / "assistant"),
            "PBANKI_SYNC_DELAY": "0",
            "PBANKI_CLAUDE_BIN": str(FAKE_DIR / "claude"),
            "PBANKI_CODEX_BIN": str(FAKE_DIR / "codex"),
            "PBANKI_TERMINAL_CMD": "/usr/bin/true",
            "PBANKI_OPEN_CMD": str(self.open_cmd),
            "FAKE_AI_MODE": "valid",
            "FAKE_AI_SLOW_SECONDS": "60",
        }
        self.procs = []
        self.addCleanup(self._kill_all)

    def _kill_all(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()

    def start(self, *args, env=None, python=None):
        proc = subprocess.Popen(
            [python or sys.executable, str(RUN_PY), *args],
            cwd=self.cwd, env={**self.env, **(env or {})}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.procs.append(proc)
        return proc

    def first_line(self, proc):
        ready, _, _ = select.select([proc.stdout], [], [], LONG)
        self.assertTrue(ready, "run.py printed nothing")
        return proc.stdout.readline().strip()

    def started(self, *args, env=None):
        proc = self.start(*args, env=env)
        line = self.first_line(proc)
        match = re.search(r"http://127\.0\.0\.1:(\d+)", line)
        self.assertTrue(match, line)
        return proc, line, match.group(0)

    def finish(self, proc, sig=signal.SIGINT, code=0):
        proc.send_signal(sig)
        try:
            out, err = proc.communicate(timeout=LONG)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("run.py did not stop")
        self.assertEqual(proc.returncode, code, err)
        return out, err

    def assert_database_closed(self):
        db = self.home / "pbanki.db"
        self.assertTrue(db.exists())
        self.assertFalse(Path(str(db) + "-wal").exists(), "the database was not closed cleanly")
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn.close()

    def test_starts_from_another_folder_answers_and_stops_cleanly(self):
        port = _free_port()
        proc, line, base = self.started("--no-open", "--port", str(port))
        self.assertEqual(line, f"pbAnki is running at http://127.0.0.1:{port}. Press Ctrl-C to quit.")
        state = fetch(f"{base}/api/state")
        self.assertEqual(state.status, 200)
        self.assertEqual(state.json["reader"]["mount"], str(self.fake.mount))
        self.assertEqual(state.json["files"]["db"], str(self.home / "pbanki.db"))
        self.assertTrue((self.home / "prompts" / "Standard" / "questions.md").is_file())
        self.assertEqual(fetch(f"{base}/api/state", headers={"Host": "evil.example"}).status, 403)
        self.assertFalse(self.opened.exists())
        out, err = self.finish(proc)
        self.assertEqual(out, "")
        self.assertEqual(err, "")
        self.assert_database_closed()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1)

    def test_sigterm_also_stops_cleanly(self):
        proc, _, base = self.started("--no-open", "--port", "0")
        self.assertEqual(fetch(f"{base}/api/books").status, 200)
        self.finish(proc, signal.SIGTERM)
        self.assert_database_closed()

    def test_opens_the_browser_unless_told_not_to(self):
        proc, _, base = self.started("--port", "0")
        self.assertTrue(wait_until(self.opened.exists, LONG))
        self.assertEqual(self.opened.read_text(encoding="utf-8").strip(), base)
        self.finish(proc)

    def test_says_so_when_the_browser_will_not_open(self):
        proc, _, base = self.started("--port", "0", env={"PBANKI_OPEN_CMD": "/usr/bin/false"})
        _, err = self.finish(proc)
        self.assertIn("The browser didn't open.", err)
        self.assertNotIn("Traceback", err)

    def test_a_taken_port_moves_to_the_next_and_says_which(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            proc, line, base = self.started("--no-open", "--port", str(port))
            used = int(base.rsplit(":", 1)[1])
            self.assertGreater(used, port)
            self.assertLess(used, port + server.PORT_TRIES)
            self.assertIn(f"http://127.0.0.1:{used}", line)
            self.assertIn(f"port {port} was taken", line)
            self.assertEqual(fetch(f"{base}/api/state").status, 200)
            self.finish(proc)

    def test_no_free_port_is_a_plain_sentence(self):
        taken = []
        self.addCleanup(lambda: [sock.close() for sock in taken])
        first = 49300
        while len(taken) < server.PORT_TRIES and first < 60000:
            sock = socket.socket()
            try:
                sock.bind(("127.0.0.1", first + len(taken)))
                sock.listen()
                taken.append(sock)
            except OSError:
                sock.close()
                for held in taken:
                    held.close()
                taken.clear()
                first += server.PORT_TRIES
        self.assertEqual(len(taken), server.PORT_TRIES)
        proc = self.start("--no-open", "--port", str(first))
        out, err = proc.communicate(timeout=LONG)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(out, "")
        self.assertIn(f"Ports {first} to {first + server.PORT_TRIES - 1} are all taken", err)
        self.assertNotIn("Traceback", err)

    def test_a_bad_port_is_refused(self):
        for value in ("nope", "-1", "70000"):
            proc = self.start("--no-open", "--port", value)
            out, err = proc.communicate(timeout=LONG)
            self.assertEqual(proc.returncode, 2, value)
            self.assertIn("whole number from 0 to 65535", err)
            self.assertNotIn("Traceback", err)

    def test_a_home_that_cannot_be_made_is_a_plain_sentence(self):
        blocker = self.tmp / "file"
        blocker.write_text("x", encoding="utf-8")
        proc = self.start("--no-open", "--port", "0", env={"PBANKI_DESKTOP_HOME": str(blocker / "home")})
        _, err = proc.communicate(timeout=LONG)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("pbAnki couldn't start", err)
        self.assertNotIn("Traceback", err)

    @unittest.skipUnless(
        Path("/usr/bin/python3").exists()
        and subprocess.run(["/usr/bin/python3", "-c", "import sys; sys.exit(sys.version_info < (3, 11))"],
                           capture_output=True, check=False).returncode == 1,
        "needs an older /usr/bin/python3",
    )
    def test_an_old_python_gets_a_plain_sentence(self):
        proc = self.start("--no-open", python="/usr/bin/python3")
        _, err = proc.communicate(timeout=LONG)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("pbAnki needs Python 3.11 or newer", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(self.home.exists())

    def test_ctrl_c_stops_a_run_and_its_ai_commands(self):
        pids = self.tmp / "pids"
        proc, _, base = self.started("--no-open", "--port", "0",
                                     env={"FAKE_AI_MODE": "slow", "FAKE_AI_PIDS": str(pids)})
        books = fetch(f"{base}/api/books").json["books"]
        pride = next(book["id"] for book in books if book["title"] == "Pride and Prejudice")
        answer = fetch(f"{base}/api/books/{pride}/open", "POST")
        self.assertEqual(answer.status, 200, answer.data[:200])
        started = fetch(f"{base}/api/books/{pride}/run", "POST", {"action": "start"})
        self.assertEqual(started.status, 200, started.data[:200])
        self.assertTrue(wait_until(lambda: pids.exists() and pids.read_text().strip(), LONG))
        running = [int(pid) for pid in pids.read_text().split()]
        self.assertTrue(all(_alive(pid) for pid in running))
        started = time.monotonic()
        self.finish(proc)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(wait_until(lambda: not any(_alive(pid) for pid in running), 5), running)
        self.assert_database_closed()

    def test_ctrl_c_ends_open_event_streams(self):
        proc, _, base = self.started("--no-open", "--port", "0")
        stream = EventStream(base)
        self.addCleanup(stream.close)
        self.finish(proc)
        self.assertTrue(stream.ended())
        self.assert_database_closed()

    def test_a_second_ctrl_c_quits_at_once(self):
        slow = self.tmp / "slow-open"
        marker = self.tmp / "slow-open.pid"
        slow.write_text(f'#!/bin/sh\necho $$ > "{marker}"\nexec sleep 20\n', encoding="utf-8")
        slow.chmod(0o755)
        proc, _, base = self.started("--no-open", "--port", "0", env={"PBANKI_OPEN_CMD": str(slow)})
        results = {}
        client = threading.Thread(target=lambda: results.update(answer=self._try_fetch(
            f"{base}/api/prompts/open", "POST", {"name": "Standard", "file": "questions.md"})))
        client.start()
        self.assertTrue(wait_until(lambda: marker.exists() and marker.read_text().strip(), LONG))
        self.addCleanup(self._kill_quietly, int(marker.read_text()))
        proc.send_signal(signal.SIGINT)
        ready, _, _ = select.select([proc.stdout], [], [], LONG)
        self.assertTrue(ready)
        self.assertIn("Press Ctrl-C again", proc.stdout.readline())
        started = time.monotonic()
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(LONG), 130)
        self.assertLess(time.monotonic() - started, 3)
        client.join(LONG)

    @staticmethod
    def _kill_quietly(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def _try_fetch(*args):
        try:
            return fetch(*args)
        except OSError:
            return None

    # One copy per data folder, and no AI commands left after quitting

    def use_slow_ai(self):
        self.temps = self.tmp / "temps"
        self.temps.mkdir()
        self.pid_log = self.tmp / "ai-pids"
        wrapper = self.tmp / "claude-wrapper"
        wrapper.write_text(f'#!/bin/sh\necho $$ >> "{self.pid_log}"\nexec "{FAKE_DIR / "claude"}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        self.env.update({"PBANKI_CLAUDE_BIN": str(wrapper), "FAKE_AI_MODE": "slow", "TMPDIR": str(self.temps)})
        self.pride = book_id_for(self.fake.books["pride"])

    def ai_pids(self):
        try:
            return [int(pid) for pid in self.pid_log.read_text().split()]
        except OSError:
            return []

    def running_ai(self):
        return [pid for pid in self.ai_pids() if _alive(pid)]

    def start_run(self, base):
        self.assertEqual(fetch(f"{base}/api/books").status, 200)
        self.assertEqual(fetch(f"{base}/api/books/{self.pride}/open", "POST", {}).status, 200)
        started = fetch(f"{base}/api/books/{self.pride}/run", "POST", {"action": "start"})
        self.assertEqual(started.status, 200, started.data)
        self.assertTrue(wait_until(self.running_ai, LONG), "no AI command started")

    def assert_nothing_left(self):
        self.assertTrue(wait_until(lambda: not self.running_ai(), 10), self.running_ai())
        self.assertEqual(list(self.temps.glob("pbanki-ai-*")), [])

    def test_a_second_copy_leaves_the_first_alone_and_points_at_it(self):
        self.use_slow_ai()
        proc, _, base = self.started("--no-open", "--port", "0")
        self.start_run(base)
        second = self.start("--port", "0")
        out, err = second.communicate(timeout=LONG)
        self.assertEqual(second.returncode, 0, err)
        self.assertEqual(out.strip(), f"pbAnki is already running at {base}.")
        self.assertIn(base, self.opened.read_text("utf-8"))
        book = fetch(f"{base}/api/books/{self.pride}").json
        self.assertEqual((book["run"]["status"], book["run"]["active"], book["run"]["offer"]), ("running", True, ""))
        self.finish(proc)

    def test_the_data_folder_is_free_again_after_any_kind_of_exit(self):
        self.use_slow_ai()
        proc, _, _ = self.started("--no-open", "--port", "0")
        self.finish(proc)
        proc, _, _ = self.started("--no-open", "--port", "0")
        proc.kill()
        proc.wait()
        proc, line, _ = self.started("--no-open", "--port", "0")
        self.assertIn("pbAnki is running at", line)
        self.finish(proc)

    def test_closing_the_terminal_window_ends_the_ai_commands(self):
        self.use_slow_ai()
        proc, _, base = self.started("--no-open", "--port", "0")
        self.start_run(base)
        out, err = self.finish(proc, signal.SIGHUP)
        self.assert_nothing_left()
        self.assertNotIn("Traceback", err)

    def test_a_second_control_c_ends_the_ai_commands(self):
        self.use_slow_ai()
        proc, _, base = self.started("--no-open", "--port", "0", env={"PBANKI_SYNC_DELAY": "30"})
        self.start_run(base)
        self.save_in_flight(base)
        proc.send_signal(signal.SIGINT)
        self.assertIn("Finishing what's under way", self.first_line(proc))
        self.assertTrue(self.running_ai())
        self.finish(proc, signal.SIGINT, code=130)
        self.assert_nothing_left()

    def test_a_terminal_that_is_already_gone_does_not_stop_the_shutdown(self):
        self.use_slow_ai()
        proc, _, base = self.started("--no-open", "--port", "0", env={"PBANKI_SYNC_DELAY": "3"})
        self.start_run(base)
        self.save_in_flight(base)
        proc.stdout.close()
        proc.send_signal(signal.SIGHUP)
        proc.wait(timeout=LONG)
        self.assert_nothing_left()

    def save_in_flight(self, base):
        book = fetch(f"{base}/api/books/{self.pride}").json
        hid = next(h["hid"] for h in book["highlights"] if h["status"] != "reviewed" and not h["writing"])
        card = fetch(f"{base}/api/books/{self.pride}/highlights/{hid}/cards", "POST", {"kind": "qa"}).json["card"]
        fetch(f"{base}/api/cards/{card['id']}", "PATCH", {"front": "Q?", "back": "A"})

        def post():
            try:
                fetch(f"{base}/api/books/{self.pride}/save", "POST", {})
            except OSError:
                pass

        thread = threading.Thread(target=post, daemon=True)
        thread.start()
        self.assertTrue(wait_until(lambda: (self.fake.mount / paths.INBOX_FILE).exists(), LONG), "the save did not start")


class Findings(Base):
    def test_a_request_with_no_headers_at_all_is_refused_quietly(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=LONG) as sock:
            sock.sendall(b"GET /api/state\r\n")
            found = b""
            while chunk := sock.recv(65536):
                found += chunk
        self.assertIn(b"pbAnki only answers at its own address", found)
        self.assertNotIn(b"Traceback", found)


if __name__ == "__main__":
    unittest.main()
