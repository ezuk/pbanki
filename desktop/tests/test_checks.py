"""checks.py: stand-alone words and near-duplicate cards."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pbanki_desktop import checks  # noqa: E402


class StandaloneTest(unittest.TestCase):
    def test_each_pointing_word_is_found(self):
        for word in ("this", "that", "these", "those", "such", "it"):
            front = f"Where did {word} go wrong?"
            warning = checks.standalone_warning({"kind": "qa", "front": front})
            self.assertEqual(warning, f'"{word}" points at nothing when you see the card alone, months from now.')

    def test_any_case_and_with_punctuation(self):
        self.assertIn('"this"', checks.standalone_warning({"kind": "qa", "front": "This matters why?"}))
        self.assertIn('"it"', checks.standalone_warning({"kind": "qa", "front": "Why does it matter?"}))
        self.assertIn('"it"', checks.standalone_warning({"kind": "qa", "front": "Why is it’s use limited?"}))
        self.assertIn('"that"', checks.standalone_warning({"kind": "qonly", "front": 'What did "that" mean?'}))

    def test_words_inside_other_words_pass(self):
        for front in ("What is thistle?", "What is suchlike?", "Is item 3 kept?", "Why do bits matter?",
                      "Who said thatch?", "What is a well-that-ends?", ""):
            self.assertIsNone(checks.standalone_warning({"kind": "qa", "front": front}), front)

    def test_cloze_cards_skip_the_check(self):
        self.assertIsNone(checks.standalone_warning({"kind": "cloze", "front": "Such {{c1::optimism}} licenses it."}))
        self.assertIsNone(checks.standalone_warning({"kind": "qa", "front": "Such {{c1::optimism}} licenses it."}))

    def test_the_mock_card(self):
        warning = checks.standalone_warning({"kind": "qonly", "front": "Where did this go wrong in my last routine?"})
        self.assertEqual(warning, '"this" points at nothing when you see the card alone, months from now.')


class DuplicateTest(unittest.TestCase):
    def test_overlap_is_jaccard_on_words(self):
        self.assertEqual(checks.overlap("a b c d", "a b c d"), 1.0)
        self.assertEqual(checks.overlap("a b", "c d"), 0.0)
        self.assertEqual(checks.overlap("a b c", "b c d"), 0.5)
        self.assertEqual(checks.overlap("", "a"), 0.0)
        self.assertAlmostEqual(checks.overlap("What is the best WAY?", "what is the best way"), 1.0)

    def test_threshold_is_inclusive(self):
        # 7 shared words of 10
        first = "one two three four five six seven eight"
        second = "one two three four five six seven nine ten"
        self.assertLess(checks.overlap(first, second), 0.8)
        cards = [{"kind": "qa", "front": first}, {"kind": "qa", "front": second}]
        self.assertEqual(checks.overlap(first, second), 0.7)
        self.assertEqual(checks.check_cards(cards), [checks.DUPLICATE_WARNING, checks.DUPLICATE_WARNING])
        just_under = [{"kind": "qa", "front": first}, {"kind": "qa", "front": "one two three four five six nine ten eleven"}]
        self.assertEqual(checks.check_cards(just_under), [None, None])

    def test_both_cards_are_marked_and_others_are_not(self):
        cards = [
            {"kind": "qa", "front": "How big should the first step of a new routine be?"},
            {"kind": "qa", "front": "What should a plan assume will go wrong?"},
            {"kind": "qa", "front": "How big should the first step of a new routine really be?"},
        ]
        self.assertEqual(checks.check_cards(cards), [checks.DUPLICATE_WARNING, None, checks.DUPLICATE_WARNING])

    def test_pointing_word_comes_first(self):
        cards = [{"kind": "qa", "front": "Where did this go wrong again?"}, {"kind": "qa", "front": "Where did this go wrong again?"}]
        self.assertEqual(checks.check_cards(cards), [checks.standalone_warning(cards[0])] * 2)

    def test_removed_cards_are_left_out(self):
        cards = [{"kind": "qa", "front": "Same front here", "state": "draft"}, {"kind": "qa", "front": "Same front here", "state": "removed"}]
        self.assertEqual(checks.check_cards(cards), [None, None])

    def test_cloze_cards_compare_hidden_words(self):
        same_text_different_words = [
            {"kind": "cloze", "front": "Be content with {{c1::quite a little}}. Allow for accidents."},
            {"kind": "cloze", "front": "Be content with quite a little. Allow for {{c1::accidents}}."},
        ]
        self.assertEqual(checks.check_cards(same_text_different_words), [None, None])
        same_words = [
            {"kind": "cloze", "front": "Be content with {{c1::quite a little}}. Allow for accidents."},
            {"kind": "cloze", "front": "Be content with {{c1::quite a little}}. Allow for accidents, too."},
        ]
        self.assertEqual(checks.check_cards(same_words), [checks.DUPLICATE_WARNING] * 2)

    def test_cloze_is_not_compared_with_questions(self):
        cards = [{"kind": "cloze", "front": "Allow for {{c1::accidents}}"}, {"kind": "qa", "front": "accidents"}]
        self.assertEqual(checks.check_cards(cards), [None, None])

    def test_blank_fronts_are_not_duplicates(self):
        self.assertEqual(checks.check_cards([{"kind": "qa", "front": ""}, {"kind": "qa", "front": ""}]), [None, None])


class AcronymAndHintTest(unittest.TestCase):
    def test_the_acronym_it_isnt_a_pointing_word(self):
        self.assertIsNone(checks.pointing_word("What does the IT department do?"))
        self.assertEqual(checks.pointing_word("What does the IT department do with it?"), "it")
        self.assertEqual(checks.pointing_word("It matters why?"), "it")

    def test_cloze_cards_hiding_the_same_word_with_different_hints_are_flagged(self):
        warnings = checks.check_cards([
            {"kind": "cloze", "front": "A {{c1::wife::first hint}} here"},
            {"kind": "cloze", "front": "A {{c1::wife::second clue}} there"},
        ])
        self.assertEqual(warnings, [checks.DUPLICATE_WARNING, checks.DUPLICATE_WARNING])


if __name__ == "__main__":
    unittest.main()
