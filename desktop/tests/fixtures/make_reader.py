"""Builds a fake reader in a plain folder, shaped like the real one, for tests.

    from fixtures.make_reader import build_reader
    fake = build_reader(tmp_path)
    fake.mount, fake.books["pride"], fake.snapshot()

Or from the shell, to look around: python3 desktop/tests/fixtures/make_reader.py <empty folder>

Books, in KOReader's history order (fake.books maps these names to device paths):
  pride        Real KOReader sidecar and ePub from real_sidecar/, 7 highlights, all pending
  mostly_done  Mostly reviewed, card history, a chosen prompt, a Companion beside it
  pending      Pending highlights, a note, two lines in reviewed.jsonl, no Companion
  scan         A PDF with fixed-layout highlights
  walden       Opened in KOReader but has no highlights
  no_sidecar   In the history, never given a settings file
  broken       A sidecar that isn't valid Lua
  gone         In the history with a sidecar, but its book file was deleted
  card         On the memory card, so not reachable
"""

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
REAL_SIDECAR = Path(__file__).resolve().parent / "real_sidecar"
PLUGIN_MAIN = REPO / "koreader/pbanki.koplugin/main.lua"

KOREADER = "applications/koreader"
PBANKI = "applications/pbanki"
PLUGIN = f"{KOREADER}/plugins/pbanki.koplugin"
HISTORY_START = 1_793_700_000
OLD_PLUGIN_TEXT = '-- pbAnki plugin, before desktop saves\nlocal PbAnki = {}\nreturn PbAnki\n'


def lua_dump(value, indent=0):
    """Text in the shape KOReader's dump.lua writes, keys sorted."""
    if value is None:
        return "nil"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\\n")
        return f'"{escaped}"'
    pad = "    " * (indent + 1)
    items = list(enumerate(value, 1)) if isinstance(value, list) else sorted(value.items(), key=lambda kv: str(kv[0]))
    if not items:
        return "{}"
    lines = [f"{pad}[{lua_dump(k)}] = {lua_dump(v, indent + 1)}," for k, v in items]
    return "{\n" + "\n".join(lines) + "\n" + "    " * indent + "}"


def lua_file(comment, value):
    return f"-- {comment}\nreturn {lua_dump(value)}\n"


def make_epub(path, title, author, chapters):
    """A small ePub with one spine document per chapter and a table of contents.

    `chapters` is [(chapter title, [paragraph, ...]), ...].
    """
    manifest = []
    spine = []
    points = []
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        for i, (chapter, paragraphs) in enumerate(chapters, 1):
            name = f"ch{i}.xhtml"
            body = "".join(f"<p>{p}</p>\n" for p in paragraphs)
            z.writestr(
                f"OEBPS/{name}",
                '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                f"<head><title>{chapter}</title></head><body>\n<h2>{chapter}</h2>\n{body}</body></html>",
            )
            manifest.append(f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{i}"/>')
            points.append(
                f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>{chapter}</text></navLabel>'
                f'<content src="{name}"/></navPoint>'
            )
        z.writestr(
            "OEBPS/toc.ncx",
            '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap>'
            + "".join(points)
            + "</navMap></ncx>",
        )
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator></metadata>"
            '<manifest><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
            + "".join(manifest)
            + '</manifest><spine toc="ncx">'
            + "".join(spine)
            + "</spine></package>",
        )


def epub_highlight(fragment, paragraph, text, when, **extra):
    """An annotation as KOReader saves it, for `paragraph` (1-based) of spine document `fragment`."""
    xpointer = f"/body/DocFragment[{fragment}]/body/p[{paragraph}]/text()"
    item = {
        "datetime": when,
        "drawer": "lighten",
        "color": "gray",
        "text": text,
        "page": f"{xpointer}.0",
        "pos0": f"{xpointer}.0",
        "pos1": f"{xpointer}.{len(text)}",
    }
    item.update(extra)
    return item


def key_of(item):
    pos0 = item["pos0"]
    return f"{item['datetime']}|{pos0['page'] if isinstance(pos0, dict) else pos0}"


