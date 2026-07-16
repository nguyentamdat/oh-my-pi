from __future__ import annotations

import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from robomp.db import Database, RoutingLineageRow, canonical_item_key, iso_seconds_ago, issue_key


def test_record_event_dedupes_by_delivery(db: Database) -> None:
    payload = {"action": "opened", "issue": {"number": 1}}
    assert db.record_event(
        delivery_id="abc",
        event_type="issues",
        repo="octo/widget",
        issue_key=issue_key("octo/widget", 1),
        payload=payload,
    )
    assert not db.record_event(
        delivery_id="abc",
        event_type="issues",
        repo="octo/widget",
        issue_key=issue_key("octo/widget", 1),
        payload=payload,
    )


def _record_routing_lineage_event(
    db: Database,
    *,
    source_canonical_key: str,
    target_delivery_id: str = "routed-target-delivery",
    target_item_number: int = 19,
    target_payload: Mapping[str, Any] | None = None,
) -> RoutingLineageRow:
    payload: Mapping[str, Any] = {"object_kind": "issue", "object_attributes": {"iid": target_item_number}}
    if target_payload is not None:
        payload = target_payload
    return db.record_routing_lineage_event(
        source_canonical_key=source_canonical_key,
        target_delivery_id=target_delivery_id,
        target_event_type="Issue Hook",
        target_canonical_event="issue.opened",
        target_task_kind="triage_issue",
        target_repo="ica/target",
        target_repository_id="202",
        target_item_kind="issue",
        target_item_number=target_item_number,
        target_issue_key="gitlab-target:202:issue:19",
        target_payload=payload,
        target_instance_id="gitlab-target",
    )


def test_routing_lineage_and_synthetic_event_are_atomic_and_canonical(db: Database) -> None:
    source = canonical_item_key("gitlab-source", "101", "issue", 7)
    target = canonical_item_key("gitlab-target", "202", "issue", 19)

    with pytest.raises(TypeError):
        _record_routing_lineage_event(
            db,
            source_canonical_key=source,
            target_payload={"unserializable": object()},
        )
    assert db.resolve_routing_lineage(source) is None
    assert db.get_event("routed-target-delivery", instance_id="gitlab-target") is None

    lineage = _record_routing_lineage_event(db, source_canonical_key=source)
    assert lineage.source_canonical_key == source
    assert lineage.target_canonical_key == target
    assert db.resolve_routing_lineage(source) == lineage
    assert db.is_routed_target(target)

    event = db.get_event("routed-target-delivery", instance_id="gitlab-target")
    assert event is not None
    assert event.state == "queued"
    assert event.canonical_key == target
    assert event.instance_id == "gitlab-target"
    assert event.repository_id == "202"
    assert event.item_kind == "issue"
    assert event.item_number == 19
    assert event.canonical_event == "issue.opened"
    assert event.task_kind == "triage_issue"


def test_routing_lineage_retry_is_a_no_op_and_conflicts_are_rejected(db: Database) -> None:
    source = canonical_item_key("gitlab-source", "101", "issue", 7)
    target = canonical_item_key("gitlab-target", "202", "issue", 19)
    first = _record_routing_lineage_event(db, source_canonical_key=source)
    retry = _record_routing_lineage_event(
        db,
        source_canonical_key=source,
        target_delivery_id="routed-target-delivery",
    )
    assert retry == first
    assert [event.delivery_id for event in db.list_events()] == ["routed-target-delivery"]
    db.remove_event("routed-target-delivery", instance_id="gitlab-target")
    repaired = _record_routing_lineage_event(db, source_canonical_key=source)
    assert repaired == first
    assert [event.delivery_id for event in db.list_events()] == ["routed-target-delivery"]

    with pytest.raises(ValueError, match="routing lineage conflict"):
        _record_routing_lineage_event(
            db,
            source_canonical_key=source,
            target_delivery_id="conflicting-target",
            target_item_number=20,
        )
    lineage = db.resolve_routing_lineage(source)
    assert lineage is not None and lineage.target_canonical_key == target
    assert db.get_event("conflicting-target", instance_id="gitlab-target") is None


def test_routing_target_comment_does_not_suppress_synthetic_event(db: Database) -> None:
    source = canonical_item_key("gitlab-source", "101", "issue", 7)
    target = canonical_item_key("gitlab-target", "202", "issue", 19)
    assert db.record_event(
        instance_id="gitlab-target",
        delivery_id="target-arrived-first",
        event_type="Note Hook",
        canonical_event="note.created",
        task_kind="handle_comment",
        repo="ica/target",
        repository_id="202",
        item_kind="issue",
        item_number=19,
        issue_key="gitlab-target:202:issue:19",
        payload={"object_kind": "note"},
    )

    _record_routing_lineage_event(
        db,
        source_canonical_key=source,
        target_delivery_id="synthetic-target-delivery",
    )
    assert db.is_routed_target(target)
    assert {event.delivery_id for event in db.list_events()} == {
        "synthetic-target-delivery",
        "target-arrived-first",
    }
    synthetic = db.get_event("synthetic-target-delivery", instance_id="gitlab-target")
    assert synthetic is not None and synthetic.state == "queued"
    assert synthetic.task_kind == "triage_issue"
    assert not db.record_event(
        instance_id="gitlab-target",
        delivery_id="target-arrived-first",
        event_type="Issue Hook",
        repo="ica/target",
        issue_key="gitlab-target:202:issue:19",
        payload={"object_kind": "issue"},
    )


