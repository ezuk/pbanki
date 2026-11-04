"""AI prompts kept as Markdown files, one folder per prompt, as in the plugin's prompts.lua.

    <home>/prompts/
        Standard/       rewritten from the repo on every start
        Self Help/      the reader's own prompt; kinds without a file use Standard's

Files are re-read for every AI call, so edits apply at once.
"""

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import paths

STANDARD = "Standard"
COPIED_FILE = ".copied"

# The first file name is the one written for Standard
KINDS = [
    {"key": "questions", "label": "questions", "files": ["questions.md", "question.md"]},
    {"key": "cloze", "label": "cloze", "files": ["cloze.md"]},
    {"key": "definition", "label": "definitions", "files": ["definitions.md", "definition.md"]},
    {"key": "connections", "label": "connections", "files": ["connections.md"]},
    {"key": "ideas", "label": "key ideas", "files": ["ideas.md"]},
    {"key": "rewrite", "label": "rewrite", "files": ["rewrite.md"]},
]

README_TEXT = """# pbAnki prompts

Each folder here is a prompt the AI can follow when it writes flashcards. Pick one on the Suggest screen.

A folder can hold these Markdown files:

- `questions.md` for question and answer cards
- `cloze.md` for cloze cards
- `definitions.md` for definition cards
- `connections.md` for connections between highlights
- `ideas.md` for key ideas you didn't highlight
- `rewrite.md` for rewriting one card

A missing or blank file means that kind of card uses the Standard prompt.

To make your own prompt, copy the Standard folder, rename the copy and edit its files. pbAnki rewrites Standard every time it starts, so edits made there are undone.

pbAnki adds the reply format itself, so the files only need the instructions. Edits apply to the next AI request.
"""

_C_SPACE = " \t\n\v\f\r"


class PromptNotFound(Exception):
    """A request named a prompt folder or file that isn't in the listing."""


def normalize(text):
    """Markdown `text` tidied for the AI: Unix line ends, no trailing spaces,
    single blank lines between paragraphs, and headings set apart."""
    if text.startswith("﻿"):
        text = text[1:]
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]+\Z", "", text)
    text = re.sub(r"\n(#+[ \t][^\n]*)", r"\n\n\1\n\n", "\n" + text + "\n")
    return re.sub(r"\n\n\n+", "\n\n", text).strip(_C_SPACE)


def _read_file(path):
    try:
        return Path(path).read_bytes().decode("utf-8", "replace")
    except OSError:
        return None


def read(folder):
    """Instructions by kind from prompt folder `folder`, without kinds that have
    no file or an empty one."""
    prompt = {}
    for kind in KINDS:
        for name in kind["files"]:
            text = _read_file(Path(folder) / name)
            if text is not None:
                text = normalize(text)
                if text:
                    prompt[kind["key"]] = text
                    break
    return prompt


def own_kinds(prompt):
    """Labels of the kinds `prompt` has its own instructions for."""
    return [kind["label"] for kind in KINDS if kind["key"] in prompt]


def names(folders):
    """Standard first, then the other folder names in alphabetical order."""
    others = sorted((n for n in folders if n != STANDARD), key=str.lower)
    return [STANDARD, *others]


def choose(folders, book_choice=None, default=None):
    """The prompt a book uses: its own choice, else the default, else Standard,
    skipping folders that no longer exist."""
    if book_choice in folders:
        return book_choice
    if default in folders:
        return default
    return STANDARD


def _plugin_standard():
    return paths.repo_root() / "koreader" / "pbanki.koplugin" / "prompts" / STANDARD


def _desktop_standard():
    return paths.desktop_root() / "prompts" / STANDARD


def _repo_prompts():
    return paths.repo_root() / "prompts"


def builtin():
    """The Standard instructions shipped in the repo, by kind."""
    return {**read(_plugin_standard()), **read(_desktop_standard())}


