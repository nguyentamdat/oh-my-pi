"""Minimal typed GitLab v4 project client."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx


class GitLabError(RuntimeError):
    """Raised when GitLab returns a non-success response."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitLab {status}: {message}")
        self.status = status
        self.message = message


@dataclass(slots=True, frozen=True)
class GitLabMergeRequestInfo:
    project_id: int
    iid: int
    title: str
    description: str
    state: str
    author: str
    source_branch: str
    target_branch: str
    web_url: str


@dataclass(slots=True, frozen=True)
class GitLabUserInfo:
    id: int
    username: str
    name: str


@dataclass(slots=True, frozen=True)
class GitLabProjectInfo:
    id: int
    path_with_namespace: str
    default_branch: str
    http_url_to_repo: str
    visibility: str
    web_url: str


@dataclass(slots=True, frozen=True)
class GitLabIssueInfo:
    project_id: int
    iid: int
    title: str
    description: str
    state: str
    author: str
    labels: tuple[str, ...]
    web_url: str
    id: int = 0
    moved_to_id: int | None = None


@dataclass(slots=True, frozen=True)
class GitLabNoteInfo:
    id: int
    author: str
    body: str
    created_at: str
    system: bool
    internal: bool


class GitLabClient:
    """Configured HTTPS client for an allowlisted set of GitLab projects."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        allowed_project_ids: frozenset[int],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_url = _api_url(base_url)
        self._allowed_project_ids = allowed_project_ids
        self._transport = transport
        self._headers = {"PRIVATE-TOKEN": token, "Accept": "application/json", "User-Agent": "robomp/0.1"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._api_url,
            headers=self._headers,
            transport=self._transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        async with self._client() as client:
            response = await client.request(method, path, json=json, params=params)
        if response.status_code >= 300:
            try:
                message = str(response.json().get("message") or response.text)
            except Exception:
                message = response.text
            raise GitLabError(response.status_code, message)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def get_project(self, project_id: int) -> GitLabProjectInfo:
        data = await self.request("GET", self._project_path(project_id))
        project = _project_from_payload(data)
        if project.id != project_id:
            raise ValueError("GitLab project response did not match requested project")
        return project

    async def get_issue(self, project_id: int, iid: int) -> GitLabIssueInfo:
        data = await self.request("GET", f"{self._project_path(project_id)}/issues/{_iid(iid)}")
        return _issue_from_payload(project_id, data)

    async def list_issue_related_merge_requests(
        self,
        project_id: int,
        iid: int,
    ) -> list[GitLabMergeRequestInfo]:
        return await self._list_issue_merge_requests(project_id, iid, "related_merge_requests")

    async def list_issue_closed_by_merge_requests(
        self,
        project_id: int,
        iid: int,
    ) -> list[GitLabMergeRequestInfo]:
        return await self._list_issue_merge_requests(project_id, iid, "closed_by")

    async def _list_issue_merge_requests(
        self,
        project_id: int,
        iid: int,
        endpoint: str,
    ) -> list[GitLabMergeRequestInfo]:
        path = f"{self._project_path(project_id)}/issues/{_iid(iid)}/{endpoint}"
        merge_requests: list[GitLabMergeRequestInfo] = []
        page = 1
        while True:
            data = await self.request("GET", path, params={"per_page": 100, "page": page})
            if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
                raise ValueError("GitLab issue merge requests response must be a list of objects")
            merge_requests.extend(_merge_request_from_payload(project_id, merge_request) for merge_request in data)
            if len(data) < 100:
                return merge_requests
            page += 1

    async def list_issue_notes(self, project_id: int, iid: int) -> list[GitLabNoteInfo]:
        path = f"{self._project_path(project_id)}/issues/{_iid(iid)}/notes"
        notes: list[GitLabNoteInfo] = []
        page = 1
        while True:
            data = await self.request("GET", path, params={"per_page": 100, "page": page})
            if not isinstance(data, list):
                raise ValueError("GitLab issue notes response must be a list")
            notes.extend(_note_from_payload(note) for note in data)
            if len(data) < 100:
                return notes
            page += 1

    async def get_authenticated_user(self, project_id: int | None = None) -> GitLabUserInfo:
        if project_id is not None:
            self._project_path(project_id)
        data = await self.request("GET", "user")
        return _user_from_payload(data)

    async def post_issue_note(self, project_id: int, iid: int, body: str) -> GitLabNoteInfo:
        data = await self.request(
            "POST",
            f"{self._project_path(project_id)}/issues/{_iid(iid)}/notes",
            json={"body": body},
        )
        return _note_from_payload(data)

    async def update_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo:
        """Replace the issue's labels; callers must provide the complete set."""
        data = await self.request(
            "PUT",
            f"{self._project_path(project_id)}/issues/{_iid(iid)}",
            json={"labels": ",".join(labels)},
        )
        return _issue_from_payload(project_id, data)

    async def add_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo:
        """Atomically add labels without replacing concurrently changed labels."""
        data = await self.request(
            "PUT",
            f"{self._project_path(project_id)}/issues/{_iid(iid)}",
            json={"add_labels": ",".join(labels)},
        )
        return _issue_from_payload(project_id, data)

    async def remove_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo:
        """Atomically remove labels without replacing concurrent changes."""
        data = await self.request(
            "PUT",
            f"{self._project_path(project_id)}/issues/{_iid(iid)}",
            json={"remove_labels": ",".join(labels)},
        )
        return _issue_from_payload(project_id, data)

    async def close_issue(self, project_id: int, iid: int) -> GitLabIssueInfo:
        data = await self.request(
            "PUT",
            f"{self._project_path(project_id)}/issues/{_iid(iid)}",
            json={"state_event": "close"},
        )
        return _issue_from_payload(project_id, data)

    async def move_issue(self, project_id: int, iid: int, to_project_id: int) -> GitLabIssueInfo:
        """Move an issue between two configured GitLab projects."""
        source_path = self._project_path(project_id)
        self._project_path(to_project_id)
        data = await self.request(
            "POST",
            f"{source_path}/issues/{_iid(iid)}/move",
            json={"to_project_id": to_project_id},
        )
        return _issue_from_payload(to_project_id, data)

    async def resolve_moved_issue(
        self,
        source_project_id: int,
        iid: int,
        expected_target_project_id: int,
    ) -> GitLabIssueInfo | None:
        """Resolve a completed move without retrying its non-idempotent POST."""
        self._project_path(expected_target_project_id)
        source_issue = await self.get_issue(source_project_id, iid)
        if source_issue.moved_to_id is None:
            return None
        data = await self.request("GET", f"issues/{source_issue.moved_to_id}")
        if not isinstance(data, Mapping):
            raise ValueError("GitLab moved issue response must be an object")
        returned_project_id = data.get("project_id")
        if not _project_id(returned_project_id):
            raise ValueError("GitLab moved issue response has an invalid project_id")
        if returned_project_id != expected_target_project_id:
            raise ValueError("GitLab moved issue response did not match expected target project")
        return _issue_from_payload(returned_project_id, data)

    async def create_merge_request(
        self,
        project_id: int,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> GitLabMergeRequestInfo:
        data = await self.request(
            "POST",
            f"{self._project_path(project_id)}/merge_requests",
            json={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
            },
        )
        return _merge_request_from_payload(project_id, data)

    def _project_path(self, project_id: int) -> str:
        if not _project_id(project_id) or project_id not in self._allowed_project_ids:
            raise ValueError("project is not on allowlist")
        return f"projects/{project_id}"


def _api_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("GitLab base URL must use HTTPS")
    root = base_url.rstrip("/")
    return root if root.endswith("/api/v4") else f"{root}/api/v4"


def _project_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _iid(value: object) -> int:
    if not _project_id(value):
        raise ValueError("IID must be a positive integer")
    return value


def _project_from_payload(data: Mapping[str, Any]) -> GitLabProjectInfo:
    return GitLabProjectInfo(
        id=int(data["id"]),
        path_with_namespace=str(data.get("path_with_namespace") or ""),
        default_branch=str(data.get("default_branch") or ""),
        http_url_to_repo=str(data.get("http_url_to_repo") or ""),
        visibility=str(data.get("visibility") or ""),
        web_url=str(data.get("web_url") or ""),
    )


def _user_from_payload(data: Mapping[str, Any]) -> GitLabUserInfo:
    return GitLabUserInfo(
        id=int(data["id"]),
        username=str(data.get("username") or ""),
        name=str(data.get("name") or ""),
    )


def _merge_request_from_payload(project_id: int, data: Mapping[str, Any]) -> GitLabMergeRequestInfo:
    author = data.get("author") or {}
    return GitLabMergeRequestInfo(
        project_id=project_id,
        iid=int(data["iid"]),
        title=str(data.get("title") or ""),
        description=str(data.get("description") or ""),
        state=str(data.get("state") or ""),
        author=str(author.get("username") or "") if isinstance(author, Mapping) else "",
        source_branch=str(data.get("source_branch") or ""),
        target_branch=str(data.get("target_branch") or ""),
        web_url=str(data.get("web_url") or ""),
    )


def _issue_from_payload(project_id: int, data: Mapping[str, Any]) -> GitLabIssueInfo:
    author = data.get("author") or {}
    return GitLabIssueInfo(
        project_id=project_id,
        iid=int(data["iid"]),
        title=str(data.get("title") or ""),
        description=str(data.get("description") or ""),
        state=str(data.get("state") or ""),
        author=str(author.get("username") or "") if isinstance(author, Mapping) else "",
        labels=tuple(str(label) for label in (data.get("labels") or [])),
        web_url=str(data.get("web_url") or ""),
        id=0 if data.get("id") is None else _iid(data["id"]),
        moved_to_id=None if data.get("moved_to_id") is None else _iid(data["moved_to_id"]),
    )


def _note_from_payload(data: Mapping[str, Any]) -> GitLabNoteInfo:
    author = data.get("author") or {}
    return GitLabNoteInfo(
        id=int(data["id"]),
        author=str(author.get("username") or "") if isinstance(author, Mapping) else "",
        body=str(data.get("body") or ""),
        created_at=str(data.get("created_at") or ""),
        system=bool(data.get("system")),
        internal=bool(data.get("internal")),
    )


__all__ = [
    "GitLabClient",
    "GitLabError",
    "GitLabIssueInfo",
    "GitLabMergeRequestInfo",
    "GitLabNoteInfo",
    "GitLabProjectInfo",
    "GitLabUserInfo",
]