def test_routing_intent_survives_pre_completion_crash_and_retry(tmp_path: Path) -> None:
    path = tmp_path / "routing.sqlite"
    source = canonical_item_key("gitlab-source", "101", "issue", 7)
    target = canonical_item_key("gitlab-target", "202", "issue", 19)
    before_move = Database(path)
    intent = before_move.begin_routing_intent(source, 202)
    assert intent.target_canonical_key is None
    before_move.close()

    after_move = Database(path)
    try:
        pending = after_move.get_incomplete_routing_intent(source)
        assert pending == intent
        assert after_move.resolve_routing_lineage(source) is None

        with pytest.raises(TypeError):
            after_move.complete_routing_intent_event(
                source_canonical_key=source,
                target_delivery_id="intent-synthetic-delivery",
                target_event_type="Issue Hook",
                target_canonical_event="issue.opened",
                target_task_kind="triage_issue",
                target_repo="ica/target",
                target_repository_id="202",
                target_item_kind="issue",
                target_item_number=19,
                target_issue_key=target,
                target_payload={"unserializable": object()},
                target_instance_id="gitlab-target",
            )
        assert after_move.get_incomplete_routing_intent(source) == intent
        assert after_move.resolve_routing_lineage(source) is None

        lineage = after_move.complete_routing_intent_event(
            source_canonical_key=source,
            target_delivery_id="intent-synthetic-delivery",
            target_event_type="Issue Hook",
            target_canonical_event="issue.opened",
            target_task_kind="triage_issue",
            target_repo="ica/target",
            target_repository_id="202",
            target_item_kind="issue",
            target_item_number=19,
            target_issue_key=target,
            target_payload={"object_kind": "issue"},
            target_instance_id="gitlab-target",
        )
        assert lineage.target_canonical_key == target
        assert after_move.get_incomplete_routing_intent(source) is None
        completed = after_move.begin_routing_intent(source, "202")
        assert completed.target_canonical_key == target
        assert completed.completed_at is not None
        after_move.remove_event("intent-synthetic-delivery", instance_id="gitlab-target")

        retry = after_move.complete_routing_intent_event(
            source_canonical_key=source,
            target_delivery_id="intent-synthetic-delivery",
            target_event_type="Issue Hook",
            target_canonical_event="issue.opened",
            target_task_kind="triage_issue",
            target_repo="ica/target",
            target_repository_id="202",
            target_item_kind="issue",
            target_item_number=19,
            target_issue_key=target,
            target_payload={"object_kind": "issue"},
            target_instance_id="gitlab-target",
        )
        assert retry == lineage
        assert [event.delivery_id for event in after_move.list_events()] == ["intent-synthetic-delivery"]
        with pytest.raises(ValueError, match="routing intent conflict"):
            after_move.begin_routing_intent(source, 303)
    finally:
        after_move.close()


def test_routing_children_persist_multiple_targets_and_events(db: Database) -> None:
    source = canonical_item_key("gitlab-source", "101", "issue", 7)
    planned = db.plan_routing_children(source, [(202, "auto_implement"), (303, "auto_move")])
    first, second = planned
    assert first.target_canonical_key is None
    assert second.target_canonical_key is None
    assert all(len(child.idempotency_token) == 48 for child in planned)
    assert len({child.idempotency_token for child in planned}) == 2
    assert db.plan_routing_children(source, [(202, "auto_implement"), (303, "auto_move")]) == planned
    with pytest.raises(ValueError, match="conflicts"):
        db.plan_routing_children(source, [(202, "auto_implement")])

    completed = []
    for project_id, iid, mode in ((202, 19, "auto_implement"), (303, 23, "auto_move")):
        delivery_id = f"route:child:{project_id}:{iid}"
        completed.append(
            db.complete_routing_child_event(
                source_canonical_key=source,
                target_project_id=project_id,
                target_delivery_id=delivery_id,
                target_event_type="Issue Hook",
                target_canonical_event="issue.routed",
                target_task_kind="triage_issue" if mode == "auto_implement" else "routing_complete",
                target_repo=f"ica/{project_id}",
                target_item_kind="issue",
                target_item_number=iid,
                target_issue_key=canonical_item_key("gitlab-target", str(project_id), "issue", iid),
                target_payload={"object_kind": "issue"},
                target_instance_id="gitlab-target",
            )
        )

    children = db.list_routing_children(source)
    assert children == completed
    assert [child.target_project_id for child in children] == ["202", "303"]
    assert all(child.completed_at is not None for child in children)
    assert all(db.is_routed_target(child.target_canonical_key or "") for child in children)
    assert db.has_synthetic_routing_event(children[0].target_canonical_key or "")
    assert not db.has_synthetic_routing_event(children[1].target_canonical_key or "")
    assert {(event.repository_id, event.task_kind, event.state) for event in db.list_events()} == {
        ("202", "triage_issue", "queued"),
        ("303", "routing_complete", "queued"),
    }