TIME_CHAPTERS = [
    (
        "The daily miracle",
        [
            "Every morning the hours arrive again, and nobody can spend them in advance.",
            "A person who wastes a morning still wakes to a whole new day.",
            "Nothing else in life is handed out so evenly among rich and poor.",
        ],
    ),
    (
        "Precautions before beginning",
        [
            "Start small, and allow for accidents along the way.",
            "A small win leads somewhere, while a grand failure leads nowhere.",
        ],
    ),
    (
        "The cause of the troubles",
        [
            "Most people treat their working hours as the whole of the day.",
            "The hours after work are treated as a margin, and so they shrink.",
        ],
    ),
]

GARDEN_CHAPTERS = [
    (
        "Chapter 1: Soil",
        [
            "Good soil crumbles in the hand and smells of rain.",
            "A garden is mostly patience, with a little digging.",
        ],
    ),
    (
        "Seeds",
        [
            "Seeds keep their promise for years if they stay cool and dry.",
            "Sow thinly; thinning later is a chore nobody enjoys.",
        ],
    ),
]


class FakeReader:
    def __init__(self, mount):
        self.mount = Path(mount)
        self.books = {}
        self.keys = {}

    def local(self, name):
        return self.mount / self.books[name][len("/mnt/ext1/"):]

    def snapshot(self):
        """{relative path: sha256} of every file, and None for folders."""
        out = {}
        for path in sorted(self.mount.rglob("*")):
            rel = path.relative_to(self.mount).as_posix()
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        return out


