import json
import shutil
import subprocess
import sys
import tempfile
import time
import os
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["PBANKI_SYNC_DELAY"] = "0"

from fixtures.make_reader import KOREADER, PBANKI, PLUGIN, build_reader, lua_file, make_epub, move_sidecar
from pbanki_desktop import epub, lua, paths, reader

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "real_sidecar"
KOREADER_SOURCE = REPO / "target/koreader-emulator/koreader"

# util.partialMD5 from KOReader, using KOReader's own md5
PARTIAL_MD5_LUA = r"""
package.path = arg[1] .. "/?.lua;" .. package.path
local lshift = require("bit").lshift
local md5 = require("ffi/sha2").md5
for i = 2, #arg do
    local file = io.open(arg[i], "rb")
    local step, size = 1024, 1024
    local update = md5()
    for j = -1, 10 do
        file:seek("set", lshift(step, 2*j))
        local sample = file:read(size)
        if sample then update(sample) else break end
    end
    file:close()
    print(update())
end
"""


class FakeReaderCase(unittest.TestCase):
    build_options = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = build_reader(Path(self.tmp.name) / "reader", **self.build_options)
        self.mount = self.fake.mount
        self.books = self.fake.books
        reader._sidecar_cache.clear()

    def row(self, name):
        rows = reader.list_books(self.mount)
        return next(r for r in rows if r["device_path"] == self.books[name])


class ReaderStatusTests(FakeReaderCase):
    def test_everything_present(self):
        status = reader.reader_status(self.mount)
        self.assertTrue(status["ready"])
        self.assertEqual(status["history_count"], 9)
        self.assertTrue(status["plugin_ok"])
        self.assertEqual(
            [(r["title"], r["path"], r["ok"], r["level"]) for r in status["rows"]],
            [
                ("reader is connected", str(self.mount), True, "ok"),
                ("KOReader found", paths.KOREADER_DIR, True, "ok"),
                ("pbAnki found", paths.PBANKI_DIR, True, "ok"),
                ("KOReader history read", "9 books", True, "ok"),
                ("pbAnki plugin can read desktop saves", paths.PLUGIN_DIR + "/main.lua", True, "ok"),
            ],
        )
        self.assertNotIn("action", status["rows"][4])

    def test_not_connected(self):
        status = reader.reader_status(self.mount / "not here")
        self.assertFalse(status["connected"])
        self.assertFalse(status["ready"])
        self.assertEqual(status["rows"][0]["title"], "not here isn't connected")
        self.assertEqual(status["rows"][0]["level"], "error")

    def test_a_folder_under_volumes_must_be_a_real_mount(self):
        with mock.patch("os.path.ismount", return_value=False) as ismount:
            self.assertFalse(reader.is_connected("/Volumes/FakeForTest"))
        ismount.assert_called_once_with("/Volumes/FakeForTest")
        with mock.patch("os.path.ismount", return_value=True):
            self.assertTrue(reader.is_connected("/Volumes/FakeForTest"))

    def test_broken_history(self):
        (self.mount / KOREADER / "history.lua").write_text("return {", encoding="utf-8")
        status = reader.reader_status(self.mount)
        self.assertFalse(status["history_ok"])
        self.assertFalse(status["ready"])
        self.assertEqual(status["rows"][3]["title"], "KOReader history couldn't be read")
        self.assertEqual(status["rows"][3]["level"], "error")
        self.assertEqual(reader.list_books(self.mount), [])

    def test_one_book_is_singular(self):
        (self.mount / KOREADER / "history.lua").write_text(
            lua_file("./history.lua", [{"time": 1, "file": self.books["pride"]}]), encoding="utf-8"
        )
        self.assertEqual(reader.reader_status(self.mount)["rows"][3]["path"], "1 book")


class ReaderPartsMissingTests(unittest.TestCase):
    def status(self, **options):
        with tempfile.TemporaryDirectory() as folder:
            fake = build_reader(Path(folder) / "PB629", **options)
            return reader.reader_status(fake.mount)

    def test_old_plugin_is_a_warning_with_the_update_button(self):
        status = self.status(plugin="old")
        row = status["rows"][4]
        self.assertTrue(status["ready"])
        self.assertFalse(status["plugin_ok"])
        self.assertEqual((row["ok"], row["level"], row["action"]), (False, "warning", "update_plugin"))
        self.assertIn("keep coming up as new", row["detail"])

    def test_no_plugin_is_the_same_warning(self):
        status = self.status(plugin=None)
        self.assertEqual(status["rows"][4]["action"], "update_plugin")
        self.assertTrue(status["ready"])

    def test_no_koreader(self):
        status = self.status(koreader=False)
        self.assertFalse(status["ready"])
        self.assertEqual(status["rows"][1]["title"], "KOReader not found. Install it on the reader, then check again.")

    def test_no_pbanki(self):
        status = self.status(pbanki=False)
        self.assertFalse(status["ready"])
        self.assertEqual(status["rows"][2]["title"], "pbAnki not found. Install it on the reader, then check again.")

    def test_no_history_yet_is_no_books_not_a_failure(self):
        status = self.status(history=False)
        self.assertTrue(status["history_ok"])
        self.assertEqual(status["rows"][3]["path"], "0 books")

    def test_the_connected_row_names_the_disk(self):
        self.assertEqual(self.status()["rows"][0]["title"], "PB629 is connected")