def test_routing_child_adopts_existing_native_activation(db: Database) -> None:
    source = canonical_item_key("gitlab-source", "101", "issue", 7)
    target = canonical_item_key("gitlab-target", "202", "issue", 19)
    db.plan_routing_children(source, [(202, "auto_implement")])
    assert db.record_event(
        instance_id="gitlab-target",
        delivery_id="native-activation",
        event_type="Issue Hook",
        canonical_event="issue.updated",
        task_kind="triage_issue",
        repo="ica/202",
        repository_id="202",
        item_kind="issue",
        item_number=19,
        canonical_key=target,
        issue_key=target,
        payload={"object_kind": "issue"},
    )

    db.complete_routing_child_event(
        source_canonical_key=source,
        target_project_id=202,
        target_delivery_id="route:child:202:19",
        target_event_type="RoboOMP Route",
        target_canonical_event="issue.routed",
        target_task_kind="triage_issue",
        target_repo="ica/202",
        target_item_kind="issue",
        target_item_number=19,
        target_issue_key=target,
        target_payload={"object_kind": "issue"},
        target_instance_id="gitlab-target",
    )

    native = db.get_event("native-activation", instance_id="gitlab-target")
    synthetic = db.get_event("route:child:202:19", instance_id="gitlab-target")
    assert native is not None and native.state == "queued"
    assert synthetic is not None and synthetic.state == "skipped"
    assert synthetic.last_error == "activation already owned by delivery native-activation"


