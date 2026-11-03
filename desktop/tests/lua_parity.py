"""Runs Lua originals on JSON-described cases, for the parity tests."""

import json
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LUA_DIR = Path(__file__).resolve().parent / "fixtures" / "lua"
ASSISTANT = Path.home() / "Documents" / "GitHub" / "assistant.koplugin"


def interpreters():
    """Lua commands on this machine: plain lua, and LuaJIT, which the reader runs."""
    return [name for name in ("lua", "luajit") if shutil.which(name)]


def run_cases(cases, lua="lua"):
    """Results for `cases`, each {"fn", "args"}, in order: {"ok", "value"} or {"ok", "error"}."""
    package_path = ";".join([
        f"{LUA_DIR}/?.lua",
        f"{REPO}/koreader/pbanki.koplugin/?.lua",
        f"{ASSISTANT}/?.lua",
    ])
    script = f'package.path = "{package_path};" .. package.path; dofile("{LUA_DIR}/harness.lua")'
    done = subprocess.run(
        [lua, "-e", script],
        input=json.dumps(cases, ensure_ascii=False).encode("utf-8"),
        capture_output=True, cwd=REPO, timeout=300, check=False,
    )
    if done.returncode != 0:
        raise AssertionError(f"{lua} failed: {done.stderr.decode('utf-8', 'replace')}")
    return json.loads(done.stdout.decode("utf-8", "surrogateescape"))
