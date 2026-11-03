import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.make_reader import make_epub
from pbanki_desktop import epub, lua

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "real_sidecar"

CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="{opf}" media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def write_epub(path, files, opf_name="content.opf", container=True):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip")
        if container:
            z.writestr("META-INF/container.xml", CONTAINER.format(opf=opf_name))
        for name, data in files.items():
            z.writestr(name, data)


def opf(items, title="Odd Book", author="Odd Author", ncx=None, base=""):
    manifest = "".join(
        f'<item id="i{i}" href="{href}" media-type="{media}"{extra}/>'
        for i, (href, media, extra) in enumerate(items, 1)
    )
    spine = "".join(f'<itemref idref="i{i}"/>' for i, item in enumerate(items, 1) if item[1] == "application/xhtml+xml")
    toc = f' toc="i{ncx}"' if ncx else ""
    return (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator><dc:creator>Second Author</dc:creator>"
        f"</metadata><manifest>{manifest}</manifest><spine{toc}>{spine}</spine></package>"
    )


XHTML = "application/xhtml+xml"

# Bad and unusual markup a real ePub might hold, one spine document
ODD_DOC = """<!DOCTYPE html>
<HTML><HEAD><TITLE>Should not appear title</TITLE>
<STYLE>p { color: red }</STYLE><script>var hidden = "script text";</script></HEAD>
<BODY>
<H1 ID="start">An Odd&nbsp;Chapter</H1>
<P>First para with &ldquo;smart&rdquo; entities &amp; a <span class="dc">D</span>rop cap word.
<P>Unclosed para with a soft­hyphen in it<br>and a line break.
<svg><title>Vector title not shown</title><text>Vector text not shown</text></svg>
<div>Text straight in a div <!-- hidden note --> keeps going after a comment.</div>
<ul><li>Item one<li>Item two</ul>
<blockquote><p>A quoted paragraph here.</p></blockquote>
<xhtml:p>A namespaced paragraph.</xhtml:p>
<p/><p>After a self-closed paragraph, here is the target phrase once.</p>
<p>And in a later paragraph, here is the target phrase again.</p>
<table><tr><td>Cell one<td>Cell two</table>
<pre>Line one
Line two</pre>
</BODY></HTML>
"""


class RealFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sidecar = lua.load_file(FIXTURES / "metadata.epub.lua")
        cls.highlights = [a for a in cls.sidecar["annotations"] if a.get("drawer")]
        cls.book = epub.open_epub(FIXTURES / "book.epub")

    def test_book_details(self):
        self.assertEqual(self.book.title, "Pride and Prejudice")
        self.assertEqual(self.book.author, "Jane Austen")
        self.assertEqual(len(self.book.spine), 17)

    def test_there_are_seven_real_highlights(self):
        self.assertEqual(len(self.highlights), 7)

    def test_every_real_highlight_round_trips(self):
        for item in self.highlights:
            with self.subTest(item["text"][:30]):
                loc = self.book.locate(item)
                self.assertTrue(loc["found"])
                self.assertEqual(loc["where"], "fragment")
                passage = self.book.passage(loc)
                self.assertEqual(epub.words(passage["mark"]), epub.words(item["text"]))
                json.dumps(loc)
                json.dumps(passage)

    def test_mark_is_the_highlight_text_itself(self):
        for item in self.highlights:
            with self.subTest(item["text"][:30]):
                mark = self.book.passage(self.book.locate(item))["mark"]
                self.assertEqual(mark.split(), item["text"].split())

    def test_docfragment_numbers_are_1_based_spine_positions(self):
        for item in self.highlights:
            with self.subTest(item["pos0"]):
                fragment = epub._fragment(item["pos0"])[0]
                loc = self.book.locate(item)
                self.assertEqual(loc["doc"], fragment)
                # Without the DocFragment hint, a whole-book search lands in the same document
                if len(item["text"]) > 30:
                    self.assertEqual(self.book.locate({"text": item["text"]})["doc"], fragment)

    def test_the_rest_of_pos0_points_at_the_match(self):
        for item in self.highlights:
            with self.subTest(item["pos0"]):
                fragment, tail = epub._fragment(item["pos0"])
                self.assertEqual(self.book.doc(fragment).resolve(tail), self.book.locate(item)["start"])

    def test_the_rest_of_pos1_points_at_the_end(self):
        # All but the two-paragraph one, whose real pos1 sits one character past the last word
        for item in self.highlights:
            if "\n" in item["text"]:
                continue
            with self.subTest(item["pos1"]):
                fragment, tail = epub._fragment(item["pos1"])
                self.assertEqual(self.book.doc(fragment).resolve(tail), self.book.locate(item)["end"])

    def test_a_repeated_phrase_picks_the_one_pos0_points_at(self):
        bennet = next(item for item in self.highlights if item["text"] == "Mr. Bennet")
        doc = self.book.doc(2)
        self.assertGreater(len(doc.corpus().find_words(["mr", "bennet"])), 1)
        loc = self.book.locate(bennet)
        passage = self.book.passage(loc)
        self.assertEqual(passage["pre"], "")
        self.assertEqual(passage["post"], " made no answer.")
        first = doc.corpus().find_words(["mr", "bennet"])[0]
        self.assertNotEqual(doc.corpus().spans[first[0]][1], loc["start"])

    def test_without_a_usable_pos0_the_first_match_is_chosen(self):
        bennet = dict(next(item for item in self.highlights if item["text"] == "Mr. Bennet"))
        bennet["pos0"] = "/body/DocFragment[2]/body/nothing[9]/text().0"
        doc = self.book.doc(2)
        first = doc.corpus().find_words(["mr", "bennet"])[0]
        self.assertEqual(self.book.locate(bennet)["start"], doc.corpus().spans[first[0]][1])

    def test_curly_quotes_stay_in_the_mark(self):
        item = next(item for item in self.highlights if "To-morrow" in item["text"])
        passage = self.book.passage(self.book.locate(item))
        self.assertEqual(passage["mark"], "“To-morrow fortnight.”")
        self.assertEqual(passage["pre"], "")
        self.assertEqual(passage["post"], "")

    def test_mid_sentence_keeps_the_rest_of_the_paragraph(self):
        item = next(item for item in self.highlights if item["text"].startswith("everywhere."))
        passage = self.book.passage(self.book.locate(item))
        self.assertTrue(passage["pre"].startswith("“I know you do: and it is that which"))
        self.assertTrue(passage["pre"].endswith("one meets with it "))
        self.assertTrue(passage["post"].startswith(" to you alone."))

    def test_two_paragraphs_keep_their_break(self):
        item = next(item for item in self.highlights if "\n" in item["text"])
        passage = self.book.passage(self.book.locate(item))
        self.assertIn("\n\n", passage["mark"])
        self.assertEqual(passage["mark"].split("\n\n")[0], item["text"].split("\n")[0])
        self.assertEqual(passage["post"], " to pieces.”")

    def test_chapter_from_annotation_and_location_agree(self):
        for item in self.highlights:
            with self.subTest(item["chapter"]):
                loc = self.book.locate(item)
                self.assertEqual(self.book.toc_title_at(loc), item["chapter"])
                self.assertEqual(self.book.chapter_at(loc)["title"], item["chapter"])
                self.assertEqual(self.book.chapter_for(item["chapter"], loc)["title"], item["chapter"])

    def test_a_chapter_running_over_spine_documents_keeps_its_context(self):
        # This highlight is in document 3, while its chapter starts in document 2
        item = next(item for item in self.highlights if "Oh, single" in item["text"])
        loc = self.book.locate(item)
        self.assertEqual(loc["doc"], 3)
        before, after = self.book.ai_context(loc, epub.MODE_CHAPTER)
        self.assertTrue(before.startswith("Chapter I."))
        self.assertIn("IT is a truth", before)
        self.assertNotIn("CHAPTER II.", after)

    def test_chapters_are_top_level_entries_in_order(self):
        chapters = self.book.chapters()
        self.assertEqual(len(chapters), 63)
        self.assertEqual(chapters[1]["number"], 2)
        self.assertEqual(chapters[1]["title"], "Chapter I.")
        self.assertEqual(chapters[1]["name"], "Chapter I.")
        self.assertEqual(chapters[2]["name"], "3 · I hope Mr. Bingley will like it. CHAPTER II.")
        self.assertEqual(chapters[2]["card"], "Chapter 3: I hope Mr. Bingley will like it. CHAPTER II.")

    def test_neighbouring_paragraphs(self):
        item = next(item for item in self.highlights if item["text"] == "Mr. Bennet")
        passage = self.book.passage(self.book.locate(item))
        self.assertTrue(passage["before"].startswith("“But it is,” returned she"))
        self.assertTrue(passage["after"].startswith("“Do not you want to know"))
        self.assertTrue(passage["before2"])
        self.assertTrue(passage["after2"])
        self.assertFalse(passage["endCh"])

    def test_a_chapter_opening_has_only_the_heading_before_it(self):
        item = next(item for item in self.highlights if item["text"].startswith("IT is a truth"))
        passage = self.book.passage(self.book.locate(item))
        self.assertEqual(passage["before"], "Chapter I.")
        self.assertEqual(passage["before2"], "")

    def test_600_words_each_side(self):
        item = next(item for item in self.highlights if item["text"].startswith("everywhere."))
        before, after = self.book.ai_context(self.book.locate(item))
        self.assertEqual(len(before.split()), 600)
        self.assertEqual(len(after.split()), 600)
        self.assertTrue(before.endswith("one meets with it"))
        self.assertTrue(after.startswith("to you alone."))

    def test_600_words_reach_into_the_neighbouring_spine_document(self):
        # 557 characters into document 3, so most of "before" comes from document 2
        item = next(item for item in self.highlights if "Oh, single" in item["text"])
        before, after = self.book.ai_context(self.book.locate(item))
        self.assertEqual(len(before.split()), 600)
        self.assertEqual(len(after.split()), 600)
        self.assertIn("\n\n", before)

    def test_the_whole_book(self):
        item = next(item for item in self.highlights if item["text"].startswith("everywhere."))
        before, after = self.book.ai_context(self.book.locate(item), epub.MODE_BOOK)
        self.assertIn("IT is a truth", before)
        self.assertIn("CHAPTER XVIII", after)
        self.assertNotIn("candid without ostentation", before + after)

    def test_a_wrong_fragment_falls_back_to_the_whole_book(self):
        item = dict(next(item for item in self.highlights if "To-morrow" in item["text"]))
        item["pos0"] = item["pos0"].replace("DocFragment[3]", "DocFragment[9]")
        item["pos1"] = item["pos1"].replace("DocFragment[3]", "DocFragment[9]")
        loc = self.book.locate(item)
        self.assertEqual((loc["found"], loc["doc"], loc["where"]), (True, 3, "book"))

    def test_a_fragment_past_the_spine_falls_back_to_the_whole_book(self):
        item = dict(next(item for item in self.highlights if "To-morrow" in item["text"]))
        item["pos0"] = "/body/DocFragment[900]/body/p[1]/text().0"
        self.assertEqual(self.book.locate(item)["doc"], 3)

    def test_unknown_text_is_not_found(self):
        self.assertEqual(self.book.locate({"text": "No such sentence in Jane Austen zzzz."}), {"found": False})
        self.assertEqual(self.book.locate({"text": ""}), {"found": False})
        self.assertEqual(self.book.locate({"text": "—…"}), {"found": False})
        self.assertEqual(self.book.locate({}), {"found": False})

    def test_not_found_gives_empty_output(self):
        passage = self.book.passage({"found": False})
        self.assertFalse(passage["found"])
        self.assertEqual(passage["mark"], "")
        self.assertEqual(self.book.ai_context({"found": False}), ("", ""))
        self.assertIsNone(self.book.chapter_at({"found": False}))
        self.assertEqual(epub.empty_passage("Only this")["mark"], "Only this")

    def test_real_book_opens_and_locates_quickly(self):
        epub._cache.clear()
        start = time.perf_counter()
        book = epub.open_epub(FIXTURES / "book.epub")
        for item in self.highlights:
            book.passage(book.locate(item))
        book.locate({"text": "No such sentence zzzz."})
        self.assertLess(time.perf_counter() - start, 0.5)


class WordsTests(unittest.TestCase):
    SAMPLES = [
        "Plain words here.",
        "“Don’t” keep—coughing… so",
        "'tis the season's end'",
        "soft­hyphen and no break",
        "ÉCOLE Čeština 日本語 שלום",
        "To-morrow fortnight.",
        "«guillemets» — and – dashes",
        "'' ’’ ‘",
        "MiXeD 123 CaSe4",
        "",
    ]

    @unittest.skipUnless(shutil.which("lua"), "lua not installed")
    def test_same_as_the_plugins_words_function(self):
        source = (REPO / "koreader/pbanki.koplugin/cardgen.lua").read_text(encoding="utf-8")
        start = source.index("local function words(text)")
        end = source.index("\nend\n", start) + len("\nend\n")
        with tempfile.TemporaryDirectory() as folder:
            program = Path(folder) / "words.lua"
            program.write_text(
                source[start:end]
                + 'for line in io.lines(arg[1]) do print(words(line)) end\n',
                encoding="utf-8",
            )
            samples = Path(folder) / "samples.txt"
            samples.write_text("\n".join(self.SAMPLES) + "\n", encoding="utf-8")
            out = subprocess.run(["lua", str(program), str(samples)], capture_output=True, check=True)
        self.assertEqual(out.stdout.decode("utf-8").split("\n")[:-1], [epub.words(s) for s in self.SAMPLES])

    def test_examples(self):
        self.assertEqual(epub.words("“Don’t” keep—coughing"), "don't keep coughing")
        self.assertEqual(epub.words("soft­hyphen"), "softhyphen")


class ChapterNameTests(unittest.TestCase):
    def test_numbered_titles_stay_as_written(self):
        for title in ("Chapter 3: Things", "3. Things", "chapter one", "CHAPTER IV."):
            self.assertEqual(epub.chapter_name(5, title), title)
            self.assertEqual(epub.chapter_card(5, title), title)

    def test_others_gain_the_number(self):
        self.assertEqual(epub.chapter_name(5, "Things"), "5 · Things")
        self.assertEqual(epub.chapter_card(5, "Things"), "Chapter 5: Things")
        self.assertEqual(epub.chapter_name(5, "Chapters of life"), "5 · Chapters of life")

    def test_no_number_leaves_the_title(self):
        self.assertEqual(epub.chapter_name(None, "Things"), "Things")
        self.assertEqual(epub.chapter_card(None, "Things"), "Things")


