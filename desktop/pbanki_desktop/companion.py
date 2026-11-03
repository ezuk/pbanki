"""Book Companions: finding the sidecar, slicing it, chapter notes, and creating one.

slice_text and position_note port assistant_companion_slicer.lua, with parity tests
against it. Chapter matching is checked against real Companions and real chapter names.
"""

import hashlib
import math
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath

from . import ai, device, paths

OVERVIEW_AND_CHAPTER = "Overview + chapter"
WHOLE = "Whole Companion"
OFF = "Off"
SEND_OPTIONS = [OVERVIEW_AND_CHAPTER, WHOLE, OFF]

ON_READER = "on the reader"
ON_COMPUTER = "on this computer"

SUFFIX = ".companion.md"
_SKILL_FILE = Path(".claude") / "skills" / "book-companion" / "SKILL.md"
_SCRIPT_NAME = "create-companion.command"
_FINISHED_MARK = "finished"
_COPYING_MARK = "copying"
# The skill's write isn't atomic, so a new file must sit this long before it counts
_SETTLE_SECONDS = 2


class CompanionError(Exception):
    """A Companion action could not run. The message is shown to the user."""


# Finding


def stem(book_path):
    """Book file name without its final extension, the skill's companion_stem rule."""
    base = os.path.basename(book_path)
    root, ext = os.path.splitext(base)
    return root if ext else base


def file_name(book_path):
    return stem(book_path) + SUFFIX


def _beside_book(device_path, mount):
    local = paths.device_to_local(device_path, mount)
    return local.parent / file_name(device_path) if local else None


def _non_empty(path):
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _cache_path(device_path):
    digest = hashlib.sha1(device_path.encode("utf-8")).hexdigest()[:16]
    return paths.home() / "companion-cache" / (digest + SUFFIX)


def _refresh_cache(source, device_path):
    # Keeps the reader's copy usable while the reader is unplugged
    target = _cache_path(device_path)
    try:
        data = source.read_bytes()
        if not target.is_file() or target.read_bytes() != data:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(".tmp")
            temp.write_bytes(data)
            os.replace(temp, target)
    except OSError:
        pass


def find(device_path, mount):
    """Where a book's Companion is: beside the book, then <home>/companions/. An empty file
    counts as absent. With the reader unplugged, the last copy seen on it still counts.
    Returns {found, path, where, label, cached, file_name}."""
    name = file_name(device_path)
    missing = {"found": False, "path": None, "where": None, "label": None, "cached": False, "file_name": name}
    beside = _beside_book(device_path, mount)
    if beside is not None and _non_empty(beside) and not copy_cut_off(device_path):
        _refresh_cache(beside, device_path)
        return {**missing, "found": True, "path": str(beside), "where": "reader", "label": ON_READER}
    if not Path(mount).is_dir() and _non_empty(_cache_path(device_path)):
        return {**missing, "found": True, "path": str(_cache_path(device_path)), "where": "reader",
                "label": ON_READER, "cached": True}
    local = paths.companions_dir() / name
    if _non_empty(local):
        return {**missing, "found": True, "path": str(local), "where": "computer", "label": ON_COMPUTER}
    return missing


def copy_cut_off(device_path):
    """Whether Copy to reader started and was never proven, so the file beside the book can't be trusted."""
    try:
        return (work_dir(device_path) / _COPYING_MARK).exists()
    except CompanionError:
        return False


def read_text(found):
    """The text of a Companion from find(), or None."""
    if not found.get("found"):
        return None
    try:
        return Path(found["path"]).read_bytes().decode("utf-8", "replace")
    except OSError:
        return None


# Slicer port

POS_EPSILON = 0.02
LAST_CHAPTER_DONE = 0.99
CHAPTER_HEAD = 0.05

