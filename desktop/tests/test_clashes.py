import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbanki_desktop import ai, cardgen, clashes
from pbanki_desktop.store import Store
from test_service import Base


def make_collection(mount, notes):
    """A small Anki collection: `notes` are (id, fields list, deck name)."""
    path = Path(mount) / clashes.COLLECTION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE notes(id INTEGER, flds TEXT); CREATE TABLE cards(id INTEGER, nid INTEGER, did INTEGER);"
                       "CREATE TABLE decks(id INTEGER, name TEXT);")
    decks = {}
    for nid, fields, deck in notes:
        did = decks.setdefault(deck, len(decks) + 1)
        conn.execute("INSERT INTO notes VALUES (?, ?)", (nid, "\x1f".join(fields)))
        conn.execute("INSERT INTO cards VALUES (?, ?, ?)", (nid, nid, did))
    for name, did in decks.items():
        conn.execute("INSERT INTO decks VALUES (?, ?)", (did, name))
    conn.commit()
    conn.close()
    return path


NOTES = [
    (1, ["What should you ask before indulging?", "Do I want this daily?"], "KOReader\x1fWillpower"),
    (2, ["When chasing a reward, what should you ask?", "How will I feel after?"], "KOReader\x1fWillpower"),
    (3, ["What is the capital of Australia?", "Canberra"], "Places"),
]


class FakeQuery:
    def __init__(self, search=None, scores=None, new=None):
        self.search = search if search is not None else {"clashes": [{"cards": [1, 2], "why": "Different questions to ask"}]}
        self.scores = scores if scores is not None else {"verdicts": [{"pair": 1, "p": 0.6}]}
        self.new = new if new is not None else {"clashes": []}
        self.prompts = []
        self.lock = threading.Lock()

    def __call__(self, model, messages, timeout=None, cancel=None):
        prompt = "\n\n".join(m["content"] for m in messages)
        with self.lock:
            self.prompts.append(prompt)
        if "You judge pairs" in prompt:
            return json.dumps(self.scores)
        if "You compare new Anki" in prompt:
            return json.dumps(self.new)
        if "find confusingly similar" in prompt:
            return json.dumps(self.search)
        return json.dumps({"cards": [{"front": "A new question?", "back": "An answer"}]})


class Parts(unittest.TestCase):
    def test_plain_keeps_a_cloze_as_brackets_and_drops_markup(self):
        self.assertEqual(clashes.plain("A <b>bold</b><br>{{c1::word::hint}} &amp; more"), "A bold [word] & more")

    def test_closest_ranks_by_shared_words(self):
        notes = [{"nid": n, "deck": "", "front": front, "back": ""}
                 for n, front in ((1, "Why does stress hurt willpower?"), (2, "Name a capital city"), (3, "Stress affects what?"))]
        self.assertEqual([note["nid"] for note in clashes.closest(notes, "stress hurts your willpower")], [1, 3])

    def test_a_search_reply_ignores_numbers_it_was_not_given(self):
        numbers = {1: {"nid": 10}, 2: {"nid": 20}}
        reply = json.dumps({"clashes": [{"cards": [1, 2, 99], "why": "w"}, {"cards": [1], "why": "alone"}]})
        self.assertEqual(clashes.parse_search(reply, numbers), {(10, 20): "w"})

    def test_a_bad_reply_is_a_plain_error(self):
        with self.assertRaises(ai.AIError):
            clashes.parse_search("no json here", {})

    def test_pairs_sharing_a_card_group_together(self):
        pairs = [{"a": 1, "b": 2}, {"a": 5, "b": 6}, {"a": 2, "b": 3}]
        self.assertEqual([len(g) for g in clashes.group(pairs)], [2, 1])

    def test_a_new_card_below_the_cutoff_is_not_reported(self):
        cards = [{"front": "Q", "back": "A"}]
        reply = json.dumps({"clashes": [{"new": 1, "other": "E7", "p": 0.1, "why": "w"}]})
        self.assertEqual(clashes.parse_new_cards(reply, cards, {7: {"front": "O", "back": "B"}}), {})

    def test_the_prompt_only_mentions_the_collection_when_there_is_one(self):
        h = {"text": "Words"}
        without = "\n".join(m["content"] for m in cardgen.build_messages(h, {"count": 1}))
        self.assertNotIn("reader_collection", without)
        with_cards = "\n".join(m["content"] for m in cardgen.build_messages(h, {"count": 1, "collection": [{"front": "Q", "back": "A"}]}))
        self.assertIn("<reader_collection>", with_cards)

    def test_an_empty_list_counts_as_no_cards_only_with_a_collection(self):
        reply = '{"cards":[]}'
        self.assertEqual(cardgen.parse_reply(reply, {"count": 1, "collection": [{}]}, {}), ([], None))
        self.assertIsNone(cardgen.parse_reply(reply, {"count": 1}, {})[0])


class ReadingTheCollection(unittest.TestCase):
    def setUp(self):
        self.mount = Path(tempfile.mkdtemp(prefix="pbanki-clash-"))
        self.addCleanup(shutil.rmtree, self.mount, ignore_errors=True)

    def test_reads_cards_and_leaves_the_reader_as_it_was(self):
        path = make_collection(self.mount, NOTES)
        before = sorted(p.name for p in path.parent.iterdir())
        data = path.read_bytes()
        notes = clashes.read_collection(self.mount)
        self.assertEqual([note["nid"] for note in notes], [1, 2, 3])
        self.assertEqual(notes[0]["deck"], "Willpower")
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), before)
        self.assertEqual(path.read_bytes(), data)

    def test_no_collection_or_a_broken_one_gives_none(self):
        self.assertIsNone(clashes.read_collection(self.mount))
        path = self.mount / clashes.COLLECTION_FILE
        path.parent.mkdir(parents=True)
        path.write_text("not a database")
        self.assertIsNone(clashes.read_collection(self.mount))


