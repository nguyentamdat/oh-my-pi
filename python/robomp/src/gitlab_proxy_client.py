"""HMAC-only GitLab client and git transport for the credential proxy.

These orchestrator-side classes deliberately accept only the proxy URL and
shared HMAC key. They contain no GitLab PAT and expose only the proxy's typed
project routes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from robomp.git_ops import GitCommandError, HeadDriftError, PushResult
from robomp.gitlab_client import (
    GitLabError,
    GitLabIssueInfo,
    GitLabMergeRequestInfo,
    GitLabNoteInfo,
    GitLabProjectInfo,
    GitLabUserInfo,
)
from robomp.proxy_hmac import HEADER_SIGNATURE, HEADER_TIMESTAMP, sign

log = logging.getLogger(__name__)


def _project_id(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("project_id must be a positive integer")
    return value


def _iid(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("iid must be a positive integer")
    return value


def _signed_headers(method: str, target: str, body: bytes, key: bytes) -> dict[str, str]:
    timestamp, signature = sign(method=method, path=target, body=body, key=key)
    return {HEADER_TIMESTAMP: timestamp, HEADER_SIGNATURE: signature}


def _decode_error(response: httpx.Response) -> Exception:
    """Restore typed proxy failures without reflecting arbitrary response text."""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = payload["error"]
        kind = error.get("kind")
        if kind == "gitlab":
            return GitLabError(
                int(error.get("status") or response.status_code),
                str(error.get("message") or "GitLab proxy error"),
            )
        if kind in ("git", "head_drift"):
            command = error.get("cmd") or ["git"]
            klass = HeadDriftError if kind == "head_drift" else GitCommandError
            return klass(
                list(command),
                int(error.get("returncode") or 1),
                str(error.get("stdout") or ""),
                str(error.get("stderr") or ""),
            )
    return GitLabError(response.status_code, "GitLab proxy request failed")


class GitLabProxyClient:
    """Typed, HMAC-authenticated GitLab API facade with no PAT field."""

    _TRANSIENT_RETRY_DELAYS = (1.0, 3.0, 10.0)

    def __init__(
        self,
        *,
        base_url: str,
        hmac_key: str | bytes,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._key = hmac_key.encode("utf-8") if isinstance(hmac_key, str) else hmac_key
        self._transport = transport
        self._timeout = httpx.Timeout(timeout, connect=10.0)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,  # type: ignore[arg-type]
            timeout=self._timeout,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
        last_error: Exception | None = None
        retry_delays = (*self._TRANSIENT_RETRY_DELAYS, None) if method.upper() in {"GET", "HEAD"} else (None,)
        for attempt, delay in enumerate(retry_delays):
            try:
                async with self._client() as client:
                    request = client.build_request(
                        method,
                        path,
                        params=params,
                        content=body if json_body is not None else None,
                    )
                    target = request.url.path
                    if request.url.query:
                        target = f"{target}?{request.url.query.decode('ascii')}"
                    request.headers.update(_signed_headers(method, target, body, self._key))
                    if json_body is not None:
                        request.headers["Content-Type"] = "application/json"
                    response = await client.send(request)
                if response.status_code >= 400:
                    raise _decode_error(response)
                if response.status_code == 204 or not response.content:
                    return None
                return response.json()
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_error = exc
                if delay is None:
                    break
                log.warning(
                    "GitLab proxy client transient error, retrying",
                    extra={"method": method, "path": path, "attempt": attempt + 1, "delay": delay},
                )
                await asyncio.sleep(delay)
        raise last_error  # type: ignore[misc]

    async def get_authenticated_user(self) -> GitLabUserInfo:
        return _user_from(await self._request("GET", "/gl/v1/authenticated_user"))

    async def get_project(self, project_id: int) -> GitLabProjectInfo:
        project_id = _project_id(project_id)
        return _project_from(await self._request("GET", f"/gl/v1/projects/{project_id}"))

    async def get_issue(self, project_id: int, iid: int) -> GitLabIssueInfo:
        project_id = _project_id(project_id)
        iid = _iid(iid)
        return _issue_from(await self._request("GET", f"/gl/v1/projects/{project_id}/issues/{iid}"))

    async def list_issue_related_merge_requests(
        self,
        project_id: int,
        iid: int,
    ) -> list[GitLabMergeRequestInfo]:
        project_id = _project_id(project_id)
        iid = _iid(iid)
        data = await self._request(
            "GET",
            f"/gl/v1/projects/{project_id}/issues/{iid}/related_merge_requests",
        )
        return [_merge_request_from(item) for item in _response_items(data)]

    async def list_issue_closed_by_merge_requests(
        self,
        project_id: int,
        iid: int,
    ) -> list[GitLabMergeRequestInfo]:
        project_id = _project_id(project_id)
        iid = _iid(iid)
        data = await self._request(
            "GET",
            f"/gl/v1/projects/{project_id}/issues/{iid}/closed_by",
        )
        return [_merge_request_from(item) for item in _response_items(data)]

    async def list_issue_notes(self, project_id: int, iid: int) -> list[GitLabNoteInfo]:
        project_id = _project_id(project_id)
        iid = _iid(iid)
        data = await self._request("GET", f"/gl/v1/projects/{project_id}/issues/{iid}/notes")
        return [_note_from(item) for item in _response_items(data)]

    async def post_issue_note(self, project_id: int, iid: int, body: str) -> GitLabNoteInfo:
        project_id = _project_id(project_id)
        iid = _iid(iid)
        if not isinstance(body, str) or not body:
            raise ValueError("body must be a non-empty string")
        return _note_from(
            await self._request(
                "POST",
                f"/gl/v1/projects/{project_id}/issues/{iid}/notes",
                json_body={"body": body},
            )
        )

    async def update_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo:
        project_id = _project_id(project_id)
        iid = _iid(iid)
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise ValueError("labels must be a list of strings")
        return _issue_from(
            await self._request(
                "PUT",
                f"/gl/v1/projects/{project_id}/issues/{iid}/labels",
                json_body={"labels": labels},
            )
        )

    async def add_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo:
        project_id = _project_id(project_id)
        iid = _iid(iid)
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise ValueError("labels must be a list of strings")
        return _issue_from(
            await self._request(
                "POST",
                f"/gl/v1/projects/{project_id}/issues/{iid}/labels/add",
                json_body={"labels": labels},
            )
        )

    async def remove_issue_labels(self, project_id: int, iid: int, labels: list[str]) -> GitLabIssueInfo:
        project_id = _project_id(project_id)
        iid = _iid(iid)
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise ValueError("labels must be a list of strings")
        return _issue_from(
            await self._request(
                "POST",
                f"/gl/v1/projects/{project_id}/issues/{iid}/labels/remove",
                json_body={"labels": labels},
            )
        )

    async def close_issue(self, project_id: int, iid: int) -> GitLabIssueInfo:
        project_id = _project_id(project_id)
        iid = _iid(iid)
        return _issue_from(
            await self._request(
                "POST",
                f"/gl/v1/projects/{project_id}/issues/{iid}/close",
                json_body={},
            )
        )

    async def create_merge_request(
        self,
        project_id: int,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> GitLabMergeRequestInfo:
        project_id = _project_id(project_id)
        fields = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
        }
        if not all(isinstance(value, str) and value for value in fields.values()):
            raise ValueError("merge request fields must be non-empty strings")
        return _merge_request_from(
            await self._request(
                "POST",
                f"/gl/v1/projects/{project_id}/merge_requests",
                json_body=fields,
            )
        )


class GitLabProxyGitTransport:
    """SandboxManager-compatible transport bound to one immutable project ID.

    The standard ``GitTransport`` API carries GitHub-style repository metadata.
    This adapter accepts that call shape so it can be injected into
    ``SandboxManager``, but never forwards its ``repo``, ``clone_url``, or
    ``default_branch`` values. The proxy resolves all GitLab destinations from
    its configured instance plus the trusted API project response.
    """

    _TRANSIENT_RETRY_DELAYS = (2.0, 5.0, 15.0)

    def __init__(
        self,
        *,
        project_id: int,
        base_url: str,
        hmac_key: str | bytes,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._project_id = _project_id(project_id)
        self._base_url = base_url.rstrip("/")
        self._key = hmac_key.encode("utf-8") if isinstance(hmac_key, str) else hmac_key
        self._transport = transport
        self._timeout = httpx.Timeout(timeout, connect=10.0)

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self._base_url, transport=self._transport, timeout=self._timeout)

    def _post(self, path: str, body: Mapping[str, Any], *, retry: bool = True) -> Mapping[str, Any]:
        raw_body = json.dumps(body).encode("utf-8")
        last_error: Exception | None = None
        retry_delays = (*self._TRANSIENT_RETRY_DELAYS, None) if retry else (None,)
        for attempt, delay in enumerate(retry_delays):
            try:
                headers = _signed_headers("POST", path, raw_body, self._key)
                headers["Content-Type"] = "application/json"
                with self._client() as client:
                    response = client.post(path, content=raw_body, headers=headers)
                if response.status_code >= 400:
                    raise _decode_error(response)
                if response.status_code == 204 or not response.content:
                    return {}
                data = response.json()
                return data if isinstance(data, dict) else {}
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_error = exc
                if delay is None:
                    break
                log.warning(
                    "GitLab proxy transport transient error, retrying",
                    extra={"path": path, "attempt": attempt + 1, "delay": delay},
                )
                time.sleep(delay)
        raise last_error  # type: ignore[misc]

    def clone_pool(self, *, repo: str, clone_url: str, default_branch: str, target: Path) -> None:
        del repo, clone_url, default_branch, target
        self._post(f"/gl/v1/projects/{self._project_id}/git/clone", {}, retry=False)

    def fetch_pool(self, *, repo: str, pool_dir: Path) -> None:
        del repo, pool_dir
        self._post(f"/gl/v1/projects/{self._project_id}/git/fetch", {})

    def fetch_base_ref(self, *, repo: str, pool_dir: Path, ref: str) -> None:
        del repo, pool_dir
        if not isinstance(ref, str) or not ref:
            raise ValueError("ref must be a non-empty string")
        self._post(f"/gl/v1/projects/{self._project_id}/git/fetch_ref", {"ref": ref})

    def fetch_pr_head(self, *, repo: str, pool_dir: Path, pr_number: int) -> None:
        """Fetch a GitLab merge request head through the typed base-ref route."""
        if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
            raise ValueError("pr_number must be a positive integer")
        self.fetch_base_ref(
            repo=repo,
            pool_dir=pool_dir,
            ref=f"refs/merge-requests/{pr_number}/head",
        )

    def push_branch(
        self,
        *,
        repo: str,
        workspace_key: str,
        repo_dir: Path,
        branch: str,
        expected_head: str,
        slot_uid: int | None = None,
    ) -> PushResult:
        del repo, repo_dir
        if not isinstance(workspace_key, str) or not workspace_key:
            raise ValueError("workspace_key must be a non-empty string")
        if not isinstance(branch, str) or not branch:
            raise ValueError("branch must be a non-empty string")
        if not isinstance(expected_head, str) or not expected_head:
            raise ValueError("expected_head must be a non-empty string")
        body: dict[str, Any] = {
            "workspace_key": workspace_key,
            "branch": branch,
            "expected_head": expected_head,
        }
        if slot_uid is not None:
            body["slot_uid"] = slot_uid
        data = self._post(f"/gl/v1/projects/{self._project_id}/git/push", body)
        return PushResult(head=str(data.get("head") or expected_head), branch=str(data.get("branch") or branch))


def _response_items(data: Any) -> list[Mapping[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise GitLabError(502, "GitLab proxy returned malformed list payload")
    items = data["items"]
    if not all(isinstance(item, Mapping) for item in items):
        raise GitLabError(502, "GitLab proxy returned malformed list item")
    return items


def _project_from(data: Any) -> GitLabProjectInfo:
    if not isinstance(data, dict):
        raise GitLabError(500, "GitLab proxy returned malformed project payload")
    return GitLabProjectInfo(
        id=int(data["id"]),
        path_with_namespace=str(data.get("path_with_namespace") or ""),
        default_branch=str(data.get("default_branch") or ""),
        http_url_to_repo=str(data.get("http_url_to_repo") or ""),
        visibility=str(data.get("visibility") or ""),
        web_url=str(data.get("web_url") or ""),
    )


def _issue_from(data: Any) -> GitLabIssueInfo:
    if not isinstance(data, dict):
        raise GitLabError(500, "GitLab proxy returned malformed issue payload")
    return GitLabIssueInfo(
        project_id=int(data["project_id"]),
        iid=int(data["iid"]),
        title=str(data.get("title") or ""),
        description=str(data.get("description") or ""),
        state=str(data.get("state") or ""),
        author=str(data.get("author") or ""),
        labels=tuple(str(label) for label in (data.get("labels") or [])),
        web_url=str(data.get("web_url") or ""),
    )


def _note_from(data: Any) -> GitLabNoteInfo:
    if not isinstance(data, dict):
        raise GitLabError(500, "GitLab proxy returned malformed note payload")
    return GitLabNoteInfo(
        id=int(data["id"]),
        author=str(data.get("author") or ""),
        body=str(data.get("body") or ""),
        created_at=str(data.get("created_at") or ""),
        system=bool(data.get("system")),
        internal=bool(data.get("internal")),
    )


def _user_from(data: Any) -> GitLabUserInfo:
    if not isinstance(data, dict):
        raise GitLabError(500, "GitLab proxy returned malformed authenticated-user payload")
    return GitLabUserInfo(
        id=int(data["id"]),
        username=str(data.get("username") or ""),
        name=str(data.get("name") or ""),
    )


def _merge_request_from(data: Any) -> GitLabMergeRequestInfo:
    if not isinstance(data, dict):
        raise GitLabError(500, "GitLab proxy returned malformed merge request payload")
    return GitLabMergeRequestInfo(
        project_id=int(data["project_id"]),
        iid=int(data["iid"]),
        title=str(data.get("title") or ""),
        description=str(data.get("description") or ""),
        state=str(data.get("state") or ""),
        author=str(data.get("author") or ""),
        source_branch=str(data.get("source_branch") or ""),
        target_branch=str(data.get("target_branch") or ""),
        web_url=str(data.get("web_url") or ""),
    )


__all__ = ["GitLabProxyClient", "GitLabProxyGitTransport"]
