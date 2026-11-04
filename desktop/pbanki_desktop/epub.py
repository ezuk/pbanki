"""Reads ePub files: spine, table of contents, text blocks, and where a highlight sits.

Zip entries are read by exact name, are capped in size and are never extracted to disk.
The book is read once and the zip closed straight away, so nothing keeps the reader's disk
busy; the text of each spine document is then parsed the first time it's needed.

A location ("loc") is a plain JSON-safe dict:
  {"found": True, "doc": 3, "start": 120, "end_doc": 3, "end": 180, "where": "fragment"}
where doc numbers are 1-based spine positions and offsets index that document's text.
Not found is {"found": False}.
"""

import bisect
import html.parser
import os
import posixpath
import re
import threading
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from collections import OrderedDict

MAX_ENTRY_BYTES = 50 * 1024 * 1024
CACHE_BOOKS = 4

MAX_ELEMENT_DEPTH = 512
# Shorter text matched ignoring word breaks would land inside unrelated words
MIN_COMPACT_LETTERS = 8
# A table-of-contents entry with less text than this before its first sub-entry has no body of its own
MIN_LEAD_WORDS = 150
# Sub-entries of one spine document this long on average are chapters, not sections
MIN_CHAPTER_WORDS = 1000
# Digits in an xpointer number, beyond which it can't be real
MAX_XPOINTER_DIGITS = 9

CONTEXT_WORDS = 600
MODE_WORDS = "600 words each side"
MODE_CHAPTER = "The chapter"
MODE_BOOK = "The whole book"
AI_MODES = (MODE_WORDS, MODE_CHAPTER, MODE_BOOK)

NOT_FOUND = {"found": False}
_PARAGRAPH_BREAK = "\n\n"

_HTML_TYPES = ("application/xhtml+xml", "text/html", "application/x-dtbook+xml")
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCKS = _HEADINGS | {
    "address", "article", "aside", "blockquote", "body", "caption", "center", "dd", "details",
    "dir", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "header",
    "hgroup", "hr", "li", "main", "menu", "nav", "ol", "p", "pre", "section", "summary", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
# Whitespace between the children of these isn't a text node, though between inline pieces it's a space
_CONTAINERS = _BLOCKS - _HEADINGS - {"p", "li", "dd", "dt", "td", "th", "pre", "caption", "figcaption", "summary"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_SKIPPED = {"script", "style", "head", "title", "svg", "template"}
# A start tag closes an open sibling of the same family, as browsers do with bad markup
_SIBLINGS = {
    "li": ("li", {"ul", "ol", "menu", "dir"}),
    "dt": ("dt", {"dl"}),
    "dd": ("dd", {"dl"}),
    "tr": ("tr", {"table", "tbody", "thead", "tfoot"}),
    "td": ("td", {"tr", "table"}),
    "th": ("th", {"tr", "table"}),
}

_XPOINTER = re.compile(r"^/body/DocFragment(?:\[(\d+)\])?(/.*)?$")
_XPOINTER_TAIL = re.compile(r"^(.*?)(?:/text\(\)(?:\[(\d+)\])?)?(?:\.(\d+))?$")
_XPOINTER_STEP = re.compile(r"^([^\[\]]+)(?:\[(\d+)\])?$")
_COLLAPSE = re.compile(r"[ \t\r\n\f]+")
_NON_SPACE = re.compile(r"\S+")
_ENCODING = re.compile(rb"""(?:encoding|charset)\s*=\s*["']?([A-Za-z0-9_.:-]+)""")
_STARTS_NUMBERED = re.compile(
    r"^\s*(?:\d|(?i:chapter\b)"
    r"|(?![LCDM]\b)(?=[IVXLCDM])M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})(?=\s*(?:$|[.:)—–-])))"
)
_CHAPTER_TITLE = re.compile(r"^\s*(?:\d|chapter\b)", re.IGNORECASE)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CSS_SELECTOR = re.compile(r"[A-Za-z0-9]*(?:\.[\w-]+)?")
_CSS_DISPLAY = re.compile(r"(?:^|;)\s*display\s*:\s*([A-Za-z-]+)", re.I)
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.I | re.S)
_LINK_TAG = re.compile(r"<link\b[^>]*>", re.I)
_LINK_HREF = re.compile(r"""\bhref\s*=\s*["']([^"']+)["']""", re.I)
_BLOCK_DISPLAYS = {"block", "list-item", "table", "flex", "grid", "flow-root"}

# Same word characters as the plugin's words(): ASCII letters and digits, the apostrophe and
# every other non-ASCII character except no-break space, en and em dash, curly double
# quotes and the ellipsis. Curly single quotes and soft hyphens are unified after matching.
_WORD = re.compile("[0-9A-Za-z'\u0080-\u009f¡-‒―-‛„-‥‧-\U0010ffff]+")
_EDGE_MARKS = "'‘’­"
_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}
_WORD_MARKS = {ord("­"): None, ord("‘"): "'", ord("’"): "'"}