_POS_RE = re.compile(r"<!-- pos: ([A-Za-z0-9.]+) -->")
_POS_PRESENT = "<!-- pos: "
_HEADING_RE = re.compile(r"(#+)[ \t]")
_DECIMAL_RE = re.compile(r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][0-9]+)?\Z")
_HEX_RE = re.compile(r"0[xX](?:[0-9a-fA-F]+\.?[0-9a-fA-F]*|\.[0-9a-fA-F]+)(?:[pP][0-9]+)?\Z")


def _lua_number(text):
    # tonumber for what the marker pattern can capture: no signs, hex allowed
    try:
        if _DECIMAL_RE.match(text):
            return float(text)
        if _HEX_RE.match(text):
            return float.fromhex(text)
    except (ValueError, OverflowError):
        pass
    return None


def _marker_pos(line):
    match = _POS_RE.search(line)
    if not match:
        return None
    number = _lua_number(match.group(1))
    if number is not None and 0 <= number <= 1:
        return number
    return None


def _heading_hashes(line):
    match = _HEADING_RE.match(line)
    return match.group(1) if match else None


def slice_text(text, progress_percent, keep_current_synopsis=False):
    """Slices a Companion down to the reader's position (0 to 100). Text without position
    markers passes through whole. Returns (text, info), with info holding sliced,
    chapters_total, chapters_kept, synopses_withheld and, when known, chapter_fraction."""
    info = {"sliced": False, "chapters_total": 0, "chapters_kept": 0, "synopses_withheld": 0}
    if not text or _POS_PRESENT not in text:
        return text, info
    info["sliced"] = True
    p = (progress_percent or 0) / 100
    cutoff = p + POS_EPSILON
    lines = text.split("\n")

    positions = []
    for line in lines:
        hashes = _heading_hashes(line)
        if hashes and len(hashes) == 3:
            pos = _marker_pos(line)
            if pos is not None:
                positions.append(pos)

    cur_i, cur_pos = None, None
    for i, pos in enumerate(positions):
        if pos <= p:
            cur_i, cur_pos = i, pos
        else:
            break
    if cur_i is not None:
        finish = positions[cur_i + 1] if cur_i + 1 < len(positions) else 1.0
        if finish > cur_pos:
            info["chapter_fraction"] = min(1, max(0, (p - cur_pos) / (finish - cur_pos)))

    out = []
    dropping = False
    in_synopsis = False
    idx = 0
    for line in lines:
        hashes = _heading_hashes(line)
        is_section_heading = hashes is not None and 2 <= len(hashes) <= 3
        if is_section_heading:
            dropping = False
            in_synopsis = False
            if len(hashes) == 3:
                pos = _marker_pos(line)
                if pos is not None:
                    idx += 1
                    info["chapters_total"] += 1
                    if pos > cutoff:
                        dropping = True
                    else:
                        info["chapters_kept"] += 1
                        if idx < len(positions):
                            completed = positions[idx] <= p
                        else:
                            completed = p >= LAST_CHAPTER_DONE
                        if not completed and not keep_current_synopsis:
                            in_synopsis = True
                            info["synopses_withheld"] += 1
        drop_line = dropping
        if not drop_line and in_synopsis and not is_section_heading:
            if line.startswith("_Notes:_"):
                in_synopsis = False
            else:
                drop_line = True
        if not drop_line and not hashes:
            pos = _marker_pos(line)
            if pos is not None and pos > cutoff:
                drop_line = True
        if not drop_line:
            out.append(line)
    return "\n".join(out), info


def position_note(info):
    """The per-request note about the reading position, or None without position data."""
    fraction = info.get("chapter_fraction") if info else None
    if fraction is None:
        return None
    pct = math.floor(fraction * 100 + 0.5)
    if fraction <= CHAPTER_HEAD:
        return ("[Position note: the reader has ONLY JUST STARTED their current chapter ("
                f"{pct}% into it) — treat the entire chapter as UNREAD. If the highlight is the"
                " chapter's epigraph or opening lines, explain it purely on its own terms (source,"
                " translation, meaning); do not connect it to this chapter or anything after it,"
                " even from your own knowledge of this book.]")
    return (f"[Position note: the reader is about {pct}"
            "% through their current chapter; everything beyond that point is unread, including"
            " the rest of this chapter.]")


