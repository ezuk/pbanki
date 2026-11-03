"""cardgen.py against the real cardgen.lua, byte for byte, plus the desktop request kinds."""

import itertools
import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import lua_parity  # noqa: E402
from pbanki_desktop import cardgen, checks  # noqa: E402

REAL_SIDECAR = Path(__file__).resolve().parent / "fixtures" / "real_sidecar" / "metadata.epub.lua"


def real_highlights():
    """The real highlight texts KOReader wrote into the fixture, with its escapes undone."""
    text = REAL_SIDECAR.read_text(encoding="utf-8")
    found = re.findall(r'\["text"\] = "((?:[^"\\]|\\.)*)"', text, re.S)
    return [s.replace("\\\n", "\n").replace('\\"', '"').replace("\\\\", "\\") for s in found]


def real_lines():
    """Text full of curly quotes, dashes and accents from real Companions, when present."""
    folder = lua_parity.ASSISTANT / "companions"
    lines = []
    for path in sorted(folder.glob("*.companion.md")):
        rows = path.read_text(encoding="utf-8").split("\n")
        lines += [row for row in rows if len(row) > 40][::37]
    return lines


H_FULL = {
    "text": "Paris is the capital of France.", "before": "It is known that", "after": "It is large.",
    "book": "Geo", "author": "A. Writer", "chapter": "One", "percent": 12.4, "note": "check",
}
EDITED = {"front": "Q2 short", "back": "A2", "suggested": {"front": "Q2 long", "back": "A2"}}
KEPT = {"front": "Q1", "back": "A1", "suggested": {"front": "Q1", "back": "A1"}}
ANSWERED = {"front": "Q", "back": "Mine", "suggested": {"front": "Q", "back": ""}}
REWORDED = {"front": "Q2", "back": "Mine", "suggested": {"front": "Q", "back": ""}}
CLOZE_EXTRA = {"front": "A {{c1::b}}", "back": "x", "suggested": {"front": "A {{c1::b}}", "back": ""}}
OWN = {"front": "Q", "back": "A"}
CLOZE_OWN = {"front": "A {{c1::b}}.", "back": ""}
LOG = [
    {"front": "E1", "back": "x", "suggested": {"front": "e1", "back": "x"}, "quote": "q1"},
    {"front": "K1", "back": "x", "suggested": {"front": "K1", "back": "x"}, "quote": "q1"},
    {"front": "K2", "back": "x", "quote": "q2"},
    {"front": "K3", "back": "x", "quote": "q2"},
    {"front": "N", "back": "y", "quote": "q3", "note": "my note"},
    {"front": "L", "back": "", "quote": "word " * 100},
    {"front": "U", "back": "é", "quote": "“Curly” — ‘quote’ … é " * 40, "note": ""},
]


def build_cases():
    """(fn, args) pairs for build_messages across the request's combinations."""
    highlights = [
        {"text": "x"},
        {"text": "x", "book": "B"},
        {"text": "x", "book": "B", "author": "A"},
        {"text": "x", "book": "B", "author": "A", "chapter": "Chapter 1: Ü"},
        {"text": "x", "book": "B", "author": "A", "chapter": "C", "percent": 0},
        {"text": "x", "book": "B", "author": "A", "chapter": "C", "percent": 12.5},
        {"text": "x", "book": "", "author": ""},
        H_FULL,
        {**H_FULL, "before": ""},
        {**H_FULL, "after": ""},
        {**H_FULL, "note": ""},
        {**H_FULL, "text": "It’s “quoted” — and é\nsecond line", "note": "é note"},
    ]
    counts = [{"count": 1}, {"count": 3}, {"max": 1}, {"max": 3}, {"max": 0}]
    flavours = [
        {},
        {"cloze": True},
        {"questions_only": True},
        {"questions_only": True, "cloze": True},
        {"definition": True},
        {"definition": True, "cloze": True},
        {"questions_only": False},
    ]
    extras = [
        {},
        {"existing": [OWN]},
        {"existing": [KEPT, EDITED, ANSWERED, REWORDED, CLOZE_EXTRA, CLOZE_OWN]},
        {"rejected": [OWN, CLOZE_OWN]},
        {"recent": LOG},
        {"recent": LOG[:2], "existing": [EDITED], "rejected": [OWN]},
        {"guidance": "Ask about the numbers"},
        {"guidance": ""},
        {"existing": [], "rejected": [], "recent": []},
        {"prompt": {"questions": "Mine.", "cloze": "  \n", "definition": "Define it."}},
    ]
    notes = [(None, None), ("COMPANION", None), ("", ""), ("Ünï — notes", "You are mid-chapter.")]
    cases = []
    for h, count in itertools.product(highlights, counts):
        cases.append(("buildMessages", [h, count, None, None]))
    for flavour, extra, count in itertools.product(flavours, extras, counts[:4]):
        cases.append(("buildMessages", [H_FULL, {**count, **flavour, **extra}, None, None]))
    for (companion, note), flavour in itertools.product(notes, flavours):
        cases.append(("buildMessages", [H_FULL, {"count": 2, **flavour}, companion, note]))
    return cases


