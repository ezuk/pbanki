# End-to-end test

`run_e2e.mjs` drives the real server and a real headless Chromium through the whole app, against a fake reader and the fake AI commands, then compares `inbox.jsonl` and `reviewed.jsonl` line by line with what the screens showed. It makes its own temporary folders and removes them, and never touches a real reader, `~/Library/Application Support/pbAnki`, Terminal or `open`.

Playwright lives outside the repo. Set it up once, in any folder:

    npm install --prefix ~/.cache/pbanki-e2e playwright
    (cd ~/.cache/pbanki-e2e && npx playwright install chromium-headless-shell)

Then run it from anywhere:

    E2E_PLAYWRIGHT_DIR=~/.cache/pbanki-e2e node desktop/tests/e2e/run_e2e.mjs

It takes about half a minute and exits 0 when every step passes. Optional settings:

- `E2E_OUTPUT`: where to keep the produced `inbox.jsonl`, `reviewed.jsonl`, the real KOReader book and sidecar, and `manifest.json`, for the Rust import and KOReader checks. Defaults to `pbanki-e2e-output` in the system's temporary folder.
- `E2E_SCREENSHOTS`: where the numbered screenshots go. Defaults to `.playwright-mcp/e2e/`, and is emptied first.
- `E2E_KEEP=1`: keeps the temporary folder, with the server log and every prompt the fake AI got.

It starts `desktop/run.py` itself, with the environment from `dev_server.py`, because `dev_server.py` removes its folder when it stops and this test has to kill the server and start it again on the same data.

## Browser regressions

`run_regressions.mjs` has one check per bug found in the browser app, such as text typed in an outage, a reload right after an edit, where the cursor goes, Back and Forward, seven tabs. It needs the same Playwright and runs the same way, in a few minutes:

    E2E_PLAYWRIGHT_DIR=~/.cache/pbanki-e2e node desktop/tests/e2e/run_regressions.mjs

- `E2E_ONLY`: runs only the checks whose title contains this text. Some checks use what earlier ones left, so this suits a single check better than a group.
- `E2E_REPO`: runs against another copy of the repo, such as an older commit, to see a check fail there.