# Sections


_H2_RE = re.compile(r"##[ \t]")
_NUMBERED_ONE_RE = re.compile(r"##[ \t]+1[.):\s]")


def overview(text):
    """The Companion's opening: everything before section 1 (the register note and any
    other leading notes), and section 1 itself. Without a numbered section 1, the first
    section stands in for it."""
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if _H2_RE.match(line)]
    if not starts:
        return text.strip()
    first = next((i for i in starts if _NUMBERED_ONE_RE.match(lines[i])), starts[0])
    end = next((i for i in starts if i > first), len(lines))
    return "\n".join(lines[:end]).strip()


_MARKER_RE = re.compile(r"<!--.*?-->")
_BOOK_MARKER_RE = re.compile(r"<!--\s*book:\s*(.*?)\s*-->")
_CHAPTER_SECTION_RE = re.compile(r"chapter|episode|talk|section-by-section|part-by-part", re.I)

_UNITS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
          "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_ROMAN_RE = re.compile(r"m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})\Z", re.I)
_ROMAN_VALUES = {"m": 1000, "d": 500, "c": 100, "l": 50, "x": 10, "v": 5, "i": 1}
_NUMERAL = r"(?:[0-9]+|[ivxlcdm]+|[a-z]+(?:[- ][a-z]+)?)"
_SEPARATOR = r"[.:)—–·-]"
_LABEL = r"chapter|ch\.?|part|book|episode|ep\.?|letter|section"
_LABELLED_RE = re.compile(rf"^(?P<label>{_LABEL})\s+(?P<num>{_NUMERAL})\s*(?:{_SEPARATOR}+\s*|$)(?P<rest>.*)$", re.I | re.S)
_BARE_RE = re.compile(rf"^(?P<num>{_NUMERAL})\s*(?:{_SEPARATOR}+\s*|$)(?P<rest>.*)$", re.I | re.S)
_TRAILING_RE = re.compile(rf"\b(?P<label>chapter)\s+(?P<num>{_NUMERAL})\s*{_SEPARATOR}*\s*$", re.I)


def _numeral_value(text):
    text = text.lower().strip()
    if text.isdigit():
        return int(text)
    if text in _UNITS:
        return _UNITS[text]
    if text in _TENS:
        return _TENS[text]
    parts = re.split(r"[- ]", text)
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _UNITS and _UNITS[parts[1]] < 10:
        return _TENS[parts[0]] + _UNITS[parts[1]]
    # Case-insensitive matching also lets the dotless ı through
    if text and text.isascii() and _ROMAN_RE.match(text):
        total, last = 0, 0
        for ch in reversed(text):
            value = _ROMAN_VALUES[ch]
            total += -value if value < last else value
            last = max(last, value)
        return total
    return None


def _title_words(text):
    text = text.lower().replace("’", "'").replace("‘", "'")
    return re.findall(r"[^\W_]+(?:'[^\W_]+)*", text)


def _label_name(label):
    label = (label or "chapter").lower().rstrip(".")
    return {"ch": "chapter", "ep": "episode"}.get(label, label)


def _parse_title(title):
    """A title's parts: its words, its number and label if it has one, and the words after."""
    text = title.strip()
    parsed = {"words": _title_words(text), "number": None, "label": None, "rest": None}
    match = _LABELLED_RE.match(text)
    if match and _numeral_value(match.group("num")) is not None:
        parsed.update(number=_numeral_value(match.group("num")), label=_label_name(match.group("label")),
                      rest=_title_words(match.group("rest")))
        return parsed
    match = _BARE_RE.match(text)
    if match and _numeral_value(match.group("num")) is not None:
        parsed.update(number=_numeral_value(match.group("num")), label="chapter", rest=_title_words(match.group("rest")))
        return parsed
    # ePubs can put text before the number, as in "A note for Miss Bennet. CHAPTER VII."
    match = _TRAILING_RE.search(text)
    if match and _numeral_value(match.group("num")) is not None:
        parsed.update(number=_numeral_value(match.group("num")), label="chapter", rest=[])
    return parsed


