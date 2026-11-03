# Real KOReader files

Written by real KOReader v2026.07.1 in Docker, not by hand. Do not edit them.

- `metadata.epub.lua`: the book's settings file, as KOReader saved it.
- `history.lua`: KOReader's history after opening the book.
- `book.epub`: the book those highlights were made in, Project Gutenberg's Pride and Prejudice (public domain), 561 KB.

KOReader saw the book as `/mnt/ext1/English/book.epub`, the same path shape as on the reader, so the settings would sit at `/mnt/ext1/English/book.sdr/metadata.epub.lua`.

`dev/koreader/make_fixture.lua` drove KOReader's own selection, Highlight and Add note code on these:

| # | Chapter | What it shows |
|---|---------|---------------|
| 1 | Chapter I. | The drop-cap opening: `pos0` is inside a `span`, `pos1` is not |
| 2 | Chapter I. | Short and repeated: "Mr. Bennet" appears all through the book |
| 3 | Chapter I. | Has a note. `pos0` is in `DocFragment[3]` while the chapter starts in `DocFragment[2]` |
| 4 | I hope Mr. Bingley will like it. CHAPTER II. | Two paragraphs, with a newline in `text` |
| 5 | I hope Mr. Bingley will like it. CHAPTER II. | Short: "To-morrow fortnight." with curly quotes |
| 6 | CHAPTER IV. | Starts and ends mid-sentence, has dashes and a `text()[3]` in `pos0` |
| 7 | A note for Miss Bennet. CHAPTER VII. | A later spine document, `DocFragment[4]` |
| 8 | Piling up the fire. CHAPTER XI. | A page bookmark, not a highlight: no `drawer`, `pos0` or `pos1` |

Reading stopped on page 150 of 675, so `percent_finished` is about 0.22. Nothing is marked reviewed and there is no card history; `dev/koreader/check_desktop_saves.sh` shows what the plugin adds.

To make them again, which changes the dates and so the highlight keys:

    dev/koreader/check_desktop_saves.sh --make-fixture
