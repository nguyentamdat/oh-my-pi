"""Forge-neutral issue operations backed by one immutable GitLab project."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, TypeVar

from robomp.github_client import (
    CommentInfo,
    GitHubError,
    IssueInfo,
    IssueSummary,
    PullRequestFileInfo,
    PullRequestInfo,
    PullRequestReviewInfo,
    ReactionInfo,
    RepoInfo,
    ReviewCommentInfo,
)
from robomp.gitlab_backend import GitLabBackend
from robomp.gitlab_client import GitLabError, GitLabMergeRequestInfo

_T = TypeVar("_T")


class GitLabIssueBackend:
    """Adapt one allowlisted GitLab project to the existing issue-task surface.

    Unsupported review operations fail explicitly. The issue implementation path
    uses repository hydration, issue/thread reads, comments, labels, closing,
    and merge-request creation.
    """

    strict_closing_lookup = True

    def __init__(self, backend: GitLabBackend, *, project_id: int, repository: str) -> None:
        if project_id <= 0:
            raise ValueError("project_id must be positive")
        if not repository:
            raise ValueError("repository must be non-empty")
        self._backend = backend
        self._project_id = project_id
        self._repository = repository

    def _repo(self, repo: str) -> None:
        if repo != self._repository:
            raise GitHubError(403, "GitLab project is not allowlisted")

    async def _call(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        try:
            return await operation()
        except GitLabError as exc:
            raise GitHubError(exc.status, exc.message) from exc

    async def get_repo(self, full_name: str) -> RepoInfo:
        self._repo(full_name)
        project = await self._call(lambda: self._backend.get_project(self._project_id))
        if project.id != self._project_id or project.path_with_namespace != self._repository:
            raise GitHubError(409, "GitLab project identity changed")
        return RepoInfo(
            full_name=project.path_with_namespace,
            default_branch=project.default_branch,
            clone_url=project.http_url_to_repo,
            private=project.visibility != "public",
        )

    async def get_issue(self, repo: str, number: int) -> IssueInfo:
        self._repo(repo)
        issue = await self._call(lambda: self._backend.get_issue(self._project_id, number))
        return IssueInfo(
            repo=self._repository,
            number=issue.iid,
            title=issue.title,
            body=issue.description,
            state=issue.state,
            author=issue.author,
            labels=issue.labels,
            is_pull_request=False,
        )

    async def list_closing_pull_requests(self, repo: str, number: int) -> tuple[int, ...]:
        self._repo(repo)
        closing = await self._call(lambda: self._backend.list_issue_closed_by_merge_requests(self._project_id, number))
        return tuple(merge_request.iid for merge_request in closing)

    async def list_comments(self, repo: str, number: int) -> list[CommentInfo]:
        self._repo(repo)
        notes = await self._call(lambda: self._backend.list_issue_notes(self._project_id, number))
        return [
            CommentInfo(id=note.id, author=note.author, body=note.body, created_at=note.created_at)
            for note in notes
            if not note.system and not note.internal
        ]

    async def get_authenticated_login(self) -> str:
        return (await self._call(self._backend.get_authenticated_user)).username

    async def post_comment(self, repo: str, number: int, body: str) -> CommentInfo:
        self._repo(repo)
        user = await self._call(self._backend.get_authenticated_user)
        notes = await self._call(lambda: self._backend.list_issue_notes(self._project_id, number))
        for existing in reversed(notes):
            if (
                not existing.system
                and not existing.internal
                and existing.author.casefold() == user.username.casefold()
                and existing.body == body
            ):
                return CommentInfo(
                    id=existing.id,
                    author=existing.author,
                    body=existing.body,
                    created_at=existing.created_at,
                )
        note = await self._call(lambda: self._backend.post_issue_note(self._project_id, number, body))
        return CommentInfo(id=note.id, author=note.author, body=note.body, created_at=note.created_at)

    async def _related_open_change(
        self,
        *,
        issue_number: int,
        head: str,
        base: str,
    ) -> PullRequestInfo | None:
        related = await self._call(
            lambda: self._backend.list_issue_related_merge_requests(self._project_id, issue_number)
        )
        for mr in related:
            if mr.state.casefold() in {"open", "opened"} and mr.source_branch == head and mr.target_branch == base:
                return self._pull_request_info(mr)
        return None

    def _pull_request_info(self, mr: GitLabMergeRequestInfo) -> PullRequestInfo:
        return PullRequestInfo(
            repo=self._repository,
            number=mr.iid,
            html_url=mr.web_url,
            head_ref=mr.source_branch,
            base_ref=mr.target_branch,
            state=mr.state,
            author=mr.author,
            head_repo=self._repository,
            title=mr.title,
            body=mr.description,
        )

    async def open_pull_request(
        self,
        *,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> PullRequestInfo:
        self._repo(repo)
        match = re.search(r"(?i)\b(?:fixes|closes|resolves)\s+#(\d+)", body)
        issue_number = int(match.group(1)) if match else None
        if issue_number is not None:
            existing = await self._related_open_change(issue_number=issue_number, head=head, base=base)
            if existing is not None:
                return existing
        mr_title = f"Draft: {title}" if draft and not title.lower().startswith("draft:") else title
        try:
            mr = await self._call(
                lambda: self._backend.create_merge_request(
                    self._project_id,
                    source_branch=head,
                    target_branch=base,
                    title=mr_title,
                    description=body,
                )
            )
        except Exception:
            if issue_number is not None:
                recovered = await self._related_open_change(issue_number=issue_number, head=head, base=base)
                if recovered is not None:
                    return recovered
            raise
        return self._pull_request_info(mr)

    async def add_issue_labels(self, repo: str, number: int, labels: Sequence[str]) -> tuple[str, ...]:
        self._repo(repo)
        issue = await self._call(lambda: self._backend.add_issue_labels(self._project_id, number, list(labels)))
        return issue.labels

    async def remove_issue_label(self, repo: str, number: int, label: str) -> None:
        self._repo(repo)
        await self._call(lambda: self._backend.remove_issue_labels(self._project_id, number, [label]))

    async def close_issue(self, repo: str, number: int, *, reason: str = "completed") -> None:
        self._repo(repo)
        del reason
        await self._call(lambda: self._backend.close_issue(self._project_id, number))

    async def list_issues(self, repo: str, *, state: str = "open", limit: int = 30) -> list[IssueSummary]:
        self._repo(repo)
        del state, limit
        return []

    async def list_comment_reactions(self, repo: str, comment_id: int) -> list[ReactionInfo]:
        self._repo(repo)
        del comment_id
        return []

    async def get_pull_request(self, repo: str, number: int) -> PullRequestInfo:
        self._repo(repo)
        del number
        raise GitHubError(501, "GitLab merge-request hydration is not implemented")

    async def list_pr_files(self, repo: str, pr_number: int) -> list[PullRequestFileInfo]:
        self._repo(repo)
        del pr_number
        raise GitHubError(501, "GitLab merge-request files are not implemented")

    async def list_review_comments(self, repo: str, pr_number: int) -> list[ReviewCommentInfo]:
        self._repo(repo)
        del pr_number
        return []

    async def list_pr_reviews(self, repo: str, pr_number: int) -> list[PullRequestReviewInfo]:
        self._repo(repo)
        del pr_number
        return []

    async def request_reviewers(self, *, repo: str, pr_number: int, reviewers: Sequence[str]) -> None:
        self._repo(repo)
        del pr_number, reviewers
        raise GitHubError(501, "GitLab reviewer requests are not implemented")

    async def submit_pr_review(
        self,
        *,
        repo: str,
        pr_number: int,
        body: str,
        event: str = "COMMENT",
        comments: Sequence[Mapping[str, Any]] = (),
    ) -> PullRequestReviewInfo:
        self._repo(repo)
        del pr_number, body, event, comments
        raise GitHubError(501, "GitLab reviews are not implemented")

    async def add_assignees(self, repo: str, number: int, assignees: Sequence[str]) -> None:
        self._repo(repo)
        del number, assignees
        raise GitHubError(501, "GitLab assignee updates are not implemented")


__all__ = ["GitLabIssueBackend"]