class TempBook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        epub._cache.clear()

    def odd_book(self):
        path = self.folder / "odd.epub"
        write_epub(path, {"content.opf": opf([("odd.xhtml", XHTML, "")]), "odd.xhtml": ODD_DOC})
        return epub.open_epub(path)


class OddMarkupTests(TempBook):
    def test_only_book_text_is_kept(self):
        text = self.odd_book().doc(1).text
        for hidden in ("Should not appear", "color: red", "script text", "Vector", "hidden note"):
            self.assertNotIn(hidden, text)

    def test_entities_are_decoded(self):
        text = self.odd_book().doc(1).text
        self.assertIn("An Odd Chapter", text)
        self.assertIn("“smart” entities & a Drop cap word.", text)

    def test_blocks_come_out_in_order_with_kinds(self):
        doc = self.odd_book().doc(1)
        blocks = [(doc.text[start:end], kind) for start, end, kind in doc.blocks]
        self.assertEqual(blocks[0], ("An Odd Chapter", "heading"))
        self.assertEqual(blocks[1][1], "paragraph")
        self.assertTrue(blocks[1][0].startswith("First para"))
        self.assertTrue(blocks[2][0].startswith("Unclosed para"))
        self.assertIn(("Text straight in a div keeps going after a comment.", "paragraph"), blocks)
        self.assertIn(("Item one", "list"), blocks)
        self.assertIn(("Item two", "list"), blocks)
        self.assertIn(("A quoted paragraph here.", "quote"), blocks)
        self.assertIn(("A namespaced paragraph.", "paragraph"), blocks)
        self.assertIn(("Cell one", "paragraph"), blocks)
        self.assertIn(("Cell two", "paragraph"), blocks)

    def test_offsets_index_the_text(self):
        doc = self.odd_book().doc(1)
        for start, end, _kind in doc.blocks:
            self.assertEqual(doc.text[start:end], doc.text[start:end].strip())
        self.assertEqual(
            doc.text, "\n\n".join(doc.text[start:end] for start, end, _kind in doc.blocks)
        )

    def test_line_breaks_and_soft_hyphens(self):
        text = self.odd_book().doc(1).text
        self.assertIn("a softhyphen in it\nand a line break.", text)
        self.assertIn("Line one\nLine two", text)

    def test_a_highlight_across_a_drop_cap_is_found(self):
        book = self.odd_book()
        loc = book.locate({"text": "a Drop cap word."})
        self.assertEqual(book.passage(loc)["mark"], "a Drop cap word.")

    def test_a_highlight_with_a_soft_hyphen_is_found(self):
        book = self.odd_book()
        passage = book.passage(book.locate({"text": "soft­hyphen in it"}))
        self.assertEqual(passage["mark"], "softhyphen in it")

    def test_a_highlight_over_a_line_break_is_found(self):
        book = self.odd_book()
        passage = book.passage(book.locate({"text": "in it and a line"}))
        self.assertEqual(passage["mark"], "in it\nand a line")

    def test_text_split_differently_from_the_book_still_matches(self):
        book = self.odd_book()
        passage = book.passage(book.locate({"text": "Dropcap word"}))
        self.assertEqual(passage["mark"], "Drop cap word")

    def test_very_short_text_isnt_matched_inside_other_words(self):
        book = self.odd_book()
        self.assertEqual(book.locate({"text": "rop ca"}), {"found": False})

    def test_many_line_breaks_stay_fast(self):
        path = self.folder / "poem.epub"
        doc = "<html><body><p>" + "A line of a long poem<br/>" * 40000 + "</p></body></html>"
        write_epub(path, {"content.opf": opf([("d.xhtml", XHTML, "")]), "d.xhtml": doc})
        start = time.perf_counter()
        book = epub.open_epub(path)
        self.assertEqual(book.doc(1).text.count("\n"), 39999)
        self.assertLess(time.perf_counter() - start, 1)

    def test_endless_unclosed_tags_are_survived(self):
        path = self.folder / "deep.epub"
        doc = "<html><body>" + "<i>" * 100000 + "<p>Deep text here.</p></body></html>"
        write_epub(path, {"content.opf": opf([("d.xhtml", XHTML, "")]), "d.xhtml": doc})
        start = time.perf_counter()
        self.assertIn("Deep text here.", epub.open_epub(path).doc(1).text)
        self.assertLess(time.perf_counter() - start, 2)

    def test_pos0_picks_between_repeats_in_bad_markup(self):
        book = self.odd_book()
        doc = book.doc(1)
        matches = doc.corpus().find_words(epub.words("here is the target phrase").split())
        self.assertEqual(len(matches), 2)
        paths = [p for p in doc.elements if p.startswith("/body[1]/p[")]
        last_p = paths[-1]
        number = int(last_p.rsplit("[", 1)[1][:-1])
        pos0 = f"/body/DocFragment/body/p[{number}]/text().0"
        loc = book.locate({"text": "here is the target phrase", "pos0": pos0})
        self.assertEqual(book.passage(loc)["pre"], "And in a later paragraph, ")

    def test_a_lone_spine_document_has_no_fragment_index(self):
        self.assertEqual(epub._fragment("/body/DocFragment/body/p[1]/text().0")[0], 1)
        self.assertEqual(epub._fragment("/body/DocFragment[7]/body/p[1]/text().0")[0], 7)
        self.assertEqual(epub._fragment("/body/p[1]"), (None, None))
        self.assertEqual(epub._fragment({"page": 1}), (None, None))

    def test_cdata_text_is_not_lost(self):
        path = self.folder / "cdata.epub"
        doc = '<html><body><p>Before <![CDATA[inside cdata]]> after.</p></body></html>'
        write_epub(path, {"content.opf": opf([("d.xhtml", XHTML, "")]), "d.xhtml": doc})
        self.assertIn("inside cdata", epub.open_epub(path).doc(1).text)

    def test_a_document_without_a_body_tag(self):
        path = self.folder / "nobody.epub"
        write_epub(path, {"content.opf": opf([("d.xhtml", XHTML, "")]), "d.xhtml": "<p>Just a paragraph.</p><p>And more."})
        book = epub.open_epub(path)
        self.assertEqual(book.doc(1).text, "Just a paragraph.\n\nAnd more.")

    def test_declared_encoding_and_byte_order_mark(self):
        path = self.folder / "enc.epub"
        latin = '<?xml version="1.0" encoding="iso-8859-1"?><html><body><p>café</p></body></html>'.encode("latin-1")
        bom = "﻿<html><body><p>café two</p></body></html>".encode("utf-8")
        wrong = '<html><body><p>café three</p></body></html>'.encode("cp1252")
        write_epub(
            path,
            {
                "content.opf": opf([("a.xhtml", XHTML, ""), ("b.xhtml", XHTML, ""), ("c.xhtml", XHTML, "")]),
                "a.xhtml": latin,
                "b.xhtml": bom,
                "c.xhtml": wrong,
            },
        )
        book = epub.open_epub(path)
        self.assertEqual(book.doc(1).text, "café")
        self.assertEqual(book.doc(2).text, "café two")
        self.assertEqual(book.doc(3).text, "café three")


