"""The whole path on a throwaway reader: this app, the reader's real Rust code, and Anki's real sync server."""

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbanki_desktop import changes, paths, service
from test_clashes import FakeQuery
from test_service import Base

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "target" / "debug" / "reader_sim"


def setUpModule():
    if shutil.which("cargo") is None:
        raise unittest.SkipTest("cargo isn't here")
    subprocess.run(["cargo", "build", "-q", "-p", "dev", "--bin", "reader_sim"], cwd=ROOT, check=True)


class Cloud:
    def __init__(self, folder):
        self.process = subprocess.Popen([TOOL, "serve", str(folder)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.url = self.process.stdout.readline().strip()

    def stop(self):
        self.process.kill()
        self.process.wait()
        self.process.stdout.close()


class Loop(Base):
    def setUp(self):
        super().setUp()
        self.cloud = Cloud(self.tmp / "cloud")
        self.addCleanup(self.cloud.stop)
        self.app = str(self.mount / paths.PBANKI_DIR)
        self.other = str(self.tmp / "other-device")
        self.ids = self.sim_json("create", self.app, json.dumps([
            {"front": "What is the aim of meditation?", "back": "To end the war with thoughts."},
            {"front": "What is meditation's purpose?", "back": "Transforming the heart."},
            {"front": "What is the capital of Australia?", "back": "Canberra"},
        ]))
        self.sim("upload", self.app, self.cloud.url)
        self.sim("download", self.other, self.cloud.url)
        shutil.copyfile(TOOL, self.mount / paths.APP_FILE)
        self.svc = self.make_service(query=FakeQuery())
        self.plug_in()

    def sim(self, *args):
        found = subprocess.run([TOOL, *args], capture_output=True, text=True, check=True)
        return found.stdout

    def sim_json(self, *args):
        return json.loads(self.sim(*args))

    def plug_in(self):
        self.svc._read_clashes(self.mount)

    def view(self):
        return self.svc.clashes_view()["changes"]

    def open_reader(self):
        return self.sim_json("open", self.app)

    def sync(self, where=None):
        # Sync compares whole-second modification times
        time.sleep(1.1)
        self.sim("sync", where or self.app, self.cloud.url)

    def note(self, where, nid):
        return next((row for row in self.sim_json("dump", where) if row["id"] == nid), None)

    def stage(self, nid):
        return next(row for row in reversed(self.svc._store.card_changes()) if row["nid"] == nid)

    def edit(self, nid, front=None, back=None, learn=False):
        note = next(n for n in self.svc._store.collection_notes() if n["nid"] == nid)
        return self.svc.card_edit(nid, front if front is not None else note["front"], back if back is not None else note["back"], learn)

    def test_an_edit_reaches_the_reader_then_ankiweb_and_another_device(self):
        nid = self.ids[0]
        self.sim("review", self.app, str(nid))
        self.sim("sync", self.app, self.cloud.url)
        self.plug_in()
        self.edit(nid, back="To change how we relate to thoughts.", learn=True)
        self.assertEqual(self.stage(nid)["stage"], changes.WAITING)
        self.svc._send_changes()
        self.assertEqual(self.stage(nid)["stage"], changes.WRITTEN)
        self.assertEqual(self.view()["banner"]["kind"], "eject")

        answer = self.open_reader()
        self.assertEqual(answer["applied"], 1)
        self.assertEqual(answer["notice"], "✓ 1 change from your Mac applied.")
        self.plug_in()
        self.assertEqual(self.stage(nid)["stage"], changes.APPLIED)
        self.assertEqual(self.view()["moving"][0]["now"], "Applied on your reader. It goes to AnkiWeb next time pbAnki syncs.")

        self.sync()
        self.plug_in()
        self.assertEqual(self.stage(nid)["stage"], changes.SYNCED)
        self.assertEqual(self.view()["done"][0]["tag"], "ON ANKIWEB")

        self.sync(self.other)
        on_other = self.note(self.other, nid)
        self.assertEqual(on_other["back"], "To change how we relate to thoughts.")
        self.assertTrue(on_other["new"])
        self.assertEqual(self.note(self.app, nid)["revlog"], 2, "the review log stays, with the reset in it")

    def test_opening_twice_applies_once(self):
        nid = self.ids[0]
        self.edit(nid, front="A new question?")
        self.svc._send_changes()
        self.assertEqual(self.open_reader()["applied"], 1)
        self.assertEqual(self.open_reader()["applied"], 0)
        results = (self.mount / paths.CHANGES_RESULTS_FILE).read_text().splitlines()
        self.assertEqual(len(results), 1)

    def test_a_stale_edit_doesnt_overwrite_and_use_my_text_does(self):
        nid = self.ids[0]
        self.edit(nid, front="My wording?")
        self.svc._send_changes()
        self.sim("edit", self.app, str(nid), "Wording from the reader?")
        self.assertEqual(self.open_reader()["refused"], 1)
        self.assertEqual(self.note(self.app, nid)["front"], "Wording from the reader?")
        self.plug_in()
        need = self.view()["needs"][0]
        self.assertEqual((need["mine"], need["theirs"]), ("My wording?", "Wording from the reader?"))
        self.svc.change_use_mine(self.stage(nid)["id"])
        self.svc._send_changes()
        self.assertEqual(self.open_reader()["applied"], 1)
        self.assertEqual(self.note(self.app, nid)["front"], "My wording?")

    def test_a_card_deleted_elsewhere_is_reported_and_your_text_kept(self):
        nid = self.ids[0]
        self.edit(nid, front="My wording?")
        self.svc._send_changes()
        self.sim("remove", self.app, str(nid))
        self.assertEqual(self.open_reader()["refused"], 1)
        self.plug_in()
        need = self.view()["needs"][0]
        self.assertEqual(need["tag"], "CARD IS GONE")
        self.assertEqual(need["mine"], "My wording?")

    def test_an_interrupted_apply_finishes_next_time(self):
        first, second = self.ids[:2]
        self.edit(first, front="First wording?")
        self.edit(second, front="Second wording?")
        self.svc._send_changes()
        # pbAnki claimed the file and applied one line, then the power went
        queued = self.mount / paths.CHANGES_FILE
        lines = queued.read_text().splitlines()
        queued.write_text(lines[0] + "\n")
        self.open_reader()
        (self.mount / paths.CHANGES_APPLYING_FILE).write_text("\n".join(lines) + "\n")
        self.assertEqual(self.open_reader()["applied"], 1)
        self.assertFalse((self.mount / paths.CHANGES_APPLYING_FILE).exists())
        self.assertEqual(self.note(self.app, first)["front"], "First wording?")
        self.assertEqual(self.note(self.app, second)["front"], "Second wording?")
        self.plug_in()
        self.assertEqual(self.stage(first)["stage"], changes.APPLIED)
        self.assertEqual(self.stage(second)["stage"], changes.APPLIED)

    def test_an_old_build_gets_nothing_until_it_is_updated(self):
        nid = self.ids[0]
        (self.mount / paths.APP_FILE).write_bytes(b"an older pbAnki")
        self.edit(nid, front="My wording?")
        self.svc._send_changes()
        self.assertFalse((self.mount / paths.CHANGES_FILE).exists())
        self.assertEqual(self.view()["banner"]["kind"], "old")
        shutil.copyfile(TOOL, self.mount / paths.APP_FILE)
        self.plug_in()
        self.assertEqual(self.open_reader()["applied"], 1)

    def test_a_write_cut_off_mid_way_loses_nothing(self):
        first, second = self.ids[:2]
        self.edit(first, front="First wording?")
        self.edit(second, front="Second wording?")
        self.svc._send_changes()
        queued = self.mount / paths.CHANGES_FILE
        data = queued.read_bytes()
        queued.write_bytes(data[: data.index(b"\n") + 20])
        answer = self.open_reader()
        self.assertEqual((answer["applied"], answer["refused"]), (1, 1))
        self.plug_in()
        self.assertEqual(self.open_reader()["applied"], 1)
        self.assertEqual(self.note(self.app, second)["front"], "Second wording?")
        self.plug_in()
        self.assertEqual(self.view()["needs"], [])

    def test_a_lost_file_goes_again(self):
        nid = self.ids[0]
        self.edit(nid, front="My wording?")
        self.svc._send_changes()
        (self.mount / paths.CHANGES_FILE).unlink()
        self.assertEqual(self.open_reader()["applied"], 0)
        self.plug_in()
        self.assertTrue((self.mount / paths.CHANGES_FILE).exists())
        self.assertEqual(self.open_reader()["applied"], 1)

    def test_lost_results_dont_apply_anything_twice(self):
        nid = self.ids[0]
        self.edit(nid, front="My wording?")
        self.svc._send_changes()
        data = (self.mount / paths.CHANGES_FILE).read_bytes()
        self.open_reader()
        (self.mount / paths.CHANGES_RESULTS_FILE).unlink()
        (self.mount / paths.CHANGES_FILE).write_bytes(data)
        self.assertEqual(self.open_reader()["refused"], 0)
        self.plug_in()
        self.assertEqual(self.stage(nid)["stage"], changes.APPLIED)

    def test_suspend_unsuspend_and_delete_for_good(self):
        nid = self.ids[0]
        self.svc.card_suspend(nid)
        self.svc._send_changes()
        self.open_reader()
        self.assertTrue(self.note(self.app, nid)["suspended"])
        self.plug_in()
        self.sync()
        self.plug_in()
        self.assertEqual(self.stage(nid)["on_ankiweb"], 1)
        self.assertEqual(self.view()["suspended"][0]["now"], "On AnkiWeb.")

        self.svc.card_unsuspend(nid)
        self.svc._send_changes()
        self.open_reader()
        self.assertFalse(self.note(self.app, nid)["suspended"])
        self.plug_in()

        self.svc.card_suspend(nid)
        self.svc._send_changes()
        self.open_reader()
        self.plug_in()
        self.svc.card_delete(nid)
        self.svc._send_changes()
        self.open_reader()
        self.assertIsNone(self.note(self.app, nid))
        self.plug_in()
        self.assertEqual(self.stage(nid)["stage"], changes.APPLIED, "not on AnkiWeb until it syncs")
        self.sync()
        self.plug_in()
        self.assertEqual(self.stage(nid)["on_ankiweb"], 1)
        self.sync(self.other)
        self.assertIsNone(self.note(self.other, nid))
        self.assertEqual(json.loads(self.stage(nid)["copy_json"])["raw_front"], "What is the aim of meditation?")

    def test_a_download_is_caught_and_apply_again_works(self):
        nid = self.ids[0]
        self.edit(nid, front="My wording?")
        self.svc._send_changes()
        self.open_reader()
        self.plug_in()
        self.sim("download", self.app, self.cloud.url)
        self.plug_in()
        need = self.view()["needs"][0]
        self.assertEqual(need["tag"], "UNDONE BY A DOWNLOAD")
        self.svc.change_use_mine(self.stage(nid)["id"])
        self.svc._send_changes()
        self.assertEqual(self.open_reader()["applied"], 1)
        self.assertEqual(self.note(self.app, nid)["front"], "My wording?")

    def test_unsynced_edits_on_the_reader_stay_reported_as_not_on_ankiweb(self):
        nid = self.ids[0]
        self.edit(nid, front="My wording?")
        self.svc._send_changes()
        self.open_reader()
        self.plug_in()
        self.plug_in()
        self.assertEqual(self.stage(nid)["on_ankiweb"], 0)
        self.assertIsNone(self.svc._store.get_setting(changes.ERROR_SETTING))


if __name__ == "__main__":
    unittest.main()