REPLIES = [
    '{"cards":[{"front":"Q1","back":"A1"},{"front":"Q2","back":"A2"}]}',
    '{"cards":[{"front":" Q1 ","back":"A1\\n"},{"front":"Q2","back":"A2"},{"front":"Q3","back":"A3"}]}',
    '```json\n{"cards":[{"front":"Q","back":"A"}]}\n```',
    '```\n{"cards":[{"front":"Q","back":"A"}]}\n```\nAnd more.',
    'Here you go:\n{"cards":[{"front":"Q","back":"A"}]}\nEnjoy!',
    '[{"front":"Q","back":"A"}]',
    '{"cards":[{"front":"","back":"x"}]}',
    '{"cards":[{"front":"  \\t","back":"x"}]}',
    '{"cards":[{"front":"Q"}]}',
    '{"cards":[{"front":"Q","back":3}]}',
    '{"cards":[{"front":3,"back":"x"},{"front":"Q","back":"A"}]}',
    '{"cards":[null,{"front":"Q","back":"A"}]}',
    '{"cards":["text",{"front":"Q","back":"A"},["nested"]]}',
    '{"cards":"none"}',
    '{"cards":null}',
    '{"cards":false}',
    '{"cards":{"front":"Q"}}',
    '{"cards":[]}',
    '{}',
    '[]',
    '{oops}',
    '{"cards":[{"front":"Q",]}',
    '"just a string"',
    '12',
    'no json here',
    '',
    '[1,2',
    '{"a":1',
    '{"cards":[{"front":"Q\\u00e9 \\ud83d\\ude00","back":"“A”\\nB"}]}',
    '{"cards":[{"front":"Qé — ü","back":" A "}]}',
    '{"cards":[{"front":"Open {brace}","back":"x"}]} trailing } brace',
    '``` {"cards":[{"front":"Q","back":"A"}]} ```',
    '```json{"cards":[{"front":"Q","back":"A"}]}```',
    '{"term":" willpower ","definition":"The ability to do what matters most."}',
    '{"term":""}',
    '{"term":"x","definition":""}',
    '{"term":3,"definition":"y"}',
    '["term","definition"]',
    'null',
]

BOOK = ("It is a truth universally acknowledged, that a single man in possession of a good fortune, "
        "must be in want of a wife.")
CLOZE_REPLIES = [
    '{"cards":[{"front":"A single man must want a {{c1::wife}}."},'
    '{"front":"must be in want of a {{c1::wife}}."},'
    '{"front":"What is universally acknowledged?","back":"A truth"}]}',
    '{"cards":[{"front":"Men want {{c1::wives}}."}]}',
    '{"cards":[{"front":"It is a {{c1::truth}} universally acknowledged"},'
    '{"front":"that a single {{c1::man}}"},{"front":"a good {{c1::fortune::money}}"}]}',
]