class ListBooksTests(FakeReaderCase):
    def test_history_order_and_times(self):
        rows = reader.list_books(self.mount)
        self.assertEqual(
            [r["device_path"] for r in rows],
            [self.books[n] for n in ("pride", "mostly_done", "pending", "scan", "walden", "no_sidecar", "broken", "gone", "card")],
        )
        self.assertEqual(rows[0]["last_read"], 1_793_700_000)
        self.assertEqual(rows[1]["last_read"], 1_793_699_000)

    def test_real_book(self):
        row = self.row("pride")
        self.assertEqual((row["title"], row["author"]), ("Pride and Prejudice", "Jane Austen"))
        self.assertEqual(row["counts"], {"total": 7, "pending": 7, "reviewed": 0})
        self.assertAlmostEqual(row["percent"], 22.22222222222222)
        self.assertEqual((row["stem"], row["ext"]), ("book", "epub"))
        self.assertEqual(row["sidecar_location"], "doc")
        self.assertIsNone(row["error"])
        self.assertFalse(row["has_companion"])

    def test_mostly_reviewed(self):
        row = self.row("mostly_done")
        self.assertEqual(row["counts"], {"total": 4, "pending": 1, "reviewed": 3})
        self.assertEqual(row["author"], "Arnold Bennett, A. N. Other")
        self.assertEqual(row["percent"], 100)
        self.assertEqual(row["pbanki_prompt"], "Self Help")
        self.assertTrue(row["has_companion"])

    def test_desktop_saves_count_as_reviewed_and_refused_lines_dont(self):
        row = self.row("pending")
        # 5 highlights, 2 in reviewed.jsonl, and a third only in a v2 line the plugin refuses
        self.assertEqual(row["counts"], {"total": 5, "pending": 3, "reviewed": 2})

    def test_a_page_bookmark_is_not_counted(self):
        self.assertEqual(self.row("pending")["counts"]["total"], 5)

    def test_stats_are_the_fallback_for_title_and_authors(self):
        row = self.row("pending")
        self.assertEqual((row["title"], row["author"]), ("Gardens, A Primer", "Ann Example"))

    def test_pdf(self):
        row = self.row("scan")
        self.assertEqual(row["ext"], "pdf")
        self.assertEqual(row["counts"], {"total": 2, "pending": 1, "reviewed": 1})
        self.assertEqual(row["author"], "")

    def test_no_highlights(self):
        row = self.row("walden")
        self.assertEqual(row["counts"], {"total": 0, "pending": 0, "reviewed": 0})
        self.assertAlmostEqual(row["percent"], 12)
        self.assertIsNone(row["error"])

    def test_no_sidecar_uses_the_file_name(self):
        row = self.row("no_sidecar")
        self.assertEqual(row["title"], "No Sidecar")
        self.assertIsNone(row["percent"])
        self.assertIsNone(row["sidecar_path"])
        self.assertIsNone(row["error"])

    def test_malformed_sidecar_is_a_row_with_an_error(self):
        row = self.row("broken")
        self.assertEqual(row["error"], reader.SIDECAR_UNREADABLE)
        self.assertEqual(row["title"], "Broken")
        self.assertEqual(row["counts"]["total"], 0)

    def test_deleted_book_is_flagged(self):
        row = self.row("gone")
        self.assertTrue(row["missing"])
        self.assertEqual(row["title"], "Deleted Book")
        self.assertEqual(row["counts"]["pending"], 1)

    def test_memory_card_book_is_listed_as_not_reachable(self):
        row = self.row("card")
        self.assertTrue(row["on_memory_card"])
        self.assertEqual(row["reachable_note"], "On the memory card")
        self.assertEqual(row["title"], "On Card")
        self.assertIsNone(row["local_path"])

    def test_rows_are_json_safe(self):
        json.dumps(reader.list_books(self.mount), ensure_ascii=False)

    def test_an_empty_companion_counts_as_absent(self):
        companion = self.fake.local("mostly_done").with_name("Time - Arnold Bennett.companion.md")
        companion.write_text("", encoding="utf-8")
        self.assertFalse(self.row("mostly_done")["has_companion"])

    def test_a_history_with_odd_entries(self):
        (self.mount / KOREADER / "history.lua").write_text(
            'return { [1] = { ["time"] = "soon", ["file"] = "/mnt/ext1/English/book.epub" }, '
            '[2] = "nonsense", [3] = { ["time"] = 5 }, '
            '[4] = { ["time"] = 6, ["file"] = "/mnt/ext1/English/book.epub" }, '
            '[5] = { ["time"] = 7, ["file"] = "/mnt/ext1/../../etc/passwd.epub" } }',
            encoding="utf-8",
        )
        rows = reader.list_books(self.mount)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["last_read"])
        self.assertIsNone(rows[1]["local_path"])

    def test_listing_reads_only(self):
        before = self.fake.snapshot()
        reader.list_books(self.mount)
        reader.reader_status(self.mount)
        for name in self.books:
            reader.load_book(self.mount, self.books[name])
        self.assertEqual(self.fake.snapshot(), before)


