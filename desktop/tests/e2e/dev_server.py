#!/usr/bin/env python3
"""Runs the real app against a fake reader, a temporary home and the fake AI commands.

    python3 desktop/tests/e2e/dev_server.py --port 8765
    python3 desktop/tests/e2e/dev_server.py unplug --port 8765
    python3 desktop/tests/e2e/dev_server.py plug --port 8765
    python3 desktop/tests/e2e/dev_server.py stop --port 8765

The address is printed once the app is up. `stop`, Ctrl-C or SIGTERM ends the app and removes the
temporary folder. `unplug` and `plug` rename the fake mount, which the app notices within 2 s.
Details of a running copy are in the control file, <temp folder>/pbanki-dev-<port>.json.

Nothing here touches a real reader, the real data folder, or the real claude, codex, Terminal or open.
"""

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent
DESKTOP = TESTS.parent
FAKE_AI = TESTS / "fixtures" / "fake_ai"

sys.path.insert(0, str(TESTS))

from fixtures.make_reader import build_reader  # noqa: E402


def control_path(port):
    return Path(tempfile.gettempdir()) / f"pbanki-dev-{port}.json"


def read_control(port):
    try:
        return json.loads(control_path(port).read_text(encoding="utf-8"))
    except OSError:
        sys.exit(f"No dev server is running on port {port}.")


def unplug(port):
    mount = Path(read_control(port)["mount"])
    if mount.is_dir():
        mount.rename(mount.with_name(mount.name + ".unplugged"))
    print("unplugged")


def plug(port):
    mount = Path(read_control(port)["mount"])
    away = mount.with_name(mount.name + ".unplugged")
    if away.is_dir():
        away.rename(mount)
    print("plugged in")


def stop_server(port):
    os.kill(read_control(port)["pid"], signal.SIGTERM)
    print("stopping")


def make_assistant(folder):
    skill = folder / ".claude" / "skills" / "book-companion"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Fake Book Companion skill\n", encoding="utf-8")


def environment(root, mount, args):
    env = dict(os.environ)
    env.update({
        "PBANKI_MOUNT": str(mount),
        "PBANKI_DESKTOP_HOME": str(root / "home"),
        "PBANKI_ASSISTANT_DIR": str(root / "assistant"),
        "PBANKI_SYNC_DELAY": "0",
        "PBANKI_CLAUDE_BIN": str(FAKE_AI / "claude"),
        "PBANKI_CODEX_BIN": str(FAKE_AI / "codex"),
        "PBANKI_TERMINAL_CMD": shlex.quote(str(HERE / "fake_terminal.py")),
        "PBANKI_OPEN_CMD": "/usr/bin/true",
        "PBANKI_CHOOSE_FOLDER_CMD": f"/bin/echo {shlex.quote(str(mount))}",
        "PBANKI_DEV_COMPANION_SECONDS": str(args.companion_seconds),
        "FAKE_AI_MODE": args.ai,
        "FAKE_AI_DELAY": str(args.ai_delay),
    })
    return env


def serve(args):
    root = Path(tempfile.mkdtemp(prefix="pbanki-dev-")).resolve()
    mount = root / "PB629"
    build_reader(mount, plugin=None if args.plugin == "none" else args.plugin, companion=not args.no_companion,
                 koreader=not args.no_koreader, pbanki=not args.no_pbanki, history=not args.no_history)
    make_assistant(root / "assistant")
    (root / "home").mkdir()
    if args.unplugged:
        mount.rename(mount.with_name(mount.name + ".unplugged"))

    command = [sys.executable, "-u", str(DESKTOP / "run.py"), "--no-open", "--port", str(args.port)]
    child = subprocess.Popen(command, env=environment(root, mount, args), stdout=subprocess.PIPE, text=True,
                             start_new_session=True)

    def stop(_number=None, _frame=None):
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    control = control_path(args.port)
    try:
        for line in child.stdout:
            print(line, end="", flush=True)
            found = line.split(" running at ")
            if len(found) == 2:
                url = found[1].split()[0].rstrip(".")
                control.write_text(json.dumps({"url": url, "mount": str(mount), "home": str(root / "home"),
                                               "root": str(root), "pid": os.getpid()}), encoding="utf-8")
                print(f"Dev server ready: {url}", flush=True)
        return child.wait()
    finally:
        stop()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
        control.unlink(missing_ok=True)
        shutil.rmtree(root, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", nargs="?", choices=["serve", "unplug", "plug", "stop"], default="serve")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--plugin", choices=["new", "old", "none"], default="new", help="plugin on the fake reader")
    parser.add_argument("--no-companion", action="store_true", help="no Companion beside How to Spend a Day")
    parser.add_argument("--no-koreader", action="store_true")
    parser.add_argument("--no-pbanki", action="store_true")
    parser.add_argument("--no-history", action="store_true")
    parser.add_argument("--unplugged", action="store_true", help="start with the reader unplugged")
    parser.add_argument("--ai", default="believable", help="FAKE_AI_MODE, see fixtures/fake_ai/_fake.py")
    parser.add_argument("--ai-delay", type=float, default=0.3, help="seconds each fake AI call takes")
    parser.add_argument("--companion-seconds", type=float, default=5, help="seconds Create takes in the fake Terminal")
    args = parser.parse_args()
    if args.action == "unplug":
        unplug(args.port)
    elif args.action == "plug":
        plug(args.port)
    elif args.action == "stop":
        stop_server(args.port)
    else:
        return serve(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