QUOTES = [
    ("a single man in possession of a good {{c1::fortune}}", BOOK),
    ("It is a {{c1::truth universally acknowledged::hint}}", BOOK),
    ("It’s fine", "It's fine."),
    ("A rich single man must want a {{c1::wife}}.", BOOK),
    ("{{c1::wife}} must be in want", BOOK),
    ("“Quoted” — words…", "quoted words"),
    ("non breaking", "non breaking"),
    ("soft­hyphen", "softhyphen"),
    ("ÉCOLE normale", "école normale"),
    ("École NORMALE", "École normale"),
    ("«guillemets» here", "guillemets here"),
    ("«guillemets» here", "«guillemets» HERE"),
    ("to-morrow fortnight", "To-morrow fortnight."),
    ("to_morrow", "to morrow"),
    ("'tis the season", "tis the season"),
    ("the dogs' bones", "the dogs bones"),
    ("don't", "dont"),
    ("x", "wax"),
    ("", "anything"),
    ("{{c1::}}", "anything"),
    ("... !!!", "anything"),
    ("שלום עולם", "אמר שלום עולם גדול"),
    ("emoji 😀 stays", "emoji 😀 stays here"),
    ("{{c1::a}} and {{c2::b::c}}", "a and b"),
    ("multi\nline {{c1::text}}", "multi line text"),
]

EXPANDS = [
    ("The key idea is {{c1::focus}}.", "First sentence here. The key idea is focus. Last sentence there."),
    ("{{c1::First}} sentence here. The key idea is focus. Last sentence there.",
     "First sentence here. The key idea is focus. Last sentence there."),
    ("A {{c1::sentence}} around a short highlight.", "short highlight"),
    ("The key idea is {{c1::focus::a hint}}.", "First sentence here. The key idea is focus. Last sentence there."),
    ("{{c1::é}} mid", "avant é mid après ü"),
    ("{{c1::}}", "text"),
    ("nothing", ""),
    ("", ""),
]

FUNCTIONS = {
    "requestKind": cardgen.request_kind,
    "systemPrompt": cardgen.system_prompt,
    "isCloze": cardgen.is_cloze,
    "showCloze": cardgen.show_cloze,
    "clozeMark": cardgen.cloze_mark,
    "wasEdited": cardgen.was_edited,
    "questionsOnly": cardgen.questions_only,
    "buildMessages": cardgen.build_messages,
    "historyEntry": cardgen.history_entry,
    "appendHistory": cardgen.append_history,
    "pickRecent": cardgen.pick_recent,
    "recentSection": cardgen.recent_section,
    "countInstruction": cardgen.count_instruction,
    "cardLimit": cardgen.card_limit,
    "extractJson": cardgen.extract_json,
    "quotesText": cardgen.quotes_text,
    "expandCloze": cardgen.expand_cloze,
    "words": cardgen.words,
    "shorten": cardgen._shorten,
}


def python_value(fn, args):
    if fn == "parseCards":
        return as_pair(cardgen.parse_cards(args[0], args[1]))
    if fn == "parseDefinition":
        return as_pair(cardgen.parse_definition(args[0]))
    if fn == "parseReply":
        return as_pair(cardgen.parse_reply(args[0], args[1], args[2]))
    return FUNCTIONS[fn](*args)


def as_pair(result):
    # The harness omits nil, so a nil side is absent here too
    first, second = result
    pair = {}
    if first is not None:
        pair["first"] = first
    if second is not None:
        pair["second"] = second
    return pair


