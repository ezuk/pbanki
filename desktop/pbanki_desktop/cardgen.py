"""Builds AI requests for flashcards and parses the replies.

The first part is a port of the plugin's cardgen.lua, function for function. Parity tests
compare it with the Lua module byte for byte, so its strings must not be reworded here
alone. The desktop request kinds follow. Parse functions return (result, None) or
(None, error message).
"""

import json
import math
import re

from . import prompts

PROMPTS = prompts.builtin()

REPLY_FORMATS = {
    "questions": """Reply with JSON only, no other text, in this shape:
{"cards": [{"front": "question", "back": "answer"}]}""",
    "cloze": """Reply with JSON only, no other text, in this shape:
{"cards": [{"front": "Sentence quoted from the text with {{c1::hidden words}}.", "back": ""}]}""",
    "definition": """Reply with JSON only, no other text, in this shape:
{"term": "term", "definition": "definition"}""",
    "connections": """Reply with JSON only, no other text, in this shape:
{"connections": [{"title": "short name", "front": "question", "back": "answer", "sources": [1, 3]}]}""",
    "ideas": """Reply with JSON only, no other text, in this shape:
{"chapters": [{"chapter": "chapter name", "big": "main idea", "points": [{"text": "key point", "covered": true}, {"text": "key point", "covered": false, "idea": 0}]}], "ideas": [{"title": "short name", "chapter": "chapter name", "note": "why it matters", "quote": "words from the book", "front": "question", "back": "answer"}]}""",
    "rewrite": """Reply with JSON only, no other text, in this shape:
{"front": "question", "back": "answer"}""",
}

LATER_RESEARCH_FORMAT = """If a "Since publication" line in the book notes questions what this highlight claims, also add a top-level "later_research" string to the JSON: one or two plain sentences on what later research found, taken from that line. Otherwise leave it out. Never write it from your own knowledge, or from a line about a different claim."""

# Lua's %s in the C locale
_C_SPACE = " \t\n\v\f\r"
_CLOZE_RE = re.compile(r"\{\{c([0-9]+)::")
_CLOZE_BLOCK_RE = re.compile(r"\{\{c[0-9]+::(.*?)\}\}", re.S)


def _is_set(value):
    # Lua truth: everything but nil and false
    return value is not None and value is not False


def request_kind(request):
    """The kind of card a request asks for: "questions", "cloze" or "definition"."""
    if _is_set(request.get("definition")):
        return "definition"
    if _is_set(request.get("cloze")):
        return "cloze"
    return "questions"


def system_prompt(prompt, kind):
    """System prompt for `kind`: the instructions from `prompt` (a dict by kind),
    or the built-in ones, then the reply format."""
    text = prompt.get(kind) if prompt else None
    if not isinstance(text, str) or not text.strip(_C_SPACE):
        text = PROMPTS[kind]
    return text + "\n\n" + REPLY_FORMATS[kind]


SYSTEM_PROMPT = system_prompt(None, "questions")
CLOZE_PROMPT = system_prompt(None, "cloze")
DEFINITION_PROMPT = system_prompt(None, "definition")


def _line(label, value):
    if value is None or value == "":
        return None
    return f"{label}: {value}"


def _card_line(card):
    back = card.get("back")
    if back == "" or back is None:
        return card["front"]
    return card["front"] + " → " + back


def is_cloze(text):
    """Whether `text` has a cloze deletion such as {{c1::answer}}."""
    return text is not None and _CLOZE_RE.search(text) is not None


def _cloze_inner(match):
    inner = match.group(1)
    split = inner.find("::")
    return inner[:split] if split >= 0 else inner


def show_cloze(text):
    """`text` with each cloze deletion shown as [answer]."""
    return _CLOZE_BLOCK_RE.sub(lambda m: "[" + _cloze_inner(m) + "]", text)


