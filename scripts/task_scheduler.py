#!/usr/bin/env python3
"""Dispatch the approved Automation schedules; never run game code here."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

REPOSITORY = "sickness8484/browser-automation"
BRANCH = "main"
API_VERSION = "2026-03-10"
UTC = timezone.utc
JST = timezone(timedelta(hours=9))
MAX_EARLY_SECONDS = 360
REPORT_PATH = Path("task-dispatch-report.json")


@dataclass(frozen=True)
class Group:
    key: str
    workflows: tuple[str, ...]
    cron: str
    trigger_minutes: tuple[int, ...]
    trigger_hours: tuple[int, ...]
    target_offset: timedelta
    grace_seconds: int


GROUPS = {
    "raid": Group(
        "raid",
        tuple(f"G{i}_test.yml" for i in range(1, 7)) + ("J1_test.yml", "J2_test.yml"),
        "5,35 * * * *", (5, 35), tuple(range(24)), timedelta(minutes=5), 120,
    ),
    "greeting": Group(
        "greeting", ("greeting.yml",), "15 18 * * *",
        (15,), (18,), timedelta(minutes=5), 300,
    ),
    "daily": Group(
        "daily", ("daily_test.yml",), "12 0 * * *",
        (12,), (0,), timedelta(minutes=5), 300,
    ),
    "normal": Group(
        "normal", ("NormalSummon.yml",), "15 8 * * *",
        (15,), (8,), timedelta(minutes=5), 300,
    ),
    "enhance": Group(
        "enhance", ("Enhance.yml",), "15 14 * * *",
        (15,), (14,), timedelta(minutes=5), 300,
    ),
    "sub": Group(
        "sub", ("sub.test.yml",), "54 0,2 * * *",
        (54,), (0, 2), timedelta(minutes=5, seconds=30), 15,
    ),
    "holy": Group(
        "holy", ("test.yml",), "54 11,12 * * *",
        (54,), (11, 12), timedelta(minutes=5, seconds=30), 15,
    ),
}
ALLOWED_WORKFLOWS = frozenset(
    workflow for group in GROUPS.values() for workflow in group.workflows
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_aware_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Expected an ISO 8601 date and time with timezone.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Expected an ISO 8601 date and time with timezone.") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("A timezone offset is required.")
    return result.astimezone(UTC)


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required.")
    return value.astimezone(UTC)


def iso(value: datetime) -> str:
    return require_aware(value).isoformat().replace("+00:00", "Z")


def jst(value: datetime) -> str:
    return require_aware(value).astimezone(JST).isoformat()


def trigger_candidates(group: Group, anchor: datetime) -> list[datetime]:
    anchor = require_aware(anchor)
    midnight = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    return [
        midnight + timedelta(days=day, hours=hour, minutes=minute)
        for day in (-1, 0, 1)
        for hour in group.trigger_hours
        for minute in group.trigger_minutes
    ]


def latest_slot(group: Group, anchor: datetime) -> datetime:
    """Bind to run creation, not runner start, so a rerun cannot move a slot."""
    anchor = require_aware(anchor)
    warm = max(t for t in trigger_candidates(group, anchor) if t <= anchor)
    return warm + group.target_offset


def next_slot(group: Group, now: datetime) -> datetime:
    now = require_aware(now)
    return min(
        t + group.target_offset
        for t in trigger_candidates(group, now)
        if t + group.target_offset > now
    )


def validate_slot(group: Group, slot: datetime) -> None:
    slot = require_aware(slot)
    warm = slot - group.target_offset
    if (
        warm.hour not in group.trigger_hours
        or warm.minute not in group.trigger_minutes
        or warm.second != 0
        or warm.microsecond != 0
    ):
        raise ValueError("The requested slot is not an approved time for this group.")


def window_status(group: Group, slot: datetime, now: datetime) -> str:
    distance = (require_aware(slot) - require_aware(now)).total_seconds()
    if distance > MAX_EARLY_SECONDS:
        return "too_early"
    if distance > 0:
        return "waiting"
    if -distance > group.grace_seconds:
        return "late"
    return "ready"


class APIError(Exception):
    """Only controlled, non-sensitive descriptions belong in this exception."""

    def __init__(
        self, message: str = "GitHub API request failed.",
        status: int | None = None, ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the token to a redirect target.
        return None


class GitHubAPI:
    def __init__(
        self, token: str, *,
        opener: Any = None, sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("GH_TOKEN is required.")
        self.token = token.strip()
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.sleep = sleep

    @staticmethod
    def workflow_path(workflow: str) -> str:
        if workflow not in ALLOWED_WORKFLOWS:
            raise ValueError("Workflow is not in the approved allowlist.")
        return "/actions/workflows/" + urllib.parse.quote(workflow, safe="")

    def request(
        self, method: str, path: str, payload: dict | None = None, *,
        timeout: float = 15, retries: int = 1,
    ) -> tuple[int, Any]:
        if method not in ("GET", "POST") or not path.startswith("/actions/"):
            raise ValueError("Unsupported API operation.")
        if "://" in path or "#" in path:
            raise ValueError("Invalid API path.")
        url = f"https://api.github.com/repos/{REPOSITORY}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        attempts = 1 + (max(0, retries) if method == "GET" else 0)
        for attempt in range(attempts):
            request = urllib.request.Request(
                url,
                data=data,
                method=method,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.token}",
                    "X-GitHub-Api-Version": API_VERSION,
                    "Content-Type": "application/json",
                    "User-Agent": "Automation-Schedule-Dispatcher",
                },
            )
            try:
                with self.opener.open(request, timeout=timeout) as response:
                    raw = response.read()
                    if not raw:
                        return response.status, None
                    try:
                        return response.status, json.loads(raw)
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise APIError(
                            "Invalid JSON response.", status=response.status,
                            ambiguous=(method == "POST"),
                        ) from exc
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or exc.code >= 500
                if attempt + 1 < attempts and retryable:
                    self.sleep(1)
                    continue
                raise APIError(
                    "GitHub HTTP request failed.", status=exc.code,
                    ambiguous=(method == "POST" and exc.code >= 500),
                ) from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
                if attempt + 1 < attempts:
                    self.sleep(1)
                    continue
                raise APIError(
                    "GitHub network request failed.", ambiguous=(method == "POST")
                ) from exc
        raise APIError()

    def get_workflow(self, workflow: str) -> dict:
        _, data = self.request("GET", self.workflow_path(workflow))
        if not isinstance(data, dict):
            raise APIError("Invalid workflow response.")
        return data

    def get_run(self, run_id: int) -> dict:
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            raise ValueError("A valid run ID is required.")
        _, data = self.request("GET", f"/actions/runs/{run_id}")
        if not isinstance(data, dict):
            raise APIError("Invalid run response.")
        return data

    def list_runs(
        self, workflow: str, *, timeout: float = 15, retries: int = 1,
    ) -> list[dict]:
        query = urllib.parse.urlencode({"branch": BRANCH, "per_page": 100})
        _, data = self.request(
            "GET", self.workflow_path(workflow) + "/runs?" + query,
            timeout=timeout, retries=retries,
        )
        if not isinstance(data, dict) or not isinstance(data.get("workflow_runs"), list):
            raise APIError("Invalid workflow runs response.")
        return data["workflow_runs"]

    def dispatch(self, workflow: str) -> tuple[int, Any]:
        return self.request(
            "POST", self.workflow_path(workflow) + "/dispatches",
            {"ref": BRANCH, "return_run_details": True}, retries=0,
        )


def id_of(run: dict) -> int:
    value = run.get("id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise APIError("Invalid workflow run identifier.")
    return value


def run_url(run_id: int) -> str:
    return f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"


def error_result(workflow: str, exc: Exception, outcome: str = "error") -> dict:
    result = {"workflow": workflow, "outcome": outcome}
    if isinstance(exc, APIError) and exc.status is not None:
        result["http_status"] = exc.status
    # Exception strings may contain transport details; do not serialize them.
    result["reason"] = "API read or dispatch could not be verified."
    return result


def checked_runs(runs: list[dict]) -> list[dict]:
    if not isinstance(runs, list):
        raise APIError("Invalid workflow run collection.")
    result = []
    for run in runs:
        if not isinstance(run, dict):
            raise APIError("Invalid workflow run record.")
        if not isinstance(run.get("head_branch"), str) or not run["head_branch"]:
            raise APIError("Missing workflow run branch.")
        if run["head_branch"] != BRANCH:
            continue
        id_of(run)
        if not isinstance(run.get("status"), str):
            raise APIError("Missing workflow run status.")
        try:
            parse_aware_time(run["created_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise APIError("Missing workflow run creation time.") from exc
        result.append(run)
    return result


def preflight(
    api: Any, workflow: str, slot: datetime, grace_seconds: int,
) -> dict:
    if workflow not in ALLOWED_WORKFLOWS:
        raise ValueError("Workflow is not in the approved allowlist.")
    try:
        metadata = api.get_workflow(workflow)
        if not isinstance(metadata, dict):
            raise APIError("Invalid workflow metadata.")
        if metadata.get("path") != f".github/workflows/{workflow}":
            raise APIError("Workflow path did not match.")
        if metadata.get("state") != "active":
            return {
                "workflow": workflow, "outcome": "disabled",
                "reason": "Workflow is not active; it was not enabled automatically.",
            }
        runs = checked_runs(api.list_runs(workflow))
        before_ids = [id_of(run) for run in runs]
        lower = require_aware(slot) - timedelta(seconds=90)
        upper = require_aware(slot) + timedelta(seconds=grace_seconds)
        duplicates = [
            id_of(run) for run in runs
            if run.get("event") == "workflow_dispatch"
            and lower <= parse_aware_time(run["created_at"]) <= upper
        ]
        if duplicates:
            return {
                "workflow": workflow, "outcome": "duplicate_skipped",
                "run_ids": duplicates, "run_urls": [run_url(i) for i in duplicates],
            }
        busy = [id_of(run) for run in runs if run["status"] != "completed"]
        if busy:
            return {
                "workflow": workflow, "outcome": "busy_skipped",
                "run_ids": busy, "run_urls": [run_url(i) for i in busy],
                "reason": "An existing main-branch run is not complete.",
            }
        # All approved jobs have a <= 6-hour runtime. If a full page cannot
        # cover even 24 hours, do not assume an older queued run is absent.
        if len(runs) >= 100 and min(
            parse_aware_time(run["created_at"]) for run in runs
        ) > slot - timedelta(hours=24):
            raise APIError("Run history is too dense to establish an idle workflow.")
        return {
            "workflow": workflow, "outcome": "ready",
            "before_ids": before_ids,
        }
    except (APIError, ValueError, KeyError, TypeError) as exc:
        return error_result(workflow, exc)


def reconcile_dispatch(
    api: Any, workflow: str, before_ids: set[int], requested_at: datetime, *,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Look for one new run; an ambiguous POST is never sent again."""
    started = time.monotonic()
    for delay in (0, 2, 4, 4):
        remaining = 18 - (time.monotonic() - started)
        if remaining <= 0:
            break
        if delay:
            sleep(min(delay, remaining))
        remaining = 18 - (time.monotonic() - started)
        if remaining <= 0:
            break
        try:
            runs = checked_runs(api.list_runs(
                workflow, timeout=min(5, remaining), retries=0
            ))
            upper = require_aware(clock()) + timedelta(seconds=5)
            candidates = [
                run for run in runs
                if id_of(run) not in before_ids
                and run.get("event") == "workflow_dispatch"
                and requested_at - timedelta(seconds=2)
                <= parse_aware_time(run["created_at"]) <= upper
            ]
            if len(candidates) == 1:
                rid = id_of(candidates[0])
                return {
                    "workflow": workflow, "outcome": "dispatched",
                    "run_id": rid, "run_url": run_url(rid),
                    "verification": "reconciled_after_response",
                }
            if len(candidates) > 1:
                return {
                    "workflow": workflow, "outcome": "uncertain",
                    "reason": "Multiple new runs matched; no POST was repeated.",
                    "run_ids": [id_of(run) for run in candidates],
                }
        except (APIError, ValueError, KeyError, TypeError):
            continue
    return {
        "workflow": workflow, "outcome": "uncertain",
        "reason": "No unique run could be verified; no POST was repeated.",
    }