def all_cases():
    cases = build_cases()
    for request in ({"definition": True}, {"cloze": True}, {}, {"questions_only": True}, {"cloze": True, "definition": True}):
        for flavour in (request, {**request, "count": 1}, {**request, "count": 2}, {**request, "max": 1}, {**request, "max": 3}):
            if "definition" not in flavour and "count" not in flavour and "max" not in flavour:
                continue
            cases.append(("systemPrompt", [None, cardgen.request_kind(flavour)]))
            cases.append(("requestKind", [flavour]))
            cases.append(("questionsOnly", [flavour]))
            if "definition" not in flavour:
                cases.append(("countInstruction", [flavour]))
                cases.append(("cardLimit", [flavour]))
    for kind in ("questions", "cloze", "definition"):
        cases.append(("systemPrompt", [None, kind]))
        cases.append(("systemPrompt", [{kind: "Mine.\n\nMore."}, kind]))
        cases.append(("systemPrompt", [{kind: "  \n "}, kind]))
    for reply in REPLIES:
        cases.append(("extractJson", [reply]))
        cases.append(("parseDefinition", [reply]))
        for count in (None, 1, 2, 5):
            cases.append(("parseCards", [reply, count]))
        for request in ({"count": 2}, {"max": 3}, {"count": 2, "questions_only": True}, {"definition": True}, {"count": 1, "cloze": True}):
            cases.append(("parseReply", [reply, request, {"text": "Q1 A1"}]))
    for reply in CLOZE_REPLIES:
        for request in ({"count": 3, "cloze": True}, {"count": 1, "cloze": True}, {"max": 2, "cloze": True}, {"count": 3}):
            cases.append(("parseReply", [reply, request, {"text": BOOK}]))
            cases.append(("parseReply", [reply, request, {"text": "a good fortune", "before": "It is a truth universally acknowledged, that a single man in possession of", "after": ", must be in want of a wife."}]))
    for front, source in QUOTES:
        cases.append(("quotesText", [front, source]))
        cases.append(("words", [front]))
        cases.append(("words", [source]))
    for front, text in EXPANDS:
        cases.append(("expandCloze", [front, text]))
    for card in (KEPT, EDITED, ANSWERED, REWORDED, CLOZE_EXTRA, OWN, CLOZE_OWN):
        cases.append(("wasEdited", [card]))
        cases.append(("historyEntry", [{"text": "Slow breathing.", "note": "for flights"}, card]))
        cases.append(("historyEntry", [{"text": "x", "note": ""}, card]))
        cases.append(("historyEntry", [{"text": "x"}, card]))
    for limit in (0, 1, 2, 3, 4, 10):
        cases.append(("pickRecent", [LOG, limit]))
        cases.append(("appendHistory", [LOG, [OWN, KEPT], limit]))
        cases.append(("appendHistory", [None, [OWN], limit]))
    cases.append(("recentSection", [LOG]))
    cases.append(("recentSection", [LOG[:1]]))
    for text in ("short", "word " * 70, "é " * 200, "a" * 299 + " b", "a" * 300, "a" * 301 + " b",
                 "word\n" * 100, "x" * 295 + " é" * 20):
        cases.append(("shorten", [text, 300]))
    for text in ("{{Front}}", "The {{c1::Seine}}", "{{c12::a}}", "{{c::a}}", "A {{c1::b::hint}} and {{c2::c}}.",
                 "{{c1::multi\nline}}", "plain"):
        cases.append(("isCloze", [text]))
        cases.append(("showCloze", [text]))
    for before, all_text in (("The ", "The x"), ("The {{c1::Sei", "The {{c1::Sei"),
                             ("The {{c1::Seine}} and ", "The {{c1::Seine}} and "),
                             ("", "{{c7::a}}"), ("{{c1::a}}{{c2::", "{{c1::a}}{{c2::b}}{{c9::d}}")):
        cases.append(("clozeMark", [before, all_text]))
    for line in real_highlights() + real_lines():
        cases.append(("words", [line]))
        cases.append(("quotesText", [line, "Before it. " + line + " After it."]))
        cases.append(("quotesText", [line.replace("'", "’").upper(), line]))
        cases.append(("expandCloze", ["{{c1::" + line[:20] + "}}" + line[20:40], line]))
        cases.append(("shorten", [line * 12, 300]))
    return [{"fn": fn, "args": args} for fn, args in cases]


@unittest.skipUnless(lua_parity.interpreters(), "lua is not installed")
class ParityTest(unittest.TestCase):
    def compare(self, lua):
        cases = all_cases()
        results = lua_parity.run_cases(cases, lua)
        self.assertEqual(len(results), len(cases))
        compared = 0
        for case, result in zip(cases, results):
            self.assertTrue(result["ok"], (lua, case, result.get("error")))
            want = result.get("value")
            got = python_value(case["fn"], case["args"])
            # An empty Lua table encodes as a list
            if got == {} and want == []:
                got = []
            self.assertEqual(got, want, (lua, case))
            compared += 1
        self.assertGreater(compared, 1500)
        print(f"\n{lua}: {compared} cases identical", file=sys.stderr)

    def test_same_as_lua_5(self):
        self.compare("lua")

    @unittest.skipUnless("luajit" in lua_parity.interpreters(), "luajit is not installed")
    def test_same_as_luajit(self):
        self.compare("luajit")

    def test_cases_cover_real_text(self):
        # Real KOReader highlights and real Companion lines, with non-ASCII among them
        lines = real_highlights() + real_lines()
        self.assertGreaterEqual(len(real_highlights()), 7)
        self.assertTrue(any(ord(ch) > 127 for line in lines for ch in line))

    def test_upper_case_accents_are_not_lowered(self):
        # Lua's %w and lower() work on bytes, so É stays É
        self.assertEqual(cardgen.words("ÉCOLE Normale"), "École normale")
        self.assertFalse(cardgen.quotes_text("ÉCOLE", "école"))


