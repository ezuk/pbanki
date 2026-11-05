import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbanki_desktop import changes, device, paths, service
from test_clashes import NOTES, FakeQuery, make_collection
from test_service import Base

Q1, A1 = NOTES[0][1]
Q2, A2 = NOTES[1][1]


class Text(unittest.TestCase):
    def test_fields_read_as_the_reader_reads_them(self):
        self.assertEqual(changes.field_text("One<br>two &amp; three&nbsp;four"), "One\ntwo & three four")
        self.assertEqual(changes.field_text("<div>One</div><div>Two</div>"), "One\nTwo")
        self.assertEqual(changes.to_raw(" One & <two>\nthree "), "One &amp; &lt;two&gt;<br>three")

    def test_formatting_is_anything_past_line_breaks(self):
        self.assertFalse(changes.has_formatting("One<br>two<div>three</div>"))
        self.assertTrue(changes.has_formatting("One <b>two</b>"))
        self.assertTrue(changes.has_formatting('<img src="a.png">'))
        self.assertTrue(changes.has_formatting("[sound:a.mp3]"))
        self.assertFalse(changes.has_formatting("1 < 2"))


class Queue(Base):
    def setUp(self):
        super().setUp()
        make_collection(self.mount, NOTES)
        (self.mount / paths.APP_FILE).write_bytes(b"lots of code " + changes.FORMAT_MARKER + b" more code")
        self.svc = self.make_service(query=FakeQuery())
        self.svc.clashes.refresh(self.mount)
        self.queued = self.mount / paths.CHANGES_FILE
        self.results = self.mount / paths.CHANGES_RESULTS_FILE

    def edit(self, nid=1, front=Q1, back=A1, learn=False):
        return self.svc.card_edit(nid, front, back, learn)

    def send(self):
        self.svc._send_changes()

    def lines(self):
        return [json.loads(line) for line in self.queued.read_text().splitlines()]

    def rows(self):
        return self.svc._store.card_changes()

    def report(self, change_id, status="applied", **fields):
        with self.results.open("a") as f:
            f.write(json.dumps({"id": change_id, "note_id": 1, "status": status, "at": 5, **fields}) + "\n")

    def come_back(self):
        """The reader took the file, reports back, and comes back."""
        self.queued.unlink(missing_ok=True)
        self.svc.clashes.refresh(self.mount)

    def test_an_edit_waits_on_this_computer_and_only_sends_what_changed(self):
        view = self.edit(front="A new question?")
        self.assertFalse(self.queued.exists())
        self.assertEqual(view["changes"]["waiting"], 1)
        card = view["cardViews"].get("1") or self.svc.clashes.card_view(self.svc.clashes.current_notes()[0], self.rows(), True)
        self.assertEqual(card["front"], "A new question?")
        self.send()
        line = self.lines()[0]
        self.assertEqual(line["op"], "edit")
        self.assertEqual(line["note_id"], 1)
        self.assertEqual(line["front"], "A new question?")
        self.assertNotIn("back", line)
        self.assertEqual(line["expect_front"], Q1)
        self.assertEqual(self.rows()[0]["stage"], changes.WRITTEN)

    def test_new_text_is_used_by_searches_and_drops_back_when_refused(self):
        self.edit(front="A new question?")
        self.assertEqual(self.svc.clashes.active_notes()[0]["front"], "A new question?")
        self.send()
        self.report(self.rows()[0]["id"], "refused", reason="changed", front="Newer words?", back=A1)
        self.come_back()
        self.assertEqual(self.svc.clashes.active_notes()[0]["front"], Q1)
        need = self.svc.clashes_view()["changes"]["needs"][0]
        self.assertEqual(need["tag"], "NOT APPLIED")
        self.assertEqual(need["mine"], "A new question?")
        self.assertEqual(need["theirs"], "Newer words?")
        self.assertEqual(need["mineLabel"], "Use my text")

    def test_use_my_text_goes_over_what_the_reader_has_now(self):
        self.edit(front="A new question?")
        self.send()
        first = self.rows()[0]["id"]
        self.report(first, "refused", reason="changed", front="Newer words?", back=A1)
        make_collection(self.mount, [(1, ["Newer words?", A1], "D"), *NOTES[1:]])
        self.come_back()
        self.svc.change_use_mine(first)
        self.send()
        line = self.lines()[0]
        self.assertNotEqual(line["id"], first)
        self.assertEqual(line["expect_front"], "Newer words?")

    def test_a_second_edit_replaces_the_first_and_returning_to_the_original_takes_it_back(self):
        self.edit(front="First try?")
        self.edit(front="Second try?")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["front"], "Second try?")
        self.assertEqual(self.rows()[0]["expect_front"], Q1)
        view = self.edit(front=Q1)
        self.assertEqual(self.rows(), [])
        self.assertEqual(view["changes"]["waiting"], 0)

    def test_an_edit_on_top_of_one_already_on_an_unplugged_reader_expects_its_text(self):
        self.edit(front="First try?")
        self.send()
        self.unplug()
        self.edit(front="Second try?")
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["expect_front"], "First try?")

    def test_the_rules_are_checked_before_queueing(self):
        with self.assertRaisesRegex(service.ServiceError, "A card needs a question."):
            self.edit(front="")
        make_collection(self.mount, [(1, ["<b>Bold</b> question", A1], "D")])
        self.svc.clashes.refresh(self.mount)
        with self.assertRaisesRegex(service.ServiceError, "formatting"):
            self.edit(front="Plain question")
        self.edit(front="Bold question", back="A new answer")
        self.assertEqual(self.rows()[0]["back"], "A new answer")

    def test_a_card_with_formatting_on_both_sides_is_locked(self):
        note = {"raw_front": "<b>a</b>", "raw_back": '<img src="b">', "notetype": "Basic", "fields": 2, "cards": 1}
        lock = changes.locks(note)
        self.assertFalse(lock["canEdit"])
        self.assertIn("formatting", lock["lockNote"])

    def test_several_cards_or_another_note_type_can_only_change_in_anki(self):
        self.assertFalse(changes.locks({"raw_front": "a", "raw_back": "b", "notetype": "Basic", "fields": 2, "cards": 2})["canEdit"])
        self.assertFalse(changes.locks({"raw_front": "a", "raw_back": "b", "notetype": "Mine", "fields": 2, "cards": 1})["canEdit"])

    def test_a_simplified_copy_cant_be_changed(self):
        self.svc._store.replace_collection_notes([{"nid": 1, "deck": "D", "front": "Q", "back": "A"}], 1)
        with self.assertRaisesRegex(service.ServiceError, "Plug in your reader once"):
            self.edit(front="New?")
        view = self.svc.clashes_view()
        self.assertTrue(view["noRaw"])

    def test_cloze_numbers_have_to_stay(self):
        make_collection(self.mount, [(1, ["Say {{c1::hello}} there", ""], "D")], notetype="Cloze")
        self.svc.clashes.refresh(self.mount)
        with self.assertRaisesRegex(service.ServiceError, "same cloze numbers"):
            self.svc.card_edit(1, "Say {{c2::hello}} there", "", False)
        self.svc.card_edit(1, "Now say {{c1::hello}} there", "", False)
        self.assertEqual(self.rows()[0]["front"], "Now say {{c1::hello}} there")

    # Undo

    def test_undo_while_waiting_leaves_nothing_behind(self):
        self.edit(front="New?")
        view = self.svc.change_undo(self.rows()[0]["id"])
        self.assertEqual(self.rows(), [])
        self.assertEqual(view["changes"]["open"], 0)

    def test_undo_on_a_plugged_in_reader_rewrites_the_file_without_it(self):
        self.edit(front="New?")
        self.edit(nid=2, front="Other?", back=A2)
        self.send()
        first, second = self.rows()
        self.svc.change_undo(first["id"])
        self.assertEqual([line["id"] for line in self.lines()], [second["id"]])

    def test_undo_is_refused_once_the_reader_is_away(self):
        self.edit(front="New?")
        self.send()
        row = self.rows()[0]
        self.unplug()
        self.assertFalse(self.svc.clashes_view()["changes"]["moving"][0]["canUndo"])
        with self.assertRaisesRegex(service.ServiceError, "on your reader already"):
            self.svc.change_undo(row["id"])
        self.assertEqual(self.rows()[0]["id"], row["id"])

    def test_undo_that_cant_be_proved_keeps_the_change(self):
        self.edit(front="New?")
        self.send()
        row = self.rows()[0]
        with mock.patch.object(device, "write_files", side_effect=device.DeviceWriteError("Gone.")):
            with self.assertRaisesRegex(service.ServiceError, "Could not take it back"):
                self.svc.change_undo(row["id"])
        self.assertEqual(self.rows()[0]["stage"], changes.WRITTEN)

    def test_undo_finds_out_first_if_pbanki_already_took_it(self):
        self.edit(front="New?")
        self.send()
        row = self.rows()[0]
        self.report(row["id"])
        with self.assertRaisesRegex(service.ServiceError, "on your reader already"):
            self.svc.change_undo(row["id"])

    # Going to the reader

    def test_it_goes_by_itself_when_the_reader_is_there(self):
        self.edit(front="New?")
        self.svc._seen_connected, self.svc._seen_mount = True, self.mount
        self.svc._seen_mount = str(self.mount)
        self.svc._poll_reader()
        self.assertEqual(self.rows()[0]["stage"], changes.WRITTEN)

    def test_it_waits_while_the_reader_is_unplugged(self):
        self.edit(front="New?")
        self.unplug()
        self.assertFalse(changes.send(self.svc._store, self.mount))
        self.assertEqual(self.rows()[0]["stage"], changes.WAITING)

    def test_an_old_build_writes_nothing_and_says_so(self):
        (self.mount / paths.APP_FILE).write_bytes(b"an older app without the mark")
        self.edit(front="New?")
        self.send()
        self.assertFalse(self.queued.exists())
        self.assertEqual(self.rows()[0]["stage"], changes.WAITING)
        view = self.svc.clashes_view()["changes"]
        self.assertEqual(view["banner"]["kind"], "old")
        self.assertIn("pbAnki is updated", view["saveHint"])
        (self.mount / paths.APP_FILE).write_bytes(b"new " + changes.FORMAT_MARKER)
        self.svc._read_clashes(self.mount)
        self.assertEqual(self.rows()[0]["stage"], changes.WRITTEN)

    def test_a_reader_without_the_app_counts_as_an_old_build(self):
        (self.mount / paths.APP_FILE).unlink()
        self.assertFalse(changes.app_takes_changes(self.svc._store, self.mount))

    def test_the_mark_is_found_across_a_chunk_edge(self):
        (self.mount / paths.APP_FILE).write_bytes(b"x" * ((1 << 20) - 5) + changes.FORMAT_MARKER)
        self.assertTrue(changes.app_takes_changes(self.svc._store, self.mount))

    def test_a_reader_unplugged_mid_write_keeps_the_change_waiting_and_tries_again(self):
        self.edit(front="New?")
        with mock.patch.object(device, "write_files", side_effect=device.DeviceWriteError("The reader disappeared.")):
            self.send()
        self.assertEqual(self.rows()[0]["stage"], changes.WAITING)
        view = self.svc.clashes_view()["changes"]
        self.assertEqual(view["banner"], {"kind": "error", "text": "The reader disappeared."})
        self.assertFalse(self.svc._changes_to_send())
        self.svc._read_clashes(self.mount)
        self.assertEqual(self.rows()[0]["stage"], changes.WRITTEN)

    def test_a_lost_file_sends_again(self):
        self.edit(front="New?")
        self.send()
        id_ = self.rows()[0]["id"]
        self.queued.unlink()
        self.svc._read_clashes(self.mount)
        self.assertEqual(self.lines()[0]["id"], id_)
        self.assertEqual(self.rows()[0]["stage"], changes.WRITTEN)

    def test_a_cut_off_file_is_written_whole_again(self):
        self.edit(front="New?")
        self.send()
        data = self.queued.read_bytes()
        self.queued.write_bytes(data[:10])
        self.svc._read_clashes(self.mount)
        self.assertEqual(self.queued.read_bytes(), data)

    def test_changes_pbanki_started_on_arent_sent_twice_or_taken_back(self):
        self.edit(front="New?")
        self.send()
        row = self.rows()[0]
        self.queued.rename(self.mount / paths.CHANGES_APPLYING_FILE)
        self.svc._read_clashes(self.mount)
        self.assertFalse(self.queued.exists())
        self.assertEqual(self.rows()[0]["claimed"], 1)
        with self.assertRaisesRegex(service.ServiceError, "on your reader already"):
            self.svc.change_undo(row["id"])

    def test_only_its_own_file_can_be_written(self):
        device.check_allowed(self.mount, self.mount / paths.CHANGES_FILE)
        with self.assertRaises(device.ForbiddenPath):
            device.check_allowed(self.mount, self.mount / paths.CHANGES_RESULTS_FILE)
        with self.assertRaises(device.ForbiddenPath):
            device.check_allowed(self.mount, self.mount / paths.PBANKI_DIR / "collection" / "collection.anki2")

    # Reading back

    def test_an_applied_edit_moves_along_as_the_collection_says(self):
        self.edit(front="New?")
        self.send()
        id_ = self.rows()[0]["id"]
        self.report(id_)
        make_collection(self.mount, [(1, ["New?", A1], "D"), *NOTES[1:]], usn=-1)
        self.come_back()
        item = self.svc.clashes_view()["changes"]["moving"][0]
        self.assertEqual(self.rows()[0]["stage"], changes.APPLIED)
        self.assertEqual(item["now"], "Applied on your reader. It goes to AnkiWeb next time pbAnki syncs.")
        self.assertFalse(item["canUndo"])
        make_collection(self.mount, [(1, ["New?", A1], "D"), *NOTES[1:]], usn=7)
        self.come_back()
        view = self.svc.clashes_view()["changes"]
        self.assertEqual(view["moving"], [])
        self.assertEqual(view["done"][0]["tag"], "ON ANKIWEB")
        self.assertEqual(view["open"], 0)

    def test_a_result_line_is_only_taken_once(self):
        self.edit(front="New?")
        self.send()
        id_ = self.rows()[0]["id"]
        self.report(id_, "refused", reason="changed", front="Newer?", back=A1)
        self.come_back()
        self.report(id_)
        self.come_back()
        self.assertEqual(self.rows()[0]["problem"], "changed")

    def test_lost_results_change_nothing_and_applied_stays_applied(self):
        self.edit(front="New?")
        self.send()
        self.report(self.rows()[0]["id"])
        make_collection(self.mount, [(1, ["New?", A1], "D"), *NOTES[1:]])
        self.come_back()
        self.results.unlink()
        self.come_back()
        self.assertEqual(self.rows()[0]["stage"], changes.APPLIED)

    def test_a_download_that_puts_the_old_text_back_needs_you(self):
        self.edit(front="New?")
        self.send()
        id_ = self.rows()[0]["id"]
        self.report(id_)
        make_collection(self.mount, [(1, ["New?", A1], "D"), *NOTES[1:]], usn=7)
        self.come_back()
        make_collection(self.mount, NOTES, usn=8)
        self.come_back()
        need = self.svc.clashes_view()["changes"]["needs"][0]
        self.assertEqual(need["tag"], "UNDONE BY A DOWNLOAD")
        self.assertEqual(need["mineLabel"], "Apply again")
        self.svc.change_use_mine(id_)
        self.send()
        self.assertEqual(self.lines()[0]["front"], "New?")

    def test_a_card_deleted_elsewhere_is_reported_with_your_text(self):
        self.edit(front="New?")
        self.send()
        id_ = self.rows()[0]["id"]
        self.report(id_, "refused", reason="gone")
        make_collection(self.mount, NOTES[1:])
        self.come_back()
        need = self.svc.clashes_view()["changes"]["needs"][0]
        self.assertEqual(need["tag"], "CARD IS GONE")
        self.assertEqual(need["mine"], "New?")
        self.assertEqual(need["dropLabel"], "OK")
        self.assertFalse(need["showMine"])
        view = self.svc.change_drop(id_)
        self.assertEqual(view["changes"]["needs"], [])

    def test_a_refusal_for_any_other_reason_says_so_without_losing_text(self):
        self.edit(front="New?")
        self.send()
        self.report(self.rows()[0]["id"], "refused", reason="failed", detail="disk full")
        self.come_back()
        need = self.svc.clashes_view()["changes"]["needs"][0]
        self.assertIn("disk full", need["title"])
        self.assertEqual(need["mineLabel"], "Try again")

    # Suspend and delete

    def test_suspending_settles_the_pair_and_unsuspending_brings_it_back(self):
        self.svc._store.set_clash_pair({"a": 1, "b": 2, "fingerprint": "f", "front_a": Q1, "back_a": A1, "front_b": Q2, "back_b": A2, "state": "new"})
        from pbanki_desktop import clashes
        by_id = {n["nid"]: n for n in self.svc._store.collection_notes()}
        self.svc._store.update_clash_pair(1, 2, fingerprint=clashes.fingerprint(by_id[1], by_id[2]))
        self.assertEqual(self.svc.clashes_view()["pairs"][0]["status"], "open")
        view = self.svc.card_suspend(1)
        pair = view["pairs"][0]
        self.assertEqual(pair["status"], "settled")
        self.assertEqual(pair["state"], "real")
        self.assertEqual(view["cardViews"]["1"]["tagText"], "WILL BE SUSPENDED")
        self.assertEqual(view["cardViews"]["1"]["undoLabel"], "Unsuspend")
        self.assertEqual(view["counts"]["clashes"], 0)
        view = self.svc.card_unsuspend(1)
        self.assertEqual(view["pairs"][0]["status"], "open")
        self.assertEqual(view["pairs"][0]["state"], "new")
        self.assertEqual(self.rows(), [])
        self.assertEqual([row["verdict"] for row in self.svc._store.clash_verdicts()], ["real", "undo-real"])

    def test_unsuspending_after_it_applied_queues_an_unsuspend(self):
        self.svc.card_suspend(1)
        self.send()
        suspend = self.rows()[0]["id"]
        self.report(suspend)
        make_collection(self.mount, NOTES, suspended=(1,))
        self.come_back()
        self.svc.card_unsuspend(1)
        self.send()
        self.assertEqual(self.lines()[0]["op"], "unsuspend")
        self.assertIsNotNone(self.svc._store.card_change(suspend)["closed_at"])

    def test_delete_keeps_a_copy_and_needs_a_suspend_first(self):
        with self.assertRaisesRegex(service.ServiceError, "Suspend this card first"):
            self.svc.card_delete(1)
        self.svc.card_suspend(1)
        view = self.svc.card_delete(1)
        row = self.rows()[-1]
        self.assertEqual(row["op"], "delete")
        self.assertEqual(json.loads(row["copy_json"])["raw_front"], Q1)
        self.assertEqual(view["changes"]["moving"][0]["kind"], "Delete for good")
        self.assertEqual(view["changes"]["suspended"], [])
        view = self.svc.change_undo(row["id"])
        self.assertEqual(len(view["changes"]["suspended"]), 1)

    def test_delete_reaches_ankiweb_once_its_grave_has_synced(self):
        self.svc.card_suspend(1)
        self.svc.card_delete(1)
        self.send()
        ids = [row["id"] for row in self.rows() if row["closed_at"] is None]
        self.report(ids[0])
        path = make_collection(self.mount, NOTES[1:])
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO graves VALUES (1, 1, -1)")
        conn.commit()
        conn.close()
        self.come_back()
        self.assertEqual(self.svc._store.card_change(ids[0])["stage"], changes.APPLIED)
        make_collection(self.mount, NOTES[1:])
        self.come_back()
        view = self.svc.clashes_view()["changes"]
        self.assertEqual(view["done"][0]["tag"], "DELETED")

    def test_a_suspend_undone_elsewhere_needs_you(self):
        self.svc.card_suspend(1)
        self.send()
        self.report(self.rows()[0]["id"])
        make_collection(self.mount, NOTES, usn=4)
        self.come_back()
        self.assertEqual(self.rows()[0]["problem"], changes.UNDONE)
        need = self.svc.clashes_view()["changes"]["needs"][0]
        self.assertEqual(need["tag"], "UNDONE")

    # The banners

    def test_banners_follow_the_reader(self):
        self.edit(front="New?")
        self.unplug()
        banner = self.svc.clashes_view()["changes"]["banner"]
        self.assertEqual(banner["title"], "Plug in your PocketBook to send 1 change")
        self.replug()
        self.assertEqual(self.svc.clashes_view()["changes"]["banner"]["title"], "Reader found. Writing your changes...")
        self.send()
        banner = self.svc.clashes_view()["changes"]["banner"]
        self.assertEqual(banner["kind"], "eject")
        self.assertEqual(banner["title"], "1 change on your reader")
        self.unplug()
        banner = self.svc.clashes_view()["changes"]["banner"]
        self.assertEqual(banner["kind"], "plain")
        self.assertIn("Open pbAnki on your reader", banner["text"])

    def test_cards_show_their_age_and_reviews(self):
        now = time.time()
        note = {"nid": int((now - 86400 * 430) * 1000), "reps": 23, "lapses": 2, "ivl": 41}
        from pbanki_desktop import clashes
        self.assertEqual(clashes._stats(note, now), "Added 14 months ago · 23 reviews · 2 lapses · Interval 41 days")
        self.assertEqual(clashes._stats({**note, "nid": int((now - 86400 * 21) * 1000), "reps": 0}, now), "Added 3 weeks ago · Not reviewed yet")
        self.assertEqual(clashes._stats({"nid": 1, "reps": None}, now), "")


if __name__ == "__main__":
    unittest.main()