def cloze_mark(before, all_text):
    """What the cloze button inserts at the cursor, given the text before it:
    "}}" to close an open deletion, else the next opening such as "{{c2::"."""
    last_open = -1
    for match in _CLOZE_RE.finditer(before):
        last_open = match.start()
    if last_open >= 0 and before.find("}}", last_open) < 0:
        return "}}"
    highest = 0
    for match in _CLOZE_RE.finditer(all_text):
        highest = max(highest, int(match.group(1)))
    return "{{c" + str(highest + 1) + "::"


def was_edited(card):
    """Whether the reader changed an AI card, which keeps its original text in `suggested`.
    Answering a question the AI left unanswered is not a change."""
    suggested = card.get("suggested")
    if suggested is None:
        return False
    if suggested.get("front") != card.get("front"):
        return True
    return suggested.get("back") != card.get("back") and (
        suggested.get("back") != "" or is_cloze(suggested.get("front"))
    )


def _tagged(tag, body, intro=None):
    block = f"<{tag}>\n{body}\n</{tag}>"
    if intro:
        return intro + "\n" + block
    return block


_QUESTIONS_ONLY_INSTRUCTION = (
    'Write only the questions and leave every back empty (""). '
    "The reader picks the questions that matter to them and answers them in their own words."
)


def questions_only(request):
    """Whether `request` asks for questions without answers."""
    return request.get("questions_only") is True and request_kind(request) == "questions"


def build_messages(h, request, companion=None, position_note=None):
    """Messages for the AI: the Book Companion first, then the instructions, then the
    highlight and the task. See cardgen.lua's buildMessages for the fields.

    Beyond the plugin: `request["later_research"]`, true to ask for the later_research
    field, which is only added when Companion notes are sent, for questions and cloze."""
    messages = []
    if companion:
        messages.append({
            "role": "system",
            "content": "Reference notes about this book (background only, never quiz on them directly):\n\n"
            + _tagged("book_notes", companion),
            "cache": True,
        })
    kind = request_kind(request)
    instructions = system_prompt(request.get("prompt"), kind)
    if companion and request.get("later_research") and kind != "definition":
        instructions += "\n\n" + LATER_RESEARCH_FORMAT
    messages.append({"role": "system", "content": instructions})

    parts = []
    percent = h.get("percent")
    about = [
        _line("Book", h.get("book")),
        _line("Author", h.get("author")),
        _line("Chapter", h.get("chapter")),
        "Position in book: %d%%" % math.floor(percent + 0.5) if percent is not None else None,
    ]
    about = [item for item in about if item is not None]
    if about:
        parts.append(_tagged("book", "\n".join(about)))
    before, after = h.get("before") or "", h.get("after") or ""
    if before != "" or after != "":
        parts.append(_tagged("passage", before + " «" + h["text"] + "» " + after,
                             "Passage around the highlight (the highlight is between « and »):"))
    parts.append(_tagged("highlight", h["text"], "Highlighted text:"))
    if h.get("note"):
        parts.append(_tagged("reader_note", h["note"], "The reader's note on this highlight. It says what "
                             "they care about here, so let it steer which ideas you pick and how you ask:"))
    if position_note:
        parts.append(position_note)
    if request.get("recent"):
        parts.append(recent_section(request["recent"]))
    if request.get("existing"):
        lines, edits = [], []
        for card in request["existing"]:
            lines.append("- " + _card_line(card))
            if was_edited(card):
                edits.append("- You suggested: " + _card_line(card["suggested"])
                             + "\n  The reader changed it to: " + _card_line(card))
        if _is_set(request.get("definition")):
            parts.append(_tagged("existing_cards", "\n".join(lines),
                                 "The reader already has these cards for this term. Write a different definition:"))
        else:
            parts.append(_tagged("existing_cards", "\n".join(lines),
                                 "The reader already has these cards. Write new ones that ask something else, "
                                 "in the same style and level of detail:"))
        if edits:
            parts.append(_tagged("edited_cards", "\n".join(edits),
                                 "The reader rewrote some of your earlier suggestions. "
                                 "Notice what they changed and write new cards the way they would:"))
    if request.get("rejected"):
        lines = ["- " + _card_line(card) for card in request["rejected"]]
        parts.append(_tagged("removed_cards", "\n".join(lines),
                             "The reader removed these suggested cards. Do not write them again or close "
                             "variants of them. Notice what they have in common (for example too easy, too long, "
                             "or about a side detail) and avoid that, while still basing new cards on the highlight:"))
    if request.get("guidance"):
        parts.append(_tagged("reader_request", request["guidance"], "The reader asked for these cards:")
                     + "\nFollow it, within the rules above.")
    if questions_only(request):
        parts.append(_QUESTIONS_ONLY_INSTRUCTION)
    if not _is_set(request.get("definition")):
        parts.append(count_instruction(request))
    parts.append("Reply with the JSON only, in the shape given in the instructions.")

    messages.append({"role": "user", "content": "\n\n".join(parts)})
    return messages