def test_routing_child_migration_marks_legacy_plan_for_manual_reconciliation(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "legacy-routing-child.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE routing_children (
          source_canonical_key TEXT NOT NULL,
          target_project_id TEXT NOT NULL,
          mode TEXT NOT NULL,
          target_canonical_key TEXT,
          target_delivery_id TEXT,
          created_at TEXT NOT NULL,
          completed_at TEXT,
          PRIMARY KEY (source_canonical_key, target_project_id)
        );
        INSERT INTO routing_children (
          source_canonical_key, target_project_id, mode, created_at
        ) VALUES (
          'gitlab-source:101:issue:7', '202', 'auto_implement', '2026-07-15T00:00:00Z'
        );
        """
    )
    conn.close()

    database = Database(path)
    try:
        children = database.list_routing_children("gitlab-source:101:issue:7")
    finally:
        database.close()

    assert len(children) == 1
    assert children[0].idempotency_token == "legacy:gitlab-source:101:issue:7:202"


def test_routing_decisions_retain_recommendation_and_later_explicit_route(db: Database) -> None:
    source = canonical_item_key("gitlab-source", "101", "issue", 7)
    candidates = [
        {"project_id": 202, "key": "target", "score": 8, "confidence": 0.9},
        {"project_id": 303, "key": "other", "score": 4, "confidence": 0.6},
    ]
    recommendation = db.record_routing_decision(
        instance_id="gitlab-source",
        delivery_id="recommendation-delivery",
        source_canonical_key=source,
        ranked_candidates=candidates,
        selected_target_key=None,
        selected_project_id=None,
        explicit=False,
        action="recommend",
        mode="recommend",
    )
    human_route = db.record_routing_decision(
        instance_id="gitlab-source",
        delivery_id="human-route-delivery",
        source_canonical_key=source,
        ranked_candidates=candidates,
        selected_target_key="target",
        selected_project_id=202,
        explicit=True,
        action="route",
        mode="auto_move",
    )
    repeat_recommendation = db.record_routing_decision(
        instance_id="gitlab-source",
        delivery_id="recommendation-delivery",
        source_canonical_key=source,
        ranked_candidates=candidates,
        selected_target_key=None,
        selected_project_id=None,
        explicit=False,
        action="recommend",
        mode="recommend",
    )

    assert repeat_recommendation == recommendation
    decisions = db.list_routing_decisions(source)
    assert [decision.delivery_id for decision in decisions] == [
        "recommendation-delivery",
        "human-route-delivery",
    ]
    assert decisions[0].candidates == candidates
    assert decisions[0].selected_target_key is None
    assert not decisions[0].explicit
    assert human_route.selected_target_key == "target"
    assert human_route.selected_project_id == "202"
    assert human_route.explicit


def test_routing_status_is_bounded_and_joins_planned_and_synthetic_children(db: Database) -> None:
    source = canonical_item_key("gitlab-zingplay", "2080", "issue", 7)
    assert db.record_event(
        instance_id="gitlab-zingplay",
        delivery_id="source-triage-7",
        event_type="Issue Hook",
        repo="ica/triage",
        repository_id="2080",
        item_kind="issue",
        item_number=7,
        canonical_key=source,
        issue_key=source,
        payload={"object_kind": "issue"},
    )
    db.record_routing_decision(
        instance_id="gitlab-zingplay",
        delivery_id="source-triage-7",
        source_canonical_key=source,
        ranked_candidates=[
            {"project_id": 202, "key": "recommendation"},
            {"project_id": 303, "key": "implementation"},
        ],
        selected_target_key=None,
        selected_project_id=None,
        explicit=False,
        action="children_queued",
        mode="none",
    )
    db.plan_routing_children(source, [(202, "recommend"), (303, "auto_implement"), (404, "auto_implement")])
    db.complete_routing_child_event(
        source_canonical_key=source,
        target_project_id=303,
        target_delivery_id="route:303:19",
        target_event_type="RoboOMP Route",
        target_repo="ica/implementation",
        target_issue_key=canonical_item_key("gitlab-zingplay", "303", "issue", 19),
        target_payload={"object_kind": "issue"},
        target_item_kind="issue",
        target_item_number=19,
        target_task_kind="triage_issue",
        target_instance_id="gitlab-zingplay",
    )
    native_target = canonical_item_key("gitlab-zingplay", "404", "issue", 20)
    assert db.record_event(
        instance_id="gitlab-zingplay",
        delivery_id="native:404:20",
        event_type="Issue Hook",
        canonical_event="issue.updated",
        task_kind="triage_issue",
        repo="ica/native-implementation",
        repository_id="404",
        item_kind="issue",
        item_number=20,
        canonical_key=native_target,
        issue_key=native_target,
        payload={"object_kind": "issue"},
    )
    db.complete_routing_child_event(
        source_canonical_key=source,
        target_project_id=404,
        target_delivery_id="route:404:20",
        target_event_type="RoboOMP Route",
        target_repo="ica/native-implementation",
        target_issue_key=native_target,
        target_payload={"object_kind": "issue"},
        target_item_kind="issue",
        target_item_number=20,
        target_task_kind="triage_issue",
        target_instance_id="gitlab-zingplay",
    )

    rows = db.list_routing_status()

    assert [row.target_project_id for row in rows] == ["202", "303", "404"]
    assert all(row.source_repo == "ica/triage" and row.source_number == 7 for row in rows)
    assert [row.target_state for row in rows] == [None, "queued", "queued"]
    assert [row.target_attempts for row in rows] == [None, 0, 0]
    assert rows[1].target_repo == "ica/implementation"
    assert rows[1].target_task_kind == "triage_issue"
    assert rows[2].target_delivery_id == "native:404:20"
    assert rows[2].target_repo == "ica/native-implementation"

    for number in range(25):
        other_source = canonical_item_key("gitlab-zingplay", str(400 + number), "issue", number)
        db.record_routing_decision(
            instance_id="gitlab-zingplay",
            delivery_id=f"later-decision-{number}",
            source_canonical_key=other_source,
            ranked_candidates=[],
            selected_target_key=None,
            selected_project_id=None,
            explicit=False,
            action="recommend",
            mode="recommend",
        )
        db.plan_routing_children(other_source, [(500 + number, "recommend")])

    bounded = db.list_routing_status()
    assert len(bounded) == 25
    assert source not in {row.source_canonical_key for row in bounded}


def test_routing_lineage_schema_is_safe_to_reopen(tmp_path: Path) -> None:
    path = tmp_path / "routing.sqlite"
    source = canonical_item_key("gitlab-source", "101", "issue", 7)
    target = canonical_item_key("gitlab-target", "202", "issue", 19)
    first = Database(path)
    _record_routing_lineage_event(first, source_canonical_key=source)
    first.close()

    reopened = Database(path)
    try:
        lineage = reopened.resolve_routing_lineage(source)
        assert lineage is not None
        assert lineage.target_canonical_key == target
        assert reopened.is_routed_target(target)
        assert reopened.get_event("routed-target-delivery", instance_id="gitlab-target") is not None
    finally:
        reopened.close()


def test_event_identity_allows_same_delivery_and_local_number_across_instances(db: Database) -> None:
    for instance_id in ("github-main", "gitlab-zingplay"):
        assert db.record_event(
            instance_id=instance_id,
            delivery_id="same-delivery",
            event_type="Issue Hook",
            canonical_event="issue.opened",
            task_kind="triage_issue",
            repo="ica/server",
            repository_id="356",
            item_kind="issue",
            item_number=42,
            issue_key=f"{instance_id}:356:issue:42",
            payload={"object_kind": "issue"},
        )

    github = db.get_event("same-delivery", instance_id="github-main")
    gitlab = db.get_event("same-delivery", instance_id="gitlab-zingplay")
    assert github is not None and gitlab is not None
    assert github.canonical_key == "github-main:356:issue:42"
    assert gitlab.canonical_key == "gitlab-zingplay:356:issue:42"

    first = db.claim_next_event()
    second = db.claim_next_event()
    assert first is not None and second is not None
    assert {first.instance_id, second.instance_id} == {"github-main", "gitlab-zingplay"}


def test_activated_item_requires_non_skipped_activation(db: Database) -> None:
    key = "gitlab-zingplay:356:issue:42"
    assert not db.has_activated_item(key)
    db.record_event(
        instance_id="gitlab-zingplay",
        delivery_id="skipped",
        event_type="Issue Hook",
        canonical_event="issue.opened",
        task_kind="triage_issue",
        repo="ica/server",
        repository_id="356",
        item_kind="issue",
        item_number=42,
        issue_key=key,
        payload={},
        state="skipped",
    )
    assert not db.has_activated_item(key)
    db.record_event(
        instance_id="gitlab-zingplay",
        delivery_id="accepted",
        event_type="Issue Hook",
        canonical_event="issue.opened",
        task_kind="triage_issue",
        repo="ica/server",
        repository_id="356",
        item_kind="issue",
        item_number=42,
        issue_key=key,
        payload={},
    )
    assert db.has_activated_item(key)


def test_claim_next_event_singleton_under_contention(db: Database) -> None:
    for i in range(5):
        db.record_event(
            delivery_id=f"d-{i}",
            event_type="issues",
            repo="octo/widget",
            issue_key=issue_key("octo/widget", i),
            payload={"i": i},
        )

    winners: list[str] = []
    lock = threading.Lock()

    def claim() -> None:
        row = db.claim_next_event()
        if row is not None:
            with lock:
                winners.append(row.delivery_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for _ in range(5):
            futures = [pool.submit(claim) for _ in range(8)]
            for f in futures:
                f.result()

    # Each delivery id should appear exactly once.
    assert sorted(winners) == [f"d-{i}" for i in range(5)]
    assert all(db.get_event(f"d-{i}").state == "running" for i in range(5))


def test_claim_next_event_leaves_same_issue_queued_while_running(db: Database) -> None:
    key = issue_key("octo/widget", 4)
    db.record_event(
        delivery_id="running",
        event_type="issue_comment",
        repo="octo/widget",
        issue_key=key,
        payload={"action": "created"},
        state="running",
    )
    db.record_event(
        delivery_id="queued",
        event_type="issue_comment",
        repo="octo/widget",
        issue_key=key,
        payload={"action": "created"},
    )

    assert db.claim_next_event() is None
    assert db.get_event("queued").state == "queued"

    db.mark_event("running", "done")
    claimed = db.claim_next_event()
    assert claimed is not None
    assert claimed.delivery_id == "queued"
    assert db.get_event("queued").state == "running"


def test_claim_next_event_skips_blocked_issue_without_stalling_others(db: Database) -> None:
    blocked = issue_key("octo/widget", 4)
    ready = issue_key("octo/widget", 5)
    db.record_event(
        delivery_id="running",
        event_type="issue_comment",
        repo="octo/widget",
        issue_key=blocked,
        payload={"action": "created"},
        state="running",
    )
    db.record_event(
        delivery_id="blocked",
        event_type="issue_comment",
        repo="octo/widget",
        issue_key=blocked,
        payload={"action": "created"},
    )
    db.record_event(
        delivery_id="ready",
        event_type="issues",
        repo="octo/widget",
        issue_key=ready,
        payload={"action": "opened"},
    )

    claimed = db.claim_next_event()
    assert claimed is not None
    assert claimed.delivery_id == "ready"
    assert db.get_event("blocked").state == "queued"


def test_requeue_event_can_be_restricted_by_source_state(db: Database) -> None:
    db.record_event(
        delivery_id="done-event",
        event_type="issues",
        repo="octo/widget",
        issue_key=issue_key("octo/widget", 1),
        payload={},
        state="done",
    )
    db.record_event(
        delivery_id="running-event",
        event_type="issues",
        repo="octo/widget",
        issue_key=issue_key("octo/widget", 2),
        payload={},
        state="running",
    )

    assert db.requeue_event("done-event", from_states=("done", "failed", "skipped"))
    assert db.get_event("done-event").state == "queued"

    assert not db.requeue_event("running-event", from_states=("done", "failed", "skipped"))
    assert db.get_event("running-event").state == "running"


def test_latest_issue_events_ignore_skipped_noise(db: Database) -> None:
    fixed = issue_key("octo/widget", 1)
    still_failed = issue_key("octo/widget", 2)
    db.record_event(
        delivery_id="fixed-failed",
        event_type="issues",
        repo="octo/widget",
        issue_key=fixed,
        payload={"action": "opened"},
        state="failed",
    )
    db.record_event(
        delivery_id="fixed-done",
        event_type="issues",
        repo="octo/widget",
        issue_key=fixed,
        payload={"action": "opened"},
        state="done",
    )
    db.record_event(
        delivery_id="failed-run",
        event_type="issues",
        repo="octo/widget",
        issue_key=still_failed,
        payload={"action": "opened"},
        state="failed",
    )
    db.record_event(
        delivery_id="label-noise",
        event_type="issues",
        repo="octo/widget",
        issue_key=still_failed,
        payload={"action": "labeled"},
        state="skipped",
        last_error="issues.labeled ignored",
    )

    latest_failed = db.latest_event_for_issue(still_failed)
    latest_raw = db.latest_event_for_issue(still_failed, include_skipped=True)
    assert latest_failed is not None
    assert latest_raw is not None
    assert latest_failed.delivery_id == "failed-run"
    assert latest_raw.delivery_id == "label-noise"

    latest = db.latest_events_for_issues((fixed, still_failed))
    assert latest[fixed].delivery_id == "fixed-done"
    assert latest[still_failed].delivery_id == "failed-run"

    counts = db.latest_issue_event_state_counts()
    assert counts["done"] == 1
    assert counts["failed"] == 1
    assert counts["skipped"] == 0


def test_reset_stuck_running_recovers(db: Database) -> None:
    db.record_event(
        delivery_id="d1",
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#1",
        payload={},
    )
    row = db.claim_next_event()
    assert row is not None
    # Capture `started_at` set by the claim so we can prove the recovery flip preserves it.
    with db._lock:  # noqa: SLF001
        before = db._conn.execute(  # noqa: SLF001
            "SELECT started_at FROM events WHERE delivery_id=?", ("d1",)
        ).fetchone()
    assert before is not None
    assert before["started_at"] is not None
    # Simulate crash: row still running.
    recovered = db.reset_stuck_running()
    assert recovered == 1
    assert db.get_event("d1").state == "queued"
    with db._lock:  # noqa: SLF001
        after = db._conn.execute(  # noqa: SLF001
            "SELECT started_at FROM events WHERE delivery_id=?", ("d1",)
        ).fetchone()
    assert after is not None
    assert after["started_at"] == before["started_at"]


def test_upsert_issue_round_trip(db: Database) -> None:
    key = issue_key("octo/widget", 7)
    row = db.upsert_issue(
        key=key,
        repo="octo/widget",
        number=7,
        state="new",
    )
    assert row.state == "new"
    row = db.upsert_issue(
        key=key,
        repo="octo/widget",
        number=7,
        state="opened",
        branch="farm/abcd1234/some-issue",
        session_dir="/tmp/s",
        pr_number=42,
    )
    assert row.state == "opened"
    assert row.branch == "farm/abcd1234/some-issue"
    assert row.pr_number == 42
    fetched = db.get_issue(key)
    assert fetched and fetched.pr_number == 42

    found = db.find_issue_by_pr("octo/widget", 42)
    assert found and found.key == key
    by_branch = db.find_issue_by_branch("octo/widget", "farm/abcd1234/some-issue")
    assert by_branch and by_branch.key == key


def test_log_tool_call(db: Database) -> None:
    db.upsert_issue(key="octo/widget#1", repo="octo/widget", number=1, state="new")
    row_id = db.log_tool_call(
        issue_key="octo/widget#1",
        tool="forge_post_comment",
        args={"body": "hi"},
        result={"comment_id": 9},
    )
    assert row_id > 0


def test_pr_review_comment_staging_round_trip(db: Database) -> None:
    first = db.stage_review_comment(
        issue_key="octo/widget#9",
        path="src/app.py",
        line=12,
        side="RIGHT",
        start_line=10,
        start_side="RIGHT",
        body="blocking finding",
    )
    db.stage_review_comment(
        issue_key="octo/widget#9",
        path="src/other.py",
        line=3,
        body="nit",
    )
    db.stage_review_comment(issue_key="octo/widget#10", path="x.py", line=1, body="other")

    rows = db.list_staged_review_comments("octo/widget#9")
    assert [row.id for row in rows] == [first.id, first.id + 1]
    assert rows[0].path == "src/app.py"
    assert rows[0].start_line == 10
    assert rows[0].start_side == "RIGHT"
    assert rows[1].side == "RIGHT"

    assert db.clear_staged_review_comments("octo/widget#9") == 2
    assert db.list_staged_review_comments("octo/widget#9") == []
    assert len(db.list_staged_review_comments("octo/widget#10")) == 1


def test_processed_issue_keys_returns_only_known(db: Database) -> None:
    db.upsert_issue(key=issue_key("octo/widget", 1), repo="octo/widget", number=1, state="new")
    db.upsert_issue(key=issue_key("octo/widget", 2), repo="octo/widget", number=2, state="reproducing")
    queried = [
        issue_key("octo/widget", 1),
        issue_key("octo/widget", 2),
        issue_key("octo/widget", 3),  # never upserted
        issue_key("octo/other", 7),  # different repo, never upserted
    ]
    result = db.processed_issue_keys(queried)
    assert result == {issue_key("octo/widget", 1), issue_key("octo/widget", 2)}


def test_processed_issue_keys_empty_input(db: Database) -> None:
    assert db.processed_issue_keys([]) == set()
    # Empty strings are filtered out, not sent as a parameter.
    assert db.processed_issue_keys(["", ""]) == set()


def test_processed_issue_keys_handles_large_batch(db: Database) -> None:
    # Confirms the 500-batch chunking path (>500 parameters would otherwise hit
    # SQLite's SQLITE_MAX_VARIABLE_NUMBER default of 999 on older builds).
    keys = [issue_key("octo/widget", n) for n in range(1, 750)]
    # Persist only every 3rd one.
    for k, n in zip(keys, range(1, 750), strict=True):
        if n % 3 == 0:
            db.upsert_issue(key=k, repo="octo/widget", number=n, state="new")
    result = db.processed_issue_keys(keys + ["bogus#1"])
    expected = {issue_key("octo/widget", n) for n in range(1, 750) if n % 3 == 0}
    assert result == expected


def test_classification_roundtrip(db: Database) -> None:
    key = issue_key("octo/widget", 7)
    db.upsert_issue(key=key, repo="octo/widget", number=7, state="new")
    row = db.get_issue(key)
    assert row is not None and row.classification is None
    db.set_issue_classification(key, "question")
    row = db.get_issue(key)
    assert row is not None and row.classification == "question"
    # Round-trip via list_issues too.
    items = db.list_issues()
    assert any(r.key == key and r.classification == "question" for r in items)


def test_migration_adds_classification_to_existing_db(tmp_path: Path) -> None:
    """Open a DB without the classification column and verify the migration."""
    import sqlite3

    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE events (delivery_id TEXT PRIMARY KEY, event_type TEXT, payload_json TEXT,
          received_at TEXT, state TEXT CHECK(state IN ('queued','running','done','failed','skipped')),
          attempts INTEGER DEFAULT 0, last_error TEXT, repo TEXT, issue_key TEXT,
          started_at TEXT, finished_at TEXT);
        CREATE TABLE issues (key TEXT PRIMARY KEY, repo TEXT, number INTEGER, branch TEXT,
          session_dir TEXT, pr_number INTEGER, state TEXT, updated_at TEXT);
        CREATE TABLE tool_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, issue_key TEXT,
          tool TEXT, args_json TEXT, result_json TEXT, error TEXT, ts TEXT);
        INSERT INTO issues VALUES ('octo/widget#1', 'octo/widget', 1, 'farm/x', '/tmp/s', NULL,
          'reproducing', '2026-01-01T00:00:00Z');
        INSERT INTO events VALUES (
          'legacy-delivery', 'issues',
          '{"action":"opened","repository":{"id":99},"issue":{"number":1}}',
          '2026-01-01T00:00:00Z', 'queued', 0, NULL, 'octo/widget', 'octo/widget#1',
          NULL, NULL
        );
        """
    )
    conn.commit()
    conn.close()
    # Opening through our Database class should auto-migrate.
    database = Database(path)
    row = database.get_issue("octo/widget#1")
    assert row is not None
    assert row.classification is None  # column exists, default NULL
    database.set_issue_classification("octo/widget#1", "bug")
    assert database.get_issue("octo/widget#1").classification == "bug"
    event = database.get_event("legacy-delivery")
    assert event is not None
    assert event.instance_id == "github-main"
    assert event.repository_id == "99"
    assert event.item_kind == "issue"
    assert event.item_number == 1
    assert event.task_kind == "triage_issue"
    assert event.canonical_key == "github-main:99:issue:1"
    database.close()