def dispatch_one(
    api: Any, workflow: str, slot: datetime, grace_seconds: int, *,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
    prepared: dict | None = None,
) -> dict:
    if workflow not in ALLOWED_WORKFLOWS:
        raise ValueError("Workflow is not in the approved allowlist.")
    now = require_aware(clock())
    if now < slot:
        return {"workflow": workflow, "outcome": "early", "reason": "Before target time."}
    if (now - slot).total_seconds() > grace_seconds:
        return {"workflow": workflow, "outcome": "late", "reason": "Dispatch window expired."}
    prepared = prepared if prepared is not None else preflight(
        api, workflow, slot, grace_seconds
    )
    if prepared.get("workflow") != workflow:
        return {"workflow": workflow, "outcome": "error", "reason": "Preflight mismatch."}
    if prepared.get("outcome") != "ready":
        return prepared
    requested_at = require_aware(clock())
    if (requested_at - slot).total_seconds() > grace_seconds:
        return {"workflow": workflow, "outcome": "late", "reason": "Read checks exceeded deadline."}
    try:
        status, data = api.dispatch(workflow)
        rid = data.get("workflow_run_id") if isinstance(data, dict) else None
        if status == 200 and isinstance(rid, int) and not isinstance(rid, bool) and rid > 0:
            return {
                "workflow": workflow, "outcome": "dispatched",
                "http_status": status, "run_id": rid, "run_url": run_url(rid),
                "verification": "dispatch_response",
                "requested_at": iso(requested_at),
            }
        if status not in (200, 204):
            return {
                "workflow": workflow, "outcome": "error",
                "http_status": status, "reason": "Unexpected dispatch response.",
            }
    except APIError as exc:
        if not exc.ambiguous and not (exc.status is not None and exc.status >= 500):
            return error_result(workflow, exc)
    result = reconcile_dispatch(
        api, workflow, set(prepared.get("before_ids", [])), requested_at,
        clock=clock, sleep=sleep,
    )
    result["requested_at"] = iso(requested_at)
    return result