# Longest quote shown with a past card
_HISTORY_QUOTE_CHARS = 300


def _shorten(text, limit):
    # Byte lengths, as in Lua. A cut through a character drops the partial bytes.
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    cut = re.sub(rb"[ \t\n\v\f\r]+[^ \t\n\v\f\r]*\Z", b"", data[:limit])
    return cut.decode("utf-8", "ignore") + " …"


def history_entry(h, card):
    """A saved card for the book's card history: the card, its AI original when
    the AI wrote it, and the highlight it came from."""
    entry = {"front": card["front"], "back": card["back"]}
    suggested = card.get("suggested")
    if suggested is not None:
        entry["suggested"] = {"front": suggested.get("front"), "back": suggested.get("back")}
    if h.get("text") is not None:
        entry["quote"] = h["text"]
    if h.get("note"):
        entry["note"] = h["note"]
    return entry


def append_history(history, entries, limit):
    """Card history with `entries` added at the end, keeping the last `limit`."""
    combined = [*(history or []), *entries]
    return combined[max(0, len(combined) - limit):]


def pick_recent(history, limit):
    """Up to `limit` history entries to show the AI: the latest edited AI cards
    first, then the latest others, in history order."""
    chosen = set()

    def take(want_edited):
        for i in range(len(history) - 1, -1, -1):
            if len(chosen) >= limit:
                return
            if i not in chosen and was_edited(history[i]) == want_edited:
                chosen.add(i)

    take(True)
    take(False)
    return [history[i] for i in sorted(chosen)]


def recent_section(recent):
    """Past cards from this book, grouped by highlight, as a style guide for the AI."""
    blocks = []
    block, last_quote = None, None
    for entry in recent:
        quote = entry.get("quote")
        if block is None or quote != last_quote:
            last_quote = quote
            block = ["Highlight: «" + _shorten(quote or "", _HISTORY_QUOTE_CHARS) + "»"]
            if entry.get("note") is not None:
                block.append("Reader's note: " + entry["note"])
            blocks.append(block)
        if was_edited(entry):
            block.append("- You suggested: " + _card_line(entry["suggested"])
                         + "\n  The reader changed it to: " + _card_line(entry))
        else:
            block.append("- " + _card_line(entry))
    texts = ["\n".join(b) for b in blocks]
    return _tagged("recent_cards", "\n\n".join(texts),
                   "Cards this reader saved recently from this book. They show what the reader chooses to ask "
                   "about and how they word questions and answers, including how they rewrote your suggestions. "
                   "Write the new cards the same way, but do not repeat these:")


def count_instruction(request):
    """How many cards to write, as an instruction for the AI."""
    maximum = request.get("max")
    if maximum is not None:
        if maximum <= 1:
            return "Write 1 card."
        return ("Decide how many cards this highlight deserves, from 1 to %d. "
                "A short, focused highlight with one idea gets 1 card. "
                "Write more only when it holds several distinct facts or ideas worth remembering, one card each."
                % maximum)
    count = request.get("count")
    if count == 1:
        return "Write 1 card."
    if _is_set(request.get("cloze")):
        return "Write %d cards, each hiding different words." % count
    return "Write %d cards, each asking something different." % count


