#!/usr/bin/env python3
"""Runs the app in live mode: open pages follow changes to the code as soon as they are saved.

    python3 desktop/live.py [--practice] [--port N] [--no-open]

Changes under web/ reach the page without a restart. Changes to Python files or standard prompts
restart the server, and the page reloads when it is back. --practice uses the fake reader and the real AI.
"""

import argparse
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent
RUN = DESKTOP / "run.py"
PRACTICE = DESKTOP / "tests" / "e2e" / "dev_server.py"
HOST = "127.0.0.1"
CHECK_SECONDS = 0.1
STOP_SECONDS = 2
KILL_SECONDS = 2
OPEN_SECONDS = 15
REAL_HOME = Path.home() / "Library" / "Application Support" / "pbAnki"
REAL_COPY = REAL_HOME.with_name("pbAnki-practice")


def restart_files():
    """Files the server only reads when it starts."""
    return [RUN, *(DESKTOP / "pbanki_desktop").rglob("*.py"), *(DESKTOP / "prompts").rglob("*.md")]


def fingerprint():
    stamps = {}
    for file in restart_files():
        try:
            info = file.stat()
        except OSError:
            continue
        stamps[file] = (info.st_mtime_ns, info.st_size)
    return stamps


def port_is_free(port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def copy_real_data():
    """Makes the practice copy of the real data folder. The real folder is only read."""
    temporary = REAL_COPY.with_name(REAL_COPY.name + ".copying")
    shutil.rmtree(temporary, ignore_errors=True)
    shutil.rmtree(REAL_COPY, ignore_errors=True)
    subprocess.run(["cp", "-cR", str(REAL_HOME), str(temporary)], check=True)
    (temporary / "running.lock").unlink(missing_ok=True)
    (temporary / "pbanki.db").unlink()
    source = sqlite3.connect(f"{(REAL_HOME / 'pbanki.db').as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(temporary / "pbanki.db")
    with target:
        source.backup(target)
    source.close()
    target.close()
    temporary.rename(REAL_COPY)


def start(args):
    script = PRACTICE if args.practice else RUN
    command = [sys.executable, "-u", str(script), "--port", str(args.port)]
    command.append("--real-ai" if args.practice else "--no-open")
    if args.real_copy:
        # Unplugged, because a connected fake reader would hide the real books
        command += ["--home", str(REAL_COPY), "--unplugged"]
    return subprocess.Popen(command, env={**os.environ, "PBANKI_LIVE_RELOAD": "1"}, start_new_session=True)


def stop(child):
    """Asks the server to finish, then insists, so a restart stays quick."""
    if child.poll() is not None:
        return
    for wait in (STOP_SECONDS, KILL_SECONDS):
        child.send_signal(signal.SIGTERM)
        try:
            child.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            pass
    child.kill()
    child.wait()


def open_when_ready(port):
    until = time.monotonic() + OPEN_SECONDS
    while time.monotonic() < until:
        try:
            socket.create_connection((HOST, port), timeout=0.2).close()
        except OSError:
            time.sleep(0.1)
            continue
        subprocess.run(["open", f"http://{HOST}:{port}"], check=False)
        return


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--practice", action="store_true", help="fake reader and the real AI")
    parser.add_argument("--port", type=int, help="port to use (default 8765, or 8766 with --practice)")
    parser.add_argument("--no-open", action="store_true", help="don't open the browser")
    parser.add_argument("--real-copy", action="store_true",
                        help="practice on a copy of your real data, made the first time and then kept")
    parser.add_argument("--fresh-copy", action="store_true", help="replace that copy with today's real data first")
    args = parser.parse_args()
    args.real_copy = args.real_copy or args.fresh_copy
    args.practice = args.practice or args.real_copy
    if args.port is None:
        args.port = 8766 if args.practice else 8765
    return args


def main():
    args = parse_args()
    if not port_is_free(args.port):
        sys.exit(f"Port {args.port} is taken. Close the other copy of pbAnki, or pick another with --port.")

    if args.fresh_copy or (args.real_copy and not REAL_COPY.is_dir()):
        print("Copying your real data for practice.", flush=True)
        copy_real_data()

    quit_now = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, lambda _number, _frame: quit_now.set())

    child = start(args)
    if not args.no_open:
        threading.Thread(target=open_when_ready, args=(args.port,), daemon=True).start()
    print("Live mode is on. Saved changes appear in the browser.", flush=True)

    stamps = fingerprint()
    try:
        while not quit_now.wait(CHECK_SECONDS):
            now = fingerprint()
            if now != stamps:
                stamps = now
                print("Server files changed, restarting.", flush=True)
                stop(child)
                child = start(args)
    finally:
        stop(child)
    return 0


if __name__ == "__main__":
    sys.exit(main())
