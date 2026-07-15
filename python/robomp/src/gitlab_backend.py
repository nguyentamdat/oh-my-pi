"""Structural backend contract for the first GitLab project slice."""

from __future__ import annotations

from typing import Protocol

from robomp.gitlab_client import (
    GitLabIssueInfo,
    GitLabMergeRequestInfo,
    GitLabNoteInfo,
    GitLabProjectInfo,
    GitLabUserInfo,
)


class GitLabBackend(Protocol):
    """GitLab operations available to later forge-neutral integrations."""

    async def get_project(self, project_id: int) -> GitLabProjectInfo: ...

    async def get_issue(self, project_id: int, iid: int) -> GitLabIssueInfo: ...

    async def create_issue(
        self,
        project_id: int,
        *,
        title: str,
        description: str,
        labels: list[str] | None = None,
    ) -> GitLabIssueInfo: ...

    async def find_issue_by_marker(self, project_id: int, marker: str) -> GitLabIssueInfo | None: ...

    async def list_issue_related_merge_requests(
        self,
        project_id: int,
        iid: int,
    ) -> list[GitLabMergeRequestInfo]: ...

    async def list_issue_closed_by_merge_requests(
        self,
        project_id: int,
        iid: int,
    ) -> list[GitLabMergeRequestInfo]: ...

    async def list_issue_notes(self, project_id: int, iid: int) -> list[GitLabNoteInfo]: ...

    async def get_authenticated_user(self, project_id: int | None = None) -> GitLabUserInfo: ...

    async def post_issue_note(self, project_id: int, iid: int, body: str) -> GitLabNoteInfo: ...

    async def update_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo: ...

    async def add_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo: ...
    async def remove_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo: ...

    async def close_issue(self, project_id: int, iid: int) -> GitLabIssueInfo: ...

    async def move_issue(self, project_id: int, iid: int, to_project_id: int) -> GitLabIssueInfo: ...

    async def resolve_moved_issue(
        self,
        source_project_id: int,
        iid: int,
        expected_target_project_id: int,
    ) -> GitLabIssueInfo | None: ...

    async def create_merge_request(
        self,
        project_id: int,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> GitLabMergeRequestInfo: ...


__all__ = ["GitLabBackend"]