def card_limit(request):
    """Most cards to accept from the reply."""
    maximum = request.get("max")
    return maximum if maximum is not None else request.get("count")


def _trim(text):
    return text.strip(_C_SPACE)


_FENCE_RE = re.compile(r"```[A-Za-z0-9]*[ \t\n\v\f\r]*(.*?)[ \t\n\v\f\r]*```", re.S)
_OPENER_RE = re.compile(r"[\[{]")


def extract_json(text):
    """The JSON part of a reply, without code fences or surrounding prose."""
    if not isinstance(text, str):
        return None
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)
    opener = _OPENER_RE.search(text)
    if not opener:
        return None
    first = opener.start()
    closer = "}" if text[first] == "{" else "]"
    last = text.rfind(closer, first)
    if last < 0:
        return None
    return text[first:last + 1]


def _decoded(text, decode):
    # The decoded JSON part of a reply when it is an object or a list, else None
    found = extract_json(text)
    if found is None:
        return None
    try:
        data = decode(found)
    except Exception:
        return None
    return data if isinstance(data, (dict, list)) else None


def parse_cards(text, count, decode=json.loads):
    """Cards from a reply: a list of {front, back}, at most `count`."""
    found = extract_json(text)
    if found is None:
        return None, "The AI reply had no cards."
    try:
        data = decode(found)
    except Exception:
        return None, "The AI reply could not be read."
    if not isinstance(data, (dict, list)):
        return None, "The AI reply could not be read."
    items = data.get("cards") if isinstance(data, dict) else None
    if items is None or items is False:
        items = data
    if not isinstance(items, (dict, list)):
        return None, "The AI reply had no cards."
    cards = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and isinstance(item.get("front"), str):
            front = _trim(item["front"])
            back = _trim(item["back"]) if isinstance(item.get("back"), str) else ""
            if front != "":
                cards.append({"front": front, "back": back})
        if count is not None and len(cards) >= count:
            break
    if not cards:
        return None, "The AI reply had no cards."
    return cards, None


_WORD_RE = re.compile(r"[A-Za-z0-9\u0080-\U0010ffff']+")
_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def words(text):
    """Lowercase words of `text`, ignoring punctuation, joined by single spaces.
    Every non-ASCII character counts as a word character, and only ASCII letters
    are lowered, as Lua's byte patterns do."""
    text = re.sub("[‘’]", "'", text)
    text = re.sub("[–—“”…]", " ", text)
    text = text.replace(" ", " ").replace("­", "")
    found = []
    for word in _WORD_RE.findall(text):
        word = word.strip("'")
        if word:
            found.append(word.translate(_ASCII_LOWER))
    return " ".join(found)


def _plain_cloze(front):
    return _CLOZE_BLOCK_RE.sub(_cloze_inner, front)


def quotes_text(front, source):
    """Whether cloze `front`, without its marks, quotes `source` word for word."""
    quote = words(_plain_cloze(front))
    return quote != "" and (" " + quote + " ") in (" " + words(source) + " ")


def expand_cloze(front, text):
    """Cloze `front` widened to the whole highlight `text` when it quotes only
    part of it, word for word."""
    plain = _plain_cloze(front)
    first = text.find(plain)
    if first < 0 or len(plain) == len(text):
        return front
    return text[:first] + front + text[first + len(plain):]