def read_event(env: dict[str, str]) -> dict:
    event_path = env.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise ValueError("GITHUB_EVENT_PATH is required.")
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("The GitHub event could not be read.") from exc
    if not isinstance(event, dict):
        raise ValueError("The GitHub event is invalid.")
    return event


def validate_context(env: dict[str, str]) -> None:
    if env.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("This dispatcher is bound to its approved repository.")
    if env.get("GITHUB_REF") != f"refs/heads/{BRANCH}":
        raise ValueError("This dispatcher only runs on main.")
    if env.get("GITHUB_EVENT_NAME") not in ("schedule", "workflow_dispatch"):
        raise ValueError("Unsupported workflow event.")


def select_group(group_name: str, event_name: str, event: dict) -> Group:
    if event_name == "schedule":
        matches = [g for g in GROUPS.values() if g.cron == event.get("schedule")]
        if len(matches) != 1:
            raise ValueError("The schedule expression is not approved.")
        group = matches[0]
        if group_name not in ("auto", group.key):
            raise ValueError("Group does not match the schedule event.")
        return group
    if group_name == "auto":
        raise ValueError("Manual dispatch requires a specific approved group.")
    return GROUPS[group_name]


def resolve_slot(
    api: Any, group: Group, explicit_slot: str | None,
    event_name: str, env: dict[str, str],
) -> tuple[datetime, str]:
    if explicit_slot:
        if event_name == "schedule":
            raise ValueError("Scheduled runs cannot override their time slot.")
        slot = parse_aware_time(explicit_slot)
        validate_slot(group, slot)
        return slot, "explicit_manual_slot"
    try:
        run_id = int(env["GITHUB_RUN_ID"])
    except (KeyError, ValueError) as exc:
        raise ValueError("GITHUB_RUN_ID is required to anchor the schedule.") from exc
    own_run = api.get_run(run_id)
    if own_run.get("head_branch") != BRANCH or own_run.get("event") != event_name:
        raise ValueError("The parent run did not match the expected context.")
    anchor = parse_aware_time(own_run["created_at"])
    return latest_slot(group, anchor), iso(anchor)


