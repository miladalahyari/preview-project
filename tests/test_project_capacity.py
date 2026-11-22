"""Project-wide capacity checks against actual lifecycle reservation behavior."""
import copy
import datetime as dt
import unittest
from unittest.mock import patch

from test_preview import AT, CATALOG, ENV, SOURCE, FakeAPI, FakeArgo, MemoryStore, preview


class ProjectCapacityTests(unittest.TestCase):
    def candidate(self):
        return preview.make_preview(SOURCE, CATALOG, AT, "b" * 40)

    def held(self, number, status="active", project="123", service="example-app", phase=None):
        source = dict(SOURCE, sourceProjectId=project, sourcePipelineId=str(1000 + number),
                      sourceTag="test-slot-" + str(number), sourceSha=f"{number:03x}" + "a" * 37)
        value = preview.make_preview(source, CATALOG, AT, "b" * 40)
        if service != "example-app":
            value.update(service=service, id=f"{service}-{project}-{1000 + number}",
                         namespace=f"test-{service}-{project}-{1000 + number}",
                         host=f"test-{number:03x}-{service}.company.example")
        value.update(imageDigest="sha256:" + "d" * 64,
                     expiresAt=preview.timestamp(AT + dt.timedelta(hours=12)))
        directory = "pending" if status == "pending" else "previews"
        record = {"status": status, "preview": value}
        if phase is not None:
            directory = "cleanup"
            record.update(status="cleaning", phase=phase, cleanupStartedAt=preview.timestamp(AT))
        return preview.record_path(directory, value), record

    def lifecycle(self, entries=()):
        lifecycle = preview.Lifecycle(FakeAPI(), MemoryStore(dict(entries)), FakeArgo(), "777", "999")
        lifecycle.verified_source = lambda env: dict(SOURCE)
        return lifecycle

    def prepare(self, lifecycle, at=AT, env=None):
        with patch.object(preview, "choose_catalog", return_value=copy.deepcopy(CATALOG)), patch.object(preview, "now", return_value=at):
            return lifecycle.prepare(env or ENV)

    def assert_full(self, entries):
        lifecycle = self.lifecycle(entries)
        before = copy.deepcopy(lifecycle.store.data)
        with self.assertRaisesRegex(preview.Failure, "3-preview limit"):
            self.prepare(lifecycle)
        self.assertEqual(lifecycle.store.data, before)
        self.assertFalse(lifecycle.store.writes)
        self.assertNotIn(preview.request_path(SOURCE), lifecycle.store.data)
        return lifecycle

    def test_new_reservations_allowed_until_third_slot(self):
        self.assertEqual(preview.MAX_PROJECT_PREVIEWS, 3)
        for occupied in range(3):
            with self.subTest(occupied=occupied):
                lifecycle = self.lifecycle([self.held(i + 1) for i in range(occupied)])
                request = self.prepare(lifecycle)
                self.assertTrue(request["buildRequired"])
                self.assertEqual(request["status"], "pending")
                self.assertEqual(len(preview.occupied_project_previews(lifecycle.all_records(), "123")), occupied + 1)
                self.assertEqual(len(lifecycle.store.writes), 1)

    def test_fourth_reservation_rejected_before_any_state_write(self):
        self.assert_full([self.held(1), self.held(2), self.held(3)])

    def test_pending_provisioning_and_active_each_consume_slot(self):
        self.assert_full([self.held(1, "pending"), self.held(2, "provisioning"), self.held(3, "active")])

    def test_expired_entries_hold_capacity_until_teardown(self):
        entries = [self.held(1, "pending"), self.held(2, "provisioning"), self.held(3, "active")]
        for _, record in entries:
            record["preview"].update(setupDeadline=preview.timestamp(AT - dt.timedelta(minutes=1)),
                                     expiresAt=preview.timestamp(AT - dt.timedelta(minutes=1)))
        self.assert_full(entries)

    def test_cleanup_awaiting_observation_or_deletion_holds_capacity(self):
        for phase in ("observe", "application", "unknown-phase"):
            with self.subTest(phase=phase):
                self.assert_full([self.held(1), self.held(2), self.held(3, phase=phase)])

    def test_watched_cleaning_and_cleanup_copy_count_once(self):
        path, cleanup = self.held(2, phase="observe")
        watched = {"status": "cleaning", "preview": copy.deepcopy(cleanup["preview"])}
        entries = [self.held(1), (path, cleanup), (preview.record_path("previews", cleanup["preview"]), watched)]
        lifecycle = self.lifecycle(entries)
        self.assertEqual(self.prepare(lifecycle)["status"], "pending")
        self.assertEqual(len(preview.occupied_project_previews(lifecycle.all_records(), "123")), 3)

    def test_confirmed_teardown_releases_capacity_before_tag_bookkeeping_finishes(self):
        for phase in ("tags", "environment"):
            with self.subTest(phase=phase):
                lifecycle = self.lifecycle([self.held(1), self.held(2), self.held(3, phase=phase)])
                self.assertTrue(self.prepare(lifecycle)["buildRequired"])
                self.assertEqual(len(preview.occupied_project_previews(lifecycle.all_records(), "123")), 3)

    def test_watched_record_keeps_slot_despite_inconsistent_finished_cleanup_phase(self):
        path, cleanup = self.held(3, phase="tags")
        watched = {"status": "cleaning", "preview": copy.deepcopy(cleanup["preview"])}
        self.assert_full([self.held(1), self.held(2), (path, cleanup),
                          (preview.record_path("previews", cleanup["preview"]), watched)])

    def test_other_project_reservations_do_not_consume_project_slots(self):
        lifecycle = self.lifecycle([self.held(i, project="456") for i in range(1, 5)] + [self.held(5), self.held(6)])
        self.assertEqual(self.prepare(lifecycle)["status"], "pending")
        self.assertEqual(len(preview.occupied_project_previews(lifecycle.all_records(), "123")), 3)

    def test_services_in_same_project_share_one_project_limit(self):
        self.assert_full([self.held(1), self.held(2, service="another-service"), self.held(3, service="third-service")])

    def test_request_aliases_and_archived_history_do_not_consume_slots(self):
        value = self.candidate()
        entries = [self.held(1), self.held(2)]
        for i in range(1, 6):
            entries.extend([(f"requests/123-{i}.json", {"status": "active", "preview": copy.deepcopy(value)}),
                            (f"archive/old-{i}.json", {"status": "stopped", "preview": copy.deepcopy(value)})])
        lifecycle = self.lifecycle(entries)
        self.assertTrue(self.prepare(lifecycle)["buildRequired"])
        self.assertEqual(len(preview.occupied_project_previews(lifecycle.all_records(), "123")), 3)

    def test_same_pending_pipeline_retry_at_capacity_preserves_deadline(self):
        value = self.candidate()
        path = preview.record_path("pending", value)
        lifecycle = self.lifecycle([(path, {"status": "pending", "preview": copy.deepcopy(value)}), self.held(1), self.held(2)])
        self.assertTrue(self.prepare(lifecycle, at=AT + dt.timedelta(minutes=5))["buildRequired"])
        self.assertEqual(lifecycle.store.read(path)["preview"], value)
        self.assertEqual(len(preview.occupied_project_previews(lifecycle.all_records(), "123")), 3)

    def test_active_preview_reuse_at_capacity_preserves_canonical_lease(self):
        value = self.candidate()
        value.update(imageDigest="sha256:" + "d" * 64, expiresAt=preview.timestamp(AT + dt.timedelta(hours=12)))
        path = preview.record_path("previews", value)
        lifecycle = self.lifecycle([(path, {"status": "active", "preview": copy.deepcopy(value)}), self.held(1), self.held(2)])
        newer = dict(SOURCE, sourcePipelineId="88888")
        lifecycle.verified_source = lambda env: newer
        request = self.prepare(lifecycle, at=AT + dt.timedelta(hours=1), env=dict(ENV, PREVIEW_SOURCE_PIPELINE_ID="88888"))
        self.assertFalse(request["buildRequired"])
        self.assertEqual(request["preview"], value)
        self.assertEqual(lifecycle.store.read(path)["preview"], value)
        self.assertEqual(len(preview.occupied_project_previews(lifecycle.all_records(), "123")), 3)

    def test_prepare_uses_one_records_snapshot_for_reuse_and_capacity(self):
        lifecycle = self.lifecycle([self.held(1), self.held(2)])
        with patch.object(lifecycle, "all_records", wraps=lifecycle.all_records) as records:
            self.prepare(lifecycle)
        records.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
