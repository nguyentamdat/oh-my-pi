"""Canonical GitLab event → queue → task → observable GitLab writes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from robomp import tasks
from robomp.config import Settings
from robomp.db import Database, EventRow
from robomp.forge import Actor, ForgeEvent, RepositoryRef, WorkItemRef
from robomp.git_ops import PushResult
from robomp.gitlab_client import (
    GitLabIssueInfo,
    GitLabMergeRequestInfo,
    GitLabNoteInfo,
    GitLabProjectInfo,
    GitLabUserInfo,
)
from robomp.gitlab_compat import GitLabIssueBackend
from robomp.queue import ForgeRuntime, WorkerPool
from robomp.sandbox import Workspace
from robomp.slot_pool import SlotPool


class _GitLabBackend:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.notes: list[str] = []
        self.merge_requests: list[tuple[str, str]] = []

    async def get_project(self, project_id: int) -> GitLabProjectInfo:
        return GitLabProjectInfo(
            id=project_id,
            path_with_namespace="ica/server",
            default_branch="main",
            http_url_to_repo="https://gitlab.zingplay.com/ica/server.git",
            visibility="private",
            web_url="https://gitlab.zingplay.com/ica/server",
        )

    async def get_issue(self, project_id: int, iid: int) -> GitLabIssueInfo:
        return GitLabIssueInfo(
            project_id=project_id,
            iid=iid,
            title="Crash on start",
            description="Steps",
            state="opened",
            author="alice",
            labels=("roboomp",),
            web_url=f"https://gitlab.zingplay.com/ica/server/-/issues/{iid}",
        )

    async def list_issue_notes(self, project_id: int, iid: int) -> list[GitLabNoteInfo]:
        del project_id, iid
        return []

    async def get_authenticated_user(self) -> GitLabUserInfo:
        return GitLabUserInfo(id=99, username="roboomp", name="RoboOMP")

    async def list_issue_related_merge_requests(
        self,
        project_id: int,
        iid: int,
    ) -> list[GitLabMergeRequestInfo]:
        del project_id, iid
        return []

    async def list_issue_closed_by_merge_requests(
        self,
        project_id: int,
        iid: int,
    ) -> list[GitLabMergeRequestInfo]:
        del project_id, iid
        return []

    async def post_issue_note(self, project_id: int, iid: int, body: str) -> GitLabNoteInfo:
        del project_id, iid
        self.notes.append(body)
        self.events.append("note")
        return GitLabNoteInfo(id=1, author="roboomp", body=body, created_at="now", system=False, internal=False)

    async def create_merge_request(
        self,
        project_id: int,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> GitLabMergeRequestInfo:
        del project_id, description
        self.merge_requests.append((source_branch, target_branch))
        self.events.append("merge_request")
        return GitLabMergeRequestInfo(
            project_id=356,
            iid=7,
            title=title,
            description="Fixes #42",
            state="opened",
            author="roboomp",
            source_branch=source_branch,
            target_branch=target_branch,
            web_url="https://gitlab.zingplay.com/ica/server/-/merge_requests/7",
        )

    async def update_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo:
        del labels
        return await self.get_issue(project_id, iid)

    async def add_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo:
        del labels
        return await self.get_issue(project_id, iid)

    async def remove_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo:
        del labels
        return await self.get_issue(project_id, iid)

    async def close_issue(self, project_id: int, iid: int) -> GitLabIssueInfo:
        return await self.get_issue(project_id, iid)


class _Sandbox:
    natives_cache = None

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[dict[str, object]] = []

    def ensure_workspace(self, **kwargs: object) -> Workspace:
        self.calls.append(kwargs)
        root = self.root / "gitlab-zingplay__356__issue__42"
        session_dir = root / "session"
        session_dir.mkdir(parents=True, exist_ok=True)
        return Workspace(
            root=root,
            repo_dir=root / "repo",
            session_dir=session_dir,
            context_dir=root / "context",
            artifacts_dir=root / "artifacts",
            branch="farm/fix-42",
            repo_full_name="ica/server",
            issue_number=42,
            instance_id="gitlab-zingplay",
            repository_id="356",
            item_kind="issue",
        )


class _Transport:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.workspace_keys: list[str] = []

    def push_branch(
        self,
        *,
        repo: str,
        workspace_key: str,
        repo_dir: Path,
        branch: str,
        expected_head: str | None = None,
        slot_uid: int | None = None,
    ) -> PushResult:
        del repo, repo_dir, expected_head, slot_uid
        self.events.append("push")
        self.workspace_keys.append(workspace_key)
        return PushResult(head="abc123", branch=branch)


@pytest.mark.asyncio
async def test_gitlab_event_runs_canonical_task_and_writes_note_and_merge_request(
    settings: Settings,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gitlab_settings = settings.model_copy(update={"gitlab_bot_login": "gitlab-robo", "git_author_name": "GitLab Robo"})
    events: list[str] = []
    native = _GitLabBackend(events)
    backend = GitLabIssueBackend(native, project_id=356, repository="ica/server")
    sandbox = _Sandbox(tmp_path)
    transport = _Transport(events)
    event = ForgeEvent(
        delivery_id="gitlab-event-42",
        source_event="Issue Hook",
        event="issue.opened",
        task_kind="triage_issue",
        item=WorkItemRef(
            repository=RepositoryRef(
                instance_id="gitlab-zingplay",
                remote_id="356",
                full_name="ica/server",
            ),
            kind="issue",
            number=42,
        ),
        actor=Actor(remote_id="7", login="alice"),
        labels=("roboomp",),
    )
    row = EventRow(
        delivery_id=event.delivery_id,
        event_type=event.source_event,
        repo="ica/server",
        issue_key=event.item.key,
        payload=event.to_dict(),
        received_at="2026-01-01T00:00:00Z",
        state="running",
        attempts=1,
        last_error=None,
        instance_id="gitlab-zingplay",
        repository_id="356",
        item_kind="issue",
        item_number=42,
        canonical_key=event.item.key,
        canonical_event=event.event,
        task_kind=event.task_kind,
    )

    async def fake_run_task(*, inputs, **_kwargs) -> None:
        assert inputs.bot_login == "gitlab-robo"
        assert inputs.author_name == "GitLab Robo"
        assert inputs.instance_id == "gitlab-zingplay"
        inputs.git_transport.push_branch(
            repo=inputs.repo.full_name,
            workspace_key=inputs.workspace.workspace_key,
            repo_dir=inputs.workspace.repo_dir,
            branch=inputs.workspace.branch,
        )
        await inputs.github.open_pull_request(
            repo=inputs.repo.full_name,
            head=inputs.workspace.branch,
            base=inputs.repo.default_branch,
            title="Fix crash",
            body="## Repro\n...\n## Cause\n...\n## Fix\n...\n## Verification\nFixes #42",
        )
        await inputs.github.post_comment(inputs.repo.full_name, inputs.issue.number, "analysis complete")

    monkeypatch.setattr(tasks, "run_task", fake_run_task)
    runtime = ForgeRuntime(
        backend=backend,  # type: ignore[arg-type]
        sandbox=sandbox,  # type: ignore[arg-type]
        git_transport=transport,  # type: ignore[arg-type]
    )
    pool = WorkerPool(
        settings=gitlab_settings,
        db=db,
        github=SimpleNamespace(),  # type: ignore[arg-type]
        sandbox=SimpleNamespace(natives_cache=None),  # type: ignore[arg-type]
        git_transport=SimpleNamespace(),  # type: ignore[arg-type]
        slot_pool=SlotPool(),
        forge_runtime_factories={"gitlab-zingplay": lambda _row: runtime},
    )

    await pool._dispatch(row)  # noqa: SLF001

    assert native.notes == ["analysis complete"]
    assert native.merge_requests == [("farm/fix-42", "main")]
    assert events == ["push", "merge_request", "note"]
    assert transport.workspace_keys == ["gitlab-zingplay__356__issue__42"]
    assert sandbox.calls == [
        {
            "repo": "ica/server",
            "number": 42,
            "title": "Crash on start",
            "clone_url": "https://gitlab.zingplay.com/ica/server.git",
            "default_branch": "main",
            "author_name": "GitLab Robo",
            "author_email": gitlab_settings.git_author_email,
            "slot_uid": None,
            "instance_id": "gitlab-zingplay",
            "repository_id": "356",
            "item_kind": "issue",
        }
    ]
    issue = db.get_issue(event.item.key)
    assert issue is not None
    assert issue.session_dir == str(tmp_path / "gitlab-zingplay__356__issue__42" / "session")