def wait_for_slot(
    group: Group, slot: datetime, *,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    announced = False
    while True:
        now = require_aware(clock())
        state = window_status(group, slot, now)
        if state != "waiting":
            return state
        if not announced:
            print(f"Waiting for approved API dispatch time {jst(slot)}.", flush=True)
            announced = True
        sleep(min(20, max(0.01, (slot - now).total_seconds())))


def plan(api: Any, now: datetime) -> dict:
    def inspect(workflow: str) -> dict:
        try:
            data = api.get_workflow(workflow)
            if not isinstance(data, dict):
                raise APIError("Invalid workflow metadata.")
            if data.get("path") != f".github/workflows/{workflow}":
                raise APIError("Workflow path did not match.")
            return {
                "workflow": workflow,
                "outcome": "plan_ready" if data.get("state") == "active" else "disabled",
            }
        except (APIError, ValueError, TypeError) as exc:
            return error_result(workflow, exc)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(inspect, sorted(ALLOWED_WORKFLOWS)))
    return {
        "mode": "plan",
        "status": "plan_ready" if all(r["outcome"] == "plan_ready" for r in results) else "error",
        "next_slots": [
            {
                "group": group.key, "warmup_cron_utc": group.cron,
                "next_dispatch_jst": jst(next_slot(group, now)),
                "workflows": list(group.workflows),
                "late_grace_seconds": group.grace_seconds,
            }
            for group in GROUPS.values()
        ],
        "results": results,
    }


