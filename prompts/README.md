# pbAnki prompts

Each folder here is a prompt the AI can follow when it writes flashcards. Pick one in KOReader under Tools → pbAnki flashcards → AI prompt, for all books or just the book you are reading.

A folder can hold up to three Markdown files:

- `questions.md` for question and answer cards
- `cloze.md` for cloze cards
- `definitions.md` for definition cards

A missing file means that kind of card uses the Standard prompt.

To make your own prompt, copy the Standard folder, rename the copy and edit its files. The plugin rewrites Standard whenever a book opens, so edits made there are lost.

The plugin adds the reply format itself, so the files only need the instructions.
