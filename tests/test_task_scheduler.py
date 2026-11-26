"""Safety and timing gates for the approved Task dispatcher.

All network behavior is replaced by FakeAPI. These tests never call GitHub.
"""

import importlib.util
import json
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "task_scheduler.py"
SPEC = importlib.util.spec_from_file_location("task_scheduler_under_test", MODULE_PATH)
scheduler = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scheduler
SPEC.loader.exec_module(scheduler)

UTC = timezone.utc
JST = timezone(timedelta(hours=9))


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def run(run_id, created, *, status="completed", branch="main"):
    return {
        "id": run_id,
        "created_at": created,
        "head_branch": branch,
        "event": "workflow_dispatch",
        "status": status,
        "conclusion": "success" if status == "completed" else None,
        "html_url": f"https://github.com/sickness8484/browser-automation/actions/runs/{run_id}",
        "workflow_id": 7,
    }


class FakeClock:
    def __init__(self, value):
        self.value = value
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


class FakeAPI:
    def __init__(self, *, existing=None, after_post=None, response=None,
                 read_error=None, post_error=None, state="active"):
        self.existing = list(existing or [])
        self.after_post = after_post
        self.response = response if response is not None else (200, {"workflow_run_id": 9001})
        self.read_error = read_error
        self.post_error = post_error
        self.state = state
        self.post_calls = []
        self.read_calls = []
        self.after_reads = 0

    def get_workflow(self, filename):
        self.read_calls.append(("workflow", filename))
        if self.read_error:
            raise self.read_error
        return {"id": 7, "state": self.state, "path": f".github/workflows/{filename}"}

    def list_runs(self, filename, **kwargs):
        self.read_calls.append(("runs", filename, kwargs))
        if self.read_error:
            raise self.read_error
        if self.post_calls and self.after_post is not None:
            if callable(self.after_post):
                self.after_reads += 1
                return self.after_post(self.after_reads)
            return list(self.after_post)
        return list(self.existing)

    def dispatch(self, filename):
        self.post_calls.append(filename)
        if self.post_error:
            raise self.post_error
        return self.response

    def get_run(self, run_id):
        self.read_calls.append(("run", run_id))
        return run(run_id, "2026-11-26T12:10:00Z", status="queued")