def publish_report(report: dict, env: dict[str, str]) -> None:
    report["repository"] = REPOSITORY
    report["ref"] = BRANCH
    report["recorded_at"] = iso(utc_now())
    report["meaning"] = "Dispatch receipt only; game-task completion is checked in child run logs."
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    REPORT_PATH.write_text(rendered + "\n", encoding="utf-8")
    print("TASK_SCHEDULER_REPORT " + json.dumps(report, ensure_ascii=False), flush=True)
    summary_path = env.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = [
            "# Automation schedule dispatch",
            "",
            f"Status: **{report.get('status', 'error')}**",
            "",
            "This records API dispatch receipts, not completion of the game tasks.",
            "",
        ]
        if report.get("slot_jst"):
            lines += [f"Scheduled API dispatch: **{report['slot_jst']}**", ""]
        lines += [
            "| Workflow | Dispatch result | Run |",
            "| --- | --- | --- |",
        ]
        for result in report.get("results", []):
            link = "-"
            if result.get("run_id"):
                link = f"[{result['run_id']}]({result['run_url']})"
            elif result.get("run_ids"):
                link = ", ".join(f"[{rid}]({run_url(rid)})" for rid in result["run_ids"])
            lines.append(f"| {result['workflow']} | {result['outcome']} | {link} |")
        if report.get("next_slots"):
            lines += ["", "## Next approved slots", ""]
            lines += [
                f"- {item['group']}: {item['next_dispatch_jst']}"
                for item in report["next_slots"]
            ]
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "dispatch"), default="plan")
    parser.add_argument("--group", choices=("auto", *GROUPS), default="auto")
    parser.add_argument("--slot", default="")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    report: dict = {"mode": args.mode, "status": "error", "results": []}
    try:
        validate_context(env)
        api = GitHubAPI(env.get("GH_TOKEN", ""))
        if args.mode == "plan":
            report = plan(api, utc_now())
        else:
            event = read_event(env)
            group = select_group(args.group, env["GITHUB_EVENT_NAME"], event)
            slot, anchor = resolve_slot(api, group, args.slot, env["GITHUB_EVENT_NAME"], env)
            report.update({
                "group": group.key, "slot_utc": iso(slot), "slot_jst": jst(slot),
                "anchor": anchor, "late_grace_seconds": group.grace_seconds,
            })
            window = wait_for_slot(group, slot)
            if window != "ready":
                report["status"] = window
                report["results"] = [
                    {"workflow": name, "outcome": window, "reason": "Outside the approved time window."}
                    for name in group.workflows
                ]
            else:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    preparations = list(executor.map(
                        lambda name: preflight(api, name, slot, group.grace_seconds),
                        group.workflows,
                    ))
                report["results"] = [
                    dispatch_one(api, name, slot, group.grace_seconds, prepared=prepared)
                    for name, prepared in zip(group.workflows, preparations)
                ]
                complete = all(
                    r["outcome"] in ("dispatched", "duplicate_skipped")
                    for r in report["results"]
                )
                report["status"] = "dispatch_receipts_confirmed" if complete else "partial_or_failed"
    except (APIError, ValueError, KeyError, TypeError, OSError) as exc:
        report["status"] = "error"
        report["error_type"] = type(exc).__name__
        # Only our configuration errors are displayed, never transport bodies.
        report["reason"] = str(exc) if isinstance(exc, ValueError) else "Dispatch setup failed."
        if isinstance(exc, APIError) and exc.status is not None:
            report["http_status"] = exc.status
    publish_report(report, env)
    return 0 if report["status"] in ("plan_ready", "dispatch_receipts_confirmed") else 1


if __name__ == "__main__":
    sys.exit(main())