@unittest.skipUnless(lua_parity.interpreters(), "lua is not installed")
class MissingBookFieldsTest(unittest.TestCase):
    def test_python_keeps_every_present_field(self):
        # In cardgen.lua a nil among book, author, chapter and percent leaves a hole in a list
        h = {"text": "x", "book": "B", "chapter": "C", "percent": 50}
        content = cardgen.build_messages(h, {"count": 1})[-1]["content"]
        self.assertIn("<book>\nBook: B\nChapter: C\nPosition in book: 50%\n</book>", content)

    def test_lua_keeps_every_present_field_too(self):
        cases = [
            {"text": "x", "book": "B", "chapter": "C", "percent": 12},
            {"text": "x", "book": "B", "author": "A", "percent": 12},
            {"text": "x", "author": "A"},
            {"text": "x", "chapter": "C", "percent": 0},
            {"text": "x", "percent": 50},
            {"text": "x"},
        ]
        for lua in lua_parity.interpreters():
            results = lua_parity.run_cases([{"fn": "buildMessages", "args": [h, {"count": 1}]} for h in cases], lua)
            for h, result in zip(cases, results):
                with self.subTest(lua=lua, h=h):
                    self.assertTrue(result["ok"], result.get("error"))
                    want = cardgen.build_messages(h, {"count": 1})
                    self.assertEqual([m["content"] for m in result["value"]], [m["content"] for m in want])


class ShortenTest(unittest.TestCase):
    def test_cut_through_a_character_drops_the_partial_bytes(self):
        # The one place a Python string can't hold Lua's bytes: no spaces and a cut inside é
        text = "é" * 200
        self.assertEqual(cardgen._shorten(text, 300), "é" * 150 + " …")
        self.assertEqual(cardgen._shorten("x" + text, 300), "x" + "é" * 149 + " …")


class LaterResearchTest(unittest.TestCase):
    def test_asked_only_with_notes_and_the_flag(self):
        with_flag = {"count": 1, "later_research": True}
        h = {"text": "x"}
        self.assertNotIn("later_research", json.dumps(cardgen.build_messages(h, with_flag)))
        self.assertNotIn("later_research", json.dumps(cardgen.build_messages(h, {"count": 1}, "NOTES")))
        asked = cardgen.build_messages(h, with_flag, "NOTES")
        self.assertIn('top-level "later_research"', asked[1]["content"])
        self.assertIn("Since publication", asked[1]["content"])
        self.assertIn('top-level "later_research"', cardgen.build_messages(h, {**with_flag, "cloze": True}, "NOTES")[1]["content"])
        self.assertNotIn("later_research", cardgen.build_messages(h, {"definition": True, "later_research": True}, "NOTES")[1]["content"])

    def test_read_from_the_reply(self):
        reply = '{"cards":[{"front":"Q","back":"A"}],"later_research":"  Replications failed.\\n"}'
        self.assertEqual(cardgen.parse_later_research(reply), "Replications failed.")
        self.assertEqual(cardgen.parse_reply(reply, {"count": 1}, {"text": "x"})[0], [{"front": "Q", "back": "A"}])
        for reply in ('{"cards":[]}', '{"later_research":""}', '{"later_research":3}', "[]", "nope", None):
            self.assertIsNone(cardgen.parse_later_research(reply))


HIGHLIGHTS = [
    {"text": "Start small.", "chapter": "Chapter 1: Begin", "page": 3, "note": "morning routine"},
    {"text": "Failure costs confidence.", "chapter": "Chapter 2: Fail"},
    {"text": "Respect the plan.", "chapter": "Chapter 2: Fail", "page": 9},
]