class WithService(Base):
    def wait(self, test, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if test():
                return
            time.sleep(0.01)
        self.fail("It didn't happen in time")

    def setUpService(self, query):
        make_collection(self.mount, NOTES)
        self.query = query
        self.svc = self.make_service(query=query)
        self.svc.clashes.refresh(self.mount)

    def search(self):
        self.svc.clashes_find()
        self.wait(lambda: not self.svc.clashes_view()["running"])
        return self.svc.clashes_view()

    def test_a_search_lists_what_the_checker_kept(self):
        self.setUpService(FakeQuery())
        view = self.search()
        self.assertIsNone(view["error"])
        self.assertEqual(view["counts"], {"new": 1, "real": 0, "no": 0})
        pair = view["groups"][0][0]
        self.assertEqual((pair["a"], pair["b"]), (1, 2))
        self.assertEqual(pair["percent"], 60)

    def test_the_checker_drops_pairs_below_the_cutoff(self):
        self.setUpService(FakeQuery(scores={"verdicts": [{"pair": 1, "p": 0.2}]}))
        self.assertEqual(self.search()["groups"], [])

    def test_a_verdict_stays_and_teaches_the_next_search(self):
        self.setUpService(FakeQuery())
        self.search()
        self.svc.clash_verdict("1-2", "real")
        self.query.prompts.clear()
        view = self.search()
        self.assertEqual(view["counts"]["real"], 1)
        self.assertTrue(any("REAL clashes" in prompt for prompt in self.query.prompts))
        self.assertFalse(any("You judge pairs" in prompt for prompt in self.query.prompts))

    def test_rewording_a_question_counts_a_real_clash_as_fixed(self):
        self.setUpService(FakeQuery())
        self.search()
        self.svc.clash_verdict("1-2", "real")
        (self.mount / clashes.COLLECTION_FILE).unlink()
        make_collection(self.mount, [(1, ["What should you ask before a daily habit?", "Do I want this daily?"], "D"), NOTES[1]])
        self.svc.clashes.refresh(self.mount)
        view = self.svc.clashes_view()
        self.assertEqual(view["fixed"], 1)
        self.assertEqual(view["groups"], [])

    def test_a_failed_search_says_why_and_keeps_verdicts(self):
        self.setUpService(FakeQuery())
        self.search()
        self.svc.clash_verdict("1-2", "real")
        def broken(*args, **kwargs):
            raise ai.AIError("The AI isn't answering.")
        self.svc.clashes._query = broken
        view = self.search()
        self.assertEqual(view["error"], "The AI isn't answering.")
        self.assertEqual(view["counts"]["real"], 1)

    def test_an_unknown_pair_is_refused(self):
        self.setUpService(FakeQuery())
        with self.assertRaises(Exception):
            self.svc.clash_verdict("8-9", "real")

    def test_nothing_on_the_reader_is_written(self):
        self.setUpService(FakeQuery())
        before = {p: p.read_bytes() for p in self.mount.rglob("*") if p.is_file()}
        self.search()
        after = {p: p.read_bytes() for p in self.mount.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_suggestions_carry_the_closest_cards(self):
        self.setUpService(FakeQuery())
        h = self.svc.open_book(self.pride)["highlights"][0]
        requested = self.svc.runner.card_context(self.pride, h["id"], {})
        self.assertIn("collection", requested["request"]) if self._shares_words(h) else None

    def _shares_words(self, h):
        return bool(clashes.closest(self.svc._store.collection_notes(), h.get("mark") or ""))

    def test_new_cards_get_a_warning_and_a_changed_question_loses_it(self):
        reply = {"clashes": [{"new": 1, "other": "E1", "p": 0.7, "why": "Reads alike"}]}
        self.setUpService(FakeQuery(new=reply))
        h = self.svc.open_book(self.pride)["highlights"][0]
        card = self.svc._store.add_card(self.pride, h["id"], "qa", "What to ask when tempted?", "How will I feel?")
        self.svc._check_new_cards(self.pride)
        self.wait(lambda: self.svc._clash_note(f"card:{card['id']}", card["front"]))
        note = self.svc._clash_note(f"card:{card['id']}", card["front"])
        self.assertEqual(note["front"], NOTES[0][1][0])
        self.assertIsNone(self.svc._clash_note(f"card:{card['id']}", "Something else?"))

    def test_without_a_collection_new_cards_ask_the_ai_nothing(self):
        query = FakeQuery()
        self.svc = self.make_service(query=query)
        h = self.svc.open_book(self.pride)["highlights"][0]
        self.svc._store.add_card(self.pride, h["id"], "qa", "A question?", "An answer")
        self.svc._check_new_cards(self.pride)
        self.assertEqual(query.prompts, [])

    def test_trial_verdicts_are_taken_in_once(self):
        import_file = self.home / clashes.IMPORT_FILE
        import_file.write_text(json.dumps({"read_at": 5, "notes": [{"nid": n, "deck": "D", "front": f"Q{n}", "back": f"A{n}"} for n in (1, 2)],
                                           "pairs": [{"a": 1, "b": 2, "state": "real", "fingerprint": clashes.fingerprint({"front": "Q1", "back": "A1"}, {"front": "Q2", "back": "A2"}),
                                                      "front_a": "Q1", "back_a": "A1", "front_b": "Q2", "back_b": "A2"}]}))
        svc = self.make_service(query=FakeQuery())
        self.assertEqual(svc.clashes_view()["counts"]["real"], 1)
        self.assertFalse(import_file.exists())


if __name__ == "__main__":
    unittest.main()