def _clean_heading(line):
    return _MARKER_RE.sub("", line.lstrip("#")).strip()


def _chapter_headings(lines):
    """### headings that can be chapters, as (line number, heading line), from the ## sections
    that hold position markers or are named for chapters."""
    sections, current = [], None
    for i, line in enumerate(lines):
        hashes = _heading_hashes(line)
        if hashes and len(hashes) == 2:
            current = {"title": line, "headings": []}
            sections.append(current)
        elif hashes and len(hashes) == 3:
            if current is None:
                current = {"title": "", "headings": []}
                sections.append(current)
            current["headings"].append((i, line))
    chosen = [s for s in sections if any(_POS_RE.search(line) for _i, line in s["headings"])]
    chosen += [s for s in sections if s not in chosen and _CHAPTER_SECTION_RE.search(s["title"])]
    chosen = chosen or sections
    return sorted(item for s in chosen for item in s["headings"])


def _starts_with(longer, shorter):
    return bool(shorter) and longer[:len(shorter)] == shorter


def _match_heading(chapter, headings):
    """The line number of the heading for `chapter`, or None. Stronger rules go first, and a
    rule that fits several headings is no match, because wrong notes are worse than none."""
    want = _parse_title(chapter)
    if not want["words"]:
        return None
    rows = []
    for i, line in headings:
        cleaned = _clean_heading(line)
        marker = _BOOK_MARKER_RE.search(line)
        rows.append({"line": i, "title": _parse_title(cleaned), "book": _parse_title(marker.group(1)) if marker else None})

    def names(parsed):
        return [parsed["words"], parsed["rest"]] if parsed["rest"] else [parsed["words"]]

    def title_names(parsed):
        # A title that is only a number has no words to compare, so "I" isn't the word I
        if parsed["number"] is not None and not parsed["rest"]:
            return []
        return [name for name in names(parsed) if _has_word(name)]

    def unique(found):
        return found[0]["line"] if len(found) == 1 else None

    # The same words, with or without a number in front
    exact = [r for r in rows if r["title"]["words"] == want["words"]]
    if exact:
        return unique(exact)
    same = [r for r in rows if any(a == b for a in names(want) for b in names(r["title"]))]
    if same:
        return unique(same)

    if want["number"] is not None:
        # The book's own number, where the heading records it
        marked = [r for r in rows if r["book"] and r["book"]["number"] == want["number"]
                  and r["book"]["label"] == want["label"]]
        if marked:
            return unique(marked)
        if not want["rest"]:
            plain = [r for r in rows if r["title"]["number"] == want["number"]
                     and r["title"]["label"] == want["label"] and not r["book"]]
            bare = [r for r in plain if not r["title"]["rest"]]
            if bare:
                return unique(bare)
            # Numbers count only when no heading records the book's own numbering
            if plain and not any(r["book"] for r in rows):
                return unique(plain)

    # One title is the start of the other, as with "Introduction: Welcome to Willpower 101"
    prefix = [r for r in rows if any(_starts_with(a, b) or _starts_with(b, a)
                                     for a in title_names(want)
                                     for b in title_names(r["title"]))]
    return unique(prefix) if prefix else None


def _has_word(words):
    return any(not word.isdigit() for word in words)


