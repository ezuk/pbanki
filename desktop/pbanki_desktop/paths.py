"""Where the app keeps its files, with environment overrides for tests."""

import os
from pathlib import Path, PurePosixPath

DEVICE_ROOT = "/mnt/ext1"
DEFAULT_MOUNT = "/Volumes/PB629"

KOREADER_DIR = "applications/koreader"
PBANKI_DIR = "applications/pbanki"
PLUGIN_DIR = "applications/koreader/plugins/pbanki.koplugin"
INBOX_FILE = "applications/pbanki/inbox.jsonl"
REVIEWED_FILE = "applications/pbanki/reviewed.jsonl"
CHANGES_FILE = "applications/pbanki/changes.jsonl"
CHANGES_APPLYING_FILE = "applications/pbanki/changes.applying.jsonl"
CHANGES_RESULTS_FILE = "applications/pbanki/changes.results.jsonl"
APP_FILE = "applications/pbanki/pbanki"


def repo_root():
    return Path(__file__).resolve().parents[2]


def desktop_root():
    return Path(__file__).resolve().parents[1]


def home():
    override = os.environ.get("PBANKI_DESKTOP_HOME")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "pbAnki"


def db_path():
    return home() / "pbanki.db"


def prompts_dir():
    return home() / "prompts"


def companions_dir():
    return home() / "companions"


def companion_work_dir():
    return home() / "companion-work"


def default_mount():
    return os.environ.get("PBANKI_MOUNT") or DEFAULT_MOUNT


def assistant_dir():
    override = os.environ.get("PBANKI_ASSISTANT_DIR")
    if override:
        return Path(override)
    return Path.home() / "Documents" / "GitHub" / "assistant.koplugin"


def sync_delay():
    return float(os.environ.get("PBANKI_SYNC_DELAY", "2"))


def device_to_local(device_path, mount):
    """Path on this Mac for a path on the reader, or None when it is not on the mounted disk."""
    pure = PurePosixPath(device_path)
    try:
        rel = pure.relative_to(DEVICE_ROOT)
    except ValueError:
        return None
    if ".." in rel.parts:
        return None
    return Path(mount) / rel


def local_to_device(local_path, mount):
    rel = Path(local_path).relative_to(mount)
    return str(PurePosixPath(DEVICE_ROOT) / rel.as_posix())