def test_set_event_model_persists_on_running_event(db: Database) -> None:
    """`set_event_model` writes the picked model so the dashboard can attribute behavior."""
    db.record_event(
        delivery_id="d-model",
        event_type="issues",
        repo="octo/widget",
        issue_key=issue_key("octo/widget", 42),
        payload={"action": "opened"},
    )
    row = db.claim_next_event()
    assert row is not None and row.delivery_id == "d-model"
    db.set_event_model("d-model", "claude-sonnet-4-5")
    running = db.list_running_events()
    assert len(running) == 1
    assert running[0]["model"] == "claude-sonnet-4-5"
    # Setting a different model later (e.g. retry) overwrites in place.
    db.set_event_model("d-model", "claude-opus-4-5")
    running = db.list_running_events()
    assert running[0]["model"] == "claude-opus-4-5"


def test_list_running_events_surfaces_last_tool_since_start(db: Database) -> None:
    """`list_running_events` joins the most recent tool_call newer than `started_at`.

    Tool calls logged before the current run (e.g. an earlier triage on the same
    issue) MUST NOT be reported as the current activity.
    """
    key = issue_key("octo/widget", 7)
    db.upsert_issue(key=key, repo="octo/widget", number=7, state="reproducing")
    # Stale tool call from a previous run (no started_at yet).
    db.log_tool_call(issue_key=key, tool="stale_tool", args={})
    db.record_event(
        delivery_id="d-7",
        event_type="issues",
        repo="octo/widget",
        issue_key=key,
        payload={"action": "opened"},
    )
    db.claim_next_event()  # sets started_at
    # Before any current-run tool call: last_tool must be NULL, not "stale_tool".
    running = db.list_running_events()
    assert len(running) == 1
    assert running[0]["last_tool"] is None
    assert running[0]["last_tool_ts"] is None
    # New tool call after start → surfaces in the snapshot.
    db.log_tool_call(issue_key=key, tool="forge_post_comment", args={"body": "hi"})
    db.log_tool_call(issue_key=key, tool="set_issue_labels", args={"labels": ["bug"]})
    running = db.list_running_events()
    assert running[0]["last_tool"] == "set_issue_labels"  # latest by ts
    assert running[0]["last_tool_ts"] is not None


