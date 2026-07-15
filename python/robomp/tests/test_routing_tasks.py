from __future__ import annotations

from dataclasses import replace

import pytest

from robomp.db import Database
from robomp.forge import Actor, ForgeEvent, RepositoryRef, WorkItemRef
from robomp.gitlab_client import GitLabIssueInfo, GitLabNoteInfo, GitLabProjectInfo
from robomp.routing import RouteCandidate, RouteDecision, RoutingPolicy
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
        self.projects = {
            332: ("ica/client", "dev"),
            356: ("ica/server", "qc"),
            357: ("ica/protocol", "master"),
        }
        self.children: dict[int, list[GitLabIssueInfo]] = {}
        self.fail_create_project_id: int | None = None
        self.resolve_result: GitLabIssueInfo | None = None
        self.moves: list[tuple[int, int, int]] = []
        self.added_labels: list[tuple[int, int, tuple[str, ...]]] = []
        self.removed_labels: list[tuple[int, int, tuple[str, ...]]] = []
        self.notes: list[tuple[int, int, str]] = []
        self.get_issue_calls = 0

    async def get_issue(self, project_id: int, iid: int) -> GitLabIssueInfo:
        self.get_issue_calls += 1
        if project_id == self.source.project_id and iid == self.source.iid:
            return self.source
        for issue in self.children.get(project_id, []):
            if issue.iid == iid:
                return issue
        return self.moved

    async def get_project(self, project_id: int) -> GitLabProjectInfo:
        path, branch = self.projects[project_id]
        return GitLabProjectInfo(
            id=project_id,
            path_with_namespace=path,
            default_branch=branch,
            http_url_to_repo=f"https://gitlab.example/{path}.git",
            visibility="private",
            web_url=f"https://gitlab.example/{path}",
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
        path, _ = self.projects[target_project_id]
        return replace(
            self.source,
            project_id=target_project_id,
            iid=11,
            web_url=f"https://gitlab.example/{path}/-/issues/11",
            id=110,
        )

    async def create_issue(
        self,
        project_id: int,
        *,
        title: str,
        description: str,
        labels: list[str] | None = None,
    ) -> GitLabIssueInfo:
        if self.fail_create_project_id == project_id:
            raise RuntimeError("injected create failure")
        path, _ = self.projects[project_id]
        iid = len(self.children.get(project_id, [])) + 20
        issue = GitLabIssueInfo(
            project_id=project_id,
            iid=iid,
            title=title,
            description=description,
            state="opened",
            author="robomp",
            labels=tuple(labels or ()),
            web_url=f"https://gitlab.example/{path}/-/issues/{iid}",
            id=project_id * 1000 + iid,
        )
        self.children.setdefault(project_id, []).append(issue)
        return issue

    async def find_issue_by_marker(self, project_id: int, marker: str) -> GitLabIssueInfo | None:
        return next(
            (issue for issue in self.children.get(project_id, []) if marker in issue.description),
            None,
        )

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


def _multi_policy() -> RoutingPolicy:
    return RoutingPolicy.from_json(
        {
            "intake_project_id": 2080,
            "targets": [
                {
                    "key": "client",
                    "project_id": 332,
                    "mode": "recommend",
                    "default_branch": "dev",
                    "paths": ["client"],
                    "aliases": ["client", "ui"],
                    "signals": ["inventory", "skin"],
                },
                {
                    "key": "server",
                    "project_id": 356,
                    "mode": "auto_implement",
                    "default_branch": "qc",
                    "paths": ["server"],
                    "aliases": ["server", "backend"],
                    "signals": ["api", "command"],
                },
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
    gitlab.source = replace(gitlab.source, labels=("suggest::server",))

    result = await route_issue(event=_event(), policy=_policy("recommend"), db=db, gitlab=gitlab)

    assert result.action is RouteAction.RECOMMENDED
    assert gitlab.moves == []
    assert gitlab.added_labels == [(2080, 7, ("needs-routing", "suggest::protocol"))]
    assert gitlab.removed_labels == [(2080, 7, ("suggest::server",))]
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


class FakeClassifier:
    def __init__(self, decision: RouteDecision) -> None:
        self.decision = decision
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    async def classify(self, title: str, body: str, paths=()) -> RouteDecision:
        self.calls.append((title, body, tuple(paths)))
        return self.decision


@pytest.mark.asyncio
async def test_llm_classifier_routes_issue_without_deterministic_keyword_match(tmp_path) -> None:
    db = Database(tmp_path / "routing.sqlite")
    gitlab = FakeGitLab()
    gitlab.source = replace(gitlab.source, title="Lỗi hiển thị vật phẩm", description="Không rõ component")
    policy = _policy("auto_move")
    target = policy.targets[0]
    candidate = RouteCandidate(
        target=target,
        score=91,
        confidence=0.91,
        paths=(),
        aliases=(),
        signals=("ownership context",),
    )
    classifier = FakeClassifier(RouteDecision(target=target, confidence=0.91, candidates=(candidate,), explicit=False))

    result = await route_issue(
        event=_event(),
        policy=policy,
        db=db,
        gitlab=gitlab,
        classifier=classifier,  # type: ignore[arg-type]
    )

    assert result.action is RouteAction.MOVED
    assert gitlab.moves == [(2080, 7, 357)]
    assert classifier.calls == [("Lỗi hiển thị vật phẩm", "Không rõ component", ())]


@pytest.mark.asyncio
async def test_explicit_route_override_skips_llm_classifier(tmp_path) -> None:
    db = Database(tmp_path / "routing.sqlite")
    gitlab = FakeGitLab()
    gitlab.source = replace(
        gitlab.source,
        title="Không có keyword",
        description="",
        labels=("route::protocol",),
    )
    classifier = FakeClassifier(RouteDecision(target=None, confidence=0.0, candidates=()))

    result = await route_issue(
        event=_event(),
        policy=_policy("auto_move"),
        db=db,
        gitlab=gitlab,
        classifier=classifier,  # type: ignore[arg-type]
    )

    assert result.action is RouteAction.MOVED
    assert classifier.calls == []
    decisions = db.list_routing_decisions("gitlab-zingplay:2080:issue:7")
    assert decisions[-1].explicit


@pytest.mark.asyncio
async def test_deterministic_target_survives_empty_llm_result(tmp_path) -> None:
    db = Database(tmp_path / "routing.sqlite")
    gitlab = FakeGitLab()
    gitlab.source = replace(
        gitlab.source,
        title="Protocol packet protobuf response field",
        description="",
    )
    classifier = FakeClassifier(RouteDecision(target=None, confidence=0.0, candidates=()))

    result = await route_issue(
        event=_event(),
        policy=_policy("auto_move"),
        db=db,
        gitlab=gitlab,
        classifier=classifier,  # type: ignore[arg-type]
    )

    assert result.action is RouteAction.MOVED
    assert classifier.calls == [("Protocol packet protobuf response field", "", ())]


def _candidate(policy: RoutingPolicy, key: str, confidence: float) -> RouteCandidate:
    target = next(target for target in policy.targets if target.key == key)
    return RouteCandidate(
        target=target,
        score=round(confidence * 100),
        confidence=confidence,
        paths=(),
        aliases=(),
        signals=("classifier",),
    )


@pytest.mark.asyncio
async def test_strong_deterministic_and_weak_contextual_target_routes_only_primary(tmp_path) -> None:
    db = Database(tmp_path / "routing.sqlite")
    gitlab = FakeGitLab()
    gitlab.source = replace(
        gitlab.source,
        title="Server backend API command fails",
        description="The backend command returns an error",
    )
    policy = _multi_policy()
    client = _candidate(policy, "client", 0.6)
    classifier = FakeClassifier(RouteDecision(target=None, confidence=0.6, candidates=(client,)))

    result = await route_issue(
        event=_event(),
        policy=policy,
        db=db,
        gitlab=gitlab,
        classifier=classifier,  # type: ignore[arg-type]
    )

    assert result.action is RouteAction.IMPLEMENTATION_QUEUED
    assert gitlab.moves == [(2080, 7, 356)]
    assert gitlab.children == {}


@pytest.mark.asyncio
async def test_multi_project_classification_creates_child_for_every_strong_target(tmp_path) -> None:
    db = Database(tmp_path / "routing.sqlite")
    gitlab = FakeGitLab()
    gitlab.source = replace(gitlab.source, title="Inventory API and UI are inconsistent", description="")
    policy = _multi_policy()
    client = _candidate(policy, "client", 0.95)
    server = _candidate(policy, "server", 0.93)
    classifier = FakeClassifier(
        RouteDecision(target=None, confidence=0.95, candidates=(client, server), explicit=False)
    )

    result = await route_issue(
        event=_event(),
        policy=policy,
        db=db,
        gitlab=gitlab,
        classifier=classifier,  # type: ignore[arg-type]
    )

    assert result.action is RouteAction.CHILDREN_QUEUED
    assert set(gitlab.children) == {332, 356}
    assert gitlab.children[332][0].labels == ("routed",)
    assert gitlab.children[356][0].labels == ("routed",)
    client_description = gitlab.children[332][0].description
    assert client_description.startswith(f"Routed from {gitlab.source.web_url}\n\n")
    assert "\n\n<!-- roboomp-child:" in client_description
    assert "\\n" not in client_description
    assert _event().item.key not in client_description
    children = db.list_routing_children(_event().item.key)
    assert [child.target_project_id for child in children] == ["332", "356"]
    client_event = db.get_event("route:delivery-1:332:20", instance_id="gitlab-zingplay")
    server_event = db.get_event("route:delivery-1:356:20", instance_id="gitlab-zingplay")
    assert client_event is not None and client_event.task_kind == "routing_complete"
    assert server_event is not None and server_event.task_kind == "triage_issue"
    server_payload = ForgeEvent.from_dict(server_event.payload)
    assert "roboomp" in server_payload.labels
    assert "ica/client#20" in gitlab.notes[-1][2]
    assert "ica/server#20" in gitlab.notes[-1][2]


@pytest.mark.asyncio
async def test_multi_project_retry_reuses_created_child_and_finishes_plan(tmp_path) -> None:
    db = Database(tmp_path / "routing.sqlite")
    gitlab = FakeGitLab()
    gitlab.source = replace(gitlab.source, title="Inventory API and UI are inconsistent", description="")
    policy = _multi_policy()
    client = _candidate(policy, "client", 0.95)
    server = _candidate(policy, "server", 0.95)
    classifier = FakeClassifier(
        RouteDecision(target=None, confidence=0.95, candidates=(client, server), explicit=False)
    )
    gitlab.fail_create_project_id = 356

    with pytest.raises(RuntimeError, match="injected create failure"):
        await route_issue(
            event=_event(),
            policy=policy,
            db=db,
            gitlab=gitlab,
            classifier=classifier,  # type: ignore[arg-type]
        )

    planned = db.list_routing_children(_event().item.key)
    assert [child.target_project_id for child in planned] == ["332", "356"]
    assert len(gitlab.children[332]) == 1
    gitlab.fail_create_project_id = None
    gitlab.projects[332] = ("ica/client", "changed-after-child")

    result = await route_issue(
        event=_event("delivery-2"),
        policy=policy,
        db=db,
        gitlab=gitlab,
        classifier=classifier,  # type: ignore[arg-type]
    )

    assert result.action is RouteAction.CHILDREN_QUEUED
    assert len(gitlab.children[332]) == 1
    assert len(gitlab.children[356]) == 1
    assert all(child.completed_at is not None for child in db.list_routing_children(_event().item.key))
    assert db.get_event("route:delivery-2:332:20", instance_id="gitlab-zingplay") is None
    recovered_event = db.get_event("route:delivery-2:356:20", instance_id="gitlab-zingplay")
    assert recovered_event is not None and recovered_event.task_kind == "triage_issue"
    repeated = await route_issue(
        event=_event("delivery-3"),
        policy=policy,
        db=db,
        gitlab=gitlab,
        classifier=classifier,  # type: ignore[arg-type]
    )
    assert repeated.action is RouteAction.CHILDREN_QUEUED
    child_notes = [body for _, _, body in gitlab.notes if "created linked child issues" in body]
    assert len(child_notes) == 1
    assert child_notes[0].index("ica/client#20") < child_notes[0].index("ica/server#20")
    assert len(classifier.calls) == 1


@pytest.mark.asyncio
async def test_multiple_explicit_route_labels_fan_out_without_classifier(tmp_path) -> None:
    db = Database(tmp_path / "routing.sqlite")
    gitlab = FakeGitLab()
    gitlab.source = replace(
        gitlab.source,
        title="Cross-project change",
        description="",
        labels=("route::client", "route::server"),
    )
    classifier = FakeClassifier(RouteDecision(target=None, confidence=0.0, candidates=()))

    result = await route_issue(
        event=_event(),
        policy=_multi_policy(),
        db=db,
        gitlab=gitlab,
        classifier=classifier,  # type: ignore[arg-type]
    )

    assert result.action is RouteAction.CHILDREN_QUEUED
    assert set(gitlab.children) == {332, 356}
    assert classifier.calls == []