class LoadRealBookTests(FakeReaderCase):
    def setUp(self):
        super().setUp()
        self.book = reader.load_book(self.mount, self.books["pride"])
        self.sidecar = lua.load_file(FIXTURES / "metadata.epub.lua")

    def test_details(self):
        book = self.book
        self.assertEqual(book["title"], "Pride and Prejudice")
        self.assertEqual(book["author"], "Jane Austen")
        self.assertEqual((book["stem"], book["ext"], book["is_epub"]), ("book", "epub", True))
        self.assertEqual(book["doc_pages"], 675)
        self.assertAlmostEqual(book["percent_finished"], 0.22222222222222)
        self.assertEqual(book["last_read"], 1_793_700_000)
        self.assertEqual(book["card_history"], [])
        self.assertIsNone(book["pbanki_prompt"])
        self.assertIsNone(book["error"])
        json.dumps(book, ensure_ascii=False)

    def test_seven_highlights_and_no_bookmark(self):
        self.assertEqual(len(self.book["highlights"]), 7)
        self.assertEqual(self.book["counts"], {"total": 7, "pending": 7, "reviewed": 0})
        self.assertNotIn("Piling up", " ".join(h["text"] for h in self.book["highlights"]))

    def test_keys_are_exactly_the_plugins(self):
        real = [a for a in self.sidecar["annotations"] if a.get("drawer")]
        self.assertEqual(
            [h["key"] for h in self.book["highlights"]],
            [f"{a['datetime']}|{a['pos0']}" for a in real],
        )
        self.assertEqual(self.book["highlights"][0]["key"], "2026-11-03 15:50:37|/body/DocFragment[2]/body/p[19]/span/text().0")

    @unittest.skipUnless(shutil.which("lua"), "lua not installed")
    def test_keys_equal_the_plugins_function_under_lua(self):
        source = (REPO / "koreader/pbanki.koplugin/main.lua").read_text(encoding="utf-8")
        start = source.index("local function highlightKey(item)")
        end = source.index("\nend\n", start) + len("\nend\n")
        program = source[start:end] + (
            'local data = dofile(arg[1])\n'
            'for _, item in ipairs(data.annotations) do\n'
            '    if item.drawer and item.text and item.text ~= "" then print(highlightKey(item)) end\n'
            'end\n'
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "keys.lua"
            path.write_text(program, encoding="utf-8")
            out = subprocess.run(["lua", str(path), str(FIXTURES / "metadata.epub.lua")], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.split("\n")[:-1], [h["key"] for h in self.book["highlights"]])

    def test_every_highlight_round_trips_through_load_book(self):
        for highlight in self.book["highlights"]:
            with self.subTest(highlight["key"]):
                self.assertTrue(highlight["location"]["found"])
                self.assertEqual(epub.words(highlight["passage"]["mark"]), epub.words(highlight["text"]))

    def test_note_page_position_percent(self):
        third = self.book["highlights"][2]
        self.assertEqual(third["note"], "Money first, then the man.")
        self.assertEqual(third["page"], 36)
        self.assertEqual(third["position"], "/body/DocFragment[3]/body/p[6]/text().0")
        self.assertAlmostEqual(third["percent"], 100 * 36 / 675)
        self.assertIsNone(self.book["highlights"][0]["note"])

    def test_chapters(self):
        third = self.book["highlights"][2]
        self.assertEqual(third["chapter"], "Chapter I.")
        self.assertEqual(third["chapter_number"], 2)
        self.assertEqual(third["chapter_name"], "Chapter I.")
        fourth = self.book["highlights"][3]
        self.assertEqual(fourth["chapter_number"], 3)
        self.assertEqual(fourth["chapter_name"], "3 · I hope Mr. Bingley will like it. CHAPTER II.")
        self.assertEqual(fourth["chapter_card"], "Chapter 3: I hope Mr. Bingley will like it. CHAPTER II.")
        chapters = self.book["chapters"]
        self.assertEqual(len(chapters), 63)
        self.assertEqual((chapters[1]["count"], chapters[1]["pending"]), (3, 3))
        self.assertEqual(sum(c["count"] for c in chapters), 7)


class LoadBookStatesTests(FakeReaderCase):
    def test_mostly_reviewed(self):
        book = reader.load_book(self.mount, self.books["mostly_done"])
        self.assertEqual([h["status"] for h in book["highlights"]], ["reviewed", "reviewed", "reviewed", "pending"])
        self.assertEqual(book["pbanki_prompt"], "Self Help")
        self.assertEqual(len(book["card_history"]), 2)
        self.assertEqual(book["card_history"][0]["suggested"]["front"], "When do hours arrive?")
        self.assertEqual(book["highlights"][3]["note"], "Exactly my week.")
        self.assertTrue(book["has_companion"])
        self.assertTrue(book["companion_path"].endswith("Time - Arnold Bennett.companion.md"))
        self.assertAlmostEqual(book["highlights"][3]["percent"], 75)
        for highlight in book["highlights"]:
            self.assertEqual(highlight["passage"]["mark"], highlight["text"])
        self.assertEqual(
            [c["name"] for c in book["chapters"]],
            ["1 · The daily miracle", "2 · Precautions before beginning", "3 · The cause of the troubles"],
        )

    def test_desktop_saves_make_highlights_reviewed(self):
        book = reader.load_book(self.mount, self.books["pending"])
        self.assertEqual([h["status"] for h in book["highlights"]], ["reviewed", "reviewed", "pending", "pending", "pending"])
        self.assertEqual(book["counts"], {"total": 5, "pending": 3, "reviewed": 2})

    def test_a_highlight_the_book_doesnt_have_is_not_matched_and_keeps_its_text(self):
        book = reader.load_book(self.mount, self.books["pending"])
        lost = book["highlights"][4]
        self.assertEqual(lost["location"], {"found": False})
        self.assertFalse(lost["passage"]["found"])
        self.assertEqual(lost["passage"]["mark"], lost["text"])
        self.assertEqual(lost["chapter"], "Chapter 1: Soil")
        self.assertEqual(lost["chapter_number"], 1)

    def test_a_highlight_without_a_chapter_gets_it_from_its_location(self):
        book = reader.load_book(self.mount, self.books["pending"])
        fourth = book["highlights"][3]
        self.assertEqual(fourth["chapter"], "Seeds")
        self.assertEqual(fourth["chapter_number"], 2)
        self.assertEqual(fourth["chapter_name"], "2 · Seeds")
        self.assertEqual(book["highlights"][0]["chapter_name"], "Chapter 1: Soil")

    def test_pdf_highlights(self):
        book = reader.load_book(self.mount, self.books["scan"])
        self.assertFalse(book["is_epub"])
        self.assertIsNone(book["error"])
        first, second = book["highlights"]
        self.assertEqual(first["key"], "2026-09-20 08:00:00|12")
        self.assertEqual((first["page"], first["position"]), (12, "12"))
        self.assertAlmostEqual(first["percent"], 15)
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["location"], {"found": False})
        self.assertEqual(first["passage"]["mark"], "A line on page twelve.")
        self.assertEqual(first["chapter_name"], "Part One")
        self.assertIsNone(first["chapter_number"])
        self.assertEqual(second["status"], "reviewed")
        self.assertEqual(second["position"], "40")
        self.assertEqual([c["title"] for c in book["chapters"]], ["Part One"])
        self.assertIsNone(book["chapters"][0]["number"])

    def test_no_highlights(self):
        book = reader.load_book(self.mount, self.books["walden"])
        self.assertEqual(book["highlights"], [])
        self.assertEqual(book["title"], "Walden")
        self.assertEqual(len(book["chapters"]), 1)

    def test_no_sidecar_takes_the_title_from_the_epub(self):
        book = reader.load_book(self.mount, self.books["no_sidecar"])
        self.assertEqual((book["title"], book["author"]), ("Never Saved", "Nobody"))
        self.assertEqual(book["highlights"], [])
        self.assertIsNone(book["error"])

    def test_malformed_sidecar_returns_an_error_and_never_raises(self):
        book = reader.load_book(self.mount, self.books["broken"])
        self.assertEqual(book["error"], reader.SIDECAR_UNREADABLE)
        self.assertEqual(book["highlights"], [])
        self.assertEqual(book["title"], "Broken Book")

    def test_deleted_book_keeps_its_highlights_without_a_passage(self):
        book = reader.load_book(self.mount, self.books["gone"])
        self.assertTrue(book["missing"])
        self.assertEqual(len(book["highlights"]), 1)
        self.assertEqual(book["highlights"][0]["location"], {"found": False})
        self.assertIsNone(book["error"])

    def test_memory_card_book(self):
        book = reader.load_book(self.mount, self.books["card"])
        self.assertTrue(book["on_memory_card"])
        self.assertEqual(book["highlights"], [])

    def test_unplugged(self):
        book = reader.load_book(self.mount / "gone", self.books["pride"])
        self.assertEqual(book["error"], reader.NOT_CONNECTED)

    def test_a_corrupt_epub_is_an_error_but_the_highlights_stay(self):
        self.fake.local("mostly_done").write_bytes(b"not a zip at all")
        book = reader.load_book(self.mount, self.books["mostly_done"])
        self.assertIn("isn't a readable ePub", book["error"])
        self.assertEqual(len(book["highlights"]), 4)
        self.assertFalse(book["highlights"][0]["location"]["found"])
        self.assertEqual(book["highlights"][0]["chapter"], "The daily miracle")

    def test_a_too_big_epub_entry_is_a_book_error(self):
        with mock.patch.object(epub, "MAX_ENTRY_BYTES", 50):
            epub._cache.clear()
            book = reader.load_book(self.mount, self.books["mostly_done"])
        epub._cache.clear()
        self.assertIn("too big", book["error"])
        self.assertEqual(len(book["highlights"]), 4)

    def test_changing_what_load_book_returns_doesnt_touch_the_next_read(self):
        book = reader.load_book(self.mount, self.books["scan"])
        book["highlights"][0]["pos0"]["page"] = 999
        self.assertEqual(reader.load_book(self.mount, self.books["scan"])["highlights"][0]["pos0"]["page"], 12)
        book = reader.load_book(self.mount, self.books["mostly_done"])
        book["card_history"][0]["front"] = "changed"
        self.assertNotEqual(reader.load_book(self.mount, self.books["mostly_done"])["card_history"][0]["front"], "changed")

    def test_a_failure_on_one_highlight_doesnt_lose_the_others(self):
        real_locate = epub.Epub.locate
        def locate(book, highlight):
            if "Seeds keep" in highlight["text"]:
                raise RuntimeError("surprise")
            return real_locate(book, highlight)
        with mock.patch.object(epub.Epub, "locate", locate):
            book = reader.load_book(self.mount, self.books["pending"])
        self.assertTrue(book["highlights"][0]["location"]["found"])
        self.assertFalse(book["highlights"][2]["location"]["found"])
        self.assertEqual(book["highlights"][2]["passage"]["mark"], book["highlights"][2]["text"])

    def test_an_annotation_of_the_wrong_shape_is_skipped(self):
        path = self.fake.local("walden").with_suffix(".sdr") / "metadata.epub.lua"
        path.write_text(
            lua_file(
                "x",
                {
                    "annotations": [
                        "nonsense",
                        {"drawer": "lighten"},
                        {"drawer": "lighten", "text": ""},
                        {"text": "no drawer"},
                        {"drawer": "lighten", "text": "Only this one.", "datetime": "d"},
                    ],
                    "doc_pages": "many",
                    "percent_finished": "half",
                    "doc_props": "nope",
                    "pbanki_reviewed": "nope",
                    "pbanki_card_history": "nope",
                    "stats": 5,
                },
            ),
            encoding="utf-8",
        )
        book = reader.load_book(self.mount, self.books["walden"])
        self.assertEqual([h["text"] for h in book["highlights"]], ["Only this one."])
        self.assertEqual(book["highlights"][0]["key"], "d|nil")
        self.assertIsNone(book["highlights"][0]["percent"])
        self.assertEqual(book["card_history"], [])
        self.assertIsNone(book["percent_finished"])

    def test_nothing_unexpected_ever_escapes(self):
        with mock.patch.object(reader, "_fill_record", side_effect=RuntimeError("surprise")):
            self.assertEqual(reader.load_book(self.mount, self.books["pride"])["error"], reader.SIDECAR_UNREADABLE)
            rows = reader.list_books(self.mount)
        self.assertEqual(rows[0]["error"], reader.SIDECAR_UNREADABLE)

    def test_a_book_from_an_older_koreader_says_so(self):
        path = self.fake.local("walden").with_suffix(".sdr") / "metadata.epub.lua"
        path.write_text(lua_file("x", {"bookmarks": [{"page": "x", "notes": "old"}]}), encoding="utf-8")
        self.assertEqual(reader.load_book(self.mount, self.books["walden"])["error"], reader.OLD_FORMAT)