def chapter_block(text, chapter):
    """The Companion's block for `chapter`: {found, heading, text, body}. `heading` and
    `text` have position markers removed. A chapter with no matching heading gets
    {found: False}."""
    if not text or not chapter:
        return {"found": False}
    lines = text.split("\n")
    line = _match_heading(chapter, _chapter_headings(lines))
    if line is None:
        return {"found": False}
    end = len(lines)
    for i in range(line + 1, len(lines)):
        hashes = _heading_hashes(lines[i])
        if hashes and 2 <= len(hashes) <= 3:
            end = i
            break
    heading = _clean_heading(lines[line])
    body = "\n".join(lines[line + 1:end]).strip()
    return {"found": True, "heading": heading, "text": ("### " + heading + "\n\n" + body).strip(), "body": body}


def context_for_highlight(text, setting, chapter, percent):
    """Companion text to send with one highlight, at its position (0 to 100). Always goes
    through the slicer with the current chapter's synopsis kept, as the plugin does."""
    if not text or setting == OFF:
        return None
    sliced, _info = slice_text(text, percent, keep_current_synopsis=True)
    if setting == WHOLE:
        return sliced
    parts = [overview(sliced)]
    block = chapter_block(sliced, chapter)
    if block["found"]:
        parts.append(block["text"])
    return "\n\n".join(part for part in parts if part)


def context_for_book(text, percent_finished):
    """The whole Companion sliced to the book's percent_finished (0 to 1), for connections
    and key ideas."""
    if not text:
        return None
    sliced, _info = slice_text(text, (percent_finished or 0) * 100, keep_current_synopsis=True)
    return sliced


# Notes panel


_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "prof", "vs", "etc", "no", "cf", "ch", "ep", "e.g", "i.e", "a.m", "p.m", "pp", "p", "c", "sr", "jr", "rev"}
_SENTENCE_END_RE = re.compile(r"[.!?][\"”’')\]*_]*(?=\s+|$)")
_BOLD_LABEL_RE = re.compile(r"^\*\*[^*\n]{1,40}?(?:[.:]\*\*|\*\*[.:])\s*")
_BULLET_RE = re.compile(r"^(\s*)[-*][ \t]+(.*)$")
_LABEL_LINE_RE = re.compile(r"^(\*\*[^*]+\*\*[.:]?|_[^_]+:?_)\s*$")