class ConnectionsTest(unittest.TestCase):
    def test_messages(self):
        messages = cardgen.build_connections(
            HIGHLIGHTS,
            {"count": 3, "guidance": "Focus on habits", "existing": [OWN], "rejected": [{"front": "R", "back": "S"}]},
            "NOTES", {"book": "B", "author": "A"},
        )
        self.assertEqual(len(messages), 3)
        self.assertIn("<book_notes>\nNOTES\n</book_notes>", messages[0]["content"])
        self.assertIn('{"connections":', messages[1]["content"])
        self.assertIn("at least two highlights", messages[1]["content"])
        user = messages[2]["content"]
        self.assertIn("<book>\nBook: B\nAuthor: A\n</book>", user)
        self.assertIn("1. [Chapter 1: Begin · p. 3] Start small.\n   Reader's note: morning routine", user)
        self.assertIn("2. [Chapter 2: Fail] Failure costs confidence.", user)
        self.assertIn("3. [Chapter 2: Fail · p. 9] Respect the plan.", user)
        self.assertIn("Focus on habits", user)
        self.assertIn("- Q → A", user)
        self.assertIn("- R → S", user)
        self.assertIn("Find up to 3 connections", user)
        self.assertTrue(user.endswith("Reply with the JSON only, in the shape given in the instructions."))
        self.assertEqual(len(cardgen.build_connections(HIGHLIGHTS, {"count": 1})), 2)

    def test_own_prompt_is_used(self):
        messages = cardgen.build_connections(HIGHLIGHTS, {"prompt": {"connections": "Mine."}})
        self.assertTrue(messages[0]["content"].startswith("Mine.\n\nReply with JSON only"))

    def test_parse_drops_answers_without_two_valid_sources(self):
        reply = json.dumps({"connections": [
            {"title": "Good", "front": " Q1 ", "back": "A1", "sources": [1, 3]},
            {"title": "One source", "front": "Q2", "back": "A2", "sources": [2]},
            {"title": "Out of range", "front": "Q3", "back": "A3", "sources": [1, 9]},
            {"title": "Same twice", "front": "Q4", "back": "A4", "sources": [2, 2]},
            {"title": "Words", "front": "Q5", "back": "A5", "sources": ["1", "2"]},
            {"title": "Booleans", "front": "Q6", "back": "A6", "sources": [True, 2]},
            {"title": "No sources", "front": "Q7", "back": "A7"},
            {"title": "No front", "front": "", "back": "A8", "sources": [1, 2]},
            "text",
            {"front": "Untitled", "back": "A9", "sources": [3.0, 2, 2, 7]},
        ]})
        found, err = cardgen.parse_connections("```json\n" + reply + "\n```", 3)
        self.assertIsNone(err)
        self.assertEqual(found, [
            {"title": "Good", "front": "Q1", "back": "A1", "sources": [1, 3]},
            {"title": "Untitled", "front": "Untitled", "back": "A9", "sources": [3, 2]},
        ])
        self.assertEqual(len(cardgen.parse_connections(reply, 3, limit=1)[0]), 1)

    def test_parse_failures(self):
        self.assertEqual(cardgen.parse_connections("nope", 3), (None, "The AI reply could not be read."))
        self.assertEqual(cardgen.parse_connections("{bad}", 3), (None, "The AI reply could not be read."))
        self.assertEqual(cardgen.parse_connections('{"cards":[]}', 3), (None, "The AI reply had no connections."))
        self.assertEqual(cardgen.parse_connections('{"connections":[]}', 3), ([], None))


IDEAS_REPLY = {
    "chapters": [
        {"chapter": "Chapter 1", "big": " Start small. ", "points": [
            {"text": "Begin small.", "covered": True},
            {"text": "Plan for accidents.", "covered": False, "idea": 0},
            {"text": "Gone idea.", "covered": False, "idea": 1},
            {"text": "Later idea.", "covered": False, "idea": 2},
            {"text": "Bad link.", "covered": False, "idea": 99},
            {"text": "", "covered": True},
            "text",
        ]},
        {"chapter": "Chapter 9", "big": "Unread", "points": []},
        {"big": "No name"},
    ],
    "ideas": [
        {"title": "Accidents", "chapter": "Chapter 1", "note": "Why.", "quote": "Allow for “accidents”—always",
         "front": "What should a plan allow for?", "back": "Accidents."},
        {"title": "Past", "chapter": "Chapter 9", "note": "", "quote": "", "front": "Q past", "back": "A"},
        {"title": "Invented", "chapter": "Chapter 1", "note": "", "quote": "Words nobody wrote at all",
         "front": "Q invented", "back": "A"},
        {"title": "No back", "chapter": "Chapter 1", "front": "Q", "back": ""},
        "text",
    ],
}
BOOK_TEXT = "Be content with quite a little. Allow for accidents, always. Allow for human nature."