class StructureTests(TempBook):
    def test_links_with_spaces_and_parent_folders(self):
        path = self.folder / "paths.epub"
        package = opf([("../Text/my%20chapter.xhtml", XHTML, "")])
        write_epub(
            path,
            {"OPS/pkg/content.opf": package, "OPS/Text/my chapter.xhtml": "<html><body><p>Found me.</p></body></html>"},
            opf_name="OPS/pkg/content.opf",
        )
        self.assertEqual(epub.open_epub(path).doc(1).text, "Found me.")

    def test_a_malformed_package_file_is_still_read(self):
        path = self.folder / "badopf.epub"
        package = opf([("a.xhtml", XHTML, "")]).replace("</metadata>", "<dc:publisher>Me & You</metadata>")
        write_epub(path, {"content.opf": package, "a.xhtml": "<html><body><p>Text here.</p></body></html>"})
        book = epub.open_epub(path)
        self.assertEqual(book.title, "Odd Book")
        self.assertEqual(book.doc(1).text, "Text here.")

    def test_a_missing_container_falls_back_to_the_package_file(self):
        path = self.folder / "nocontainer.epub"
        write_epub(
            path,
            {"x/content.opf": opf([("a.xhtml", XHTML, "")]), "x/a.xhtml": "<p>Text.</p>"},
            container=False,
        )
        self.assertEqual(epub.open_epub(path).doc(1).text, "Text.")

    def test_two_authors_are_joined(self):
        self.assertEqual(self.odd_book().author, "Odd Author, Second Author")

    def test_a_missing_spine_document_keeps_the_numbering(self):
        path = self.folder / "gap.epub"
        package = opf([("gone.xhtml", XHTML, ""), ("here.xhtml", XHTML, "")])
        write_epub(path, {"content.opf": package, "here.xhtml": "<html><body><p>Second document.</p></body></html>"})
        book = epub.open_epub(path)
        self.assertEqual(book.doc(1).text, "")
        self.assertEqual(book.doc(2).text, "Second document.")
        self.assertEqual(book.locate({"text": "Second document."})["doc"], 2)

    def nav_book(self):
        path = self.folder / "nav.epub"
        nav = (
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            '<nav epub:type="landmarks"><ol><li><a href="one.xhtml">Start here</a></li></ol></nav>'
            '<nav epub:type="toc"><ol>'
            '<li><a href="one.xhtml">Part One</a><ol>'
            '<li><a href="one.xhtml#s1">Section A</a></li>'
            '<li><a href="one.xhtml#s2">Section B</a></li></ol></li>'
            '<li><span>Part Two</span><ol><li><a href="two.xhtml#s3">Section C</a></li></ol></li>'
            "</ol></nav></body></html>"
        )
        one = (
            '<html><body><h1>Part One</h1><p>Opening words.</p><h2 id="s1">Section A</h2><p>Alpha words.</p>'
            '<h2 id="s2">Section B</h2><p>Beta words.</p></body></html>'
        )
        two = '<html><body><p>Front of two.</p><h2 id="s3">Section C</h2><p>Gamma words.</p></body></html>'
        package = opf([("nav.xhtml", "application/x", ' properties="nav"'), ("one.xhtml", XHTML, ""), ("two.xhtml", XHTML, "")])
        write_epub(path, {"content.opf": package, "nav.xhtml": nav, "one.xhtml": one, "two.xhtml": two})
        return epub.open_epub(path)

    def test_nav_table_of_contents_with_levels(self):
        book = self.nav_book()
        self.assertEqual([(e["title"], e["level"]) for e in book.toc], [
            ("Part One", 1), ("Section A", 2), ("Section B", 2), ("Part Two", 1), ("Section C", 2),
        ])
        self.assertEqual([c["number"] for c in book.chapters()], [1, 2])
        self.assertEqual(book.chapters()[1]["title"], "Part Two")

    def test_nested_titles_map_to_their_top_level_chapter(self):
        book = self.nav_book()
        loc = book.locate({"text": "Beta words."})
        self.assertEqual(book.toc_title_at(loc), "Section B")
        self.assertEqual(book.chapter_at(loc), {"number": 1, "title": "Part One"})
        self.assertEqual(book.chapter_for("Section B", loc), {"number": 1, "title": "Part One"})
        self.assertEqual(book.chapter_for("Section C", {"found": False}), {"number": 2, "title": "Part Two"})

    def test_an_entry_without_a_link_starts_where_its_first_child_does(self):
        book = self.nav_book()
        loc = book.locate({"text": "Front of two."})
        # Before Section C's anchor in document 2, so still in Part One
        self.assertEqual(book.chapter_at(loc)["title"], "Part One")
        self.assertEqual(book.chapter_at(book.locate({"text": "Gamma words."}))["title"], "Part Two")

    def test_chapter_bounds_end_at_the_next_top_level_entry(self):
        book = self.nav_book()
        loc = book.locate({"text": "Beta words."})
        before, after = book.ai_context(loc, epub.MODE_CHAPTER)
        self.assertTrue(before.startswith("Part One"))
        self.assertTrue(before.endswith("Section B"))
        self.assertEqual(after, "Front of two.")

    def test_without_a_table_of_contents_the_chapter_is_the_spine_document(self):
        path = self.folder / "notoc.epub"
        package = opf([("a.xhtml", XHTML, ""), ("b.xhtml", XHTML, "")])
        write_epub(
            path,
            {
                "content.opf": package,
                "a.xhtml": "<html><body><p>One.</p><p>Two.</p></body></html>",
                "b.xhtml": "<html><body><p>Three.</p></body></html>",
            },
        )
        book = epub.open_epub(path)
        self.assertEqual(book.chapters(), [])
        loc = book.locate({"text": "Two."})
        self.assertEqual(book.ai_context(loc, epub.MODE_CHAPTER), ("One.", ""))
        self.assertEqual(book.ai_context(loc, epub.MODE_BOOK), ("One.", "Three."))
        self.assertTrue(book.passage(loc)["endCh"])
        self.assertIsNone(book.chapter_at(loc))


