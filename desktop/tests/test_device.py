import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["PBANKI_SYNC_DELAY"] = "0"

from pbanki_desktop import device, paths


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mount = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_appends_with_a_missing_final_newline(self):
        inbox = self.mount / paths.INBOX_FILE
        inbox.parent.mkdir(parents=True)
        inbox.write_bytes(b'{"a":1}')
        device.append_lines(self.mount, inbox, ['{"b":2}'])
        self.assertEqual(inbox.read_bytes(), b'{"a":1}\n{"b":2}\n')

    def test_refuses_files_that_are_not_pbankis(self):
        for rel in (
            "system/config/desktop/view.json",
            "applications/koreader/history.lua",
            "applications/koreader/settings.reader.lua",
            "English/Book.sdr/metadata.epub.lua",
            "applications/pbanki/config.toml",
            "applications/x.companion.md",
            "../outside.companion.md",
        ):
            with self.assertRaises(device.ForbiddenPath, msg=rel):
                device.write_file(self.mount, self.mount / rel, b"x")

    def test_allows_companion_beside_a_book_and_plugin_files(self):
        device.write_file(self.mount, self.mount / "English/Book.companion.md", b"x")
        device.write_file(self.mount, self.mount / paths.PLUGIN_DIR / "main.lua", b"y")

    def test_fails_loudly_when_the_write_does_not_land(self):
        inbox = self.mount / paths.INBOX_FILE
        with mock.patch.object(device, "_on_disk", return_value=b"something else"):
            with self.assertRaises(device.DeviceWriteError):
                device.write_file(self.mount, inbox, b"data")

    def test_rewrites_only_what_vanished_after_the_remount(self):
        inbox = self.mount / paths.INBOX_FILE
        reviewed = self.mount / paths.REVIEWED_FILE
        writes = []
        real_flush = device._flush_to_disk
        vanished = []

        def flush(path, data):
            writes.append(path.name)
            real_flush(path, data)

        def remount(_mount):
            if not vanished:
                vanished.append(True)
                reviewed.unlink()

        with mock.patch.object(device, "_flush_to_disk", flush), mock.patch.object(device, "remount", remount):
            device.write_files(self.mount, {inbox: b"a", reviewed: b"b"})
        self.assertEqual(writes, ["inbox.jsonl", "reviewed.jsonl", "reviewed.jsonl"])
        self.assertEqual(reviewed.read_bytes(), b"b")

    def test_a_fake_reader_folder_is_not_remounted(self):
        with mock.patch.object(device, "_run_diskutil") as diskutil:
            device.remount(self.mount)
        diskutil.assert_not_called()

    def test_a_real_disk_is_unmounted_and_mounted_again(self):
        calls = []

        def diskutil(*args):
            calls.append(args[0])
            out = plistlib.dumps({"DeviceIdentifier": "disk4"}) if args[0] == "info" else b""
            return subprocess.CompletedProcess(args, 0, out, b"")

        with mock.patch.object(device, "_run_diskutil", diskutil), mock.patch.object(Path, "is_dir", return_value=True):
            device.remount("/Volumes/PB629")
        self.assertEqual(calls, ["info", "unmount", "mount"])

    def test_a_busy_disk_is_reported_in_plain_words(self):
        def diskutil(*args):
            out = plistlib.dumps({"DeviceIdentifier": "disk4"}) if args[0] == "info" else b""
            return subprocess.CompletedProcess(args, 1 if args[0] == "unmount" else 0, out, b"")

        with mock.patch.object(device, "_run_diskutil", diskutil):
            with self.assertRaisesRegex(device.DeviceWriteError, "using the reader"):
                device.remount("/Volumes/PB629")


class InPlaceWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "file.jsonl"

    def writes_from(self, data):
        """Writes `data` through _flush_to_disk and returns where in the file it started writing."""
        seeks = []
        real = device.os.fdopen

        def fdopen(fd, mode):
            f = real(fd, mode)

            class Spy:
                def __getattr__(self, name):
                    return getattr(f, name)

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    f.close()

                def seek(self, at):
                    seeks.append(at)
                    return f.seek(at)

            return Spy()

        with mock.patch.object(device.os, "fdopen", fdopen):
            device._flush_to_disk(self.path, data)
        return seeks[0]

    def test_a_new_file_is_written_whole(self):
        self.assertEqual(self.writes_from(b"abc\n"), 0)
        self.assertEqual(self.path.read_bytes(), b"abc\n")

    def test_appending_leaves_what_is_there_alone(self):
        self.path.write_bytes(b"old\n")
        self.assertEqual(self.writes_from(b"old\nnew\n"), 4)
        self.assertEqual(self.path.read_bytes(), b"old\nnew\n")

    def test_a_cut_off_end_is_replaced_from_where_it_differs(self):
        self.path.write_bytes(b'old\n{"fr')
        self.assertEqual(self.writes_from(b'old\n{"x":1}\n'), 6)
        self.assertEqual(self.path.read_bytes(), b'old\n{"x":1}\n')

    def test_shorter_contents_shrink_the_file(self):
        self.path.write_bytes(b"old\nlonger and longer\n")
        self.writes_from(b"old\n")
        self.assertEqual(self.path.read_bytes(), b"old\n")

    def test_nothing_new_writes_nothing(self):
        self.path.write_bytes(b"same\n")
        self.assertEqual(self.writes_from(b"same\n"), 5)
        self.assertEqual(self.path.read_bytes(), b"same\n")


if __name__ == "__main__":
    unittest.main()
