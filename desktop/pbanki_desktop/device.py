"""Proven writes to the reader.

The reader's FAT disk has dropped newly created files and folders even after a clean
copy, and a read in the same mount session can be served from this Mac's cache. So a
write only counts once the disk has been unmounted and mounted again and the bytes
read back match. Only pbAnki's own files can be written; anything else raises before
touching the disk.
"""

import fcntl
import os
import plistlib
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath

from . import paths

ROUNDS = 3
MOUNT_WAIT_SECONDS = 20

# Held while the disk is briefly away, so the mount watcher doesn't report it unplugged
remount_lock = threading.Lock()


class DeviceWriteError(Exception):
    """A write to the reader could not be proven. The message is shown to the user."""


class ForbiddenPath(Exception):
    """The app tried to write a file on the reader that it must never touch."""


def check_allowed(mount, target):
    """Raises ForbiddenPath unless `target` is a file the app may write on the reader."""
    root = Path(mount).resolve()
    resolved = Path(target).resolve()
    try:
        rel = PurePosixPath(resolved.relative_to(root).as_posix())
    except ValueError:
        raise ForbiddenPath(f"{target} is not on the reader") from None
    text = str(rel)
    if text in (paths.INBOX_FILE, paths.REVIEWED_FILE, paths.CHANGES_FILE):
        return
    if text.startswith(paths.PLUGIN_DIR + "/"):
        return
    if text.endswith(".companion.md") and not text.startswith(("applications/", "system/")):
        return
    raise ForbiddenPath(f"{text} is not one of pbAnki's own files")


def _full_sync(fd):
    # os.sync() can block for minutes on other disks, so only this file is flushed
    try:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    except OSError:
        os.fsync(fd)


def _flush_to_disk(path, data):
    """Writes only from where `data` differs from the file, so a write cut off part way
    can't touch the lines already there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = len(os.path.commonprefix([_on_disk(path) or b"", data]))
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT, 0o666), "wb") as f:
        f.seek(keep)
        f.write(data[keep:])
        f.truncate(len(data))
        f.flush()
        _full_sync(f.fileno())
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        _full_sync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _on_disk(path):
    try:
        return path.read_bytes()
    except OSError:
        return None


def _run_diskutil(*args):
    return subprocess.run(["diskutil", *args], capture_output=True, timeout=60, check=False)


def is_real_disk(mount):
    """Whether `mount` is a mounted volume, not a plain folder such as a test's fake reader."""
    return Path(mount).parent == Path("/Volumes")


def remount(mount):
    """Unmounts and mounts the disk again, so later reads come from the disk itself."""
    if not is_real_disk(mount):
        return
    info = _run_diskutil("info", "-plist", str(mount))
    if info.returncode != 0:
        raise DeviceWriteError("The reader disappeared while checking the files. Plug it in and try again.")
    node = plistlib.loads(info.stdout)["DeviceIdentifier"]
    with remount_lock:
        if _run_diskutil("unmount", str(mount)).returncode != 0:
            raise DeviceWriteError(
                "Could not check the files, because something on this Mac is using the reader. "
                "Close it and try again."
            )
        mounted = _run_diskutil("mount", node)
        deadline = time.monotonic() + MOUNT_WAIT_SECONDS
        while time.monotonic() < deadline:
            if Path(mount).is_dir():
                return
            time.sleep(0.5)
    detail = mounted.stderr.decode("utf-8", "replace").strip()
    raise DeviceWriteError(f"The reader did not come back after checking the files. Unplug it and plug it in. {detail}")


def write_files(mount, files):
    """Writes `files` ({path: bytes}) to the reader and proves they landed, in up to 3 rounds."""
    for target in files:
        check_allowed(mount, target)
    pending = {Path(target): data for target, data in files.items()}
    for _round in range(ROUNDS):
        try:
            for target, data in pending.items():
                _flush_to_disk(target, data)
        except OSError as e:
            raise DeviceWriteError(f"Could not write to the reader: {e.strerror or e}") from e
        time.sleep(paths.sync_delay())
        remount(mount)
        pending = {t: d for t, d in pending.items() if _on_disk(t) != d}
        if not pending:
            return
    names = ", ".join(sorted(t.name for t in pending))
    raise DeviceWriteError(f"These files did not land on the reader after {ROUNDS} tries: {names}")


def write_file(mount, target, data):
    write_files(mount, {target: data})


def with_lines_appended(target, lines):
    """What JSON lines file `target` should hold once `lines` are appended."""
    existing = _on_disk(Path(target)) or b""
    if existing and not existing.endswith(b"\n"):
        existing += b"\n"
    return existing + "".join(line + "\n" for line in lines).encode("utf-8")


def append_lines(mount, target, lines):
    """Appends `lines` (strings without newlines) to a JSON lines file on the reader and proves it."""
    write_files(mount, {target: with_lines_appended(target, lines)})