class IdeasTest(unittest.TestCase):
    def test_messages(self):
        messages = cardgen.build_ideas(HIGHLIGHTS, {"count": 4, "guidance": "Habits"}, "NOTES", {"book": "B"})
        self.assertIn("<book_notes>\nNOTES\n</book_notes>", messages[0]["content"])
        self.assertIn('{"chapters":', messages[1]["content"])
        self.assertIn("word for word", messages[1]["content"])
        self.assertIn("Everything the reader highlighted", messages[2]["content"])
        self.assertIn("Write at most 4 ideas.", messages[2]["content"])
        self.assertIn("The reader has no highlights", cardgen.build_ideas([], {}, "NOTES")[2]["content"])

    def test_quotes_and_progress_are_checked(self):
        allowed = lambda name: name != "Chapter 9"
        found, err = cardgen.parse_ideas(json.dumps(IDEAS_REPLY), BOOK_TEXT, allowed)
        self.assertIsNone(err)
        self.assertEqual([i["title"] for i in found["ideas"]], ["Accidents", "Invented"])
        self.assertEqual(found["ideas"][0]["quote"], "Allow for “accidents”—always")
        self.assertEqual(found["ideas"][1]["quote"], "", "a quote not in the book is dropped")
        self.assertEqual(found["ideas"][1]["front"], "Q invented", "the idea itself stays")
        self.assertEqual([c["chapter"] for c in found["chapters"]], ["Chapter 1"])
        self.assertEqual(found["chapters"][0]["big"], "Start small.")
        self.assertEqual(found["chapters"][0]["points"], [
            {"text": "Begin small.", "covered": True},
            {"text": "Plan for accidents.", "covered": False, "idea": 0},
            {"text": "Gone idea.", "covered": False},
            {"text": "Later idea.", "covered": False, "idea": 1},
            {"text": "Bad link.", "covered": False},
        ])

    def test_without_the_book_no_quote_survives(self):
        found, _err = cardgen.parse_ideas(json.dumps(IDEAS_REPLY))
        self.assertTrue(all(i["quote"] == "" for i in found["ideas"]))
        self.assertEqual(len(found["ideas"]), 3)

    def test_failures(self):
        self.assertEqual(cardgen.parse_ideas("nope"), (None, "The AI reply could not be read."))
        self.assertEqual(cardgen.parse_ideas("[]"), (None, "The AI reply could not be read."))
        self.assertEqual(cardgen.parse_ideas('{"x":1}'), (None, "The AI reply had no key ideas."))
        self.assertEqual(cardgen.parse_ideas('{"ideas":[]}'), ({"chapters": [], "ideas": []}, None))


class RewriteTest(unittest.TestCase):
    def test_messages(self):
        messages = cardgen.build_rewrite({"front": "Q", "back": "A"}, H_FULL, "Shorter please", {"rewrite": "Mine."}, "NOTES")
        self.assertEqual(len(messages), 3)
        self.assertTrue(messages[1]["content"].startswith("Mine.\n\nReply with JSON only"))
        user = messages[2]["content"]
        self.assertIn("<card>\nFront: Q\nBack: A\n</card>", user)
        self.assertIn("<reader_request>\nShorter please\n</reader_request>", user)
        self.assertIn("«Paris is the capital of France.»", user)
        self.assertNotIn("reader_request", cardgen.build_rewrite({"front": "Q", "back": ""}, {"text": "x"})[1]["content"])

    def test_parse(self):
        self.assertEqual(cardgen.parse_rewrite('{"front":" Q ","back":"A"}'), ({"front": "Q", "back": "A"}, None))
        self.assertEqual(cardgen.parse_rewrite('{"front":"Q"}'), ({"front": "Q", "back": ""}, None))
        self.assertEqual(cardgen.parse_rewrite('{"back":"A"}'), (None, "The AI reply had no card."))
        self.assertEqual(cardgen.parse_rewrite("nope"), (None, "The AI reply could not be read."))
        self.assertEqual(cardgen.parse_rewrite("[]"), (None, "The AI reply could not be read."))

    def test_cloze_stays_cloze(self):
        original = {"front": "A {{c1::b}}", "back": ""}
        self.assertEqual(cardgen.parse_rewrite('{"front":"What is A?","back":"b"}', original),
                         (None, "The AI's rewrite lost the hidden words. Try again."))
        self.assertEqual(cardgen.parse_rewrite('{"front":"{{c1::A}} b","back":""}', original)[0]["front"], "{{c1::A}} b")