class PassageTests(TempBook):
    def book(self):
        path = self.folder / "b.epub"
        make_epub(
            path,
            "Book",
            "Author",
            [
                ("One", [f"Chapter one para {i}." for i in range(1, 7)]),
                ("Two", ["Chapter two para 1.", "Chapter two para 2."]),
            ],
        )
        return epub.open_epub(path)

    def test_end_of_chapter_is_flagged_after_the_last_paragraph(self):
        book = self.book()
        passage = book.passage(book.locate({"text": "Chapter one para 5."}))
        self.assertEqual(passage["after"], "Chapter one para 6.")
        self.assertTrue(passage["endCh"])
        self.assertEqual(passage["after2"], "")
        self.assertTrue(passage["endCh2"])

    def test_the_last_paragraph_has_nothing_after_it(self):
        book = self.book()
        passage = book.passage(book.locate({"text": "Chapter one para 6."}))
        self.assertEqual(passage["after"], "")
        self.assertTrue(passage["endCh"])

    def test_a_paragraph_two_from_the_end(self):
        book = self.book()
        passage = book.passage(book.locate({"text": "Chapter one para 4."}))
        self.assertEqual(passage["after"], "Chapter one para 5.")
        self.assertFalse(passage["endCh"])
        self.assertEqual(passage["after2"], "Chapter one para 6.")
        self.assertTrue(passage["endCh2"])

    def test_show_more_widens_by_one_paragraph_each_way(self):
        book = self.book()
        loc = book.locate({"text": "Chapter one para 3."})
        first = book.passage(loc)
        self.assertEqual(first["before"], "Chapter one para 2.")
        self.assertEqual(first["before2"], "Chapter one para 1.")
        self.assertEqual(first["after"], "Chapter one para 4.")
        self.assertEqual(first["after2"], "Chapter one para 5.")
        wider = book.passage(loc, more=1)
        self.assertEqual(wider["before2"], "One\n\nChapter one para 1.")
        self.assertEqual(wider["after2"], "Chapter one para 5.\n\nChapter one para 6.")
        self.assertTrue(wider["endCh2"])

    def test_context_stops_at_the_chapter_start(self):
        book = self.book()
        passage = book.passage(book.locate({"text": "Chapter two para 1."}))
        self.assertEqual(passage["before"], "Two")
        self.assertEqual(passage["before2"], "")

    def test_a_highlight_across_two_spine_documents(self):
        book = self.book()
        loc = book.locate(
            {
                "text": "Chapter one para 6.\nTwo\nChapter two para 1.",
                "pos0": "/body/DocFragment[1]/body/p[6]/text().0",
                "pos1": "/body/DocFragment[2]/body/p[1]/text().19",
            }
        )
        self.assertEqual((loc["doc"], loc["end_doc"], loc["where"]), (1, 2, "fragment"))
        passage = book.passage(loc)
        self.assertEqual(passage["mark"], "Chapter one para 6.\n\nTwo\n\nChapter two para 1.")
        self.assertEqual(passage["before"], "Chapter one para 5.")
        self.assertEqual(passage["after"], "Chapter two para 2.")
        before, after = book.ai_context(loc)
        self.assertTrue(before.endswith("Chapter one para 5."))
        self.assertEqual(after, "Chapter two para 2.")

    def test_the_same_across_documents_without_a_position(self):
        book = self.book()
        loc = book.locate({"text": "Chapter one para 6.\nTwo\nChapter two para 1."})
        self.assertEqual((loc["doc"], loc["end_doc"], loc["where"]), (1, 2, "book"))

    def test_context_on_unusual_mode_is_the_default(self):
        book = self.book()
        loc = book.locate({"text": "Chapter one para 3."})
        self.assertEqual(book.ai_context(loc, "Anything else"), book.ai_context(loc))


class SafetyTests(TempBook):
    def test_an_oversized_spine_entry_is_a_book_error(self):
        path = self.folder / "huge.epub"
        write_epub(
            path,
            {
                "content.opf": opf([("huge.xhtml", XHTML, "")]),
                "huge.xhtml": b"<html><body><p>" + b" " * (51 * 1024 * 1024) + b"</p></body></html>",
            },
        )
        self.assertLess(path.stat().st_size, 1024 * 1024)
        start = time.perf_counter()
        with self.assertRaisesRegex(epub.EpubError, "too big"):
            epub.open_epub(path)
        self.assertLess(time.perf_counter() - start, 1)

    def test_a_size_lie_in_the_zip_is_refused_by_the_read_cap(self):
        path = self.folder / "small.epub"
        write_epub(path, {"content.opf": opf([("a.xhtml", XHTML, "")]), "a.xhtml": "<p>" + "x" * 5000 + "</p>"})
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("a.xhtml")
            info.file_size = 10
            with mock.patch.object(epub, "MAX_ENTRY_BYTES", 100):
                # Declared size is under the cap; the stream stops at the declared size or fails
                try:
                    data = epub._read_entry(archive, "a.xhtml")
                except (zipfile.BadZipFile, epub.EpubError):
                    data = b""
                self.assertLessEqual(len(data), 100)

    def test_an_oversized_image_doesnt_matter_because_it_isnt_read(self):
        path = self.folder / "image.epub"
        write_epub(
            path,
            {
                "content.opf": opf([("a.xhtml", XHTML, ""), ("big.jpg", "image/jpeg", "")]),
                "a.xhtml": "<p>Fine.</p>",
                "big.jpg": b"\0" * (51 * 1024 * 1024),
            },
        )
        self.assertEqual(epub.open_epub(path).doc(1).text, "Fine.")

    def test_only_exact_entry_names_are_read(self):
        path = self.folder / "names.epub"
        write_epub(
            path,
            {
                "content.opf": opf([("../../../outside.xhtml", XHTML, ""), ("IN.xhtml", XHTML, "")]),
                "in.xhtml": "<p>Wrong case, so not read.</p>",
            },
        )
        book = epub.open_epub(path)
        self.assertEqual(book.doc(1).text, "")
        self.assertEqual(book.doc(2).text, "")

    def test_nothing_is_extracted_to_disk(self):
        path = self.folder / "b.epub"
        make_epub(path, "Book", "Author", [("One", ["Words here."])])
        before = sorted(p.name for p in self.folder.rglob("*"))
        book = epub.open_epub(path)
        book.doc(1)
        self.assertEqual(sorted(p.name for p in self.folder.rglob("*")), before)

    def test_files_that_arent_epubs(self):
        cases = {
            "text.epub": b"this is not a zip",
            "empty.epub": b"",
        }
        for name, data in cases.items():
            path = self.folder / name
            path.write_bytes(data)
            with self.assertRaises(epub.EpubError, msg=name):
                epub.open_epub(path)
        with self.assertRaises(epub.EpubError):
            epub.open_epub(self.folder / "missing.epub")

    def test_a_zip_with_nothing_to_read(self):
        path = self.folder / "nospine.epub"
        write_epub(path, {"content.opf": "<package><manifest/><spine/></package>"})
        with self.assertRaises(epub.EpubError):
            epub.open_epub(path)
        path = self.folder / "noopf.epub"
        write_epub(path, {"other.txt": "hi"}, container=False)
        with self.assertRaises(epub.EpubError):
            epub.open_epub(path)

    def test_a_broken_spine_entry_doesnt_lose_the_rest(self):
        path = self.folder / "corrupt.epub"
        write_epub(
            path,
            {
                "content.opf": opf([("a.xhtml", XHTML, ""), ("b.xhtml", XHTML, "")]),
                "a.xhtml": "<html><body><p>" + "A sentence of real text. " * 200 + "</p></body></html>",
                "b.xhtml": "<html><body><p>The second document.</p></body></html>",
            },
        )
        raw = bytearray(path.read_bytes())
        at = raw.find(b"a.xhtml") + len("a.xhtml") + 10
        raw[at:at + 40] = b"\xff" * 40
        path.write_bytes(bytes(raw))
        book = epub.open_epub(path)
        self.assertEqual(book.doc(1).text, "")
        self.assertEqual(book.doc(2).text, "The second document.")

    def test_a_package_file_with_exploding_entities_is_survived(self):
        entities = '<!ENTITY a "aaaaaaaaaa">' + "".join(
            f'<!ENTITY {chr(98 + i)} "{("&" + chr(97 + i) + ";") * 10}">' for i in range(9)
        )
        package = opf([("a.xhtml", XHTML, "")], title="&j;").replace(
            "<package ", f"<!DOCTYPE package [{entities}]><package ", 1
        )
        path = self.folder / "laughs.epub"
        write_epub(path, {"content.opf": package, "a.xhtml": "<p>Fine.</p>"})
        start = time.perf_counter()
        book = epub.open_epub(path)
        self.assertEqual(book.doc(1).text, "Fine.")
        self.assertLess(len(book.title), 1000)
        self.assertLess(time.perf_counter() - start, 2)

    def test_a_broken_table_of_contents_is_no_table(self):
        path = self.folder / "badtoc.epub"
        package = opf(
            [("a.xhtml", XHTML, ""), ("toc.ncx", "application/x-dtbncx+xml", "")], ncx=2
        )
        write_epub(path, {"content.opf": package, "a.xhtml": "<p>Fine.</p>", "toc.ncx": "<ncx><navMap><navPoint"})
        book = epub.open_epub(path)
        self.assertEqual(book.chapters(), [])
        self.assertEqual(book.doc(1).text, "Fine.")