# Finding a place in a book also breaks words at the thin Unicode spaces, and ignores
# zero-width marks, which books set against words and selections leave out
_INVISIBLE = "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2061\u2062\u2063\u2064\ufeff"
_PLACE_WORD = re.compile(
    "[0-9A-Za-z'\u0080-\u009f¡-\u1fff\u200b-‒―-‛„-‥‧\u202a-\u202e‰-\u205e\u2060-\u2fff\u3001-\U0010ffff]+"
)
_PLACE_EDGE_MARKS = _EDGE_MARKS + _INVISIBLE
_PLACE_MARKS = {**_WORD_MARKS, **{ord(c): None for c in _INVISIBLE}}


class EpubError(Exception):
    """The book can't be read. The message is shown to the user."""


def _scan(text, pattern, edges, marks):
    out = []
    for match in pattern.finditer(text):
        raw = match.group()
        if raw.isalnum() and raw.isascii():
            out.append((raw.translate(_ASCII_LOWER), match.start(), match.end()))
            continue
        lead = len(raw) - len(raw.lstrip(edges))
        trail = len(raw) - len(raw.rstrip(edges))
        word = raw[lead:len(raw) - trail].translate(marks)
        if word:
            out.append((word.translate(_ASCII_LOWER), match.start() + lead, match.end() - trail))
    return out


def _tokens(text):
    """(word, start, end) for each word, normalised the way the plugin's words() does."""
    return _scan(text, _WORD, _EDGE_MARKS, _WORD_MARKS)


def _place_tokens(text):
    """(word, start, end) for each word, as compared when finding a place in a book."""
    return _scan(text, _PLACE_WORD, _PLACE_EDGE_MARKS, _PLACE_MARKS)


def words(text):
    """Lowercase words of `text`, ignoring punctuation, joined by single spaces."""
    return " ".join(token[0] for token in _tokens(text or ""))


def is_numbered_title(title):
    """Whether a chapter's title already starts with its number, or with the word Chapter."""
    return bool(_STARTS_NUMBERED.match(title or ""))


def chapter_name(number, title):
    """How lists show a chapter: as written when numbered already, otherwise `N · Title`."""
    if number is None or _STARTS_NUMBERED.match(title or ""):
        return title
    return f"{number} · {title}"


def chapter_card(number, title):
    """How cards show a chapter: as written when numbered already, otherwise `Chapter N: Title`."""
    if number is None or _STARTS_NUMBERED.match(title or ""):
        return title
    return f"Chapter {number}: {title}"


def empty_passage(text=""):
    """What the screen shows when the highlight isn't matched: its text alone."""
    return {
        "found": False, "before2": "", "before": "", "pre": "", "mark": text or "", "post": "",
        "after": "", "after2": "", "endCh": False, "endCh2": False,
    }


