#!/usr/bin/env python3
"""Stands in for Terminal in dev_server.py: returns at once, and a few seconds later writes the
Book Companion the real skill would write beside the .command script."""

import os
import subprocess
import sys
import time
from pathlib import Path

TEXT = """# {title}

## 1. Overview

A short, practical book. It says that time and attention are the only things worth managing, and that small daily habits do more than grand plans.

## 2. Chapter by chapter

### The first part

- The main claim is stated plainly.
- An objection is raised and answered.
- A small exercise follows.

## 3. Since publication

Later studies support the broad idea, though the effect is smaller than the book suggests.
"""


def finish(folder, delay):
    time.sleep(delay)
    books = [p for p in Path(folder).iterdir() if p.suffix in (".epub", ".pdf")]
    if books:
        book = books[0]
        output = book.with_name(book.stem + ".companion.md")
        output.write_text(TEXT.format(title=book.stem), encoding="utf-8")


def main():
    if sys.argv[1] == "--finish":
        finish(sys.argv[2], float(sys.argv[3]))
        return
    delay = os.environ.get("PBANKI_DEV_COMPANION_SECONDS", "5")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--finish", str(Path(sys.argv[1]).parent), delay],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


if __name__ == "__main__":
    main()