class HighlightKeyTests(unittest.TestCase):
    def test_reflowable(self):
        self.assertEqual(reader.highlight_key({"datetime": "d", "pos0": "/body/x"}), "d|/body/x")

    def test_fixed_layout_float_page_prints_like_lua(self):
        self.assertEqual(reader.highlight_key({"datetime": "d", "pos0": {"page": 12.0}}), "d|12")
        self.assertEqual(reader.highlight_key({"datetime": "d", "pos0": {"x": 1}}), "d|nil")

    def test_a_table_with_only_numbered_entries_has_no_page(self):
        self.assertEqual(reader.highlight_key({"datetime": "d", "pos0": [1, 2]}), "d|nil")
        self.assertEqual(reader.highlight_key({"datetime": "d", "pos0": lua.loads("return {[1]=1}")}), "d|nil")
        self.assertEqual(reader.highlight_key({"datetime": "d", "pos0": {}}), "d|nil")

    def test_a_number_datetime(self):
        self.assertEqual(reader.highlight_key({"datetime": 1700000000, "pos0": "p"}), "1700000000|p")


class DesktopSaveLineTests(FakeReaderCase):
    def keys(self, *lines):
        (self.mount / paths.REVIEWED_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return reader.reviewed_keys(self.mount)

    def test_rules_match_the_plugins_parser(self):
        good = {"v": 1, "file": "f", "key": "k", "cards": [{"front": "a", "back": "b", "suggested": "ignored", "quote": 5}]}
        self.assertEqual(self.keys(json.dumps(good)), {"f": {"k"}})
        self.assertEqual(self.keys(json.dumps({"v": 1, "file": "f", "key": "k2"})), {"f": {"k2"}})
        self.assertEqual(self.keys(json.dumps({"v": 1, "file": "f", "key": "k3", "cards": [{"front": "a"}]})), {"f": {"k3"}})

    def test_malformed_lines_dont_count(self):
        for bad in (
            {"v": 2, "file": "f", "key": "k"},
            {"v": True, "file": "f", "key": "k"},
            {"file": "f", "key": "k"},
            {"v": 1, "key": "k"},
            {"v": 1, "file": "f"},
            {"v": 1, "file": "f", "key": ""},
            {"v": 1, "file": "f", "key": "k", "cards": "x"},
            {"v": 1, "file": "f", "key": "k", "cards": [{"back": "b"}]},
            {"v": 1, "file": "f", "key": "k", "cards": [{"front": "a", "back": 1}]},
            ["v"],
        ):
            self.assertEqual(self.keys(json.dumps(bad)), {}, bad)

    def test_a_missing_file_is_no_keys(self):
        (self.mount / paths.REVIEWED_FILE).unlink()
        self.assertEqual(reader.reviewed_keys(self.mount), {})

    @unittest.skipUnless(shutil.which("lua"), "lua not installed")
    def test_same_answer_as_the_real_parser_under_lua(self):
        cases = [
            {"v": 1, "file": "f", "key": "ok", "cards": []},
            {"v": 1, "file": "f", "key": "ok2"},
            {"v": 2, "file": "f", "key": "no"},
            {"v": 1, "file": "f", "key": "ok3", "cards": [{"front": "a"}]},
            {"v": 1, "file": "f", "key": "no2", "cards": [{"back": "b"}]},
            {"v": 1, "file": "f", "key": "no3", "cards": [{"front": "a", "back": 1}]},
            {"v": 1, "file": "f", "key": "ok4", "cards": [{"front": "a", "back": "b", "quote": 5, "suggested": 1}]},
            {"v": 1, "file": "f", "key": "no4", "cards": "x"},
            {"v": 1, "file": "f", "key": ""},
            {"v": 1, "file": "other", "key": "elsewhere"},
        ]
        lines = [json.dumps(c) for c in cases] + ["not json"]
        ours = self.keys(*lines)
        # A tiny decoder over a table written by Python keeps this free of a Lua JSON library
        def to_lua(value):
            if isinstance(value, dict):
                return "{" + ",".join(f"[{json.dumps(k)}]={to_lua(v)}" for k, v in value.items()) + "}"
            if isinstance(value, list):
                return "{" + ",".join(to_lua(v) for v in value) + "}"
            return json.dumps(value)
        table = "{" + ",".join(f"[{json.dumps(line)}]={to_lua(c)}" for line, c in zip(lines, cases)) + "}"
        program = (
            f'package.path = "{REPO}/koreader/pbanki.koplugin/?.lua;" .. package.path\n'
            'local cardgen = require("cardgen")\n'
            f"local decoded = {table}\n"
            "local function decode(line) local d = decoded[line]; if d == nil then error('bad') end; return d end\n"
            "local text = io.open(arg[1], 'rb'):read('*a')\n"
            'for _, file in ipairs({"f", "other"}) do\n'
            "    local saves = cardgen.parseDesktopSaves(text, file, decode)\n"
            "    for _, s in ipairs(saves) do print(s.file .. '|' .. s.key) end\n"
            "end\n"
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "saves.lua"
            path.write_text(program, encoding="utf-8")
            out = subprocess.run(["lua", str(path), str(self.mount / paths.REVIEWED_FILE)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        theirs = {}
        for line in out.stdout.split("\n")[:-1]:
            file, key = line.split("|", 1)
            theirs.setdefault(file, set()).add(key)
        self.assertEqual(ours, theirs)


class SidecarLocationTests(FakeReaderCase):
    def load(self):
        reader._sidecar_cache.clear()
        return reader.load_book(self.mount, self.books["pride"])

    def set_folder(self, value):
        (self.mount / KOREADER / "settings.reader.lua").write_text(
            lua_file("x", {"document_metadata_folder": value}), encoding="utf-8"
        )

    def test_default_setting(self):
        self.assertEqual(reader.document_metadata_folder(self.mount), "doc")
        (self.mount / KOREADER / "settings.reader.lua").unlink()
        self.assertEqual(reader.document_metadata_folder(self.mount), "doc")
        (self.mount / KOREADER / "settings.reader.lua").write_text("broken {", encoding="utf-8")
        self.assertEqual(reader.document_metadata_folder(self.mount), "doc")

    def test_doc(self):
        book = self.load()
        self.assertEqual(book["sidecar_location"], "doc")
        self.assertEqual(len(book["highlights"]), 7)
        self.assertTrue(book["sidecar_path"].endswith("English/book.sdr/metadata.epub.lua"))

    def test_dir(self):
        self.set_folder("dir")
        move_sidecar(self.fake, "pride", "dir")
        self.assertEqual(reader.document_metadata_folder(self.mount), "dir")
        book = self.load()
        self.assertEqual(book["sidecar_location"], "dir")
        self.assertTrue(
            book["sidecar_path"].endswith("applications/koreader/docsettings/mnt/ext1/English/book.sdr/metadata.epub.lua")
        )
        self.assertEqual(len(book["highlights"]), 7)
        self.assertEqual(self.row("pride")["counts"]["total"], 7)

    def test_hash(self):
        self.set_folder("hash")
        digest = reader.partial_md5(self.fake.local("pride"))
        move_sidecar(self.fake, "pride", "hash", digest)
        self.assertEqual(reader.document_metadata_folder(self.mount), "hash")
        book = self.load()
        self.assertEqual(book["sidecar_location"], "hash")
        self.assertTrue(book["sidecar_path"].endswith(f"hashdocsettings/7b/{digest}.sdr/metadata.epub.lua"))
        self.assertEqual(len(book["highlights"]), 7)

    def test_hash_with_the_book_gone_says_so_in_plain_words(self):
        self.set_folder("hash")
        digest = reader.partial_md5(self.fake.local("pride"))
        move_sidecar(self.fake, "pride", "hash", digest)
        self.fake.local("pride").unlink()
        reader._sidecar_cache.clear()
        book = reader.load_book(self.mount, self.books["pride"])
        self.assertEqual(book["error"], reader.NO_SETTINGS_HASH)
        self.assertEqual(self.row("pride")["error"], reader.NO_SETTINGS_HASH)

    def test_the_most_recently_saved_location_wins_as_in_koreader(self):
        # KOReader's DocSettings:open reads the newest file wherever it is, whatever the setting
        target = self.mount / KOREADER / "docsettings/mnt/ext1/English/book.sdr"
        target.mkdir(parents=True)
        (target / "metadata.epub.lua").write_text(
            lua_file("x", {"doc_props": {"title": "From dir"}}), encoding="utf-8"
        )
        doc = self.mount / "English/book.sdr/metadata.epub.lua"
        now = time.time()
        os.utime(doc, (now - 100, now - 100))
        os.utime(target / "metadata.epub.lua", (now, now))
        self.assertEqual(self.load()["title"], "From dir")
        os.utime(doc, (now + 100, now + 100))
        self.assertEqual(self.load()["title"], "Pride and Prejudice")

    def test_same_time_goes_to_the_higher_priority_location(self):
        target = self.mount / KOREADER / "docsettings/mnt/ext1/English/book.sdr"
        target.mkdir(parents=True)
        (target / "metadata.epub.lua").write_text(
            lua_file("x", {"doc_props": {"title": "From dir"}}), encoding="utf-8"
        )
        doc = self.mount / "English/book.sdr/metadata.epub.lua"
        now = time.time()
        os.utime(doc, (now, now))
        os.utime(target / "metadata.epub.lua", (now, now))
        self.assertEqual(self.load()["title"], "Pride and Prejudice")

    def test_a_bad_file_falls_back_to_its_backup(self):
        doc = self.mount / "English/book.sdr/metadata.epub.lua"
        shutil.copy(doc, f"{doc}.old")
        doc.write_text("return { cut", encoding="utf-8")
        book = self.load()
        self.assertEqual(len(book["highlights"]), 7)
        self.assertIsNone(book["error"])
        self.assertTrue(book["sidecar_path"].endswith(".old"))

    def test_a_backup_never_goes_ahead_of_its_file(self):
        doc = self.mount / "English/book.sdr/metadata.epub.lua"
        old = Path(f"{doc}.old")
        old.write_text(lua_file("x", {"doc_props": {"title": "From backup"}}), encoding="utf-8")
        now = time.time()
        os.utime(doc, (now - 100, now - 100))
        os.utime(old, (now, now))
        self.assertEqual(self.load()["title"], "Pride and Prejudice")

    def test_an_empty_file_and_an_empty_table_are_ignored(self):
        doc = self.mount / "English/book.sdr/metadata.epub.lua"
        original = doc.read_bytes()
        doc.write_bytes(b"")
        self.assertEqual(self.load()["sidecar_path"], None)
        doc.write_text("return {}", encoding="utf-8")
        book = self.load()
        self.assertIsNone(book["sidecar_path"])
        self.assertIsNone(book["error"])
        doc.write_bytes(original)

    def test_older_locations_are_read_too(self):
        doc_folder = self.mount / "English/book.sdr"
        data = (doc_folder / "metadata.epub.lua").read_bytes()
        (doc_folder / "metadata.epub.lua").unlink()
        (doc_folder / "book.epub.lua").write_bytes(data)
        self.assertEqual(self.load()["sidecar_path"], str(doc_folder / "book.epub.lua"))
        (doc_folder / "book.epub.lua").unlink()
        (self.mount / KOREADER / "history").mkdir()
        legacy = self.mount / KOREADER / "history" / "[#mnt#ext1#English#] book.epub.lua"
        legacy.write_bytes(data)
        self.assertEqual(self.load()["sidecar_location"], "hist")
        legacy.unlink()
        kpdfview = self.mount / "English/book.epub.kpdfview.lua"
        kpdfview.write_bytes(data)
        self.assertEqual(self.load()["sidecar_path"], str(kpdfview))

    def test_a_changed_sidecar_is_read_again_and_an_unchanged_one_is_cached(self):
        doc = self.mount / "English/book.sdr/metadata.epub.lua"
        reader.load_book(self.mount, self.books["pride"])
        with mock.patch.object(lua, "load_file", side_effect=AssertionError("read again")):
            data = reader.read_sidecar(self.mount, self.books["pride"])
        self.assertEqual(data[2], "doc")
        doc.write_text(lua_file("x", {"doc_props": {"title": "Edited"}}), encoding="utf-8")
        later = time.time() + 10
        os.utime(doc, (later, later))
        self.assertEqual(reader.load_book(self.mount, self.books["pride"])["title"], "Edited")

    def test_file_names_follow_koreader(self):
        self.assertEqual(reader._sidecar_filename("/a/b.EPUB"), "metadata.EPUB.lua")
        self.assertEqual(reader._sidecar_filename("/a/b.tar.gz"), "metadata.gz.lua")
        self.assertEqual(reader._sidecar_filename("/a/b"), "metadata._.lua")
        self.assertEqual(reader._without_suffix("/a/b.tar.gz"), "/a/b.tar")

    def test_a_path_with_dots_never_leaves_the_reader(self):
        self.assertEqual(reader._sidecar_candidates(self.mount, "/mnt/ext2/../../../etc/x.epub"), [])


class PartialMd5Tests(unittest.TestCase):
    def test_matches_the_checksum_koreader_saved_for_the_real_book(self):
        saved = lua.load_file(FIXTURES / "metadata.epub.lua")["partial_md5_checksum"]
        self.assertEqual(reader.partial_md5(FIXTURES / "book.epub"), saved)

    def test_a_missing_file_is_none(self):
        self.assertIsNone(reader.partial_md5("/nonexistent/book.epub"))

    @unittest.skipUnless(
        shutil.which("luajit") and (KOREADER_SOURCE / "ffi/sha2.lua").is_file(),
        "luajit or KOReader's source is missing",
    )
    def test_matches_koreaders_algorithm_run_for_real(self):
        with tempfile.TemporaryDirectory() as folder:
            files = []
            for size in (0, 1, 1023, 1024, 1025, 4096, 5000, 20000, 1_500_000, 5_000_000):
                path = Path(folder) / f"f{size}"
                path.write_bytes(bytes(i % 251 for i in range(min(size, 251))) * (size // 251 + 1))
                path.write_bytes(path.read_bytes()[:size])
                files.append(path)
            program = Path(folder) / "partial.lua"
            program.write_text(PARTIAL_MD5_LUA, encoding="utf-8")
            out = subprocess.run(
                ["luajit", str(program), str(KOREADER_SOURCE), *map(str, files)],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(out.stdout.split(), [reader.partial_md5(path) for path in files])


class BuilderTests(unittest.TestCase):
    def test_the_builder_writes_valid_lua(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = build_reader(Path(folder) / "r")
            value = lua.load_file(fake.local("mostly_done").with_suffix(".sdr") / "metadata.epub.lua")
            self.assertEqual(value["doc_props"]["authors"], "Arnold Bennett\nA. N. Other")

    def test_the_builder_writes_lua_that_lua_itself_reads(self):
        if not shutil.which("lua"):
            self.skipTest("lua not installed")
        with tempfile.TemporaryDirectory() as folder:
            fake = build_reader(Path(folder) / "r")
            for path in fake.mount.rglob("*.lua"):
                if "plugins" in path.parts or path.parent.name == "Broken.sdr":
                    continue
                subprocess.run(["lua", "-e", f"assert(type(dofile({json.dumps(str(path))})) == 'table')"], check=True)

    def test_companion_can_be_left_out(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = build_reader(Path(folder) / "r", companion=False)
            row = next(r for r in reader.list_books(fake.mount) if r["device_path"] == fake.books["mostly_done"])
            self.assertFalse(row["has_companion"])

    def test_the_snapshot_notices_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = build_reader(Path(folder) / "r")
            before = fake.snapshot()
            (fake.mount / PBANKI / "inbox.jsonl").write_text("x", encoding="utf-8")
            self.assertNotEqual(fake.snapshot(), before)

    def test_plugin_files_are_where_the_plan_says(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = build_reader(Path(folder) / "r", plugin="old")
            self.assertTrue((fake.mount / PLUGIN / "main.lua").is_file())
            self.assertEqual(PLUGIN, paths.PLUGIN_DIR)

    def test_make_epub_round_trips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "x.epub"
            make_epub(path, "T", "A", [("One", ["Para."])])
            self.assertEqual(epub.open_epub(path).title, "T")


class BadShapesTests(FakeReaderCase):
    def history(self, text):
        (Path(self.mount) / reader.HISTORY_FILE).write_text(text, encoding="utf-8")

    def test_a_table_as_a_key_in_the_history_is_reported_not_raised(self):
        for text in ("return {[{}]=1}", "return {[{1}]=2,}"):
            with self.subTest(text=text):
                self.history(text)
                self.assertEqual(reader.list_books(self.mount), [])
                status = reader.reader_status(self.mount)
                self.assertFalse(status["history_ok"])
                self.assertFalse(status["ready"])

    def sidecar_at(self, name):
        return reader.read_sidecar(self.mount, self.books[name])[1]

    def test_numbers_that_arent_finite_dont_reach_the_screens(self):
        self.history('return {[1]={["time"]=1e999,["file"]="' + self.books["mostly_done"] + '"},}')
        (Path(self.sidecar_at("mostly_done"))).write_text(
            "return {\n"
            '["percent_finished"]=1e999,["doc_pages"]=1e-300,\n'
            '["annotations"]={[1]={["datetime"]="d",["drawer"]="lighten",["text"]="T",["pos0"]="/x",["pageno"]=1e308,'
            '["pos1"]={["x"]=1e999}},},\n'
            '["pbanki_card_history"]={[1]={["front"]="F",["weight"]=1e999},},\n'
            "}\n",
            encoding="utf-8",
        )
        reader._sidecar_cache.clear()
        row = reader.list_books(self.mount)[0]
        self.assertIsNone(row["last_read"])
        self.assertIsNone(row["percent"])
        book = reader.load_book(self.mount, self.books["mostly_done"])
        self.assertIsNone(book["percent_finished"])
        self.assertIsNone(book["last_read"])
        self.assertIsNone(book["highlights"][0]["percent"])
        self.assertIsNone(book["highlights"][0]["pos1"]["x"])
        self.assertIsNone(book["card_history"][0]["weight"])
        json.dumps(row, allow_nan=False)
        json.dumps(book, allow_nan=False)


class NestedTableOfContentsTests(FakeReaderCase):
    def test_highlights_list_under_their_own_chapters(self):
        local = Path(self.mount) / "English" / "Nested.epub"
        package = '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata/><manifest>'
        package += '<item id="n" href="n.xhtml" media-type="application/x" properties="nav"/>'
        names = ["t", "h", "c1", "c2"]
        package += "".join(f'<item id="{n}" href="{n}.xhtml" media-type="application/xhtml+xml"/>' for n in names)
        package += "</manifest><spine>" + "".join(f'<itemref idref="{n}"/>' for n in names) + "</spine></package>"
        nav = (
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>'
            '<li><a href="t.xhtml">Titlepage</a></li><li><a href="h.xhtml">The Book</a><ol>'
            '<li><a href="c1.xhtml">I</a></li><li><a href="c2.xhtml">II: Home</a></li></ol></li></ol></nav></body></html>'
        )
        with zipfile.ZipFile(local, "w") as z:
            z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="c.opf"/></rootfiles></container>')
            z.writestr("c.opf", package)
            z.writestr("n.xhtml", nav)
            for name, text in zip(names, ("Title.", "Half title.", "Words of the first chapter.", "Words of the second chapter.")):
                z.writestr(f"{name}.xhtml", f"<html><body><p>{text}</p></body></html>")
        device_path = "/mnt/ext1/English/Nested.epub"
        sidecar = local.with_suffix(".sdr")
        sidecar.mkdir()
        (sidecar / "metadata.epub.lua").write_text(
            'return {["annotations"]={'
            '[1]={["datetime"]="a",["drawer"]="lighten",["text"]="first chapter",["chapter"]="I",["pos0"]="/body/DocFragment[3]/body/p/text().0"},'
            '[2]={["datetime"]="b",["drawer"]="lighten",["text"]="second chapter",["chapter"]="II: Home",["pos0"]="/body/DocFragment[4]/body/p/text().0"},'
            '[3]={["datetime"]="c",["drawer"]="lighten",["text"]="Half title",["chapter"]="The Book",["pos0"]="/body/DocFragment[2]/body/p/text().0"},'
            "}}\n",
            encoding="utf-8",
        )
        book = reader.load_book(self.mount, device_path)
        self.assertIsNone(book["error"])
        self.assertEqual([h["chapter_name"] for h in book["highlights"]], ["I", "II: Home", "The Book"])
        self.assertEqual([h["chapter_card"] for h in book["highlights"]], ["I", "II: Home", "The Book"])
        self.assertEqual([h["chapter_number"] for h in book["highlights"]], [2, 3, None])
        self.assertEqual(
            [(c["name"], c["count"]) for c in book["chapters"]],
            [("1 · Titlepage", 0), ("I", 1), ("II: Home", 1), ("The Book", 1)],
        )
        self.assertTrue(book["highlights"][0]["passage"]["endCh"])


if __name__ == "__main__":
    unittest.main()
