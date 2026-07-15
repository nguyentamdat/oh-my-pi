"""Focused GitLab compatibility tests for duplicate merge-request avoidance."""

from __future__ import annotations

import pytest

from robomp.gitlab_client import GitLabMergeRequestInfo, GitLabNoteInfo, GitLabUserInfo
from robomp.gitlab_compat import GitLabIssueBackend


class _RelatedMergeRequestBackend:
    async def list_issue_related_merge_requests(
        self,
        project_id: int,
        iid: int,
    ) -> list[GitLabMergeRequestInfo]:
        assert (project_id, iid) == (356, 42)
        return [
            GitLabMergeRequestInfo(
                project_id=356,
                iid=7,
                title="Open fix",
                description="",
                state="opened",
                author="robomp",
                source_branch="farm/fix",
                target_branch="main",
                web_url="https://gitlab.example.test/ica/server/-/merge_requests/7",
            ),
            GitLabMergeRequestInfo(
                project_id=356,
                iid=8,
                title="Legacy fix",
                description="",
                state="merged",
                author="robomp",
                source_branch="farm/old",
                target_branch="main",
                web_url="https://gitlab.example.test/ica/server/-/merge_requests/8",
            ),
            GitLabMergeRequestInfo(
                project_id=356,
                iid=9,
                title="Alternate open spelling",
                description="",
                state="open",
                author="robomp",
                source_branch="farm/alternate",
                target_branch="main",
                web_url="https://gitlab.example.test/ica/server/-/merge_requests/9",
            ),
        ]

    async def list_issue_closed_by_merge_requests(
        self,
        project_id: int,
        iid: int,
    ) -> list[GitLabMergeRequestInfo]:
        assert (project_id, iid) == (356, 42)
        return [
            GitLabMergeRequestInfo(
                project_id=356,
                iid=10,
                title="Actual closing fix",
                description="Closes #42",
                state="opened",
                author="robomp",
                source_branch="farm/closing",
                target_branch="main",
                web_url="https://gitlab.example.test/ica/server/-/merge_requests/10",
            )
        ]


class _AmbiguousCreateBackend:
    def __init__(self) -> None:
        self.created = 0

    async def list_issue_related_merge_requests(
        self,
        project_id: int,
        iid: int,
    ) -> list[GitLabMergeRequestInfo]:
        assert (project_id, iid) == (356, 42)
        if self.created == 0:
            return []
        return [
            GitLabMergeRequestInfo(
                project_id=356,
                iid=10,
                title="Fix crash",
                description="Fixes #42",
                state="opened",
                author="roboomp",
                source_branch="farm/fix",
                target_branch="main",
                web_url="https://gitlab.example.test/ica/server/-/merge_requests/10",
            )
        ]

    async def create_merge_request(self, *_args, **_kwargs) -> GitLabMergeRequestInfo:
        self.created += 1
        raise TimeoutError("response lost")


class _ProjectScopedIdentityBackend:
    def __init__(self) -> None:
        self.identity_project_ids: list[int | None] = []
        self.post_calls = 0

    async def get_authenticated_user(self, project_id: int | None = None) -> GitLabUserInfo:
        self.identity_project_ids.append(project_id)
        return GitLabUserInfo(id=1, username="robomp", name="RoboMP")

    async def list_issue_notes(self, project_id: int, iid: int) -> list[GitLabNoteInfo]:
        assert (project_id, iid) == (356, 42)
        return [
            GitLabNoteInfo(
                id=100,
                author="robomp",
                body="Already posted",
                created_at="2026-01-01T00:00:00Z",
                system=False,
                internal=False,
            )
        ]

    async def post_issue_note(self, *_args, **_kwargs) -> GitLabNoteInfo:
        self.post_calls += 1
        raise AssertionError("duplicate note should not be posted")


@pytest.mark.asyncio
async def test_closing_pull_requests_excludes_reference_only_related_merge_requests() -> None:
    backend = GitLabIssueBackend(
        _RelatedMergeRequestBackend(),  # type: ignore[arg-type]
        project_id=356,
        repository="ica/server",
    )

    assert await backend.list_closing_pull_requests("ica/server", 42) == (10,)


@pytest.mark.asyncio
async def test_duplicate_comment_retry_uses_project_scoped_identity() -> None:
    native = _ProjectScopedIdentityBackend()
    backend = GitLabIssueBackend(
        native,  # type: ignore[arg-type]
        project_id=356,
        repository="ica/server",
    )

    comment = await backend.post_comment("ica/server", 42, "Already posted")

    assert comment.id == 100
    assert native.identity_project_ids == [356]
    assert native.post_calls == 0


@pytest.mark.asyncio
async def test_open_change_recovers_after_ambiguous_create_without_duplicate() -> None:
    native = _AmbiguousCreateBackend()
    backend = GitLabIssueBackend(
        native,  # type: ignore[arg-type]
        project_id=356,
        repository="ica/server",
    )

    change = await backend.open_pull_request(
        repo="ica/server",
        head="farm/fix",
        base="main",
        title="Fix crash",
        body="Fixes #42",
    )

    assert change.number == 10
    assert native.created == 1