class CacheAndSpeedTests(TempBook):
    def test_an_unchanged_book_is_not_read_twice(self):
        path = self.folder / "b.epub"
        make_epub(path, "Book", "Author", [("One", ["Words here."])])
        self.assertIs(epub.open_epub(path), epub.open_epub(path))

    def test_a_changed_book_is_read_again(self):
        path = self.folder / "b.epub"
        make_epub(path, "Book", "Author", [("One", ["Words here."])])
        first = epub.open_epub(path)
        make_epub(path, "Book Two", "Author", [("One", ["Other words."])])
        later = time.time() + 5
        os.utime(path, (later, later))
        second = epub.open_epub(path)
        self.assertIsNot(first, second)
        self.assertEqual(second.title, "Book Two")

    def test_the_cache_holds_a_few_books(self):
        for i in range(epub.CACHE_BOOKS + 3):
            path = self.folder / f"b{i}.epub"
            make_epub(path, "Book", "Author", [("One", ["Words here."])])
            epub.open_epub(path)
        self.assertLessEqual(len(epub._cache), epub.CACHE_BOOKS)

    def test_a_1_mb_book_opens_and_locates_well_under_a_second(self):
        sentences = [f"Sentence number {i} says something about reading books slowly." for i in range(1400)]
        chapters = [(f"Chapter {c}", sentences) for c in range(1, 15)]
        path = self.folder / "big.epub"
        make_epub(path, "Big", "Author", chapters)
        total = sum(zipfile.ZipFile(path).getinfo(f"OEBPS/ch{c}.xhtml").file_size for c in range(1, 15))
        self.assertGreater(total, 1_000_000)
        start = time.perf_counter()
        book = epub.open_epub(path)
        for chapter in range(1, 15):
            text = f"Sentence number {chapter * 90} says something"
            loc = book.locate({"text": text, "pos0": f"/body/DocFragment[{chapter}]/body/p[{chapter * 90 + 1}]/text().0"})
            self.assertEqual((loc["doc"], loc["where"]), (chapter, "fragment"))
            book.passage(loc)
        self.assertEqual(book.locate({"text": "Not in the book at all zzzz."}), {"found": False})
        self.assertLess(time.perf_counter() - start, 0.75)


def nav_html(items):
    """A nav document from nested [(title, href, children), ...]."""

    def listing(entries):
        return "<ol>" + "".join(
            f'<li><a href="{href}">{title}</a>{listing(children) if children else ""}</li>'
            for title, href, children in entries
        ) + "</ol>"

    return (
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
        f'<nav epub:type="toc">{listing(items)}</nav></body></html>'
    )


def sentences(word, count):
    """`count` words of made-up text in one paragraph."""
    return " ".join(f"{word}{i}" for i in range(count))


class InlineSpaceTests(TempBook):
    def doc_text(self, body):
        path = self.folder / "b.epub"
        write_epub(path, {"content.opf": opf([("a.xhtml", XHTML, "")]), "a.xhtml": f"<html><body>{body}</body></html>"})
        return epub.open_epub(path).doc(1).text

    def test_a_space_between_inline_pieces_in_a_container_is_kept(self):
        # KOReader's text for these, recorded from the real program
        self.assertEqual(self.doc_text("<div><i>Alphax so</i> <i>fond bravox</i> <i>of charliex</i></div>"),
                         "Alphax so fond bravox of charliex")
        self.assertEqual(self.doc_text("<blockquote><i>Ffonex</i> <i>ggonex</i></blockquote>"), "Ffonex ggonex")
        self.assertEqual(self.doc_text("<section><b>Aa</b>\n  <b>Bb</b></section>"), "Aa Bb")
        self.assertEqual(self.doc_text("<i>Aa</i> <i>Bb</i>"), "Aa Bb")

    def test_space_around_blocks_adds_nothing(self):
        self.assertEqual(self.doc_text("<div>\n<p>One</p>\n<p>Two</p>\n</div>"), "One\n\nTwo")
        self.assertEqual(self.doc_text("<div><i>One</i> </div>\n<div> <i>Two</i></div>"), "One\n\nTwo")
        self.assertEqual(self.doc_text("<section>\n  <i>One</i>\n</section>"), "One")

    def test_element_positions_still_line_up(self):
        path = self.folder / "b.epub"
        body = "<div><i>Alpha so</i> <i>fond of</i></div>"
        write_epub(path, {"content.opf": opf([("a.xhtml", XHTML, "")]), "a.xhtml": f"<html><body>{body}</body></html>"})
        doc = epub.open_epub(path).doc(1)
        self.assertEqual(doc.text[doc.resolve("/body/div/i[2]/text().0"):], "fond of")

    def test_a_short_highlight_lands_on_its_real_spot(self):
        path = self.folder / "b.epub"
        files = {
            "content.opf": opf([("a.xhtml", XHTML, ""), ("b.xhtml", XHTML, "")]),
            "a.xhtml": "<html><body><div><i>everyone is so</i> <i>fucking fond</i> <i>of saying</i></div></body></html>",
            "b.xhtml": "<html><body><p>Everyone is fond of books.</p></body></html>",
        }
        write_epub(path, files)
        book = epub.open_epub(path)
        loc = book.locate({"text": "fond of", "pos0": "/body/DocFragment[1]/body/div/i[2]/text().8"})
        self.assertEqual((loc["doc"], loc["where"]), (1, "fragment"))
        self.assertEqual(book.passage(loc)["mark"], "fond of")