def test_record_submission_dedupes_by_delivery(db: Database) -> None:
    assert db.record_submission(delivery_id="d-1", login="Alice", repo="octo/widget")
    # Retry of the same delivery id is a no-op (idempotent webhook delivery).
    assert not db.record_submission(delivery_id="d-1", login="alice", repo="octo/widget")


def test_admit_submission_dedupes_by_delivery_before_rate_limit(db: Database) -> None:
    since = iso_seconds_ago(60)
    first = db.admit_submission(
        delivery_id="d-1",
        login="Alice",
        repo="octo/widget",
        since=since,
        cap=1,
    )
    assert first.accepted
    assert not first.duplicate
    assert first.used == 1

    duplicate = db.admit_submission(
        delivery_id="d-1",
        login="alice",
        repo="octo/widget",
        since=since,
        cap=1,
    )
    assert duplicate.accepted
    assert duplicate.duplicate
    assert duplicate.used == 1

    rejected = db.admit_submission(
        delivery_id="d-2",
        login="ALICE",
        repo="octo/widget",
        since=since,
        cap=1,
    )
    assert not rejected.accepted
    assert not rejected.duplicate
    assert rejected.used == 1
    assert db.count_submissions_since("alice", since) == 1


def test_admit_submission_enforces_cap_atomically_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "admission.sqlite"
    # Pre-warm: open + migrate the schema once so the two racing threads below
    # collide only on `admit_submission` (which is what the test is exercising),
    # not on `Database.__init__`. `executescript(SCHEMA)` flips journal_mode to
    # WAL, which needs a brief exclusive lock — without pre-warming, one
    # thread can lose that race and never reach `barrier.wait()`, deadlocking
    # its peer at the barrier (no timeout) and hanging `future.result()`.
    Database(path).close()
    barrier = threading.Barrier(2, timeout=10)

    def admit(delivery_id: str) -> bool:
        database = Database(path)
        try:
            barrier.wait()
            return database.admit_submission(
                delivery_id=delivery_id,
                login="alice",
                repo="octo/widget",
                since=iso_seconds_ago(60),
                cap=1,
            ).accepted
        finally:
            database.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(admit, f"d-{i}") for i in range(2)]
        accepted = [future.result(timeout=15) for future in futures]

    verifier = Database(path)
    try:
        assert sorted(accepted) == [False, True]
        assert verifier.count_submissions_since("alice", iso_seconds_ago(60)) == 1
    finally:
        verifier.close()


