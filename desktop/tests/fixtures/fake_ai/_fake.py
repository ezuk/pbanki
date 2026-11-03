"""Shared logic of the fake claude and codex commands used by the tests.

Pick behaviour with FAKE_AI_MODE, or a marker anywhere in the prompt such as
[[fake:malformed]] (the marker wins, so a test can steer one request by its hint):

  valid          a correct reply for the request kind (the default)
  later          valid, plus a later_research field on questions and cloze
  fenced         valid, inside a ```json fence with prose around it
  malformed      text that is not JSON
  is_error       claude: is_error true with a message. codex: an error on stderr, exit 1
  not_logged_in  the signed-out failure of each command
  exit           no answer, exit code 3
  empty          an empty answer
  no_output      claude: nothing on stdout, exit 0. codex: exit 0 without writing the -o file
  slow           sleeps FAKE_AI_SLOW_SECONDS (default 30), then answers as valid

FAKE_AI_LOG        append one JSON line per run: tool, argv, cwd, stdin, path
FAKE_AI_PIDS       write "<own pid> <child pid>" to this file when slow, with a child process running
FAKE_AI_VERSION    what --version prints
"""

import json
import os
import re
import subprocess
import sys
import time

MODES = ("valid", "later", "fenced", "malformed", "is_error", "not_logged_in", "exit", "empty", "no_output", "slow")


def mode_for(prompt):
    marker = re.search(r"\[\[fake:(\w+)\]\]", prompt)
    if marker and marker.group(1) in MODES:
        return marker.group(1)
    return os.environ.get("FAKE_AI_MODE", "valid")


def log_run(tool, prompt):
    path = os.environ.get("FAKE_AI_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"tool": tool, "argv": sys.argv[1:], "cwd": os.getcwd(),
                                "stdin": prompt, "path": os.environ.get("PATH", "")}) + "\n")


def hold_when_slow(mode):
    if mode != "slow":
        return
    seconds = float(os.environ.get("FAKE_AI_SLOW_SECONDS", "30"))
    pids = os.environ.get("FAKE_AI_PIDS")
    if pids:
        child = subprocess.Popen(["sleep", str(max(seconds, 1))])
        with open(pids, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {child.pid}")
    time.sleep(seconds)


def _section(prompt, tag):
    match = re.search(f"<{tag}>\n(.*?)\n</{tag}>", prompt, re.S)
    return match.group(1) if match else ""


def _numbered_highlights(prompt):
    rows = re.findall(r"^(\d+)\. \[(.*?)\] (.*)$", _section(prompt, "highlights"), re.M)
    return [(int(number), where.split(" · p. ")[0], text) for number, where, text in rows]


def _card_count(prompt):
    wanted = re.search(r"Write (\d+) cards", prompt) or re.search(r"from 1 to (\d+)", prompt)
    return min(int(wanted.group(1)), 2) if wanted and "from 1 to" in wanted.group(0) else (int(wanted.group(1)) if wanted else 1)


def _cloze_cards(prompt):
    words = _section(prompt, "highlight").split(" ")
    cards = []
    for index in range(min(_card_count(prompt), max(len(words), 1))):
        marked = list(words)
        marked[index] = "{{c1::" + marked[index] + "}}"
        cards.append({"front": " ".join(marked), "back": ""})
    return cards


def valid_reply(prompt, mode="valid"):
    if '{"connections":' in prompt:
        rows = _numbered_highlights(prompt)
        found = []
        if len(rows) >= 2:
            found.append({"title": "Fake connection", "front": f"How do {rows[0][1]} and {rows[1][1]} connect?",
                          "back": "Through a fake link.", "sources": [rows[0][0], rows[1][0]]})
            found.append({"title": "Only one source", "front": "Dropped for citing one highlight?",
                          "back": "Yes.", "sources": [rows[0][0]]})
        return {"connections": found}
    if '{"chapters":' in prompt:
        rows = _numbered_highlights(prompt)
        chapter = rows[0][1] if rows else "Chapter 1"
        quote = rows[0][2] if rows else ""
        return {
            "chapters": [{"chapter": chapter, "big": "A fake big idea.", "points": [
                {"text": "A point the highlights cover.", "covered": True},
                {"text": "A point nobody highlighted.", "covered": False, "idea": 0},
            ]}],
            "ideas": [
                {"title": "Fake idea", "chapter": chapter, "note": "Why the fake idea matters.", "quote": quote,
                 "front": "What is the fake idea?", "back": "A real test."},
                {"title": "Invented quote", "chapter": chapter, "note": "", "quote": "Words no book ever printed here.",
                 "front": "What is the second fake idea?", "back": "Another test."},
            ],
        }
    if '{"term":' in prompt:
        return {"term": "Define " + _section(prompt, "highlight"), "definition": "A fake meaning for tests."}
    if '{"cards": [{"front": "Sentence quoted' in prompt:
        reply = {"cards": _cloze_cards(prompt)}
    elif '{"cards":' in prompt:
        back = "" if "Write only the questions" in prompt else "Fake answer {n}."
        reply = {"cards": [{"front": f"Fake question {n} about {'abcdefgh'[n]} {'ijklmnop'[n]}?", "back": back.format(n=n)}
                           for n in range(_card_count(prompt))]}
    else:
        return {"front": "A rewritten fake question?", "back": "A rewritten answer."}
    if mode == "later" and "later_research" in prompt:
        reply["later_research"] = "Later studies questioned this fake claim."
    return reply


def answer_text(prompt, mode):
    if mode == "malformed":
        return "Sorry, here are your cards: {not json at all}"
    if mode == "empty":
        return ""
    text = json.dumps(valid_reply(prompt, mode), ensure_ascii=False)
    if mode == "fenced":
        return "Here you go:\n```json\n" + text + "\n```\nEnjoy."
    return text