def parse_definition(text, decode=json.loads):
    """The term and definition in a reply as two cards: term → definition and
    definition → term."""
    data = _decoded(text, decode)
    if data is None:
        return None, "The AI reply could not be read."
    fields = data if isinstance(data, dict) else {}
    term = _trim(fields["term"]) if isinstance(fields.get("term"), str) else ""
    definition = _trim(fields["definition"]) if isinstance(fields.get("definition"), str) else ""
    if term == "" or definition == "":
        return None, "The AI reply had no definition."
    return [
        {"front": term, "back": definition},
        {"front": definition, "back": term},
    ], None


def parse_reply(text, request, h, decode=json.loads):
    """Cards from a reply to `request` for highlight `h`. Cloze cards that do not quote
    the book are dropped; the rest cover the whole highlight. Questions-only cards come
    without answers."""
    if _is_set(request.get("definition")):
        return parse_definition(text, decode)
    limit = card_limit(request)
    if not _is_set(request.get("cloze")):
        cards, err = parse_cards(text, limit, decode)
        if cards and questions_only(request):
            for card in cards:
                card["back"] = ""
        return cards, err
    cards, err = parse_cards(text, None, decode)
    if not cards:
        return None, err
    source = " ".join([h.get("before") or "", h.get("text") or "", h.get("after") or ""])
    kept = []
    for card in cards:
        if is_cloze(card["front"]) and quotes_text(card["front"], source):
            card["front"] = expand_cloze(card["front"], h.get("text") or "")
            kept.append(card)
        if limit is not None and len(kept) >= limit:
            break
    if not kept:
        return None, "The AI's cloze cards did not quote the book. Try Suggest again."
    return kept, None


# Desktop additions


def parse_later_research(text, decode=json.loads):
    """The reply's optional later_research sentence, or None."""
    data = _decoded(text, decode)
    if not isinstance(data, dict) or not isinstance(data.get("later_research"), str):
        return None
    return _trim(data["later_research"]) or None


def _about_block(book):
    book = book or {}
    about = [item for item in (_line("Book", book.get("book")), _line("Author", book.get("author"))) if item]
    return [_tagged("book", "\n".join(about))] if about else []


def _notes_message(companion):
    return {
        "role": "system",
        "content": "Reference notes about this book (background only, never quiz on them directly):\n\n"
        + _tagged("book_notes", companion),
        "cache": True,
    }


def _system_messages(kind, prompt, companion):
    messages = [_notes_message(companion)] if companion else []
    messages.append({"role": "system", "content": system_prompt(prompt, kind)})
    return messages


def _highlight_list(highlights):
    rows = []
    for number, h in enumerate(highlights, 1):
        where = h.get("chapter") or "Unknown chapter"
        if h.get("page") is not None:
            where += f" · p. {h['page']}"
        row = f"{number}. [{where}] {h['text']}"
        if h.get("note"):
            row += "\n   Reader's note: " + h["note"]
        rows.append(row)
    return "\n".join(rows)


def _card_list(cards):
    return "\n".join("- " + _card_line(card) for card in cards)


def build_connections(highlights, request, companion=None, book=None):
    """Messages asking for connections across numbered `highlights`, each a dict of
    text, chapter (number and title), optional page and note.
    `request`: count, and optional prompt, guidance, existing, rejected."""
    parts = _about_block(book)
    parts.append(_tagged("highlights", _highlight_list(highlights), "The reader's highlights, numbered:"))
    if request.get("existing"):
        parts.append(_tagged("existing_cards", _card_list(request["existing"]),
                             "The reader already has these connection cards. Find different connections:"))
    if request.get("rejected"):
        parts.append(_tagged("removed_cards", _card_list(request["rejected"]),
                             "The reader removed these suggested connections. Do not write them again or close "
                             "variants of them:"))
    if request.get("guidance"):
        parts.append(_tagged("reader_request", request["guidance"], "The reader asked for these connections:")
                     + "\nFollow it, within the rules above.")
    count = request.get("count")
    if count is not None:
        parts.append("Find up to %d connections. Fewer is fine when the highlights don't support more." % count)
    parts.append("Reply with the JSON only, in the shape given in the instructions.")
    messages = _system_messages("connections", request.get("prompt"), companion)
    messages.append({"role": "user", "content": "\n\n".join(parts)})
    return messages