def _decode(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    declared = _ENCODING.search(raw[:1024])
    if declared:
        try:
            return raw.decode(declared.group(1).decode("ascii"))
        except (LookupError, UnicodeDecodeError):
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def _local_name(name):
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


class _Node:
    """A lenient element tree, the same whichever parser built it."""

    __slots__ = ("tag", "attrs", "children", "parts")

    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = attrs
        self.children = []
        self.parts = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()

    def all(self, tag):
        return [node for node in self.walk() if node.tag == tag]

    def first(self, tag):
        return next((node for node in self.walk() if node.tag == tag), None)

    def kids(self, tag):
        return [c for c in self.children if isinstance(c, _Node) and c.tag == tag]

    def text(self):
        out = []
        for child in self.children:
            out.append(child.text() if isinstance(child, _Node) else child)
        return _COLLAPSE.sub(" ", "".join(out)).strip()


def _node_from_xml(element):
    node = _Node(_local_name(element.tag) if isinstance(element.tag, str) else "", {})
    for key, value in element.attrib.items():
        node.attrs[_local_name(key)] = value
    if element.text:
        node.children.append(element.text)
    for child in element:
        node.children.append(_node_from_xml(child))
        if child.tail:
            node.children.append(child.tail)
    return node


class _TreeParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(_local_name(tag), {_local_name(k): v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        self.stack.append(node)

    def handle_endtag(self, tag):
        name = _local_name(tag)
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == name:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _parse_markup(raw):
    """Tree of an XML file, falling back to the forgiving HTML parser when it's malformed."""
    try:
        return _node_from_xml(ET.fromstring(raw))
    except (ET.ParseError, ValueError, LookupError):
        parser = _TreeParser()
        parser.feed(_decode(raw))
        parser.close()
        return parser.root


def _display_rules(css):
    """{selector: display} from a stylesheet's plain tag, .class and tag.class rules, outside @ blocks."""
    text = _CSS_COMMENT.sub("", css)
    rules = {}
    at = 0
    while True:
        brace = text.find("{", at)
        if brace < 0:
            return rules
        depth, end = 1, brace + 1
        while end < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            end += 1
        head = text[at:brace].rsplit(";", 1)[-1].strip()
        display = _CSS_DISPLAY.search(text[brace + 1:end - 1].strip())
        at = end
        if display and not head.startswith("@"):
            for selector in head.split(","):
                selector = selector.strip()
                if selector and _CSS_SELECTOR.fullmatch(selector):
                    rules[selector] = display.group(1).lower()


class _Element:
    __slots__ = ("name", "path", "start", "nodes", "counts", "open_text", "block")

    def __init__(self, name, path, start, block=False):
        self.name = name
        self.path = path
        self.start = start
        self.nodes = []
        self.counts = {}
        self.open_text = False
        self.block = block


class _DocParser(html.parser.HTMLParser):
    """Turns one spine document into text blocks, element positions and anchors."""

    def __init__(self, display_rules=None):
        super().__init__(convert_charrefs=True)
        self.display_rules = display_rules or {}
        self.parts = []
        self.total = 0
        self.blocks = []
        self.anchors = {}
        self.elements = {}
        self.root = _Element("", "", 0)
        self.stack = [self.root]
        self.skipping = []
        self.current = []
        self.current_len = 0
        self.current_kind = None
        self.ends_in_space = True

    def _block_start(self):
        return self.total + len(_PARAGRAPH_BREAK) if self.total else 0

    def _position(self):
        return self._block_start() + self.current_len

    def _flush(self):
        if not self.current:
            return
        text = "".join(self.current).rstrip(" \n")
        self.current = []
        self.current_len = 0
        self.ends_in_space = True
        kind = self.current_kind
        self.current_kind = None
        if not text:
            return
        start = self._block_start()
        if self.total:
            self.parts.append(_PARAGRAPH_BREAK)
        self.parts.append(text)
        self.total = start + len(text)
        self.blocks.append((start, self.total, kind))

    def _kind(self):
        for element in reversed(self.stack):
            if element.name in _HEADINGS:
                return "heading"
            if element.name == "li":
                return "list"
            if element.name == "blockquote":
                return "quote"
        return "paragraph"

    def _close_through(self, index):
        del self.stack[index:]
        self.stack[-1].open_text = False

    def _implicit_close(self, name):
        if name in _BLOCKS:
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i].name == "p":
                    self._close_through(i)
                    break
        family = _SIBLINGS.get(name)
        if family:
            same, bounds = family
            for i in range(len(self.stack) - 1, 0, -1):
                element = self.stack[i].name
                if element in bounds:
                    break
                if element == same or (name in ("td", "th") and element in ("td", "th")):
                    self._close_through(i)
                    break
        if name == "body":
            self.skipping = []

    def _display(self, name, attrs):
        # The cascade of the rules read: inline style, then tag.class, .class and tag
        values = dict(attrs)
        inline = _CSS_DISPLAY.search((values.get("style") or "").strip())
        if inline:
            return inline.group(1).lower()
        rules = self.display_rules
        if not rules:
            return None
        classes = (values.get("class") or "").split()
        for selector in [f"{name}.{c}" for c in classes] + [f".{c}" for c in classes] + [name]:
            if selector in rules:
                return rules[selector]
        return None

    def handle_starttag(self, tag, attrs):
        name = _local_name(tag)
        if name == "body":
            self.skipping = []
        if self.skipping:
            if name == self.skipping[-1]:
                self.skipping.append(name)
            return
        if name in _SKIPPED:
            self.skipping.append(name)
            return
        if name == "html":
            return
        display = self._display(name, attrs)
        block = name in _BLOCKS or display in _BLOCK_DISPLAYS
        if block:
            self._flush()
        self._implicit_close(name)
        if len(self.stack) >= MAX_ELEMENT_DEPTH:
            return
        parent = self.stack[-1]
        parent.open_text = False
        index = parent.counts.get(name, 0) + 1
        parent.counts[name] = index
        path = f"{parent.path}/{name}[{index}]"
        element = _Element(name, path, self._position(), block)
        self.elements.setdefault(path, element)
        for key, value in attrs:
            if key in ("id", "name") and value:
                self.anchors.setdefault(value, element.start)
        if name == "br":
            self._append_break()
        if name in _VOID:
            return
        if display == "none":
            self.skipping.append(name)
        else:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        name = _local_name(tag)
        if self.skipping:
            if name == self.skipping[-1]:
                self.skipping.pop()
            return
        if name == "html" or name in _VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].name == name:
                if any(e.block for e in self.stack[i:]):
                    self._flush()
                self._close_through(i)
                return

    def _append_break(self):
        if self.current:
            last = self.current[-1]
            self.current[-1] = last.rstrip(" ")
            self.current_len -= len(last) - len(self.current[-1])
            self.current.append("\n")
            self.current_len += 1
            self.ends_in_space = True

    def _in_pre(self):
        return any(element.name == "pre" for element in self.stack)

    def handle_data(self, data):
        if self.skipping:
            return
        data = data.replace("­", "")
        element = self.stack[-1]
        if not data.strip(" \t\r\n\f") and (element.name in _CONTAINERS or not element.name):
            if self.current and not self.ends_in_space:
                self.current.append(" ")
                self.current_len += 1
                self.ends_in_space = True
            return
        if self._in_pre():
            text = data.replace("\r\n", "\n")
        else:
            text = _COLLAPSE.sub(" ", data)
        if self.ends_in_space:
            text = text.lstrip(" \n")
        start = self._position()
        if not element.open_text:
            element.nodes.append([start, 0])
            element.open_text = True
        if not text:
            return
        element.nodes[-1][1] += len(text)
        if self.current_kind is None:
            self.current_kind = self._kind()
        self.current.append(text)
        self.current_len += len(text)
        self.ends_in_space = text[-1] in " \n"

    def unknown_decl(self, data):
        if data.startswith("CDATA["):
            self.handle_data(data[len("CDATA["):])


class _Corpus:
    """Word sequence of one or more spine documents, searchable with a way back to offsets."""

    def __init__(self, docs):
        self.words = []
        self.spans = []
        for doc in docs:
            for word, start, end in doc.tokens():
                self.words.append(word)
                self.spans.append((doc.number, start, end))
        self.starts = []
        at = 1
        for word in self.words:
            self.starts.append(at)
            at += len(word) + 1
        self.hay = " " + " ".join(self.words) + " "
        self.compact = None
        self.compact_starts = None

    def find_words(self, query):
        needle = " " + " ".join(query) + " "
        found = []
        at = self.hay.find(needle)
        while at >= 0:
            first = bisect.bisect_right(self.starts, at + 1) - 1
            found.append((first, first + len(query) - 1))
            at = self.hay.find(needle, at + 1)
        return found

    def find_compact(self, query):
        """Matches ignoring word breaks, for text the book splits or joins differently."""
        if self.compact is None:
            self.compact = "".join(self.words)
            self.compact_starts = []
            at = 0
            for word in self.words:
                self.compact_starts.append(at)
                at += len(word)
        needle = "".join(query)
        found = []
        at = self.compact.find(needle) if len(needle) >= MIN_COMPACT_LETTERS else -1
        while at >= 0:
            first = bisect.bisect_right(self.compact_starts, at) - 1
            last = bisect.bisect_right(self.compact_starts, at + len(needle) - 1) - 1
            found.append((first, last))
            at = self.compact.find(needle, at + 1)
        return found


class _Doc:
    """One spine document's text."""

    __slots__ = ("number", "name", "text", "blocks", "block_starts", "anchors", "elements", "_tokens", "_corpus")

    def __init__(self, number, name, raw, sheets=None):
        self.number = number
        self.name = name
        text = _decode(raw) if raw else ""
        parser = _DocParser(_doc_display_rules(text, name, sheets or {}))
        if raw:
            parser.feed(text)
            parser.close()
            parser._flush()
        self.text = "".join(parser.parts)
        self.blocks = parser.blocks
        self.block_starts = [block[0] for block in parser.blocks]
        self.anchors = parser.anchors
        self.elements = parser.elements
        self._tokens = None
        self._corpus = None

    def tokens(self):
        if self._tokens is None:
            self._tokens = _place_tokens(self.text)
        return self._tokens

    def corpus(self):
        if self._corpus is None:
            self._corpus = _Corpus([self])
        return self._corpus

    def block_at(self, offset):
        """Index of the block holding `offset`, or the nearest one before it."""
        return max(bisect.bisect_right(self.block_starts, offset) - 1, 0)

    def resolve(self, tail):
        """Offset an xpointer's part after DocFragment[N] points at, or None."""
        if not tail:
            return None
        match = _XPOINTER_TAIL.match(tail)
        steps = []
        for step in match.group(1).split("/"):
            if step:
                found = _XPOINTER_STEP.match(step)
                if not found:
                    return None
                steps.append(f"/{found.group(1)}[{found.group(2) or 1}]")
        element = self.elements.get("".join(steps))
        if element is None:
            return None
        offset = _xpointer_int(match.group(3) or "0")
        if "/text()" not in tail or offset is None:
            return element.start
        nodes = element.nodes
        if not nodes:
            return element.start
        index = _xpointer_int(match.group(2) or "1")
        if index is None:
            return element.start
        index -= 1
        if index >= len(nodes):
            start, length = nodes[-1]
            return start + length
        start, length = nodes[index]
        return start + min(offset, length)


def _xpointer_int(digits):
    return int(digits) if len(digits) <= MAX_XPOINTER_DIGITS else None


def _doc_display_rules(text, name, sheets):
    """Display rules of one document: its linked stylesheets, then its own style blocks."""
    rules = {}
    head_end = text.lower().find("</head")
    for link in _LINK_TAG.findall(text[:max(head_end, 0)]):
        href = _LINK_HREF.search(link)
        if href and "stylesheet" in link.lower():
            rules.update(sheets.get(_resolve_href(posixpath.dirname(name), href.group(1)), {}))
    for block in _STYLE_BLOCK.findall(text):
        rules.update(_display_rules(block))
    return rules


def _fragment(xpointer):
    """(N, rest) of an xpointer's DocFragment[N], or (None, None). A lone fragment has no [N]."""
    if not isinstance(xpointer, str):
        return None, None
    match = _XPOINTER.match(xpointer)
    if not match:
        return None, None
    number = _xpointer_int(match.group(1) or "1")
    return (None, None) if number is None else (number, match.group(2))


def _read_entry(archive, name):
    """Bytes of the entry called exactly `name`, or None when there is none."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_ENTRY_BYTES:
        raise EpubError("This book has a part too big to read, so its text can't be used.")
    with archive.open(info) as f:
        data = f.read(MAX_ENTRY_BYTES + 1)
    if len(data) > MAX_ENTRY_BYTES:
        raise EpubError("This book has a part too big to read, so its text can't be used.")
    return data


class Epub:
    """An opened ePub. Get one from open_epub()."""

    def __init__(self, path):
        self.path = str(path)
        self.title = ""
        self.author = ""
        self.spine = []
        self.toc = []
        self._regions = None
        self._sheets = {}
        self._raw = {}
        self._docs = {}
        self._book_corpus = None
        self._lock = threading.RLock()
        try:
            with zipfile.ZipFile(self.path) as archive:
                self._read(archive)
        except EpubError:
            raise
        except zipfile.BadZipFile:
            raise EpubError("This book's file isn't a readable ePub.") from None
        except OSError as e:
            raise EpubError(f"This book's file couldn't be read: {e.strerror or e}") from None
        except Exception:
            raise EpubError("This book's file isn't a readable ePub.") from None

    def _read(self, archive):
        opf_name = self._find_opf(archive)
        opf_raw = _read_entry(archive, opf_name)
        if opf_raw is None:
            raise EpubError("This book's file isn't a readable ePub.")
        opf = _parse_markup(opf_raw)
        base = posixpath.dirname(opf_name)

        metadata = opf.first("metadata") or opf
        title = metadata.first("title")
        self.title = title.text() if title else ""
        self.author = ", ".join(c.text() for c in metadata.all("creator") if c.text())

        manifest = {}
        for item in opf.all("item"):
            if item.attrs.get("id") and item.attrs.get("href"):
                manifest[item.attrs["id"]] = item
        spine_node = opf.first("spine")
        for itemref in spine_node.kids("itemref") if spine_node else []:
            item = manifest.get(itemref.attrs.get("idref"))
            if item is None:
                continue
            name = _resolve_href(base, item.attrs["href"])
            media = item.attrs.get("media-type", "").lower()
            self.spine.append(name)
            number = len(self.spine)
            if media in _HTML_TYPES or not media:
                try:
                    self._raw[number] = _read_entry(archive, name)
                except EpubError:
                    raise
                except Exception:
                    self._raw[number] = None
        if not self.spine:
            raise EpubError("This book's file isn't a readable ePub.")

        for item in manifest.values():
            if item.attrs.get("media-type", "").lower() == "text/css":
                self._read_sheet(archive, _resolve_href(base, item.attrs["href"]))
        self._read_toc(archive, manifest, spine_node, base)

    def _read_sheet(self, archive, name):
        try:
            raw = _read_entry(archive, name)
            if raw:
                self._sheets[name] = _display_rules(_decode(raw))
        except Exception:
            pass

    @staticmethod
    def _find_opf(archive):
        container = _read_entry(archive, "META-INF/container.xml")
        if container is not None:
            for rootfile in _parse_markup(container).all("rootfile"):
                name = rootfile.attrs.get("full-path")
                if name:
                    return name
        for name in sorted(archive.namelist()):
            if name.lower().endswith(".opf"):
                return name
        raise EpubError("This book's file isn't a readable ePub.")

    def _read_toc(self, archive, manifest, spine_node, base):
        tree = []
        try:
            nav = next((i for i in manifest.values() if "nav" in i.attrs.get("properties", "").split()), None)
            if nav is not None:
                name = _resolve_href(base, nav.attrs["href"])
                raw = _read_entry(archive, name)
                if raw:
                    tree = _nav_entries(_parse_markup(raw), posixpath.dirname(name))
            if not tree:
                ncx = manifest.get(spine_node.attrs.get("toc")) if spine_node else None
                if ncx is None:
                    ncx = next(
                        (i for i in manifest.values() if i.attrs.get("media-type") == "application/x-dtbncx+xml"),
                        None,
                    )
                if ncx is not None:
                    name = _resolve_href(base, ncx.attrs["href"])
                    raw = _read_entry(archive, name)
                    if raw:
                        tree = _ncx_entries(_parse_markup(raw), posixpath.dirname(name))
        except EpubError:
            raise
        except Exception:
            tree = []
        numbers = {}
        for i, name in enumerate(self.spine, 1):
            numbers.setdefault(name, i)
        self.toc = _flatten(tree, numbers)

    def doc(self, number):
        """The parsed spine document at 1-based `number`."""
        with self._lock:
            doc = self._docs.get(number)
            if doc is None:
                raw = self._raw.pop(number, None)
                try:
                    doc = _Doc(number, self.spine[number - 1], raw, self._sheets)
                except Exception:
                    doc = _Doc(number, self.spine[number - 1], None)
                self._docs[number] = doc
            return doc

    def _corpus(self, numbers):
        if len(numbers) == 1:
            with self._lock:
                return self.doc(numbers[0]).corpus()
        return _Corpus([self.doc(n) for n in numbers])

    def _whole_book(self):
        with self._lock:
            if self._book_corpus is None:
                self._book_corpus = _Corpus([self.doc(n) for n in range(1, len(self.spine) + 1)])
            return self._book_corpus

    # Locating highlights

    def locate(self, highlight):
        """Where `highlight` ({text, pos0, pos1}) sits in the book. See the module docstring."""
        text = highlight.get("text")
        if not isinstance(text, str):
            return dict(NOT_FOUND)
        stripped = text.strip()
        tokens = _place_tokens(stripped)
        if not tokens:
            return dict(NOT_FOUND)
        query = [token[0] for token in tokens]
        prefix = stripped[:tokens[0][1]]
        suffix = stripped[tokens[-1][2]:]

        fragment, tail = _fragment(highlight.get("pos0"))
        last_fragment, _rest = _fragment(highlight.get("pos1"))
        if fragment is not None and not 1 <= fragment <= len(self.spine):
            fragment = None
        if fragment is not None:
            last = last_fragment if last_fragment and fragment <= last_fragment <= len(self.spine) else fragment
            corpus = self._corpus(list(range(fragment, last + 1)))
            matches = corpus.find_words(query) or corpus.find_compact(query)
            if matches:
                target = self.doc(fragment).resolve(tail)
                match = self._nearest(corpus, matches, fragment, target)
                if target is not None and not self._in_block_of(corpus, match, fragment, target):
                    # A selection cut off mid-word only matches whole words somewhere else
                    closer = self._nearest(corpus, corpus.find_compact(query) or [match], fragment, target)
                    if self._in_block_of(corpus, closer, fragment, target):
                        match = closer
                return self._loc(corpus, match, prefix, suffix, "fragment")

        corpus = self._whole_book()
        matches = corpus.find_words(query) or corpus.find_compact(query)
        if not matches:
            return dict(NOT_FOUND)
        match = self._nearest(corpus, matches, fragment, None)
        return self._loc(corpus, match, prefix, suffix, "book")

    def _in_block_of(self, corpus, match, fragment, target):
        number, start, _end = corpus.spans[match[0]]
        doc = self.doc(fragment)
        return number == fragment and doc.block_at(start) == doc.block_at(target)

    @staticmethod
    def _nearest(corpus, matches, fragment, target):
        if fragment is None:
            return matches[0]

        def distance(match):
            number, start, _end = corpus.spans[match[0]]
            if number != fragment:
                return (abs(number - fragment), 0)
            return (0, abs(start - target) if target is not None else 0)

        return min(matches, key=distance)

    def _loc(self, corpus, match, prefix, suffix, where):
        doc_a, start, _end = corpus.spans[match[0]]
        doc_b, _start, end = corpus.spans[match[1]]
        # Takes in the punctuation around the highlight's words, when the book has it too
        text_a = self.doc(doc_a).text
        if prefix and text_a[max(start - len(prefix), 0):start] == prefix:
            start -= len(prefix)
        text_b = self.doc(doc_b).text
        if suffix and text_b[end:end + len(suffix)] == suffix:
            end += len(suffix)
        return {"found": True, "doc": doc_a, "start": start, "end_doc": doc_b, "end": end, "where": where}

    def _slice(self, doc_a, start, doc_b, end):
        """Text from one position to another, with a paragraph break between documents."""
        if doc_a == doc_b:
            return self.doc(doc_a).text[start:end]
        parts = [self.doc(doc_a).text[start:]]
        parts += [self.doc(n).text for n in range(doc_a + 1, doc_b)]
        parts.append(self.doc(doc_b).text[:end])
        return _PARAGRAPH_BREAK.join(part for part in parts if part)

    # Chapters

    def _is_part(self, entry):
        """Whether a table-of-contents entry only holds chapters, like a part or a book title in a
        collection, rather than being a chapter that its sub-entries divide into sections."""
        children = [child for child in entry["children"] if child["doc"] is not None]
        if not children or entry["doc"] is None or _CHAPTER_TITLE.match(entry["title"]):
            return False
        if len({child["doc"] for child in children}) > 1:
            return True
        doc = children[0]["doc"]
        if entry["doc"] != doc:
            return True
        text = self.doc(doc).text
        first = self._entry_offset(children[0])
        if len(text[self._entry_offset(entry):first].split()) >= MIN_LEAD_WORDS:
            return False
        after = next((e for e in self.toc[entry["index"] + 1:]
                      if e["doc"] is not None and e["level"] <= entry["level"]), None)
        end = self._entry_offset(after) if after and after["doc"] == doc else len(text)
        return len(text[first:end].split()) >= MIN_CHAPTER_WORDS * len(children)

    def regions(self):
        """Entries that divide the book, in reading order: chapters, numbered, and the parts
        holding them, with number None. Each table-of-contents entry gets its `region`."""
        with self._lock:
            if self._regions is None:
                self._regions = []
                count = 0

                def claim(entry, region):
                    entry["region"] = region
                    for child in entry["children"]:
                        claim(child, region)

                def visit(entry):
                    nonlocal count
                    self._regions.append(entry)
                    if self._is_part(entry):
                        entry["number"] = None
                        entry["region"] = entry
                        for child in entry["children"]:
                            visit(child)
                    else:
                        count += 1
                        entry["number"] = count
                        claim(entry, entry)

                for entry in self.toc:
                    if entry["parent"] is None:
                        visit(entry)
            return self._regions

    def chapters(self):
        """Chapters from the table of contents as {number, title, name, card, doc}."""
        return [
            {
                "number": entry["number"],
                "title": entry["title"],
                "name": chapter_name(entry["number"], entry["title"]),
                "card": chapter_card(entry["number"], entry["title"]),
                "doc": entry["doc"],
            }
            for entry in self.regions()
            if entry["number"] is not None
        ]

    def _entry_offset(self, entry):
        if not entry["anchor"]:
            return 0
        return self.doc(entry["doc"]).anchors.get(entry["anchor"], 0)

    def _entry_at(self, entries, doc, offset):
        """Index of the last entry starting at or before a position, or None."""
        best = None
        for i, entry in enumerate(entries):
            number = entry["doc"]
            if number is None:
                continue
            if number < doc or (number == doc and self._entry_offset(entry) <= offset):
                best = i
        return best

    def _chapter_bounds(self, doc, offset):
        """(start doc, start offset, end doc, end offset) of the chapter around a position."""
        regions = self.regions()
        # One entry doesn't divide the book
        if sum(entry["doc"] is not None for entry in regions) < 2:
            return (doc, 0, doc, len(self.doc(doc).text))
        index = self._entry_at(regions, doc, offset)
        if index is None:
            start_doc, start = 1, 0
        else:
            start_doc, start = regions[index]["doc"], self._entry_offset(regions[index])
        for entry in regions[(index + 1 if index is not None else 0):]:
            if entry["doc"] is None:
                continue
            position = (entry["doc"], self._entry_offset(entry))
            if position > (doc, offset):
                return (start_doc, start, position[0], position[1])
        last = len(self.spine)
        return (start_doc, start, last, len(self.doc(last).text))

    def _fits(self, loc):
        """Whether `loc` is a found location that fits this book. A cached one may not, once the
        file is replaced by another edition."""
        if not isinstance(loc, dict) or not loc.get("found"):
            return False
        first, start, second, end = (loc.get(field) for field in ("doc", "start", "end_doc", "end"))
        if not all(type(value) is int for value in (first, start, second, end)):
            return False
        if not 1 <= first <= second <= len(self.spine):
            return False
        doc_a, doc_b = self.doc(first), self.doc(second)
        if not doc_a.blocks or not doc_b.blocks:
            return False
        return 0 <= start <= len(doc_a.text) and 0 <= end <= len(doc_b.text)

    def chapter_at(self, loc):
        """Chapter or part holding `loc` as {number, title}, or None before the first one. A part
        has number None."""
        if not self._fits(loc):
            return None
        regions = self.regions()
        index = self._entry_at(regions, loc["doc"], loc["start"])
        if index is None:
            return None
        entry = regions[index]
        return {"number": entry["number"], "title": entry["title"]}

    def toc_title_at(self, loc):
        """Title of the deepest table-of-contents entry holding `loc`, or None."""
        if not self._fits(loc):
            return None
        index = self._entry_at(self.toc, loc["doc"], loc["start"])
        return None if index is None else self.toc[index]["title"]

    def chapter_for(self, title, loc):
        """Chapter {number, title} for an annotation's `chapter` title and location.

        The title decides when the table of contents has it once. A repeated title, or
        none, is settled by the location.
        """
        wanted = _COLLAPSE.sub(" ", title).strip() if isinstance(title, str) else ""
        matching = [entry for entry in self.toc if wanted and entry["title"] == wanted]
        if len(matching) == 1:
            self.regions()
            region = matching[0]["region"]
            return {"number": region["number"], "title": region["title"]}
        return self.chapter_at(loc)

    # Display and AI text

    def _walk_blocks(self, doc, block, step, bounds, count):
        """Up to `count` block texts going away from a block, staying inside `bounds`."""
        start_doc, start, end_doc, end = bounds
        out = []
        number = doc
        index = block + step
        while len(out) < count:
            current = self.doc(number)
            if not 0 <= index < len(current.blocks):
                number += step
                if number < start_doc or number > end_doc:
                    break
                index = 0 if step > 0 else len(self.doc(number).blocks) - 1
                continue
            block_start, block_end, _kind = current.blocks[index]
            if step < 0 and (number, block_end) <= (start_doc, start):
                break
            if step > 0 and (number, block_start) >= (end_doc, end):
                break
            out.append(current.text[block_start:block_end])
            index += step
        return out

    def passage(self, loc, more=0):
        """Text around a location for display.

        `pre`, `mark` and `post` make up the highlight's own paragraphs. `before` and `after`
        are the neighbouring paragraphs. `before2` and `after2` are the 1 + `more` paragraphs
        beyond those, for Show more. `endCh` says the chapter ends right after `after`, and
        `endCh2` that it ends right after `after2`. Context stays inside the chapter.
        """
        if not self._fits(loc):
            return empty_passage()
        more = max(int(more or 0), 0)
        first, start, second, end = loc["doc"], loc["start"], loc["end_doc"], loc["end"]
        doc_a, doc_b = self.doc(first), self.doc(second)
        block_a = doc_a.block_at(start)
        block_b = doc_b.block_at(max(end - 1, 0))
        wanted = more + 2
        before = self._walk_blocks(first, block_a, -1, self._chapter_bounds(first, start), wanted)
        after = self._walk_blocks(second, block_b, 1, self._chapter_bounds(second, max(end - 1, 0)), wanted + 1)
        return {
            "found": True,
            "before2": _PARAGRAPH_BREAK.join(reversed(before[1:])),
            "before": before[0] if before else "",
            "pre": doc_a.text[doc_a.blocks[block_a][0]:start],
            "mark": self._slice(first, start, second, end),
            "post": doc_b.text[end:doc_b.blocks[block_b][1]],
            "after": after[0] if after else "",
            "after2": _PARAGRAPH_BREAK.join(after[1:wanted]),
            "endCh": len(after) < 2,
            "endCh2": len(after) <= wanted,
        }

    def _words_before(self, doc, offset, count):
        parts = []
        while doc >= 1 and count > 0:
            text = self.doc(doc).text[:offset]
            spans = [match.start() for match in _NON_SPACE.finditer(text)]
            if len(spans) >= count:
                parts.append(text[spans[-count]:])
                break
            if spans:
                parts.append(text[spans[0]:])
            count -= len(spans)
            doc -= 1
            offset = len(self.doc(doc).text) if doc >= 1 else 0
        return _PARAGRAPH_BREAK.join(part.rstrip() for part in reversed(parts))

    def _words_after(self, doc, offset, count):
        parts = []
        while doc <= len(self.spine) and count > 0:
            text = self.doc(doc).text[offset:]
            spans = [match.end() for match in _NON_SPACE.finditer(text)]
            if len(spans) >= count:
                parts.append(text[:spans[count - 1]])
                break
            if spans:
                parts.append(text[:spans[-1]])
            count -= len(spans)
            doc += 1
            offset = 0
        return _PARAGRAPH_BREAK.join(part.lstrip() for part in parts)

    def ai_context(self, loc, mode=MODE_WORDS):
        """(before, after) text the AI reads around a location, for one of AI_MODES."""
        if not self._fits(loc):
            return ("", "")
        first, start, second, end = loc["doc"], loc["start"], loc["end_doc"], loc["end"]
        if mode == MODE_BOOK:
            last = len(self.spine)
            before = self._slice(1, 0, first, start)
            after = self._slice(second, end, last, len(self.doc(last).text))
        elif mode == MODE_CHAPTER:
            start_doc, chapter_start, _a, _b = self._chapter_bounds(first, start)
            _c, _d, end_doc, chapter_end = self._chapter_bounds(second, max(end - 1, 0))
            before = self._slice(start_doc, chapter_start, first, start)
            after = self._slice(second, end, end_doc, chapter_end)
        else:
            before = self._words_before(first, start, CONTEXT_WORDS)
            after = self._words_after(second, end, CONTEXT_WORDS)
        return (before.strip(), after.strip())


def _resolve_href(base, href):
    """Name of the zip entry an OPF or table-of-contents link points at, without its #anchor."""
    path = urllib.parse.unquote(href.split("#", 1)[0])
    return posixpath.normpath(posixpath.join(base, path))


def _ncx_entries(root, base):
    def read(point):
        label = point.first("navlabel") if point.kids("navlabel") else None
        title = label.text() if label else ""
        content = point.kids("content")
        source = content[0].attrs.get("src", "") if content else ""
        return {"title": title, "href": source, "base": base, "children": [read(p) for p in point.kids("navpoint")]}

    nav_map = root.first("navmap")
    return [read(point) for point in nav_map.kids("navpoint")] if nav_map else []


def _nav_entries(root, base):
    navs = root.all("nav")
    nav = next((n for n in navs if "toc" in n.attrs.get("type", "").split()), navs[0] if navs else None)
    if nav is None:
        return []

    def read_list(listing):
        out = []
        for item in listing.kids("li"):
            link = next((c for c in item.children if isinstance(c, _Node) and c.tag in ("a", "span")), None)
            nested = next((c for c in item.children if isinstance(c, _Node) and c.tag in ("ol", "ul")), None)
            out.append(
                {
                    "title": link.text() if link else "",
                    "href": link.attrs.get("href", "") if link else "",
                    "base": base,
                    "children": read_list(nested) if nested else [],
                }
            )
        return out

    listing = nav.first("ol") or nav.first("ul")
    return read_list(listing) if listing else []


def _flatten(tree, numbers):
    """Entries in reading order as {title, level, index, doc, anchor, parent, children}, skipping nameless ones."""
    flat = []

    def add(entry, level, parent):
        href = entry["href"]
        name = _resolve_href(entry["base"], href) if href else None
        anchor = urllib.parse.unquote(href.split("#", 1)[1]) if "#" in href else None
        record = {
            "title": entry["title"], "level": level, "doc": numbers.get(name), "anchor": anchor,
            "parent": parent, "children": [],
        }
        if record["title"]:
            record["index"] = len(flat)
            flat.append(record)
            if parent is not None:
                parent["children"].append(record)
        return record

    def visit(entries, level, parent):
        for entry in entries:
            if level == 1 and not entry["title"]:
                continue
            record = add(entry, level, parent)
            visit(entry["children"], level + 1, record if record["title"] else parent)
            # An entry with no link of its own starts where its first child does
            if record["doc"] is None:
                child = next((c for c in record["children"] if c["doc"]), None)
                if child:
                    record["doc"], record["anchor"] = child["doc"], child["anchor"]

    visit(tree, 1, None)
    return flat


_cache = OrderedDict()
_cache_lock = threading.Lock()


def open_epub(path):
    """The Epub at `path`, cached by path and modification time. Raises EpubError."""
    path = str(path)
    try:
        stat = os.stat(path)
    except OSError as e:
        raise EpubError(f"This book's file couldn't be read: {e.strerror or e}") from None
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        hit = _cache.get(path)
        if hit and hit[0] == stamp:
            _cache.move_to_end(path)
            return hit[1]
    book = Epub(path)
    with _cache_lock:
        _cache[path] = (stamp, book)
        _cache.move_to_end(path)
        while len(_cache) > CACHE_BOOKS:
            _cache.popitem(last=False)
    return book