def test_count_submissions_since_is_case_insensitive(db: Database) -> None:
    db.record_submission(delivery_id="d-1", login="Alice", repo="octo/widget")
    db.record_submission(delivery_id="d-2", login="ALICE", repo="octo/widget")
    db.record_submission(delivery_id="d-3", login="bob", repo="octo/widget")
    # Window covering the whole test run.
    since = iso_seconds_ago(60)
    assert db.count_submissions_since("alice", since) == 2
    assert db.count_submissions_since("ALICE", since) == 2
    assert db.count_submissions_since("bob", since) == 1
    assert db.count_submissions_since("nobody", since) == 0


def test_count_submissions_since_respects_window(db: Database) -> None:
    db.record_submission(delivery_id="d-1", login="alice", repo="octo/widget")
    # Future cutoff means the just-inserted row is *before* the window.
    future = iso_seconds_ago(-60)
    assert db.count_submissions_since("alice", future) == 0


# -------- pending_closures ---------------------------------------------


_KEY = issue_key("octo/widget", 42)


def _seed_pending(db: Database, *, close_at: str = "2026-05-15T00:00:00.000000Z") -> None:
    db.upsert_pending_closure(
        issue_key=_KEY,
        repo="octo/widget",
        number=42,
        comment_id=999,
        issue_author="Alice",
        close_at=close_at,
    )