class InvisibleCharacterTests(TempBook):
    def book(self, *paragraphs):
        path = self.folder / "b.epub"
        body = "".join(f"<p>{text}</p>" for text in paragraphs)
        write_epub(path, {"content.opf": opf([("a.xhtml", XHTML, "")]), "a.xhtml": f"<html><body>{body}</body></html>"})
        return epub.open_epub(path)

    def test_a_short_highlight_after_a_hair_space_matches_its_real_word(self):
        # Wuthering Heights in Standard Ebooks: a real KOReader selection, and the same words elsewhere
        book = self.book("She looked as if she would fain beg assistance; but I did not.",
                         "“\u200a‘But I cannot tell how to do,’")
        loc = book.locate({"text": "But I", "pos0": "/body/DocFragment[1]/body/p[2]/text().3"})
        passage = book.passage(loc)
        self.assertEqual((passage["pre"], passage["mark"]), ("“\u200a‘", "But I"))

    def test_the_mark_leaves_out_invisible_characters_and_quotes(self):
        book = self.book("“\u200a‘Deltax went homex")
        self.assertEqual(book.passage(book.locate({"text": "Deltax went homex"}))["mark"], "Deltax went homex")

    def test_zero_width_marks_dont_split_or_hide_a_word(self):
        book = self.book("Plain words\ufeff here\u2060 and sep\u200barate ones.")
        self.assertEqual(book.passage(book.locate({"text": "words here"}))["mark"], "words\ufeff here")
        self.assertTrue(book.locate({"text": "separate ones"})["found"])
        self.assertTrue(book.locate({"text": "sep\u200barate ones"})["found"])

    def test_the_plugins_word_rule_stays_as_it_was(self):
        self.assertEqual(epub.words("“\u200a‘But I"), "\u200a'but i")


class DisplayRuleTests(TempBook):
    def book(self, body, css="", head=""):
        path = self.folder / "b.epub"
        package = opf([("a.xhtml", XHTML, ""), ("s.css", "text/css", "")])
        xhtml = f'<html><head><link rel="stylesheet" href="s.css"/>{head}</head><body>{body}</body></html>'
        write_epub(path, {"content.opf": package, "a.xhtml": xhtml, "s.css": css})
        return epub.open_epub(path)

    def test_hidden_text_is_left_out_as_koreader_does(self):
        book = self.book('<p>Hidax <span class="hid">gone</span> hidbx</p>', ".hid{display:none}")
        self.assertEqual(book.doc(1).text, "Hidax hidbx")
        self.assertTrue(book.locate({"text": "Hidax hidbx"})["found"])

    def test_inline_style_hides_text(self):
        book = self.book('<p>Mikex <span style="color: red; display: none">gone</span> novemberx</p>')
        self.assertEqual(book.doc(1).text, "Mikex novemberx")

    def test_a_span_shown_as_a_block_starts_a_new_paragraph(self):
        book = self.book('<span class="blk">Blockax one</span><span class="blk">two blockbx</span>', "span.blk { display: block }")
        self.assertEqual(book.doc(1).text, "Blockax one\n\ntwo blockbx")

    def test_a_style_block_counts_and_the_most_specific_rule_wins(self):
        book = self.book(
            '<p>Aa <span class="x y">one</span> <b class="y">two</b></p>',
            ".x{display:none}",
            "<style>.y{display:none} span.y{display:inline}</style>",
        )
        self.assertEqual(book.doc(1).text, "Aa one")

    def test_rules_inside_at_blocks_and_unlinked_sheets_dont_count(self):
        book = self.book('<p>Aa <span class="k">kept</span></p>', "@media amzn-mobi { .k { display: none } }")
        self.assertEqual(book.doc(1).text, "Aa kept")
        path = self.folder / "c.epub"
        package = opf([("a.xhtml", XHTML, ""), ("s.css", "text/css", "")])
        xhtml = '<html><body><p>Aa <span class="k">kept</span></p></body></html>'
        write_epub(path, {"content.opf": package, "a.xhtml": xhtml, "s.css": ".k{display:none}"})
        self.assertEqual(epub.open_epub(path).doc(1).text, "Aa kept")

    def test_positions_after_a_hidden_sibling_still_count_it(self):
        book = self.book('<div><span class="h">gone</span><span>two</span></div>', ".h{display:none}")
        doc = book.doc(1)
        self.assertEqual(doc.text[doc.resolve("/body/div/span[2]/text().0"):], "two")

    def test_a_hidden_element_with_the_same_tag_inside_stays_hidden(self):
        book = self.book('<div style="display:none"><div>inner</div>still</div><p>After</p>')
        self.assertEqual(book.doc(1).text, "After")


