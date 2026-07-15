from __future__ import annotations

from dataclasses import replace

import pytest

from robomp.db import Database
from robomp.forge import Actor, ForgeEvent, RepositoryRef, WorkItemRef
from robomp.gitlab_client import GitLabIssueInfo, GitLabNoteInfo, GitLabProjectInfo
from robomp.routing import RoutingPolicy
from robomp.routing_tasks import RouteAction, apply_target_routing, route_issue


class FakeGitLab:
    def __init__(self) -> None:
        self.source = GitLabIssueInfo(
            project_id=2080,
            iid=7,
            title="Protocol packet schema is wrong",
            description="Update protocol/login.proto response field",
            state="opened",
            author="alice",
            labels=(),
            web_url="https://gitlab.example/ica/triage/-/issues/7",
            id=70,
        )
        self.moved = replace(
            self.source,
            project_id=357,
            iid=11,
            web_url="https://gitlab.example/ica/protocol/-/issues/11",
            id=110,
        )
        self.resolve_result: GitLabIssueInfo | None = None
        self.moves: list[tuple[int, int, int]] = []
        self.added_labels: list[tuple[int, int, tuple[str, ...]]] = []
        self.removed_labels: list[tuple[int, int, tuple[str, ...]]] = []
        self.notes: list[tuple[int, int, str]] = []
        self.get_issue_calls = 0

    async def get_issue(self, project_id: int, iid: int) -> GitLabIssueInfo:
        self.get_issue_calls += 1
        return self.source

    async def get_project(self, project_id: int) -> GitLabProjectInfo:
        return GitLabProjectInfo(
            id=project_id,
            path_with_namespace="ica/protocol",
            default_branch="master",
            http_url_to_repo="https://gitlab.example/ica/protocol.git",
            visibility="private",
            web_url="https://gitlab.example/ica/protocol",
        )

    async def resolve_moved_issue(
        self,
        source_project_id: int,
        iid: int,
        expected_target_project_id: int,
    ) -> GitLabIssueInfo | None:
        return self.resolve_result

    async def move_issue(self, source_project_id: int, iid: int, target_project_id: int) -> GitLabIssueInfo:
        self.moves.append((source_project_id, iid, target_project_id))
        return self.moved

    async def add_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo:
        self.added_labels.append((project_id, iid, tuple(labels)))
        return replace(self.moved, labels=tuple(dict.fromkeys((*self.moved.labels, *labels))))

    async def remove_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo:
        self.removed_labels.append((project_id, iid, tuple(labels)))
        return self.moved

    async def list_issue_notes(self, project_id: int, iid: int) -> list[GitLabNoteInfo]:
        return [
            GitLabNoteInfo(
                id=index,
                author="robomp",
                body=body,
                created_at="2026-07-15T00:00:00Z",
                system=False,
                internal=False,
            )
            for index, (note_project, note_iid, body) in enumerate(self.notes, start=1)
            if note_project == project_id and note_iid == iid
        ]

    async def post_issue_note(self, project_id: int, iid: int, body: str) -> GitLabNoteInfo:
        self.notes.append((project_id, iid, body))
        return GitLabNoteInfo(
            id=len(self.notes),
            author="robomp",
            body=body,
            created_at="2026-07-15T00:00:00Z",
            system=False,
            internal=False,
        )


def _policy(mode: str) -> RoutingPolicy:
    return RoutingPolicy.from_json(
        {
            "intake_project_id": 2080,
            "targets": [
                {
                    "key": "protocol",
                    "project_id": 357,
                    "mode": mode,
                    "default_branch": "master",
                    "paths": ["protocol"],
                    "aliases": ["protocol", "packet"],
                    "signals": ["protobuf", "response field"],
                }
            ],
        }
    )


