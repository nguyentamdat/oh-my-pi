"""Contract test for the robomp server API response vs status-contract.json.

Regenerate the committed fixture with:
    ROBOMP_UPDATE_STATUS_CONTRACT=1 pytest tests/test_status_contract.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from robomp.config import Settings, reset_settings_cache
from robomp.db import get_database
from robomp.server import create_app

# Runtime/timestamp fields vary every run; normalize them so the live payload
# can be compared against (or regenerated into) a byte-stable committed fixture.
_VOLATILE_TS_KEYS = {"received_at", "started_at", "finished_at", "last_tool_ts", "updated_at", "created_at"}
_FIXED_TS = "2024-01-01T00:00:00+00:00"
_UPDATE_ENV = "ROBOMP_UPDATE_STATUS_CONTRACT"


def _normalize_for_fixture(value: Any) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if key == "uptime_seconds":
                normalized[key] = 0.0
            elif key in _VOLATILE_TS_KEYS and item is not None:
                normalized[key] = _FIXED_TS
            else:
                normalized[key] = _normalize_for_fixture(item)
        return normalized
    if isinstance(value, list):
        return [_normalize_for_fixture(item) for item in value]
    return value


def test_status_contract(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOMP_GITLAB_BASE_URL", "https://dashboard:secret@gitlab.zingplay.com")
    monkeypatch.setenv("ROBOMP_GITLAB_WEBHOOK_SECRET", "test-gitlab-webhook-secret")
    monkeypatch.setenv("ROBOMP_GITLAB_PROJECT_IDS", "2080,332,356")
    monkeypatch.setenv("ROBOMP_GITLAB_BOT_LOGIN", "robomp-gitlab-bot")
    reset_settings_cache()
    settings = Settings()  # type: ignore[call-arg]
    settings.ensure_paths()
    app = create_app(settings)
    with TestClient(app) as client:
        # Seed AFTER startup:
        db = get_database(settings.sqlite_path)

        # 1. A running issue with live detail:
        db.upsert_issue(
            key="octo/widget#1",
            state="reproducing",
            repo="octo/widget",
            number=1,
            branch="farm/abc12345/fix",
            pr_number=77,
        )
        db.record_event(
            delivery_id="run-x",
            event_type="issue_comment",
            repo="octo/widget",
            issue_key="octo/widget#1",
            payload={"action": "created"},
        )
        # claim_next_event will return run-x since it is queued
        claimed = db.claim_next_event()
        assert claimed is not None
        assert claimed.delivery_id == "run-x"
        db.set_event_model("run-x", "anthropic/claude-3-5-sonnet")
        db.log_tool_call(issue_key="octo/widget#1", tool="edit", args={})

        # 2. A failed issue:
        db.upsert_issue(
            key="octo/widget#2",
            state="fixing",
            repo="octo/widget",
            number=2,
        )
        db.record_event(
            delivery_id="failed-x",
            event_type="issues",
            repo="octo/widget",
            issue_key="octo/widget#2",
            state="failed",
            last_error="repro diverged",
            payload={"action": "opened"},
        )

        # 3. A superseded failed issue (older failed event + newer done event):
        db.upsert_issue(
            key="octo/widget#3",
            state="fixing",
            repo="octo/widget",
            number=3,
        )
        db.record_event(
            delivery_id="superseded-failed",
            event_type="issues",
            repo="octo/widget",
            issue_key="octo/widget#3",
            state="failed",
            last_error="old error",
            payload={"action": "opened"},
        )
        db.record_event(
            delivery_id="new-done",
            event_type="issues",
            repo="octo/widget",
            issue_key="octo/widget#3",
            state="done",
            payload={"action": "opened"},
        )

        # 4. An issue-less failed (orphan failed):
        db.record_event(
            delivery_id="orphan-failed-x",
            event_type="issues",
            repo="octo/widget",
            issue_key=None,
            state="failed",
            last_error="orphan failed error",
            payload={"action": "opened"},
        )

        # 5. A terminal issue:
        db.upsert_issue(
            key="octo/widget#4",
            state="merged",
            repo="octo/widget",
            number=4,
        )
        db.record_event(
            delivery_id="terminal-done",
            event_type="issues",
            repo="octo/widget",
            issue_key="octo/widget#4",
            state="done",
            payload={"action": "opened"},
        )

        # 6. A queued issue:
        db.upsert_issue(
            key="octo/widget#5",
            state="new",
            repo="octo/widget",
            number=5,
        )
        db.record_event(
            delivery_id="queued-x",
            event_type="issues",
            repo="octo/widget",
            issue_key="octo/widget#5",
            state="queued",
            payload={"action": "opened"},
        )

        # 7. An active issue (no latest event):
        db.upsert_issue(
            key="octo/widget#6",
            state="new",
            repo="octo/widget",
            number=6,
        )

        # 8. A multi-project GitLab fan-out from triage #7. One child is
        # planned only; the other has its queued synthetic event.
        source = "gitlab-zingplay:2080:issue:7"
        db.record_event(
            instance_id="gitlab-zingplay",
            delivery_id="source-triage-7",
            event_type="Issue Hook",
            repo="ica/triage",
            repository_id="2080",
            item_kind="issue",
            item_number=7,
            canonical_key=source,
            issue_key=source,
            state="done",
            payload={"object_kind": "issue"},
        )
        db.record_routing_decision(
            instance_id="gitlab-zingplay",
            delivery_id="source-triage-7",
            source_canonical_key=source,
            ranked_candidates=[
                {"project_id": 332, "key": "client"},
                {"project_id": 356, "key": "server"},
            ],
            selected_target_key=None,
            selected_project_id=None,
            explicit=False,
            action="children_queued",
            mode="none",
        )
        db.plan_routing_children(source, [(332, "recommend"), (356, "auto_implement")])
        db.complete_routing_child_event(
            source_canonical_key=source,
            target_project_id=356,
            target_delivery_id="route:356:19",
            target_event_type="RoboOMP Route",
            target_repo="ica/server",
            target_issue_key="gitlab-zingplay:356:issue:19",
            target_payload={"object_kind": "issue"},
            target_item_kind="issue",
            target_item_number=19,
            target_task_kind="triage_issue",
            target_instance_id="gitlab-zingplay",
        )

        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()

        # Assert Python-side: top-level keys exactly:
        expected_keys = {
            "runtime",
            "event_counts",
            "issue_event_counts",
            "running_events",
            "inflight",
            "issues",
            "recent_events",
            "routing_flows",
        }
        assert set(data.keys()) == expected_keys

        # check running_events[0] keys:
        running_ev = data["running_events"]
        assert len(running_ev) == 1
        assert set(running_ev[0].keys()) == {
            "delivery_id",
            "event_type",
            "repo",
            "issue_key",
            "received_at",
            "started_at",
            "attempts",
            "model",
            "last_tool",
            "last_tool_ts",
        }
        assert running_ev[0]["model"] == "anthropic/claude-3-5-sonnet"
        assert running_ev[0]["last_tool"] == "edit"

        # check issues keys / latest_event keys:
        for issue_row in data["issues"]:
            assert set(issue_row.keys()) == {
                "key",
                "repo",
                "number",
                "branch",
                "pr_number",
                "state",
                "classification",
                "updated_at",
                "latest_event",
            }
            latest = issue_row["latest_event"]
            if latest is not None:
                assert set(latest.keys()) == {
                    "delivery_id",
                    "event_type",
                    "state",
                    "attempts",
                    "received_at",
                    "last_error",
                }

        # check runtime:
        assert data["runtime"]["repo_allowlist"] == ["octo/widget"]

        # Compare the normalized payload against the committed fixture. Normal
        # pytest runs assert equality; regenerate only when ROBOMP_UPDATE_STATUS_CONTRACT=1.
        fixture_path = Path(__file__).parent.parent / "web/test/fixtures/status-contract.json"
        actual = _normalize_for_fixture(data)
        if os.environ.get(_UPDATE_ENV) == "1":
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            fixture_path.write_text(json.dumps(actual, indent=2), encoding="utf-8")
        else:
            expected = json.loads(fixture_path.read_text(encoding="utf-8"))
            assert actual == expected


def _enable_replay(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "trigger-secret"
    monkeypatch.setenv("ROBOMP_REPLAY_TOKEN", token)
    reset_settings_cache()
    return token


def test_cancel_happy_path(env, monkeypatch: pytest.MonkeyPatch) -> None:
    token = _enable_replay(monkeypatch)
    cfg = Settings()
    cfg.ensure_paths()
    app = create_app(cfg)
    with TestClient(app) as client:
        db = get_database(cfg.sqlite_path)
        db.record_event(
            delivery_id="run-cancel-1",
            event_type="issues",
            repo="octo/widget",
            issue_key="octo/widget#1",
            payload={"action": "opened"},
        )
        # claim it to make it running
        claimed = db.claim_next_event()
        assert claimed is not None
        assert claimed.delivery_id == "run-cancel-1"

        # Happy path
        resp = client.post(
            "/api/cancel",
            json={"delivery_id": "run-cancel-1"},
            headers={"X-Robomp-Replay-Token": token},
        )
        assert resp.status_code == 202
        assert resp.json() == {
            "delivery": "run-cancel-1",
            "instance": "github-main",
            "fired": False,
            "previous_state": "running",
        }


def test_cancel_errors_and_gating(env, monkeypatch: pytest.MonkeyPatch) -> None:
    token = _enable_replay(monkeypatch)
    cfg = Settings()
    cfg.ensure_paths()
    app = create_app(cfg)
    with TestClient(app) as client:
        db = get_database(cfg.sqlite_path)
        db.record_event(
            delivery_id="run-cancel-2",
            event_type="issues",
            repo="octo/widget",
            issue_key="octo/widget#1",
            payload={"action": "opened"},
        )
        # Not claimed, so it is queued

        # 404 unknown
        resp = client.post(
            "/api/cancel",
            json={"delivery_id": "nope"},
            headers={"X-Robomp-Replay-Token": token},
        )
        assert resp.status_code == 404

        # 409 queued/non-running delivery: do not poison WorkerPool._cancelled.
        resp = client.post(
            "/api/cancel",
            json={"delivery_id": "run-cancel-2"},
            headers={"X-Robomp-Replay-Token": token},
        )
        assert resp.status_code == 409
        queued = db.get_event("run-cancel-2")
        assert queued is not None
        assert queued.state == "queued"

        # 401 with a bad token (header present but wrong value)
        resp = client.post(
            "/api/cancel",
            json={"delivery_id": "run-cancel-2"},
            headers={"X-Robomp-Replay-Token": "bad-token"},
        )
        assert resp.status_code == 401

        # 401 with the header missing entirely (distinct from a bad token)
        resp = client.post(
            "/api/cancel",
            json={"delivery_id": "run-cancel-2"},
        )
        assert resp.status_code == 401

    # with replay disabled (token not set) -> 404
    monkeypatch.setenv("ROBOMP_REPLAY_TOKEN", "")
    reset_settings_cache()
    cfg_disabled = Settings()
    cfg_disabled.ensure_paths()
    app_disabled = create_app(cfg_disabled)
    with TestClient(app_disabled) as client:
        resp = client.post(
            "/api/cancel",
            json={"delivery_id": "run-cancel-2"},
            headers={"X-Robomp-Replay-Token": token},
        )
        assert resp.status_code == 404


def test_retry_state_transition(env, monkeypatch: pytest.MonkeyPatch) -> None:
    token = _enable_replay(monkeypatch)
    cfg = Settings()
    cfg.ensure_paths()
    app = create_app(cfg)
    with TestClient(app) as client:
        db = get_database(cfg.sqlite_path)
        db.record_event(
            delivery_id="running-same-issue",
            event_type="issue_comment",
            repo="octo/widget",
            issue_key="octo/widget#1",
            payload={"action": "created"},
        )
        claimed = db.claim_next_event()
        assert claimed is not None
        assert claimed.delivery_id == "running-same-issue"

        db.record_event(
            delivery_id="failed-retry-1",
            event_type="issues",
            repo="octo/widget",
            issue_key="octo/widget#1",
            state="failed",
            last_error="error",
            payload={"action": "opened"},
        )

        resp = client.post(
            "/api/trigger",
            json={"mode": "retry", "delivery_id": "failed-retry-1"},
            headers={"X-Robomp-Replay-Token": token},
        )
        assert resp.status_code == 202
        assert resp.json() == {
            "delivery": "failed-retry-1",
            "instance": "github-main",
            "state": "queued",
            "mode": "retry",
        }

        # Verify DB side state transition
        evt = db.get_event("failed-retry-1")
        assert evt is not None
        assert evt.state == "queued"