class NestedChapterTests(TempBook):
    def jane(self):
        """Laid out like Jane Eyre in Standard Ebooks: front matter, then chapters under the book's title."""
        path = self.folder / "jane.epub"
        chapters = ["I", "II", "XXXVIII: Conclusion"]
        toc = [("Titlepage", "t.xhtml", []), ("Preface", "p.xhtml", []),
               ("Jane Eyre", "h.xhtml", [(title, f"c{i}.xhtml", []) for i, title in enumerate(chapters)])]
        files = {
            "t.xhtml": "<html><body><p>Title page.</p></body></html>",
            "p.xhtml": "<html><body><p>Preface words.</p></body></html>",
            "h.xhtml": "<html><body><h1>Jane Eyre</h1><p>An Autobiography.</p></body></html>",
        }
        items = [("nav.xhtml", "application/x", ' properties="nav"'), ("t.xhtml", XHTML, ""), ("p.xhtml", XHTML, ""), ("h.xhtml", XHTML, "")]
        for i, title in enumerate(chapters):
            files[f"c{i}.xhtml"] = (
                f"<html><body><h2>{title}</h2><p>{sentences(f'first{i}x', 40)}.</p><p>Last words of chapter {i}.</p></body></html>"
            )
            items.append((f"c{i}.xhtml", XHTML, ""))
        write_epub(path, {"content.opf": opf(items), "nav.xhtml": nav_html(toc), **files})
        return epub.open_epub(path)

    def test_chapters_under_a_title_entry_are_the_chapters(self):
        chapters = self.jane().chapters()
        self.assertEqual([(c["number"], c["name"]) for c in chapters], [
            (1, "1 · Titlepage"), (2, "2 · Preface"), (3, "I"), (4, "II"), (5, "XXXVIII: Conclusion"),
        ])
        self.assertEqual(chapters[2]["card"], "I")

    def test_a_highlight_gets_its_own_chapter(self):
        book = self.jane()
        loc = book.locate({"text": "Last words of chapter 2."})
        self.assertEqual(book.chapter_at(loc), {"number": 5, "title": "XXXVIII: Conclusion"})
        self.assertEqual(book.chapter_for("XXXVIII: Conclusion", loc), {"number": 5, "title": "XXXVIII: Conclusion"})
        self.assertEqual(book.chapter_for("I", {"found": False}), {"number": 3, "title": "I"})

    def test_the_chapter_for_the_ai_is_only_that_chapter(self):
        book = self.jane()
        before, after = book.ai_context(book.locate({"text": "first0x10"}), epub.MODE_CHAPTER)
        self.assertTrue(before.startswith("I\n\nfirst0x0"))
        self.assertTrue(after.endswith("Last words of chapter 0."))

    def test_the_passage_stops_at_the_real_chapter_end(self):
        book = self.jane()
        passage = book.passage(book.locate({"text": "Last words of chapter 0."}))
        self.assertEqual(passage["after"], "")
        self.assertTrue(passage["endCh"])

    def test_text_on_the_part_page_belongs_to_the_part_with_no_number(self):
        book = self.jane()
        loc = book.locate({"text": "An Autobiography."})
        self.assertEqual(book.chapter_at(loc), {"number": None, "title": "Jane Eyre"})
        self.assertEqual(book.chapter_for("Jane Eyre", loc), {"number": None, "title": "Jane Eyre"})
        self.assertEqual(book.passage(book.locate({"text": "Preface words."}))["after"], "")

    def one_document(self, lead, per_section, title="Part One"):
        path = self.folder / "one.epub"
        toc = [(title, "a.xhtml", [("Alpha", "a.xhtml#s1", []), ("Beta", "a.xhtml#s2", [])]), ("Next", "b.xhtml", [])]
        one = (
            f"<html><body><h1>{title}</h1><p>{sentences('lead', lead)}</p>"
            f'<h2 id="s1">Alpha</h2><p>{sentences("alpha", per_section)}</p>'
            f'<h2 id="s2">Beta</h2><p>{sentences("beta", per_section)}</p></body></html>'
        )
        items = [("nav.xhtml", "application/x", ' properties="nav"'), ("a.xhtml", XHTML, ""), ("b.xhtml", XHTML, "")]
        write_epub(path, {"content.opf": opf(items), "nav.xhtml": nav_html(toc), "a.xhtml": one, "b.xhtml": "<html><body><p>Next.</p></body></html>"})
        return epub.open_epub(path)

    def test_a_chapter_with_a_body_keeps_its_short_sections_inside_it(self):
        book = self.one_document(lead=300, per_section=200)
        self.assertEqual([c["title"] for c in book.chapters()], ["Part One", "Next"])
        self.assertEqual(book.chapter_for("Beta", {"found": False})["title"], "Part One")

    def test_short_sections_right_after_the_heading_stay_sections(self):
        self.assertEqual([c["title"] for c in self.one_document(lead=3, per_section=200).chapters()], ["Part One", "Next"])

    def test_long_entries_after_a_short_lead_in_one_document_are_chapters(self):
        book = self.one_document(lead=3, per_section=1500)
        self.assertEqual([c["title"] for c in book.chapters()], ["Alpha", "Beta", "Next"])
        before, after = book.ai_context(book.locate({"text": "alpha5"}), epub.MODE_CHAPTER)
        self.assertNotIn("beta0", after)

    def test_a_title_saying_chapter_is_a_chapter_even_with_sections_in_other_files(self):
        path = self.folder / "split.epub"
        toc = [("Chapter 1: Things", "a.xhtml", [("Sec A", "a.xhtml", []), ("Sec B", "b.xhtml", [])]), ("Chapter 2", "c.xhtml", [])]
        items = [("nav.xhtml", "application/x", ' properties="nav"'), ("a.xhtml", XHTML, ""), ("b.xhtml", XHTML, ""), ("c.xhtml", XHTML, "")]
        page = "<html><body><p>Words {}.</p></body></html>"
        write_epub(path, {"content.opf": opf(items), "nav.xhtml": nav_html(toc), "a.xhtml": page.format("a"), "b.xhtml": page.format("b"), "c.xhtml": page.format("c")})
        self.assertEqual([c["title"] for c in epub.open_epub(path).chapters()], ["Chapter 1: Things", "Chapter 2"])

    def test_parts_nest_to_any_depth(self):
        path = self.folder / "deep.epub"
        toc = [("Collected", "a.xhtml", [("Book One", "b.xhtml", [("One", "c.xhtml", []), ("Two", "d.xhtml", [])]),
                                         ("Book Two", "e.xhtml", [("Three", "f.xhtml", []), ("Four", "g.xhtml", [])])])]
        names = "abcdefg"
        items = [("nav.xhtml", "application/x", ' properties="nav"')] + [(f"{n}.xhtml", XHTML, "") for n in names]
        files = {f"{n}.xhtml": f"<html><body><p>Words {n}.</p></body></html>" for n in names}
        write_epub(path, {"content.opf": opf(items), "nav.xhtml": nav_html(toc), **files})
        chapters = epub.open_epub(path).chapters()
        self.assertEqual([(c["number"], c["title"]) for c in chapters], [(1, "One"), (2, "Two"), (3, "Three"), (4, "Four")])


class NumberedTitleTests(unittest.TestCase):
    def test_roman_numbered_titles_are_shown_as_written(self):
        for title in ("I", "XXXVIII: Conclusion", "IV.", "XI - Home"):
            self.assertEqual(epub.chapter_card(5, title), title)
            self.assertEqual(epub.chapter_name(5, title), title)

    def test_words_that_only_look_roman_get_a_number(self):
        for title in ("I Love You", "Civil War", "Mix", "C", "M", "IM"):
            self.assertEqual(epub.chapter_card(5, title), f"Chapter 5: {title}")


class LocationThatDoesntFitTests(TempBook):
    def book(self):
        path = self.folder / "b.epub"
        make_epub(path, "Book", "Author", [("One", ["Chapter one para."]), ("Two", ["Chapter two para."])])
        return epub.open_epub(path)

    BAD = (
        {"found": True, "doc": 99, "start": 0, "end_doc": 99, "end": 5},
        {"found": True, "doc": 0, "start": 0, "end_doc": 0, "end": 5},
        {"found": True, "doc": -1, "start": 0, "end_doc": 1, "end": 5},
        {"found": True, "doc": 1, "start": 0, "end_doc": 99, "end": 5},
        {"found": True, "doc": 2, "start": 0, "end_doc": 1, "end": 5},
        {"found": True, "doc": 1, "start": 99999, "end_doc": 1, "end": 99999},
        {"found": True, "doc": 1, "start": -5, "end_doc": 1, "end": 2},
        {"found": True, "doc": "1", "start": 0, "end_doc": 1, "end": 2},
        {"found": True},
        {"found": False},
    )

    def test_each_reads_as_not_found(self):
        book = self.book()
        for loc in self.BAD:
            with self.subTest(loc=loc):
                self.assertEqual(book.passage(loc), epub.empty_passage())
                for mode in epub.AI_MODES:
                    self.assertEqual(book.ai_context(loc, mode), ("", ""))
                self.assertIsNone(book.chapter_at(loc))
                self.assertIsNone(book.toc_title_at(loc))

    def test_absurdly_long_numbers_in_a_position_dont_raise(self):
        book = self.book()
        nines = "9" * 5000
        for pos0 in (f"/body/DocFragment[{nines}]/body/p", f"/body/DocFragment[1]/body/p/text().{nines}",
                     f"/body/DocFragment[1]/body/p/text()[{nines}].0"):
            with self.subTest(pos0=pos0[:30]):
                self.assertTrue(book.locate({"text": "Chapter one para.", "pos0": pos0})["found"])
        self.assertTrue(book.locate({"text": "Chapter one para.", "pos0": "/body/DocFragment[1]/body/p", "pos1": f"/body/DocFragment[{nines}]/x"})["found"])


if __name__ == "__main__":
    unittest.main()