def _source_numbers(value, highlight_count):
    # Valid highlight numbers, each once, in the reply's order
    numbers = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            continue
        if isinstance(item, float) and not math.isfinite(item):
            continue
        if item != int(item):
            continue
        number = int(item)
        if 1 <= number <= highlight_count and number not in numbers:
            numbers.append(number)
    return numbers


def _text_field(item, key):
    value = item.get(key)
    return _trim(value) if isinstance(value, str) else ""


def parse_connections(text, highlight_count, limit=None, decode=json.loads):
    """Connections from a reply: a list of {title, front, back, sources}. Answers citing
    fewer than two valid highlight numbers are dropped. An empty list is a valid reply."""
    data = _decoded(text, decode)
    if data is None:
        return None, "The AI reply could not be read."
    items = data.get("connections") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None, "The AI reply had no connections."
    connections = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sources = _source_numbers(item.get("sources"), highlight_count)
        front, back = _text_field(item, "front"), _text_field(item, "back")
        if len(sources) < 2 or front == "" or back == "":
            continue
        connections.append({"title": _text_field(item, "title") or front, "front": front,
                            "back": back, "sources": sources})
        if limit is not None and len(connections) >= limit:
            break
    return connections, None


def build_ideas(highlights, request, companion, book=None):
    """Messages asking for key ideas: `companion` is the sliced Companion, `highlights`
    every highlight with its chapter. `request`: optional prompt, guidance, count, rejected, existing."""
    parts = _about_block(book)
    if highlights:
        parts.append(_tagged("highlights", _highlight_list(highlights), "Everything the reader highlighted:"))
    else:
        parts.append("The reader has no highlights in this book yet.")
    if request.get("existing"):
        parts.append(_tagged("existing_ideas", _card_list(request["existing"]),
                             "These key ideas are already written. Do not write them again or close variants of them:"))
    if request.get("rejected"):
        parts.append(_tagged("removed_cards", _card_list(request["rejected"]),
                             "The reader removed these suggested ideas. Do not write them again or close "
                             "variants of them:"))
    if request.get("guidance"):
        parts.append(_tagged("reader_request", request["guidance"], "The reader asked for these ideas:")
                     + "\nFollow it, within the rules above.")
    if request.get("count") is not None:
        parts.append("Write at most %d ideas." % request["count"])
    parts.append("Reply with the JSON only, in the shape given in the instructions.")
    messages = _system_messages("ideas", request.get("prompt"), companion)
    messages.append({"role": "user", "content": "\n\n".join(parts)})
    return messages


def parse_ideas(text, book_text=None, chapter_allowed=None, decode=json.loads):
    """Chapter notes and key ideas from a reply: {"chapters": [...], "ideas": [...]}.

    Each quote is checked word for word against `book_text`; one that isn't found becomes
    "". Chapters and ideas for which `chapter_allowed(name)` is false are dropped, so a
    point's `idea` is renumbered, or removed when its idea was dropped."""
    data = _decoded(text, decode)
    if not isinstance(data, dict):
        return None, "The AI reply could not be read."
    raw_chapters, raw_ideas = data.get("chapters"), data.get("ideas")
    if not isinstance(raw_chapters, list) and not isinstance(raw_ideas, list):
        return None, "The AI reply had no key ideas."
    allowed = chapter_allowed or (lambda _name: True)
    source = " " + words(book_text) + " " if book_text else ""

    ideas, new_index = [], {}
    for index, item in enumerate(raw_ideas if isinstance(raw_ideas, list) else []):
        if not isinstance(item, dict):
            continue
        chapter = _text_field(item, "chapter")
        front, back = _text_field(item, "front"), _text_field(item, "back")
        if front == "" or back == "" or not allowed(chapter):
            continue
        quote = _text_field(item, "quote")
        spoken = words(_plain_cloze(quote))
        if not spoken or (" " + spoken + " ") not in source:
            quote = ""
        new_index[index] = len(ideas)
        ideas.append({"title": _text_field(item, "title") or front, "chapter": chapter,
                      "note": _text_field(item, "note"), "quote": quote, "front": front, "back": back})

    chapters = []
    for item in raw_chapters if isinstance(raw_chapters, list) else []:
        if not isinstance(item, dict):
            continue
        name = _text_field(item, "chapter")
        if name == "" or not allowed(name):
            continue
        points = []
        for raw in item.get("points") if isinstance(item.get("points"), list) else []:
            if not isinstance(raw, dict) or _text_field(raw, "text") == "":
                continue
            point = {"text": _text_field(raw, "text"), "covered": raw.get("covered") is True}
            idea = raw.get("idea")
            if not point["covered"] and isinstance(idea, int) and not isinstance(idea, bool) and idea in new_index:
                point["idea"] = new_index[idea]
            points.append(point)
        chapters.append({"chapter": name, "big": _text_field(item, "big"), "points": points})
    return {"chapters": chapters, "ideas": ideas}, None