class DefinitionTest(unittest.TestCase):
    def test_messages_use_the_term_as_the_highlight(self):
        messages = cardgen.build_definition("ardour", {"text": "the whole highlight", "before": "Warn", "after": "you", "book": "B"})
        self.assertEqual(messages[0]["content"], cardgen.DEFINITION_PROMPT)
        self.assertIn("<highlight>\nardour\n</highlight>", messages[1]["content"])
        self.assertIn("Warn «ardour» you", messages[1]["content"])
        self.assertNotIn("the whole highlight", messages[1]["content"])

    def test_cards_add_define_once(self):
        for term in ("ardour", "Define ardour", "define: ardour", "DEFINE   ardour"):
            reply = json.dumps({"term": term, "definition": "Burning enthusiasm."})
            self.assertEqual(cardgen.definition_cards(reply), ([
                {"front": "Define ardour", "back": "Burning enthusiasm."},
                {"front": "Burning enthusiasm.", "back": "ardour"},
            ], None), term)

    def test_a_term_starting_with_define_is_kept(self):
        self.assertEqual(cardgen.strip_define("define"), "define")
        self.assertEqual(cardgen.strip_define("defined benefit"), "defined benefit")

    def test_failures(self):
        self.assertEqual(cardgen.definition_cards("nope"), (None, "The AI reply could not be read."))
        self.assertEqual(cardgen.definition_cards('{"term":"x"}'), (None, "The AI reply had no definition."))


class PromptsFilesTest(unittest.TestCase):
    def test_desktop_prompts_ask_for_cards_that_stand_alone(self):
        for kind, reply in (("connections", '"sources"'), ("ideas", '"covered"'), ("rewrite", '"front"')):
            text = cardgen.PROMPTS[kind]
            self.assertIn("stands on its own", text, kind)
            self.assertIn("months later", text, kind)
            self.assertIn("Plain text only, no Markdown.", text, kind)
            self.assertIn(reply, cardgen.system_prompt(None, kind), kind)
            self.assertNotIn("—", text, kind)

    def test_good_cards_pass_the_checks(self):
        # The prompts' own "Right" examples pass the card checks they teach
        for text in (cardgen.PROMPTS["connections"], cardgen.PROMPTS["ideas"], cardgen.PROMPTS["rewrite"]):
            right = re.search(r'Right: "([^"]+)"', text).group(1)
            self.assertIsNone(checks.standalone_warning({"kind": "qa", "front": right}), right)


class OddReplyTest(unittest.TestCase):
    def test_sources_that_arent_finite_numbers_are_ignored(self):
        for bad in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(bad=bad):
                reply = '{"connections":[{"front":"a","back":"b","sources":[%s,1,2]}]}' % bad
                found, error = cardgen.parse_connections(reply, 3)
                self.assertIsNone(error)
                self.assertEqual(found[0]["sources"], [1, 2])

    def test_a_hyphenated_term_keeps_its_define(self):
        self.assertEqual(cardgen.strip_define("define-x"), "define-x")
        self.assertEqual(cardgen.strip_define("Defined"), "Defined")
        self.assertEqual(cardgen.strip_define("Define: thrift"), "thrift")
        self.assertEqual(cardgen.strip_define("define thrift"), "thrift")


if __name__ == "__main__":
    unittest.main()