def build_reader(root, plugin="new", companion=True, koreader=True, pbanki=True, metadata_folder="doc", history=True):
    """Writes a fake reader at `root` and returns a FakeReader.

    plugin: "new" is the repo's real main.lua, "old" one that can't read desktop saves,
    None no plugin at all. companion: whether mostly_done has its Companion. koreader and
    pbanki: whether those folders exist. metadata_folder: value of KOReader's setting; the
    sidecars stay beside the books, so use move_sidecar() to test other locations.
    """
    fake = FakeReader(root)
    mount = fake.mount
    mount.mkdir(parents=True, exist_ok=True)
    (mount / "system/config").mkdir(parents=True)
    (mount / "system/config/desktop.json").write_text('{"not":"ours"}\n', encoding="utf-8")
    (mount / "English").mkdir()

    fake.books.update(
        {
            "pride": "/mnt/ext1/English/book.epub",
            "mostly_done": "/mnt/ext1/English/Time - Arnold Bennett.epub",
            "pending": "/mnt/ext1/English/Gardens, A Primer - Ann Example.epub",
            "scan": "/mnt/ext1/English/Scanned Notes.pdf",
            "walden": "/mnt/ext1/English/Walden.epub",
            "no_sidecar": "/mnt/ext1/English/No Sidecar.epub",
            "broken": "/mnt/ext1/English/Broken.epub",
            "gone": "/mnt/ext1/English/Deleted Book.epub",
            "card": "/mnt/ext2/Books/On Card.epub",
        }
    )

    # Real KOReader files
    shutil.copy(REAL_SIDECAR / "book.epub", mount / "English/book.epub")
    (mount / "English/book.sdr").mkdir()
    shutil.copy(REAL_SIDECAR / "metadata.epub.lua", mount / "English/book.sdr/metadata.epub.lua")

    def sidecar(name, data, suffix="epub"):
        folder = fake.local(name).with_suffix(".sdr")
        folder.mkdir(parents=True, exist_ok=True)
        data = dict(data, doc_path=fake.books[name])
        (folder / f"metadata.{suffix}.lua").write_text(
            lua_file(f"{fake.books[name][:-len(suffix) - 1]}.sdr/metadata.{suffix}.lua", data), encoding="utf-8"
        )

    # Mostly reviewed
    make_epub(fake.local("mostly_done"), "How to Spend a Day", "Arnold Bennett", TIME_CHAPTERS)
    done = [
        epub_highlight(1, 1, TIME_CHAPTERS[0][1][0], "2026-10-01 09:00:00", chapter="The daily miracle", pageno=3),
        epub_highlight(1, 2, TIME_CHAPTERS[0][1][1], "2026-10-01 09:01:00", chapter="The daily miracle", pageno=4),
        epub_highlight(2, 1, TIME_CHAPTERS[1][1][0], "2026-10-02 09:00:00", chapter="Precautions before beginning", pageno=9),
        epub_highlight(
            3, 1, TIME_CHAPTERS[2][1][0], "2026-10-03 09:00:00", chapter="The cause of the troubles", pageno=15,
            note="Exactly my week.",
        ),
    ]
    sidecar(
        "mostly_done",
        {
            "annotations": done,
            "pbanki_reviewed": {key_of(item): True for item in done[:3]},
            "pbanki_card_history": [
                {
                    "front": "When do the hours arrive?",
                    "back": "Every morning.",
                    "suggested": {"front": "When do hours arrive?", "back": "Every morning."},
                    "quote": TIME_CHAPTERS[0][1][0],
                },
                {"front": "What should you allow for?", "back": "Accidents.", "quote": TIME_CHAPTERS[1][1][0], "note": "Yes."},
            ],
            "pbanki_prompt": "Self Help",
            "percent_finished": 1.0,
            "doc_pages": 20,
            "doc_props": {"title": "How to Spend a Day", "authors": "Arnold Bennett\nA. N. Other"},
        },
    )
    if companion:
        fake.local("mostly_done").with_name("Time - Arnold Bennett.companion.md").write_text(
            "# How to Spend a Day\n\n## 1. Overview\n\nA short book about hours.\n\n### The daily miracle\n\n- Time arrives.\n",
            encoding="utf-8",
        )

    # Pending, with desktop saves not yet picked up by the plugin
    make_epub(fake.local("pending"), "Gardens, A Primer", "Ann Example", GARDEN_CHAPTERS)
    pending = [
        epub_highlight(1, 1, GARDEN_CHAPTERS[0][1][0], "2026-10-05 10:00:00", chapter="Chapter 1: Soil", pageno=2),
        epub_highlight(1, 2, GARDEN_CHAPTERS[0][1][1], "2026-10-05 10:01:00", chapter="Chapter 1: Soil", pageno=3, note="Ha."),
        epub_highlight(2, 1, GARDEN_CHAPTERS[1][1][0], "2026-10-05 10:02:00", chapter="Seeds", pageno=6),
        epub_highlight(2, 2, GARDEN_CHAPTERS[1][1][1], "2026-10-05 10:03:00", pageno=7),
        epub_highlight(1, 1, "Words that are not in this book at all.", "2026-10-05 10:04:00", chapter="Chapter 1: Soil", pageno=2),
    ]
    # A page bookmark, which isn't a highlight
    pending.append(
        {"datetime": "2026-10-05 10:05:00", "page": "/body/DocFragment[2]/body/p[1]/text().0", "pageno": 6, "text": "in Seeds"}
    )
    sidecar(
        "pending",
        {
            "annotations": pending,
            "percent_finished": 0.5,
            "doc_pages": 10,
            "stats": {"title": "Gardens, A Primer", "authors": "Ann Example", "pages": 10, "series": "N/A"},
        },
    )
    fake.keys["pending"] = [key_of(item) for item in pending[:5]]

    # A PDF, whose positions are tables with a page
    (mount / "English/Scanned Notes.pdf").write_bytes(b"%PDF-1.4\n% not a real PDF, nothing opens it\n")
    scan = [
        {
            "datetime": "2026-09-20 08:00:00",
            "drawer": "lighten",
            "text": "A line on page twelve.",
            "chapter": "Part One",
            "page": 12,
            "pageno": 12,
            "pos0": {"page": 12, "x": 10.5, "y": 20.25, "rotation": 0, "zoom": 1.5},
            "pos1": {"page": 12, "x": 90.5, "y": 20.25, "rotation": 0, "zoom": 1.5},
        },
        {
            "datetime": "2026-09-20 08:01:00",
            "drawer": "lighten",
            "text": "A line on page forty.",
            "page": 40,
            "pos0": {"page": 40, "x": 1, "y": 2},
            "pos1": {"page": 40, "x": 3, "y": 2},
        },
    ]
    sidecar(
        "scan",
        {
            "annotations": scan,
            "pbanki_reviewed": {"2026-09-20 08:01:00|40": True},
            "doc_pages": 80,
            "percent_finished": 0.5,
            "doc_props": {"title": "Scanned Notes"},
        },
        suffix="pdf",
    )

    # No highlights, only reading progress
    make_epub(fake.local("walden"), "Walden", "Henry David Thoreau", [("Economy", ["I went to the woods."])])
    sidecar(
        "walden",
        {"percent_finished": 0.12, "doc_pages": 300, "doc_props": {"title": "Walden", "authors": "Henry David Thoreau"}},
    )

    # In the history but never opened long enough to save settings
    make_epub(fake.local("no_sidecar"), "Never Saved", "Nobody", [("One", ["Only a sentence."])])

    # Malformed sidecar
    make_epub(fake.local("broken"), "Broken Book", "Somebody", [("One", ["Still has text."])])
    folder = fake.local("broken").with_suffix(".sdr")
    folder.mkdir()
    (folder / "metadata.epub.lua").write_bytes(b'return { ["annotations"] = { [1] = { ["text"] = "cut off')

    # Book file deleted, sidecar left behind
    sidecar(
        "gone",
        {
            "annotations": [epub_highlight(1, 1, "A line from a deleted book.", "2026-08-01 08:00:00", chapter="One", pageno=1)],
            "doc_pages": 10,
            "doc_props": {"title": "Deleted Book"},
        },
    )

    if koreader:
        (mount / KOREADER).mkdir(parents=True)
        names = ["pride", "mostly_done", "pending", "scan", "walden", "no_sidecar", "broken", "gone", "card"]
        if history:
            entries = [
                {"time": HISTORY_START - 1000 * i, "file": fake.books[name]} for i, name in enumerate(names)
            ]
            (mount / KOREADER / "history.lua").write_text(lua_file("./history.lua", entries), encoding="utf-8")
        (mount / KOREADER / "settings.reader.lua").write_text(
            lua_file(
                "./settings.reader.lua",
                {
                    "document_metadata_folder": metadata_folder,
                    "home_dir": "/mnt/ext1/English",
                    "lastfile": fake.books["pride"],
                    "highlight_action_on_single_word": False,
                },
            ),
            encoding="utf-8",
        )
        if plugin:
            (mount / PLUGIN).mkdir(parents=True)
            if plugin == "new":
                shutil.copy(PLUGIN_MAIN, mount / PLUGIN / "main.lua")
            else:
                (mount / PLUGIN / "main.lua").write_text(OLD_PLUGIN_TEXT, encoding="utf-8")

    if pbanki:
        (mount / PBANKI).mkdir(parents=True)
        (mount / PBANKI / "reviewed.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"v": 1, "batch": "b1", "file": fake.books["pending"], "key": fake.keys["pending"][0], "cards": []}),
                    json.dumps(
                        {
                            "v": 1,
                            "batch": "b1",
                            "file": fake.books["pending"],
                            "key": fake.keys["pending"][1],
                            "cards": [{"front": "What is a garden?", "back": "Patience.", "quote": "x"}],
                        }
                    ),
                    # Another book, and two lines the plugin would refuse
                    json.dumps({"v": 1, "batch": "b2", "file": "/mnt/ext1/English/Other.epub", "key": "k|1", "cards": []}),
                    json.dumps({"v": 2, "file": fake.books["pending"], "key": fake.keys["pending"][2]}),
                    "not json",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    return fake


def move_sidecar(fake, name, location, digest=None):
    """Moves a book's sidecar from beside the book to the "dir" or "hash" location."""
    device_path = fake.books[name]
    source = fake.local(name).with_suffix(".sdr")
    if location == "dir":
        target = fake.mount / KOREADER / "docsettings" / Path(device_path).with_suffix(".sdr").relative_to("/")
    else:
        target = fake.mount / KOREADER / "hashdocsettings" / digest[:2] / f"{digest}.sdr"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return target


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: make_reader.py <empty folder>")
    print(json.dumps(build_reader(sys.argv[1]).books, indent=2))