def _event(delivery_id: str = "delivery-1") -> ForgeEvent:
    return ForgeEvent(
        delivery_id=delivery_id,
        source_event="Issue Hook",
        event="issue.opened",
        task_kind="route_issue",
        item=WorkItemRef(
            repository=RepositoryRef(
                instance_id="gitlab-zingplay",
                remote_id="2080",
                full_name="ica/triage",
            ),
            kind="issue",
            number=7,
        ),
        actor=Actor(remote_id="1", login="alice"),
        title="Protocol packet schema is wrong",
        body="Update protocol/login.proto response field",
        labels=(),
        source_payload={},
    )


@pytest.mark.asyncio
async def test_recommend_mode_records_decision_without_moving(tmp_path) -> None:
    db = Database(tmp_path / "routing.sqlite")
    gitlab = FakeGitLab()

    result = await route_issue(event=_event(), policy=_policy("recommend"), db=db, gitlab=gitlab)

    assert result.action is RouteAction.RECOMMENDED
    assert gitlab.moves == []
    assert gitlab.added_labels == [(2080, 7, ("needs-routing", "suggest::protocol"))]
    decisions = db.list_routing_decisions("gitlab-zingplay:2080:issue:7")
    assert [(decision.action, decision.selected_project_id) for decision in decisions] == [("recommended", "357")]


@pytest.mark.asyncio
async def test_auto_move_persists_lineage_and_defers_target_decoration(tmp_path) -> None:
    db = Database(tmp_path / "routing.sqlite")
    gitlab = FakeGitLab()

    result = await route_issue(event=_event(), policy=_policy("auto_move"), db=db, gitlab=gitlab)

    assert result.action is RouteAction.MOVED
    assert gitlab.moves == [(2080, 7, 357)]
    assert gitlab.added_labels == []
    lineage = db.resolve_routing_lineage("gitlab-zingplay:2080:issue:7")
    assert lineage is not None
    assert lineage.target_canonical_key == "gitlab-zingplay:357:issue:11"
    target_event = db.get_event("route:delivery-1:357:11", instance_id="gitlab-zingplay")
    assert target_event is not None
    assert target_event.task_kind == "routing_complete"


@pytest.mark.asyncio
async def test_auto_implement_target_event_decorates_before_triage(tmp_path) -> None:
    db = Database(tmp_path / "routing.sqlite")
    gitlab = FakeGitLab()

    result = await route_issue(event=_event(), policy=_policy("auto_implement"), db=db, gitlab=gitlab)
    row = db.get_event("route:delivery-1:357:11", instance_id="gitlab-zingplay")
    assert row is not None
    target_event = ForgeEvent.from_dict(row.payload)

    assert result.action is RouteAction.IMPLEMENTATION_QUEUED
    assert target_event.task_kind == "triage_issue"
    await apply_target_routing(target_event, gitlab)
    await apply_target_routing(target_event, gitlab)
    assert gitlab.added_labels == [
        (357, 11, ("routed", "roboomp")),
        (357, 11, ("routed", "roboomp")),
    ]
    assert len(gitlab.notes) == 1
    assert "Implementation has been queued" in gitlab.notes[0][2]


@pytest.mark.asyncio
async def test_incomplete_intent_reconciles_without_reclassifying_source(tmp_path) -> None:
    db = Database(tmp_path / "routing.sqlite")
    gitlab = FakeGitLab()
    gitlab.resolve_result = gitlab.moved
    event = _event()
    db.record_routing_decision(
        instance_id="gitlab-zingplay",
        delivery_id=event.delivery_id,
        source_canonical_key=event.item.key,
        ranked_candidates=[{"key": "protocol", "project_id": 357, "score": 4.0, "confidence": 0.95}],
        selected_target_key="protocol",
        selected_project_id=357,
        explicit=False,
        action="moved",
        mode="auto_move",
    )
    db.begin_routing_intent(event.item.key, 357)

    result = await route_issue(event=event, policy=_policy("auto_move"), db=db, gitlab=gitlab)

    assert result.action is RouteAction.MOVED
    assert gitlab.get_issue_calls == 0
    assert gitlab.moves == []
    assert db.resolve_routing_lineage(event.item.key) is not None