def test_upsert_pending_closure_lowercases_author_and_starts_pending(db: Database) -> None:
    _seed_pending(db)
    row = db.get_pending_closure(_KEY)
    assert row is not None
    assert row.state == "pending"
    assert row.cancel_reason is None
    assert row.issue_author == "alice"  # author stored lower-cased for cheap eq
    assert row.comment_id == 999


def test_upsert_pending_closure_overwrites_prior_schedule(db: Database) -> None:
    _seed_pending(db)
    db.finalize_closure(_KEY, state="cancelled", reason="user_replied")
    # A follow-up bot answer should reset the row to pending and update fields.
    db.upsert_pending_closure(
        issue_key=_KEY,
        repo="octo/widget",
        number=42,
        comment_id=1234,
        issue_author="alice",
        close_at="2030-01-01T00:00:00.000000Z",
    )
    row = db.get_pending_closure(_KEY)
    assert row is not None
    assert row.state == "pending"
    assert row.cancel_reason is None
    assert row.comment_id == 1234
    assert row.close_at == "2030-01-01T00:00:00.000000Z"


def test_claim_due_closures_only_returns_due_pending(db: Database) -> None:
    _seed_pending(db, close_at="2000-01-01T00:00:00.000000Z")  # past
    db.upsert_pending_closure(
        issue_key=issue_key("octo/widget", 7),
        repo="octo/widget",
        number=7,
        comment_id=10,
        issue_author="bob",
        close_at="2999-01-01T00:00:00.000000Z",  # future
    )
    claimed = db.claim_due_closures(now="2026-05-15T00:00:00.000000Z")
    assert [r.issue_key for r in claimed] == [_KEY]
    assert all(r.state == "claimed" for r in claimed)
    # And re-claiming returns nothing because the first one is no longer pending.
    again = db.claim_due_closures(now="2026-05-15T00:00:00.000000Z")
    assert again == []


def test_claim_due_closures_atomic_under_contention(db: Database) -> None:
    """Two concurrent claims see disjoint rows."""
    for n in range(5):
        db.upsert_pending_closure(
            issue_key=issue_key("octo/widget", n),
            repo="octo/widget",
            number=n,
            comment_id=100 + n,
            issue_author="alice",
            close_at="2000-01-01T00:00:00.000000Z",
        )
    seen: list[str] = []
    lock = threading.Lock()

    def claim_some() -> None:
        rows = db.claim_due_closures(now="2026-05-15T00:00:00.000000Z", limit=2)
        with lock:
            seen.extend(r.issue_key for r in rows)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for _ in range(4):
            list(pool.map(lambda _: claim_some(), range(4)))
    # Each row must appear at most once across all claims.
    assert sorted(seen) == sorted({issue_key("octo/widget", n) for n in range(5)})


def test_cancel_pending_closure_only_fires_when_pending(db: Database) -> None:
    _seed_pending(db)
    assert db.cancel_pending_closure(_KEY, reason="user_replied")
    row = db.get_pending_closure(_KEY)
    assert row is not None
    assert row.state == "cancelled"
    assert row.cancel_reason == "user_replied"
    # A second cancel against an already-cancelled row is a no-op.
    assert not db.cancel_pending_closure(_KEY, reason="user_replied")


def test_cancel_pending_closure_skips_claimed_rows(db: Database) -> None:
    """A `claimed` row must be left for the scheduler tick that owns it."""
    _seed_pending(db, close_at="2000-01-01T00:00:00.000000Z")
    claimed = db.claim_due_closures(now="2026-05-15T00:00:00.000000Z")
    assert claimed and claimed[0].state == "claimed"
    assert not db.cancel_pending_closure(_KEY, reason="user_replied")
    row = db.get_pending_closure(_KEY)
    assert row is not None and row.state == "claimed"


def test_finalize_closure_rejects_non_terminal_state(db: Database) -> None:
    _seed_pending(db)
    import pytest

    with pytest.raises(ValueError):
        db.finalize_closure(_KEY, state="pending", reason=None)  # type: ignore[arg-type]


def test_requeue_claimed_closure_only_flips_claimed(db: Database) -> None:
    _seed_pending(db, close_at="2000-01-01T00:00:00.000000Z")
    db.claim_due_closures(now="2026-05-15T00:00:00.000000Z")
    assert db.requeue_claimed_closure(_KEY)
    row = db.get_pending_closure(_KEY)
    assert row is not None and row.state == "pending"
    # Now in pending state, requeue is a no-op.
    assert not db.requeue_claimed_closure(_KEY)