class ScheduleContractTests(unittest.TestCase):
    def test_only_approved_workflows_are_addressable(self):
        expected = {
            "raid": tuple([f"G{i}_test.yml" for i in range(1, 7)] + ["J1_test.yml", "J2_test.yml"]),
            "greeting": ("greeting.yml",),
            "daily": ("daily_test.yml",),
            "normal": ("NormalSummon.yml",),
            "enhance": ("Enhance.yml",),
            "sub": ("sub.test.yml",),
            "holy": ("test.yml",),
        }
        self.assertEqual(set(scheduler.GROUPS), set(expected))
        for group, workflows in expected.items():
            with self.subTest(group=group):
                self.assertEqual(tuple(scheduler.GROUPS[group].workflows), workflows)
        self.assertEqual(sum(len(g.workflows) for g in scheduler.GROUPS.values()), 14)

    def test_all_local_target_times_including_next_day(self):
        cases = [
            ("raid", "2026-11-26T14:05:02Z", "2026-11-26T23:10:00+09:00"),
            ("raid", "2026-11-26T14:35:02Z", "2026-11-26T23:40:00+09:00"),
            ("raid", "2026-11-26T15:05:02Z", "2026-11-27T00:10:00+09:00"),
            ("greeting", "2026-11-26T18:15:02Z", "2026-11-27T03:20:00+09:00"),
            ("daily", "2026-11-26T00:12:02Z", "2026-11-26T09:17:00+09:00"),
            ("normal", "2026-11-26T08:15:02Z", "2026-11-26T17:20:00+09:00"),
            ("enhance", "2026-11-26T14:15:02Z", "2026-11-26T23:20:00+09:00"),
            ("sub", "2026-11-26T00:54:02Z", "2026-11-26T09:59:30+09:00"),
            ("sub", "2026-11-26T02:54:02Z", "2026-11-26T11:59:30+09:00"),
            ("holy", "2026-11-26T11:54:02Z", "2026-11-26T20:59:30+09:00"),
            ("holy", "2026-11-26T12:54:02Z", "2026-11-26T21:59:30+09:00"),
        ]
        for key, created_at, expected in cases:
            with self.subTest(group=key, created_at=created_at):
                actual = scheduler.latest_slot(scheduler.GROUPS[key], instant(created_at))
                self.assertEqual(actual, instant(expected))

    def test_next_slot_uses_target_time_not_prewarm_time(self):
        group = scheduler.GROUPS["raid"]
        self.assertEqual(scheduler.next_slot(group, instant("2026-11-26T12:06:00Z")),
                         instant("2026-11-26T12:10:00Z"))
        self.assertEqual(scheduler.next_slot(group, instant("2026-11-26T12:11:00Z")),
                         instant("2026-11-26T12:40:00Z"))
        self.assertEqual(scheduler.next_slot(scheduler.GROUPS["greeting"], instant("2026-11-26T18:21:00Z")),
                         instant("2026-11-27T18:20:00Z"))

    def test_original_run_anchor_does_not_roll_forward_on_rerun(self):
        group = scheduler.GROUPS["raid"]
        original_slot = scheduler.latest_slot(group, instant("2026-11-26T12:35:00Z"))
        later_start = instant("2026-11-26T13:07:00Z")
        self.assertEqual(original_slot, instant("2026-11-26T12:40:00Z"))
        self.assertEqual(scheduler.window_status(group, original_slot, later_start), "late")
        self.assertNotEqual(original_slot, scheduler.latest_slot(group, later_start))

    def test_manual_timestamp_requires_timezone(self):
        with self.assertRaises(ValueError):
            scheduler.parse_aware_time("2026-11-26T21:59:30")
        self.assertEqual(scheduler.parse_aware_time("2026-11-26T21:59:30+09:00"),
                         instant("2026-11-26T12:59:30Z"))

    def test_manual_slot_must_be_on_approved_schedule(self):
        invalid = [
            ("raid", "2026-11-26T12:30:00Z"),
            ("raid", "2026-11-26T12:10:01Z"),
            ("daily", "2026-11-26T00:15:00Z"),
            ("holy", "2026-11-26T12:59:00Z"),
            ("sub", "2026-11-26T01:59:30Z"),
        ]
        for key, when in invalid:
            with self.subTest(group=key, when=when), self.assertRaises(ValueError):
                scheduler.validate_slot(scheduler.GROUPS[key], instant(when))
        scheduler.validate_slot(scheduler.GROUPS["holy"], instant("2026-11-26T12:59:30Z"))

    def test_time_window_boundaries(self):
        slot = instant("2026-11-26T12:59:30Z")
        group = scheduler.GROUPS["holy"]
        for offset, expected in [(-361, "too_early"), (-360, "waiting"),
                                 (-1, "waiting"), (0, "ready"),
                                 (15, "ready"), (16, "late")]:
            with self.subTest(offset=offset):
                self.assertEqual(scheduler.window_status(group, slot, slot + timedelta(seconds=offset)), expected)

    def test_wrong_repository_branch_or_event_is_rejected(self):
        valid = {
            "GITHUB_REPOSITORY": "sickness8484/browser-automation",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "schedule",
        }
        scheduler.validate_context(valid)
        for key, value in [("GITHUB_REPOSITORY", "other/repo"),
                           ("GITHUB_REF", "refs/heads/other"),
                           ("GITHUB_EVENT_NAME", "pull_request")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                scheduler.validate_context({**valid, key: value})

    def test_resolver_reads_original_parent_creation_time(self):
        api = FakeAPI()
        own_run = {"head_branch": "main", "event": "schedule",
                   "created_at": "2026-11-26T12:35:00Z"}
        with patch.object(api, "get_run", return_value=own_run) as read:
            slot, anchor = scheduler.resolve_slot(api, scheduler.GROUPS["raid"], "", "schedule",
                                           {"GITHUB_RUN_ID": "500"})
        read.assert_called_once_with(500)
        self.assertEqual(slot, instant("2026-11-26T12:40:00Z"))
        self.assertEqual(scheduler.parse_aware_time(anchor), instant("2026-11-26T12:35:00Z"))

    def test_schedule_event_cannot_change_group_or_override_slot(self):
        with self.assertRaises(ValueError):
            scheduler.select_group("holy", "schedule", {"schedule": "5,35 * * * *"})
        with self.assertRaises(ValueError):
            scheduler.select_group("auto", "schedule", {"schedule": "* * * * *"})
        with self.assertRaises(ValueError):
            scheduler.resolve_slot(FakeAPI(), scheduler.GROUPS["raid"], "2026-11-26T12:10:00Z",
                            "schedule", {"GITHUB_RUN_ID": "500"})

    def test_plan_only_reads_metadata_and_has_no_game_side_effect(self):
        api = FakeAPI()
        result = scheduler.plan(api, instant("2026-11-26T12:00:00Z"))
        self.assertEqual(result["status"], "plan_ready")
        self.assertEqual(len(result["results"]), 14)
        self.assertEqual(len(result["next_slots"]), 7)
        self.assertEqual(api.post_calls, [])
        self.assertEqual(len(api.read_calls), 14)


class DispatchSafetyTests(unittest.TestCase):
    def setUp(self):
        self.slot = instant("2026-11-26T12:10:00Z")
        self.workflow = "G1_test.yml"
        self.clock = FakeClock(self.slot)

    def dispatch(self, api, *, now=None, prepared=None, workflow=None, slot=None, grace=120):
        clock = self.clock if now is None else FakeClock(now)
        result = scheduler.dispatch_one(api, workflow or self.workflow, slot or self.slot, grace,
                                 clock=clock.now, sleep=clock.sleep, prepared=prepared)
        return result

    def test_previous_completed_slot_does_not_block_new_slot(self):
        api = FakeAPI(existing=[run(100, "2026-11-26T11:40:02Z")])
        prepared = scheduler.preflight(api, self.workflow, self.slot, 120)
        self.assertEqual(prepared["outcome"], "ready")
        result = self.dispatch(api, prepared=prepared)
        self.assertEqual(result["outcome"], "dispatched")
        self.assertEqual(api.post_calls, [self.workflow])

    def test_same_slot_is_not_dispatched_twice(self):
        api = FakeAPI(existing=[run(101, "2026-11-26T12:10:01Z")])
        prepared = scheduler.preflight(api, self.workflow, self.slot, 120)
        self.assertEqual(prepared["outcome"], "duplicate_skipped")
        result = self.dispatch(api, prepared=prepared)
        self.assertEqual(result["outcome"], "duplicate_skipped")
        self.assertEqual(api.post_calls, [])

    def test_previous_slot_still_active_is_not_overlapped(self):
        for status in ["in_progress", "queued", "waiting", "pending", "requested"]:
            with self.subTest(status=status):
                api = FakeAPI(existing=[run(102, "2026-11-26T11:40:00Z", status=status)])
                prepared = scheduler.preflight(api, self.workflow, self.slot, 120)
                self.assertEqual(prepared["outcome"], "busy_skipped")
                self.dispatch(api, prepared=prepared)
                self.assertEqual(api.post_calls, [])

    def test_disabled_workflow_is_not_reenabled_or_dispatched(self):
        api = FakeAPI(state="disabled_manually")
        result = self.dispatch(api)
        self.assertEqual(result["outcome"], "disabled")
        self.assertEqual(api.post_calls, [])

    def test_read_failure_prevents_post(self):
        api = FakeAPI(read_error=scheduler.APIError("read unavailable", status=503))
        result = self.dispatch(api)
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(api.post_calls, [])

    def test_runs_read_failure_prevents_post(self):
        api = FakeAPI()
        with patch.object(api, "list_runs", side_effect=scheduler.APIError("unavailable", status=503)):
            result = self.dispatch(api)
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(api.post_calls, [])

    def test_malformed_branch_record_does_not_look_idle(self):
        record = run(200, "2026-11-26T11:40:00Z", status="in_progress")
        del record["head_branch"]
        api = FakeAPI(existing=[record])
        result = self.dispatch(api)
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(api.post_calls, [])

    def test_too_early_or_late_dispatch_never_posts(self):
        for offset in [-1, 121]:
            with self.subTest(offset=offset):
                api = FakeAPI()
                result = self.dispatch(api, now=self.slot + timedelta(seconds=offset))
                self.assertNotEqual(result["outcome"], "dispatched")
                self.assertEqual(api.post_calls, [])

    def test_late_holywar_does_not_start_game_actions(self):
        slot = instant("2026-11-26T12:59:30Z")
        api = FakeAPI()
        result = self.dispatch(api, workflow="test.yml", slot=slot, grace=15,
                               now=instant("2026-11-26T12:59:46Z"))
        self.assertNotEqual(result["outcome"], "dispatched")
        self.assertEqual(api.post_calls, [])

    def test_unapproved_workflow_never_posts(self):
        for workflow in ["J3_test.yml", "mt.yml", "../test.yml", "evil.yml"]:
            with self.subTest(workflow=workflow):
                api = FakeAPI()
                try:
                    result = self.dispatch(api, workflow=workflow)
                except ValueError:
                    pass
                else:
                    self.assertNotEqual(result["outcome"], "dispatched")
                self.assertEqual(api.post_calls, [])

    def test_200_receipt_confirms_single_post(self):
        api = FakeAPI(response=(200, {"workflow_run_id": 9002}))
        result = self.dispatch(api)
        self.assertEqual(result["outcome"], "dispatched")
        self.assertEqual(api.post_calls, [self.workflow])
        self.assertIn("9002", str(result))

    def test_204_receipt_is_reconciled_without_second_post(self):
        api = FakeAPI(response=(204, None), after_post=[run(9003, "2026-11-26T12:10:01Z", status="queued")])
        result = self.dispatch(api)
        self.assertEqual(result["outcome"], "dispatched")
        self.assertEqual(api.post_calls, [self.workflow])
        self.assertIn("9003", str(result))

    def test_post_connection_failure_can_reconcile_one_new_run(self):
        api = FakeAPI(post_error=scheduler.APIError("connection lost", ambiguous=True),
                      after_post=[run(9004, "2026-11-26T12:10:01Z", status="queued")])
        result = self.dispatch(api)
        self.assertEqual(result["outcome"], "dispatched")
        self.assertEqual(api.post_calls, [self.workflow])

    def test_uncertain_post_without_receipt_is_not_retried(self):
        api = FakeAPI(post_error=scheduler.APIError("connection lost", ambiguous=True), after_post=[])
        result = self.dispatch(api)
        self.assertEqual(result["outcome"], "uncertain")
        self.assertEqual(api.post_calls, [self.workflow])
        self.assertLessEqual(sum(self.clock.sleeps), 20)

    def test_multiple_new_runs_are_not_claimed_as_confirmed(self):
        api = FakeAPI(response=(204, None), after_post=[
            run(9005, "2026-11-26T12:10:01Z", status="queued"),
            run(9006, "2026-11-26T12:10:02Z", status="queued"),
        ])
        result = self.dispatch(api)
        self.assertEqual(result["outcome"], "uncertain")
        self.assertEqual(api.post_calls, [self.workflow])


class TransportContractTests(unittest.TestCase):
    def test_post_transport_does_not_automatically_retry_503(self):
        calls = []

        class FailingOpener:
            def open(self, request, timeout):
                calls.append(request)
                raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)

        api = scheduler.GitHubAPI("fake-test-token", opener=FailingOpener(), sleep=lambda _: None)
        with self.assertRaises(scheduler.APIError) as caught:
            api.dispatch("G1_test.yml")
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual(request.full_url,
                         "https://api.github.com/repos/sickness8484/browser-automation/actions/workflows/G1_test.yml/dispatches")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"ref": "main", "return_run_details": True})

    def test_missing_token_and_redirect_are_rejected(self):
        with self.assertRaises(ValueError):
            scheduler.GitHubAPI("")
        handler = scheduler.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://other.example"))


if __name__ == "__main__":
    unittest.main()
