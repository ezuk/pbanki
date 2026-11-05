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

from pbanki_desktop import ai, cardgen, clashes, service
from pbanki_desktop.store import Store
from test_service import Base


def make_collection(mount, notes, notetype="Basic", usn=-1, suspended=()):
    """A small Anki collection: `notes` are (id, fields list, deck name)."""
    path = Path(mount) / clashes.COLLECTION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE notes(id INTEGER, mid INTEGER, usn INTEGER, tags TEXT, flds TEXT);"
        "CREATE TABLE cards(id INTEGER, nid INTEGER, did INTEGER, usn INTEGER, queue INTEGER, ivl INTEGER, reps INTEGER, lapses INTEGER);"
        "CREATE TABLE decks(id INTEGER, name TEXT);"
        "CREATE TABLE notetypes(id INTEGER, name TEXT);"
        "CREATE TABLE graves(oid INTEGER, type INTEGER, usn INTEGER);")
    conn.execute("INSERT INTO notetypes VALUES (1, ?)", (notetype,))
    decks = {}
    for nid, fields, deck in notes:
        did = decks.setdefault(deck, len(decks) + 1)
        conn.execute("INSERT INTO notes VALUES (?, 1, ?, '', ?)", (nid, usn, "\x1f".join(fields)))
        conn.execute("INSERT INTO cards VALUES (?, ?, ?, ?, ?, 41, 23, 2)", (nid, nid, did, usn, -1 if nid in suspended else 2))
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
        self.assertEqual(view["counts"], {"clashes": 1, "changes": 0, "dismissed": 0})
        pair = view["pairs"][0]
        self.assertEqual((pair["a"], pair["b"]), (1, 2))
        self.assertEqual(pair["score"], 0.6)

    def test_the_checker_drops_pairs_below_the_cutoff(self):
        self.setUpService(FakeQuery(scores={"verdicts": [{"pair": 1, "p": 0.2}]}))
        self.assertEqual(self.search()["pairs"], [])

    def real(self):
        self.search()
        self.svc._store.update_clash_pair(1, 2, state="real")

    def test_a_real_clash_stays_and_teaches_the_next_search(self):
        self.setUpService(FakeQuery())
        self.real()
        self.query.prompts.clear()
        view = self.search()
        self.assertEqual(view["counts"]["clashes"], 1)
        self.assertEqual(view["pairs"][0]["state"], "real")
        self.assertTrue(any("REAL clashes" in prompt for prompt in self.query.prompts))
        self.assertFalse(any("You judge pairs" in prompt for prompt in self.query.prompts))

    def test_a_search_never_erases_an_answer(self):
        self.setUpService(FakeQuery())
        self.search()
        self.svc._store.set_clash_pair({"a": 1, "b": 2, "fingerprint": "other", "front_a": "Q", "back_a": "", "front_b": "R", "back_b": "",
                                        "state": "new"})
        self.assertEqual(self.svc._store.clash_pair(1, 2)["fingerprint"], "other")
        self.svc._store.update_clash_pair(1, 2, state="real")
        self.svc._store.set_clash_pair({"a": 1, "b": 2, "fingerprint": "third", "front_a": "Q", "back_a": "", "front_b": "R", "back_b": "",
                                        "state": "new"})
        self.assertEqual(self.svc._store.clash_pair(1, 2)["state"], "real")

    def test_rewording_a_question_asks_whether_it_is_fixed(self):
        self.setUpService(FakeQuery())
        self.real()
        make_collection(self.mount, [(1, ["What should you ask before a daily habit?", "Do I want this daily?"], "D"), NOTES[1]])
        self.svc.clashes.refresh(self.mount)
        pair = self.svc.clashes_view()["pairs"][0]
        self.assertEqual(pair["status"], "ask")
        self.assertEqual(pair["msg"], "Reworded. Does this fix it?")
        self.assertFalse(pair["closed"])
        self.query.prompts.clear()
        view = self.svc.clash_pair_action("1-2", "fixed")
        self.assertEqual(view["pairs"][0]["status"], "fixed")
        self.assertEqual(view["counts"]["clashes"], 0)
        self.assertEqual(self.query.prompts, [])
        view = self.svc.clash_pair_action("1-2", "undo")
        self.assertEqual(view["pairs"][0]["status"], "ask")
        view = self.svc.clash_pair_action("1-2", "still")
        self.assertEqual(view["pairs"][0]["msg"], "Reworded, still a clash.")
        self.assertEqual(view["counts"]["clashes"], 1)
        verdicts = [row["verdict"] for row in self.svc._store.clash_verdicts()]
        self.assertEqual(verdicts, ["fixed", "undo-fixed", "still"])

    def test_the_ai_only_looks_at_reworded_pairs_when_asked(self):
        self.setUpService(FakeQuery(scores={"verdicts": [{"pair": 1, "p": 0.1}]}))
        self.svc.clashes.check_reworded  # noqa: B018
        self.svc._store.set_clash_pair({"a": 1, "b": 2, "fingerprint": "older words", "front_a": "Q", "back_a": "", "front_b": "R", "back_b": "", "state": "real"})
        self.assertEqual(self.query.prompts, [])
        self.svc.clash_pair_action("1-2", "ask_ai")
        self.wait(lambda: not self.svc.clashes_view()["running"])
        pair = self.svc.clashes_view()["pairs"][0]
        self.assertEqual(pair["status"], "ai")
        self.assertIn("no longer read alike", pair["msg"])
        self.assertEqual(self.svc._store.clash_verdicts()[-1]["verdict"], "ai")
        self.assertEqual(self.svc._store.clash_verdicts()[-1]["score"], 0.1)

    def test_a_failed_search_says_why_and_keeps_verdicts(self):
        self.setUpService(FakeQuery())
        self.real()
        def broken(*args, **kwargs):
            raise ai.AIError("The AI isn't answering.")
        self.svc.clashes._query = broken
        view = self.search()
        self.assertEqual(view["error"], "The AI isn't answering.")
        self.assertEqual(view["pairs"][0]["state"], "real")

    def test_an_unknown_pair_is_refused(self):
        self.setUpService(FakeQuery())
        with self.assertRaises(service.ServiceError):
            self.svc.clash_pair_action("8-9", "dismiss")

    # Dismissing

    def test_a_dismissed_pair_is_logged_and_stays_hidden_while_it_reads_about_the_same(self):
        self.setUpService(FakeQuery())
        self.search()
        view = self.svc.clash_pair_action("1-2", "dismiss")
        self.assertEqual(view["counts"]["clashes"], 0)
        self.assertEqual(view["counts"]["dismissed"], 1)
        self.assertEqual(view["dismissed"][0]["source"], "From Find clashes")
        self.assertEqual(self.svc._store.clash_verdicts()[-1]["verdict"], "no")
        self.query.prompts.clear()
        view = self.search()
        self.assertEqual(view["pairs"][0]["status"], "dismissed")
        self.assertTrue(view["pairs"][0]["closed"])
        self.assertFalse(any("You judge pairs" in prompt for prompt in self.query.prompts))
        self.assertIn("Left out 1 pair you already answered.", view["learn"])

    def test_a_pair_reworded_a_lot_can_come_up_again(self):
        self.setUpService(FakeQuery())
        self.search()
        self.svc.clash_pair_action("1-2", "dismiss")
        make_collection(self.mount, [(1, ["Name the question to pose in front of something tempting", "Do I want this daily?"], "D"), NOTES[1]])
        self.svc.clashes.refresh(self.mount)
        view = self.search()
        self.assertEqual(view["counts"]["clashes"], 1)
        self.assertEqual(view["counts"]["dismissed"], 0)

    def test_undoing_a_dismissal_brings_the_pair_back(self):
        self.setUpService(FakeQuery())
        self.search()
        view = self.svc.clash_pair_action("1-2", "dismiss")
        view = self.svc.clash_undo_dismissal(view["dismissed"][0]["id"])
        self.assertEqual(view["counts"], {"clashes": 1, "changes": 0, "dismissed": 0})

    def test_a_dismissal_from_find_also_stops_the_save_check_warning(self):
        reply = {"clashes": [{"new": 1, "other": "E1", "p": 0.7, "why": "Alike"}]}
        self.setUpService(FakeQuery(new=reply))
        self.search()
        self.svc._store.add_clash_dismissal({"front_a": "What to ask when tempted?", "back_a": "", "front_b": NOTES[0][1][0], "back_b": "", "source": "find"})
        self.save_cards(("What to ask when tempted?", "How will I feel?"))
        self.assertEqual(self.checked()["clashCheck"]["found"], 0)

    def test_suspended_cards_are_left_out_of_searches_and_checks(self):
        self.setUpService(FakeQuery())
        self.svc.card_suspend("3")
        self.query.prompts.clear()
        self.search()
        self.assertFalse(any("capital of Australia" in prompt for prompt in self.query.prompts))
        self.save_cards(("Answered?", "Yes"))
        self.checked()
        self.assertNotIn("capital of Australia", self.query.prompts[-1])

    def test_old_answers_move_to_the_dismissed_list_once(self):
        self.setUpService(FakeQuery())
        self.svc._store.set_setting(clashes.NOT_A_CLASH_SETTING, [{"front_a": "A?", "back_a": "", "front_b": "B?", "back_b": "", "state": "no", "decided_at": 1}])
        self.svc._store.set_clash_pair({"a": 1, "b": 2, "fingerprint": "f", "front_a": "C?", "back_a": "", "front_b": "D?", "back_b": "", "state": "no"})
        self.svc.clashes.import_trial_file()
        self.svc.clashes.import_trial_file()
        self.assertEqual(sorted(row["source"] for row in self.svc._store.clash_dismissals()), ["find", "save"])

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
        record = f"card:{card['id']}"
        self.wait(lambda: self.svc._clash_note(record, card["front"], card["back"]))
        note = self.svc._clash_note(record, card["front"], card["back"])
        self.assertEqual(note["front"], NOTES[0][1][0])
        self.assertIsNone(self.svc._clash_note(record, "Something else?", card["back"]))
        self.assertIsNone(self.svc._clash_note(record, card["front"], "Something else"))

    # The check on the Save cards screen

    def save_cards(self, *cards):
        """Opens the first highlights of the book, one card each. Returns the cards."""
        highlights = self.svc.open_book(self.pride)["highlights"]
        made = []
        for h, (front, back) in zip(highlights, cards):
            self.svc.open_highlight(self.pride, h["id"])
            made.append(self.svc._store.add_card(self.pride, h["id"], "qa", front, back))
        return made

    def checked(self):
        self.svc.save_check(self.pride)
        self.wait(lambda: self.svc.save_preview(self.pride)["clashCheck"]["state"] not in ("checking", "pending"))
        return self.svc.save_preview(self.pride)

    def rows(self, preview):
        return {row["front_raw"]: row for group in preview["groups"] for row in group["rows"]}

    def test_a_save_waits_for_a_check_of_the_cards_in_it(self):
        self.setUpService(FakeQuery())
        answered, unanswered = self.save_cards(("Answered?", "Yes"), ("Unanswered?", ""))
        preview = self.svc.save_preview(self.pride)
        self.assertEqual(preview["clashCheck"]["state"], "pending")
        self.assertTrue(self.rows(preview)["Answered?"]["unchecked"])
        preview = self.checked()
        self.assertEqual(preview["clashCheck"]["state"], "done")
        self.assertEqual(preview["clashCheck"]["found"], 0)
        self.assertEqual(preview["clashCheck"]["total"], 3)
        self.assertIn("Answered?", self.query.prompts[-1])
        self.assertNotIn("Unanswered?", self.query.prompts[-1])
        self.assertNotIn("unchecked", self.rows(preview)["Answered?"])

    def test_without_a_collection_there_is_no_check(self):
        self.svc = self.make_service(query=FakeQuery())
        self.save_cards(("A question?", "An answer"))
        self.assertEqual(self.svc.save_preview(self.pride)["clashCheck"]["state"], "none")

    def test_a_failed_check_says_why_and_can_be_tried_again(self):
        self.setUpService(FakeQuery())
        self.save_cards(("A question?", "An answer"))
        real = self.svc.clashes._ask

        def broken(*_args, **_kwargs):
            raise clashes.ai.AIError("The AI isn't answering.")

        self.svc.clashes._ask = broken
        preview = self.checked()
        self.assertEqual(preview["clashCheck"]["state"], "failed")
        self.assertEqual(preview["clashCheck"]["error"], "The AI isn't answering.")
        self.assertFalse(preview["saveBlocked"])
        self.svc.clashes._ask = real
        self.assertEqual(self.checked()["clashCheck"]["state"], "done")

    def test_a_clash_with_the_collection_shows_on_its_row(self):
        self.setUpService(FakeQuery(new={"clashes": [{"new": 1, "other": "E1", "p": 0.7, "why": "Alike"}]}))
        self.save_cards(("What to ask when tempted?", "How will I feel?"))
        preview = self.checked()
        self.assertEqual(preview["clashCheck"]["found"], 1)
        self.assertEqual(self.rows(preview)["What to ask when tempted?"]["clash"]["front"], NOTES[0][1][0])

    def test_changing_an_answer_asks_again(self):
        self.setUpService(FakeQuery())
        card = self.save_cards(("A question?", "An answer"))[0]
        self.checked()
        self.svc.update_card(card["id"], back="Another answer")
        self.assertEqual(self.svc.save_preview(self.pride)["clashCheck"]["state"], "pending")

    def test_new_cards_are_checked_against_each_other_and_both_are_flagged(self):
        self.setUpService(FakeQuery(new={"clashes": [{"new": 1, "other": "N2", "p": 0.8, "why": "Alike"}]}))
        self.save_cards(("How do tomatoes taste?", "Great"), ("What is the taste of tomatoes?", "Meh"))
        rows = self.rows(self.checked())
        self.assertEqual(rows["How do tomatoes taste?"]["clash"]["front"], "What is the taste of tomatoes?")
        self.assertEqual(rows["What is the taste of tomatoes?"]["clash"]["front"], "How do tomatoes taste?")

    def test_a_new_card_is_compared_with_new_cards_already_checked(self):
        self.setUpService(FakeQuery())
        self.save_cards(("How do tomatoes taste?", "Great"))
        self.checked()
        highlight = self.svc.book(self.pride)["highlights"][1]
        self.svc.open_highlight(self.pride, highlight["id"])
        self.svc._store.add_card(self.pride, highlight["id"], "qa", "What is the taste of tomatoes?", "Meh")
        self.query.new = {"clashes": [{"new": 1, "other": "E-1", "p": 0.8, "why": "Alike"}]}
        rows = self.rows(self.checked())
        self.assertIn("E-1. Q: How do tomatoes taste?", self.query.prompts[-1])
        self.assertEqual(rows["What is the taste of tomatoes?"]["clash"]["front"], "How do tomatoes taste?")
        self.assertEqual(rows["How do tomatoes taste?"]["clash"]["front"], "What is the taste of tomatoes?")

    def test_removing_one_of_two_clashing_cards_checks_the_other_again(self):
        self.setUpService(FakeQuery(new={"clashes": [{"new": 1, "other": "N2", "p": 0.8, "why": "Alike"}]}))
        first, second = self.save_cards(("How do tomatoes taste?", "Great"), ("What is the taste of tomatoes?", "Meh"))
        self.checked()
        self.query.new = {"clashes": []}
        self.svc.remove_card(second["id"])
        preview = self.svc.save_preview(self.pride)
        self.assertEqual(preview["clashCheck"]["state"], "pending")
        self.assertTrue(self.rows(preview)["How do tomatoes taste?"]["unchecked"])
        preview = self.checked()
        self.assertEqual(preview["clashCheck"]["state"], "done")
        self.assertEqual(preview["clashCheck"]["found"], 0)

    def test_not_a_clash_clears_both_cards_and_teaches_later_checks(self):
        self.setUpService(FakeQuery(new={"clashes": [{"new": 1, "other": "N2", "p": 0.8, "why": "Alike"}]}))
        first, second = self.save_cards(("How do tomatoes taste?", "Great"), ("What is the taste of tomatoes?", "Meh"))
        rows = self.rows(self.checked())
        self.svc.not_a_clash(self.pride, rows["How do tomatoes taste?"]["path"])
        preview = self.svc.save_preview(self.pride)
        self.assertEqual(preview["clashCheck"]["found"], 0)
        self.assertNotIn("unchecked", self.rows(preview)["How do tomatoes taste?"])
        self.assertNotIn("unchecked", self.rows(preview)["What is the taste of tomatoes?"])

        # The AI sees the answer as an example, and the same pair can't be flagged again
        self.svc.update_card(first["id"], back="Great, really")
        preview = self.checked()
        self.assertIn("NOT clashes", self.query.prompts[-1])
        self.assertIn("Q1: How do tomatoes taste?", self.query.prompts[-1])
        self.assertEqual(preview["clashCheck"]["found"], 0)

    def test_not_a_clash_on_a_card_not_in_the_save_is_refused(self):
        self.setUpService(FakeQuery())
        self.save_cards(("A question?", "An answer"))
        with self.assertRaises(service.ServiceError):
            self.svc.not_a_clash(self.pride, "/api/cards/nope")

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
        self.assertEqual(svc.clashes_view()["pairs"][0]["state"], "real")
        self.assertFalse(import_file.exists())


if __name__ == "__main__":
    unittest.main()