def build_rewrite(card, h, instruction=None, prompt=None, companion=None):
    """Messages asking to rewrite one card, from highlight `h`."""
    parts = []
    about = [item for item in (_line("Book", h.get("book")), _line("Author", h.get("author")),
                               _line("Chapter", h.get("chapter"))) if item]
    if about:
        parts.append(_tagged("book", "\n".join(about)))
    before, after = h.get("before") or "", h.get("after") or ""
    if h.get("text"):
        if before != "" or after != "":
            parts.append(_tagged("passage", before + " «" + h["text"] + "» " + after,
                                 "Passage around the highlight (the highlight is between « and »):"))
        parts.append(_tagged("highlight", h["text"], "Highlighted text:"))
    if h.get("note"):
        parts.append(_tagged("reader_note", h["note"], "The reader's note on this highlight:"))
    parts.append(_tagged("card", "Front: " + card["front"] + "\nBack: " + (card.get("back") or ""),
                         "The card to rewrite:"))
    if instruction:
        parts.append(_tagged("reader_request", instruction, "The reader asked for this:")
                     + "\nFollow it, within the rules above.")
    parts.append("Reply with the JSON only, in the shape given in the instructions.")
    messages = _system_messages("rewrite", prompt, companion)
    messages.append({"role": "user", "content": "\n\n".join(parts)})
    return messages


def parse_rewrite(text, original=None, decode=json.loads):
    """The rewritten {front, back} from a reply. A cloze card must stay a cloze card."""
    data = _decoded(text, decode)
    if not isinstance(data, dict):
        return None, "The AI reply could not be read."
    front, back = _text_field(data, "front"), _text_field(data, "back")
    if front == "":
        return None, "The AI reply had no card."
    if original and is_cloze(original.get("front")) and not is_cloze(front):
        return None, "The AI's rewrite lost the hidden words. Try again."
    return {"front": front, "back": back}, None


def build_definition(term, h, request=None, companion=None):
    """Messages asking to define `term`, with the passage around it from highlight `h`."""
    full = {**(request or {}), "definition": True}
    return build_messages({**h, "text": term}, full, companion)


_DEFINE_RE = re.compile(r"^define\b(?![-'’])[ \t:]*(?=\S)", re.I)


def strip_define(term):
    """`term` without any "define" the AI already added."""
    return _DEFINE_RE.sub("", term)


def definition_cards(text, decode=json.loads):
    """The pair of definition cards: `Define <term>` → definition and definition → `<term>`."""
    cards, err = parse_definition(text, decode)
    if not cards:
        return None, err
    term = strip_define(cards[0]["front"])
    definition = cards[0]["back"]
    return [
        {"front": "Define " + term, "back": definition},
        {"front": definition, "back": term},
    ], None
