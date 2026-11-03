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
  believable     text built from the prompt, so screens look right. Waits FAKE_AI_DELAY seconds first
  no_connections a correct reply that lists no connections

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

MODES = ("valid", "later", "fenced", "malformed", "is_error", "not_logged_in", "exit", "empty", "no_output", "slow",
         "believable", "no_connections")


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
    if mode == "believable":
        time.sleep(float(os.environ.get("FAKE_AI_DELAY", "0")))
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


def _opening(text, words=7):
    parts = text.split()
    return " ".join(parts[:words]).rstrip(",.;:") + ("..." if len(parts) > words else "")


def _sentence(text):
    match = re.match(r"(.+?[.!?])(\s|$)", text.strip(), re.S)
    return match.group(1) if match else text.strip()


def _believable_cards(prompt):
    text = _section(prompt, "highlight")
    if '{"cards": [{"front": "Sentence quoted' in prompt:
        return _cloze_cards(prompt)
    fronts = [
        f'What is the author getting at with "{_opening(text)}"?',
        f'How would you explain "{_opening(text, 5)}" to a friend?',
        "Why does this idea matter in everyday life?",
        f'What would change if "{_opening(text, 4)}" were not true?',
    ]
    backs = [
        _sentence(text),
        "Put simply, it is about what the passage says plainly: " + _sentence(text).lower(),
        "It changes how you act on the rest of the chapter.",
        "The rest of the argument would have nothing to stand on.",
    ]
    bare = "Write only the questions" in prompt
    return [{"front": fronts[n % 4], "back": "" if bare else backs[n % 4]} for n in range(_card_count(prompt))]


def believable_reply(prompt):
    if '{"connections":' in prompt:
        rows = _numbered_highlights(prompt)
        found = []
        for first, second in zip(rows, rows[1:]):
            found.append({
                "title": f"{first[1]} and {second[1]}"[:60],
                "front": f'How does "{_opening(first[2], 5)}" bear on "{_opening(second[2], 5)}"?',
                "back": f"{_sentence(first[2])} Read beside it: {_sentence(second[2])}",
                "sources": [first[0], second[0]],
            })
        return {"connections": found[:3]}
    if '{"chapters":' in prompt:
        rows = _numbered_highlights(prompt)
        chapters, ideas = [], []
        for chapter in dict.fromkeys(row[1] for row in rows):
            quote = next(row[2] for row in rows if row[1] == chapter)
            chapters.append({"chapter": chapter, "big": f"{chapter} turns on one claim: {_sentence(quote)}", "points": [
                {"text": _sentence(quote), "covered": True},
                {"text": f"What {chapter} leaves unsaid, and why the author leaves it there.", "covered": False,
                 "idea": len(ideas)},
            ]})
            ideas.append({"title": f"The quiet point of {chapter}"[:60], "chapter": chapter,
                          "note": f"The notes say {chapter} rests on an assumption your highlights don't touch.",
                          "quote": quote, "front": f"What does {chapter} assume without saying so?",
                          "back": "That the reader will act on it, and not only agree with it."})
        return {"chapters": chapters, "ideas": ideas}
    if '{"term":' in prompt:
        term = _section(prompt, "term") or _section(prompt, "highlight")
        return {"term": term, "definition": "As the book uses it, the plain sense of the word in this passage."}
    if '{"cards":' in prompt:
        reply = {"cards": _believable_cards(prompt)}
        text = _section(prompt, "highlight")
        if "later_research" in prompt and len(text) % 3 == 0:
            reply["later_research"] = "Later studies support the broad idea, though the effect is smaller than the book suggests."
        return reply
    return {"front": "Put another way, what does the passage ask you to do?", "back": "Act on it today, in a small way."}


def valid_reply(prompt, mode="valid"):
    if mode == "believable":
        return believable_reply(prompt)
    if mode == "no_connections" and '{"connections":' in prompt:
        return {"connections": []}
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