def _plain(text):
    # Markdown emphasis removed, for the panel's plain text
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"\1", text)
    text = re.sub(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def first_sentence(text):
    """The first sentence of `text`, skipping common abbreviations."""
    for match in _SENTENCE_END_RE.finditer(text):
        before = text[:match.start()].rsplit(None, 1)
        last_word = before[-1].lower().lstrip("(\"“") if before else ""
        if last_word in _ABBREVIATIONS:
            continue
        following = text[match.end():].lstrip()
        if following and following[0].islower():
            continue
        return _without_open_quote(text[:match.end()].strip())
    return _without_open_quote(text.strip())


def _without_open_quote(sentence):
    # A sentence cut from a longer quotation keeps no stray opening mark
    if sentence.startswith('"') and sentence.count('"') % 2 == 1:
        return sentence[1:]
    if sentence.startswith("\u201c") and "\u201d" not in sentence:
        return sentence[1:]
    return sentence


def _block_items(body):
    """A block's content as (kind, text, label) rows, with wrapped lines joined."""
    items, current, label = [], None, None
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            current = None
            continue
        if _LABEL_LINE_RE.match(stripped):
            label = _plain(stripped).rstrip(".:").lower()
            current = None
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            current = {"kind": "bullet", "text": bullet.group(2), "label": label}
            items.append(current)
        elif current is not None:
            current["text"] += " " + stripped
        else:
            current = {"kind": "para", "text": stripped, "label": label}
            items.append(current)
    return items


def notes_panel(text, chapter, percent=None):
    """The notes panel before key ideas have run: {found, big, points}, where `big` is the
    block's first sentence and `points` its bullet lines, untagged. Under a "Key points"
    label, as practical Companions have, only those bullets count. Pass `percent` (0 to
    100) to slice first."""
    if percent is not None:
        text, _info = slice_text(text, percent, keep_current_synopsis=True)
    block = chapter_block(text, chapter)
    if not block["found"]:
        return {"found": False, "big": "", "points": []}
    items = _block_items(block["body"])
    big = ""
    for item in items:
        # Lines that are only emphasis, like "Introduction, 1 of 2", aren't the point
        if item["kind"] == "para" and re.fullmatch(r"_[^_]+_", item["text"]):
            continue
        big = first_sentence(_plain(_BOLD_LABEL_RE.sub("", item["text"], count=1)))
        break
    bullets = [item for item in items if item["kind"] == "bullet"]
    key_points = [item for item in bullets if item["label"] == "key points"]
    points = [_plain(item["text"]) for item in (key_points or bullets)]
    return {"found": True, "big": big, "points": [p for p in points if p and p != big]}


# Creating one


def skill_status():
    """Whether Create can run: {ok, path, problem}, with `problem` a plain sentence."""
    folder = paths.assistant_dir()
    status = {"ok": False, "path": str(folder), "problem": None}
    if not folder.is_dir():
        status["problem"] = f"The assistant folder isn't at {folder}."
    elif not (folder / _SKILL_FILE).is_file():
        status["problem"] = f"The Book Companion skill isn't in {folder}."
    elif not ai.find_binary("claude"):
        status["problem"] = "Claude Code isn't installed, or pbAnki can't find it."
    else:
        status["ok"] = True
    return status


def work_dir(device_path):
    name = stem(device_path)
    if name in ("", ".", "..") or "/" in name:
        raise CompanionError("That book's file name can't be used.")
    return paths.companion_work_dir() / name


def _quoted_for_prompt(path):
    # Single quotes as in the skill's usage; double quotes when the name has an apostrophe.
    # Either way a shell parsing the prompt gets the path back whole.
    if "'" not in path:
        return "'" + path + "'"
    return '"' + re.sub(r'([\\"$`])', r"\\\1", path) + '"'


def skill_prompt(epub_path):
    """The prompt given to Claude Code for one local book file."""
    return "/book-companion " + _quoted_for_prompt(str(epub_path))


def _home_short(path):
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text.startswith(home + "/") else text


def create_preview(device_path):
    """What the Create drawer shows: {command, watching}."""
    prompt = "/book-companion " + _quoted_for_prompt(PurePosixPath(device_path).name)
    command = "cd " + _home_short(paths.assistant_dir()) + "\nclaude " + _double_quoted(prompt)
    return {"command": command, "watching": file_name(device_path)}


def _double_quoted(text):
    return '"' + re.sub(r'([\\"$`])', r"\\\1", text) + '"'


def script_text(epub_path, assistant_dir=None, claude=None):
    """The .command script: cd to the assistant folder and run the skill on the local copy.
    It removes itself when the session ends, which is what stops the Create counting as running."""
    folder = assistant_dir or paths.assistant_dir()
    binary = claude or ai.find_binary("claude") or "claude"
    script = Path(epub_path).with_name(_SCRIPT_NAME)
    return ("#!/bin/bash\n"
            f"script={shlex.quote(str(script))}\n"
            "trap 'rm -f \"$script\"' EXIT\n"
            f"cd {shlex.quote(str(folder))} || exit 1\n"
            f"{shlex.quote(str(binary))} {shlex.quote(skill_prompt(epub_path))}\n")


def _launch_terminal(script):
    override = os.environ.get("PBANKI_TERMINAL_CMD")
    command = [*shlex.split(override), str(script)] if override else ["open", "-a", "Terminal", str(script)]
    try:
        done = subprocess.run(command, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise CompanionError(f"Terminal could not be opened: {e}") from e
    if done.returncode != 0:
        raise CompanionError("Terminal could not be opened.")


def create(device_path, mount):
    """Copies the book to the Mac, writes the .command script there and opens it in Terminal.
    Only ever runs on the user's click. Returns {work_dir, epub, script, watching}."""
    status = skill_status()
    if not status["ok"]:
        raise CompanionError(status["problem"])
    source = paths.device_to_local(device_path, mount)
    if source is None or not source.is_file():
        raise CompanionError("Plug in the reader first, so pbAnki can copy the book.")
    folder = work_dir(device_path)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / _FINISHED_MARK).unlink(missing_ok=True)
    output = _work_output(device_path)
    if output.exists():
        os.replace(output, folder / ("previous-" + output.name))
    epub = folder / PurePosixPath(device_path).name
    try:
        shutil.copyfile(source, epub)
    except OSError as e:
        raise CompanionError(f"The book could not be copied from the reader: {e.strerror or e}") from e
    script = folder / _SCRIPT_NAME
    script.write_text(script_text(epub), encoding="utf-8")
    script.chmod(0o755)
    try:
        _launch_terminal(script)
    except CompanionError:
        # Nothing is running, so creation_status mustn't say it is
        script.unlink(missing_ok=True)
        raise
    return {"work_dir": str(folder), "epub": str(epub), "script": str(script), "watching": file_name(device_path)}


def _work_output(device_path):
    return work_dir(device_path) / file_name(device_path)


def creation_status(device_path):
    """Where Create stands for this book: {stage, path}, with stage "none", "running", "found"
    (the file is there and awaits Copy or Keep) or "done"."""
    folder = work_dir(device_path)
    output = _work_output(device_path)
    if (folder / _FINISHED_MARK).exists():
        return {"stage": "done", "path": str(output) if output.exists() else None}
    if _non_empty(output) and time.time() - output.stat().st_mtime >= _SETTLE_SECONDS:
        return {"stage": "found", "path": str(output)}
    if (folder / _SCRIPT_NAME).exists() or output.exists():
        return {"stage": "running", "path": None}
    return {"stage": "none", "path": None}


def forget_creation(device_path):
    """Stops treating a Create as running, for when the Terminal window was closed.
    An empty output file goes too, as it would keep counting as running."""
    (work_dir(device_path) / _SCRIPT_NAME).unlink(missing_ok=True)
    output = _work_output(device_path)
    if not _non_empty(output):
        output.unlink(missing_ok=True)


def watch(device_path, on_found, interval=2.0, stop=None):
    """Calls `on_found(path)` once when the output appears, from a background thread.
    Set the `stop` Event to end the watch. Returns (thread, stop)."""
    stop = stop or threading.Event()

    def loop():
        while True:
            status = creation_status(device_path)
            if status["stage"] == "found":
                on_found(status["path"])
                return
            if status["stage"] != "running" or stop.wait(interval):
                return

    thread = threading.Thread(target=loop, name="companion-watch", daemon=True)
    thread.start()
    return thread, stop


def _finished_output(device_path):
    output = _work_output(device_path)
    if not _non_empty(output):
        raise CompanionError("The Book Companion file isn't there any more.")
    return output


def copy_to_reader(device_path, mount):
    """Copies the finished file beside the book, as a proven write. Returns the path on the reader."""
    output = _finished_output(device_path)
    target = _beside_book(device_path, mount)
    if target is None:
        raise CompanionError("This book isn't on the reader's main storage.")
    mark = work_dir(device_path) / _COPYING_MARK
    mark.touch()
    device.write_file(mount, target, output.read_bytes())
    mark.unlink(missing_ok=True)
    (work_dir(device_path) / _FINISHED_MARK).write_text("reader\n", encoding="utf-8")
    return str(target)


def keep_local(device_path):
    """Keeps the finished file on this computer. Returns its path."""
    output = _finished_output(device_path)
    target = paths.companions_dir() / file_name(device_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_bytes(output.read_bytes())
    os.replace(temp, target)
    (work_dir(device_path) / _FINISHED_MARK).write_text("computer\n", encoding="utf-8")
    return str(target)