def seed():
    """Rewrites Standard from the repo and copies the repo's other prompts when absent."""
    root = paths.prompts_dir()
    standard = root / STANDARD
    standard.mkdir(parents=True, exist_ok=True)
    for source in (_plugin_standard(), _desktop_standard()):
        for path in sorted(source.glob("*.md")):
            (standard / path.name).write_bytes(path.read_bytes())
    (root / "README.md").write_text(README_TEXT, encoding="utf-8")
    if not _repo_prompts().is_dir():
        return
    # Names already copied, so a folder the user deleted stays deleted
    marks = root / COPIED_FILE
    copied = set(marks.read_text(encoding="utf-8").split("\n")) - {""} if marks.is_file() else set()
    for folder in sorted(_repo_prompts().iterdir()):
        if not folder.is_dir() or folder.name == STANDARD or folder.name.startswith("."):
            continue
        target = root / folder.name
        if folder.name in copied:
            continue
        copied.add(folder.name)
        if target.exists():
            continue
        target.mkdir()
        for path in sorted(folder.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                (target / path.name).write_bytes(path.read_bytes())
    marks.write_text("".join(f"{name}\n" for name in sorted(copied)), encoding="utf-8")


def folder_names():
    """Prompt folders on disk: Standard first, then the others alphabetically."""
    root = paths.prompts_dir()
    found = []
    if root.is_dir():
        found = [p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    return names(found)


def check_name(name):
    """Raises PromptNotFound unless `name` is a listed prompt folder."""
    if not isinstance(name, str) or name not in folder_names():
        raise PromptNotFound("That prompt isn't in the prompts folder.")
    return name


def effective(name=None):
    """Instructions by kind for prompt `name`: its own files, else Standard's."""
    prompt = {**builtin(), **read(paths.prompts_dir() / STANDARD)}
    if name in folder_names():
        prompt.update(read(paths.prompts_dir() / name))
    return prompt


def default_for_book(book_choice=None, last_used=None):
    """The sidecar's `pbanki_prompt` when that folder exists here, else the last used, else Standard."""
    return choose(folder_names(), book_choice, last_used)


def _own_file(folder, kind):
    # Same rule as read(): the first file that isn't blank
    for file_name in kind["files"]:
        text = _read_file(folder / file_name)
        if text is not None and normalize(text):
            return file_name
    return None


def _file_row(name, kind):
    folder = paths.prompts_dir() / name
    own = _own_file(folder, kind)
    file_name = own or kind["files"][0]
    path = folder / file_name if own else _standard_path(kind)
    return {
        "kind": kind["key"],
        "label": kind["label"],
        "file": file_name,
        "status": "this prompt" if own else "uses Standard",
        "path": str(path),
        "own_path": str(folder / file_name),
    }


def _standard_path(kind):
    seeded = paths.prompts_dir() / STANDARD / kind["files"][0]
    if seeded.is_file():
        return seeded
    for source in (_plugin_standard(), _desktop_standard()):
        if (source / kind["files"][0]).is_file():
            return source / kind["files"][0]
    return seeded


def list_folders():
    """Every prompt folder and its files, each marked "this prompt" or "uses Standard"."""
    return [
        {"name": name, "files": [_file_row(name, kind) for kind in KINDS]}
        for name in folder_names()
    ]


def _resolve(name, file_name):
    check_name(name)
    for kind in KINDS:
        if file_name == kind["key"] or file_name in kind["files"]:
            return _file_row(name, kind)
    raise PromptNotFound("That prompt file isn't one pbAnki uses.")


def read_one(name, file_name):
    """One prompt file with its text. `file_name` is a file name or a kind."""
    row = _resolve(name, file_name)
    return {**row, "name": name, "text": _read_file(row["path"]) or ""}


def make_own(name, file_name):
    """Gives a folder its own copy of a file it takes from Standard, so edits to it stay.
    Returns the file's row."""
    row = _resolve(name, file_name)
    if row["status"] == "uses Standard" and name != STANDARD:
        Path(row["own_path"]).write_bytes(Path(row["path"]).read_bytes())
        row = _resolve(name, file_name)
    return row


def _launch(args):
    subprocess.run(args, check=False, capture_output=True, timeout=30)


def open_in_editor(name, file_name):
    """Opens a prompt file in the default text editor."""
    row = make_own(name, file_name)
    _launch(["open", "-t", row["path"]])
    return row


def reveal(name, file_name):
    """Shows a prompt file in Finder."""
    row = _resolve(name, file_name)
    _launch(["open", "-R", row["path"]])
    return row


class PromptWatcher:
    """Records each prompt file's mtime, to report "reloaded N s ago" and notify the page."""

    def __init__(self, on_change=None):
        self.on_change = on_change
        self.last_reload = time.time()
        self._mtimes = self._scan()
        self._stop = threading.Event()
        self._thread = None

    def _scan(self):
        mtimes = {}
        for path in paths.prompts_dir().glob("*/*.md"):
            try:
                mtimes[str(path)] = path.stat().st_mtime_ns
            except OSError:
                pass
        return mtimes

    def check(self):
        """Paths added, changed or removed since the last check."""
        now = self._scan()
        changed = sorted(
            path for path in now.keys() | self._mtimes.keys()
            if now.get(path) != self._mtimes.get(path)
        )
        self._mtimes = now
        if changed:
            self.last_reload = time.time()
            if self.on_change:
                self.on_change(changed)
        return changed

    def seconds_since_reload(self):
        return int(time.time() - self.last_reload)

    def start(self, interval=2.0):
        def loop():
            while not self._stop.wait(interval):
                self.check()
        self._thread = threading.Thread(target=loop, name="prompt-watcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
