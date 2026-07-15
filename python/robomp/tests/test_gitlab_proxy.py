"""Focused HMAC, allowlist, and trusted-destination tests for GitLab proxying."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from robomp.config import Settings
from robomp.gitlab_client import GitLabClient, GitLabError, GitLabProjectInfo
from robomp.gitlab_proxy_client import GitLabProxyClient, GitLabProxyGitTransport
from robomp.proxy.server import (
    _gitlab_pool_dir,
    _resolve_gitlab_project_token,
    _trusted_gitlab_clone_url,
    create_proxy_app,
)
from robomp.proxy_hmac import HEADER_SIGNATURE, HEADER_TIMESTAMP, sign, verify
from robomp.sandbox import SandboxManager

_HMAC = "test-hmac-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_GITLAB_TOKEN = "glpat-test-token-must-not-leak"
_PROJECT_ID = 356
_BASE_URL = "https://gitlab.example.test"


def _settings(tmp_path: Path) -> Settings:
    cfg = Settings.model_construct(
        github_token=SecretStr("ghp_test_token_value"),
        github_webhook_secret=SecretStr("webhook-secret"),
        bot_login="robomp-bot",
        git_author_email="robomp-bot@example.invalid",
        gh_proxy_url=None,
        gh_proxy_hmac_key=SecretStr(_HMAC),
        workspace_root=tmp_path / "workspaces",
        sqlite_path=tmp_path / "robomp.sqlite",
        log_dir=tmp_path / "logs",
        gitlab_token=SecretStr(_GITLAB_TOKEN),
        gitlab_base_url=_BASE_URL,
        gitlab_project_ids_raw=str(_PROJECT_ID),
    )
    cfg.ensure_paths()
    return cfg


def _project_payload() -> dict[str, object]:
    return {
        "id": _PROJECT_ID,
        "path_with_namespace": "group/widget",
        "default_branch": "main",
        "http_url_to_repo": f"{_BASE_URL}/group/widget.git",
        "visibility": "private",
        "web_url": f"{_BASE_URL}/group/widget",
    }


def _app(tmp_path: Path, handler) -> object:
    cfg = _settings(tmp_path)
    app = create_proxy_app(cfg)
    app.state.settings = cfg
    app.state.gitlab_by_project = {
        _PROJECT_ID: GitLabClient(
            _BASE_URL,
            _GITLAB_TOKEN,
            allowed_project_ids=frozenset({_PROJECT_ID}),
            transport=httpx.MockTransport(handler),
        )
    }
    app.state.gitlab_routing = GitLabClient(
        _BASE_URL,
        _GITLAB_TOKEN,
        allowed_project_ids=frozenset({_PROJECT_ID}),
        transport=httpx.MockTransport(handler),
    )
    return app


def _mapped_app(
    tmp_path: Path,
    handler,
    *,
    project_ids: frozenset[int],
    project_tokens: dict[int, str],
    routing_token: str,
    legacy_token: str | None = None,
) -> object:
    cfg = _settings(tmp_path).model_copy(
        update={
            "gitlab_token": SecretStr(legacy_token) if legacy_token is not None else None,
            "gitlab_routing_token": SecretStr(routing_token),
            "gitlab_project_tokens_json": SecretStr(
                json.dumps({str(project_id): token for project_id, token in project_tokens.items()})
            ),
            "gitlab_project_ids_raw": ",".join(str(project_id) for project_id in sorted(project_ids)),
        }
    )
    cfg.ensure_paths()
    app = create_proxy_app(cfg)
    app.state.settings = cfg
    app.state.gitlab_by_project = {
        project_id: GitLabClient(
            _BASE_URL,
            token,
            allowed_project_ids=frozenset({project_id}),
            transport=httpx.MockTransport(handler),
        )
        for project_id, token in project_tokens.items()
    }
    app.state.gitlab_routing = GitLabClient(
        _BASE_URL,
        routing_token,
        allowed_project_ids=project_ids,
        transport=httpx.MockTransport(handler),
    )
    return app


def _headers(method: str, path: str, body: bytes = b"") -> dict[str, str]:
    timestamp, signature = sign(method=method, path=path, body=body, key=_HMAC.encode("utf-8"))
    return {HEADER_TIMESTAMP: timestamp, HEADER_SIGNATURE: signature}


async def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy.test")  # type: ignore[arg-type]


def test_proxy_starts_with_gitlab_token_only(tmp_path: Path) -> None:
    cfg = _settings(tmp_path).model_copy(update={"github_token": None})
    with TestClient(create_proxy_app(cfg)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200


async def test_gitlab_routes_reject_missing_hmac(tmp_path: Path) -> None:
    app = _app(tmp_path, lambda _: httpx.Response(200, json=_project_payload()))
    async with await _client(app) as client:
        response = await client.get(f"/gl/v1/projects/{_PROJECT_ID}")
    assert response.status_code == 401


async def test_gitlab_project_allowlist_allows_356_and_denies_other_project(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == f"/api/v4/projects/{_PROJECT_ID}"
        return httpx.Response(200, json=_project_payload())

    app = _app(tmp_path, handler)
    async with await _client(app) as client:
        allowed = await client.get(
            f"/gl/v1/projects/{_PROJECT_ID}",
            headers=_headers("GET", f"/gl/v1/projects/{_PROJECT_ID}"),
        )
        denied = await client.get(
            "/gl/v1/projects/357",
            headers=_headers("GET", "/gl/v1/projects/357"),
        )
    assert allowed.status_code == 200
    assert allowed.json()["id"] == _PROJECT_ID
    assert denied.status_code == 403
    assert len(calls) == 1


async def test_project_token_map_selects_exact_project_client(tmp_path: Path) -> None:
    source_project_id = _PROJECT_ID
    target_project_id = 357
    source_token = "glpat-source-token"
    target_token = "glpat-target-token"
    routing_token = "glpat-routing-token"
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers["PRIVATE-TOKEN"]))
        project_id = int(request.url.path.split("/")[4])
        return httpx.Response(
            200,
            json={
                "id": project_id * 10,
                "iid": 7,
                "title": "T",
                "description": "D",
                "state": "opened",
                "author": {"username": "alice"},
                "labels": [],
                "web_url": f"{_BASE_URL}/group/widget/-/issues/7",
            },
        )

    app = _mapped_app(
        tmp_path,
        handler,
        project_ids=frozenset({source_project_id, target_project_id}),
        project_tokens={source_project_id: source_token, target_project_id: target_token},
        routing_token=routing_token,
    )
    proxy = GitLabProxyClient(
        base_url="http://proxy.test",
        hmac_key=_HMAC,
        transport=httpx.ASGITransport(app=app),
    )

    assert (await proxy.get_issue(source_project_id, 7)).project_id == source_project_id
    assert (await proxy.get_issue(target_project_id, 7)).project_id == target_project_id
    cfg = app.state.settings
    assert _resolve_gitlab_project_token(cfg, source_project_id) == source_token
    assert _resolve_gitlab_project_token(cfg, target_project_id) == target_token
    assert seen == [
        (f"/api/v4/projects/{source_project_id}/issues/7", source_token),
        (f"/api/v4/projects/{target_project_id}/issues/7", target_token),
    ]


async def test_project_scoped_authenticated_user_uses_exact_project_token(tmp_path: Path) -> None:
    source_project_id = _PROJECT_ID
    target_project_id = 357
    source_token = "glpat-source-token"
    target_token = "glpat-target-token"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/user"
        seen.append(request.headers["PRIVATE-TOKEN"])
        return httpx.Response(200, json={"id": 1, "username": "robomp", "name": "RoboMP"})

    app = _mapped_app(
        tmp_path,
        handler,
        project_ids=frozenset({source_project_id, target_project_id}),
        project_tokens={source_project_id: source_token, target_project_id: target_token},
        routing_token="glpat-routing-token",
    )
    proxy = GitLabProxyClient(
        base_url="http://proxy.test",
        hmac_key=_HMAC,
        transport=httpx.ASGITransport(app=app),
    )

    assert (await proxy.get_authenticated_user(source_project_id)).username == "robomp"
    assert (await proxy.get_authenticated_user(target_project_id)).username == "robomp"
    assert seen == [source_token, target_token]


async def test_project_token_map_rejects_allowlisted_project_without_token(tmp_path: Path) -> None:
    source_project_id = _PROJECT_ID
    target_project_id = 357
    upstream_calls: list[httpx.Request] = []
    app = _mapped_app(
        tmp_path,
        lambda request: upstream_calls.append(request) or httpx.Response(200, json={}),
        project_ids=frozenset({source_project_id, target_project_id}),
        project_tokens={source_project_id: "glpat-source-token"},
        routing_token="glpat-routing-token",
    )
    path = f"/gl/v1/projects/{target_project_id}/issues/7"

    async with await _client(app) as client:
        response = await client.get(path, headers=_headers("GET", path))

    assert response.status_code == 503
    assert upstream_calls == []


async def test_move_and_recovery_use_routing_token_and_validate_both_projects(tmp_path: Path) -> None:
    source_project_id = _PROJECT_ID
    target_project_id = 357
    source_token = "glpat-source-token"
    target_token = "glpat-target-token"
    routing_token = "glpat-routing-token"
    seen: list[tuple[str, str, object]] = []

    def issue_payload(*, iid: int, issue_id: int, moved_to_id: int | None = None) -> dict[str, object]:
        return {
            "id": issue_id,
            "iid": iid,
            "title": "T",
            "description": "D",
            "state": "opened",
            "author": {"username": "alice"},
            "labels": [],
            "moved_to_id": moved_to_id,
            "web_url": f"{_BASE_URL}/group/widget/-/issues/{iid}",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.url.path, request.headers["PRIVATE-TOKEN"], body))
        if request.url.path == f"/api/v4/projects/{source_project_id}/issues/7/move":
            assert body == {"to_project_id": target_project_id}
            return httpx.Response(200, json=issue_payload(iid=99, issue_id=200))
        if request.url.path == f"/api/v4/projects/{source_project_id}/issues/7":
            return httpx.Response(200, json=issue_payload(iid=7, issue_id=100, moved_to_id=200))
        if request.url.path == "/api/v4/issues/200":
            payload = issue_payload(iid=99, issue_id=200)
            payload["project_id"] = target_project_id
            return httpx.Response(200, json=payload)
        if request.url.path == f"/api/v4/projects/{source_project_id}/issues/8":
            return httpx.Response(200, json=issue_payload(iid=8, issue_id=101))
        raise AssertionError(f"unexpected route {request.url.path}")

    app = _mapped_app(
        tmp_path,
        handler,
        project_ids=frozenset({source_project_id, target_project_id}),
        project_tokens={source_project_id: source_token, target_project_id: target_token},
        routing_token=routing_token,
    )
    proxy = GitLabProxyClient(
        base_url="http://proxy.test",
        hmac_key=_HMAC,
        transport=httpx.ASGITransport(app=app),
    )

    moved = await proxy.move_issue(source_project_id, 7, target_project_id)
    recovered = await proxy.resolve_moved_issue(source_project_id, 7, target_project_id)
    assert await proxy.resolve_moved_issue(source_project_id, 8, target_project_id) is None

    assert moved.project_id == target_project_id
    assert moved.id == 200
    assert recovered is not None
    assert recovered.project_id == target_project_id
    assert recovered.id == 200
    assert seen == [
        (f"/api/v4/projects/{source_project_id}/issues/7/move", routing_token, {"to_project_id": target_project_id}),
        (f"/api/v4/projects/{source_project_id}/issues/7", routing_token, None),
        ("/api/v4/issues/200", routing_token, None),
        (f"/api/v4/projects/{source_project_id}/issues/8", routing_token, None),
    ]

    async with await _client(app) as client:
        disallowed_target_path = f"/gl/v1/projects/{source_project_id}/issues/7/move"
        disallowed_target_body = b'{"to_project_id":358}'
        disallowed_target = await client.post(
            disallowed_target_path,
            content=disallowed_target_body,
            headers={
                **_headers("POST", disallowed_target_path, disallowed_target_body),
                "Content-Type": "application/json",
            },
        )
        disallowed_source_path = "/gl/v1/projects/358/issues/7/move"
        disallowed_source_body = b'{"to_project_id":356}'
        disallowed_source = await client.post(
            disallowed_source_path,
            content=disallowed_source_body,
            headers={
                **_headers("POST", disallowed_source_path, disallowed_source_body),
                "Content-Type": "application/json",
            },
        )
    assert disallowed_target.status_code == 403
    assert disallowed_source.status_code == 403
    assert len(seen) == 4


async def test_move_error_scrubs_every_configured_gitlab_token(tmp_path: Path) -> None:
    legacy_token = "glpat-prefix"
    source_token = "glpat-source-token"
    target_token = "glpat-target-token"
    routing_token = "glpat-prefix-routing-token"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"message": f"{legacy_token} {target_token} {routing_token}"},
        )

    app = _mapped_app(
        tmp_path,
        handler,
        project_ids=frozenset({_PROJECT_ID, 357}),
        project_tokens={_PROJECT_ID: source_token, 357: target_token},
        routing_token=routing_token,
        legacy_token=legacy_token,
    )
    proxy = GitLabProxyClient(
        base_url="http://proxy.test",
        hmac_key=_HMAC,
        transport=httpx.ASGITransport(app=app),
    )

    with pytest.raises(GitLabError) as exc:
        await proxy.move_issue(_PROJECT_ID, 7, 357)

    assert all(token not in str(exc.value) for token in (legacy_token, source_token, target_token, routing_token))
    assert str(exc.value) == "GitLab 401: ******** ******** ********"


async def test_gitlab_proxy_client_uses_only_typed_project_api_shapes(tmp_path: Path) -> None:
    seen: list[tuple[str, str, object]] = []

    def issue_payload() -> dict[str, object]:
        return {
            "iid": 7,
            "title": "T",
            "description": "D",
            "state": "opened",
            "author": {"username": "alice"},
            "labels": ["bug"],
            "web_url": f"{_BASE_URL}/group/widget/-/issues/7",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == f"/api/v4/projects/{_PROJECT_ID}":
            return httpx.Response(200, json=_project_payload())
        if request.url.path.endswith("/related_merge_requests"):
            return httpx.Response(
                200,
                json=[
                    {
                        "iid": 8,
                        "title": "Fix",
                        "description": "D",
                        "state": "opened",
                        "author": {"username": "robomp"},
                        "source_branch": "bot/fix",
                        "target_branch": "main",
                        "web_url": f"{_BASE_URL}/group/widget/-/merge_requests/8",
                    }
                ],
            )
        if request.url.path.endswith("/closed_by"):
            return httpx.Response(
                200,
                json=[
                    {
                        "iid": 9,
                        "title": "Closing Fix",
                        "description": "Closes issue",
                        "state": "opened",
                        "author": {"username": "robomp"},
                        "source_branch": "bot/close",
                        "target_branch": "main",
                        "web_url": f"{_BASE_URL}/group/widget/-/merge_requests/9",
                    }
                ],
            )
        if request.url.path.endswith("/notes"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=issue_payload())

    app = _app(tmp_path, handler)
    proxy = GitLabProxyClient(
        base_url="http://proxy.test",
        hmac_key=_HMAC,
        transport=httpx.ASGITransport(app=app),
    )
    await proxy.get_project(_PROJECT_ID)
    await proxy.get_issue(_PROJECT_ID, 7)
    related = await proxy.list_issue_related_merge_requests(_PROJECT_ID, 7)
    closing = await proxy.list_issue_closed_by_merge_requests(_PROJECT_ID, 7)
    assert await proxy.list_issue_notes(_PROJECT_ID, 7) == []
    updated = await proxy.update_issue_labels(_PROJECT_ID, 7, ["bug", "triage"])

    assert updated.labels == ("bug",)
    assert [merge_request.iid for merge_request in related] == [8]
    assert [merge_request.iid for merge_request in closing] == [9]
    assert seen == [
        ("GET", f"/api/v4/projects/{_PROJECT_ID}", None),
        ("GET", f"/api/v4/projects/{_PROJECT_ID}/issues/7", None),
        ("GET", f"/api/v4/projects/{_PROJECT_ID}/issues/7/related_merge_requests", None),
        ("GET", f"/api/v4/projects/{_PROJECT_ID}/issues/7/closed_by", None),
        ("GET", f"/api/v4/projects/{_PROJECT_ID}/issues/7/notes", None),
        ("PUT", f"/api/v4/projects/{_PROJECT_ID}/issues/7", {"labels": "bug,triage"}),
    ]


async def test_gitlab_proxy_client_implements_remaining_backend_operations(tmp_path: Path) -> None:
    seen: list[tuple[str, str, object]] = []

    def issue_payload() -> dict[str, object]:
        return {
            "iid": 7,
            "title": "T",
            "description": "D",
            "state": "opened",
            "author": {"username": "alice"},
            "labels": ["bug"],
            "web_url": f"{_BASE_URL}/group/widget/-/issues/7",
        }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.url.path == "/api/v4/user":
            return httpx.Response(200, json={"id": 5, "username": "robomp", "name": "RoboMP"})
        if request.url.path.endswith("/merge_requests"):
            return httpx.Response(
                201,
                json={
                    "iid": 8,
                    "title": "Fix",
                    "description": "Details",
                    "state": "opened",
                    "author": {"username": "robomp"},
                    "source_branch": "bot/fix",
                    "target_branch": "main",
                    "web_url": f"{_BASE_URL}/group/widget/-/merge_requests/8",
                },
            )
        return httpx.Response(200, json=issue_payload())

    app = _app(tmp_path, handler)
    proxy = GitLabProxyClient(
        base_url="http://proxy.test",
        hmac_key=_HMAC,
        transport=httpx.ASGITransport(app=app),
    )
    user = await proxy.get_authenticated_user()
    added = await proxy.add_issue_labels(_PROJECT_ID, 7, ["triage"])
    closed = await proxy.close_issue(_PROJECT_ID, 7)
    merge_request = await proxy.create_merge_request(
        _PROJECT_ID,
        source_branch="bot/fix",
        target_branch="main",
        title="Fix",
        description="Details",
    )

    assert user.username == "robomp"
    assert added.labels == ("bug",)
    assert closed.state == "opened"
    assert merge_request.iid == 8
    assert seen == [
        ("GET", "/api/v4/user", None),
        ("PUT", f"/api/v4/projects/{_PROJECT_ID}/issues/7", {"add_labels": "triage"}),
        ("PUT", f"/api/v4/projects/{_PROJECT_ID}/issues/7", {"state_event": "close"}),
        (
            "POST",
            f"/api/v4/projects/{_PROJECT_ID}/merge_requests",
            {
                "source_branch": "bot/fix",
                "target_branch": "main",
                "title": "Fix",
                "description": "Details",
            },
        ),
    ]


async def test_gitlab_proxy_route_exhausts_related_merge_request_pages(tmp_path: Path) -> None:
    requested_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v4/projects/{_PROJECT_ID}/issues/7/related_merge_requests"
        requested_pages.append(request.url.params["page"])
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=[
                {
                    "iid": merge_request_id,
                    "title": f"MR {merge_request_id}",
                    "description": "",
                    "state": "opened",
                    "author": {"username": "robomp"},
                    "source_branch": f"bot/{merge_request_id}",
                    "target_branch": "main",
                    "web_url": f"{_BASE_URL}/group/widget/-/merge_requests/{merge_request_id}",
                }
                for merge_request_id in (range(1, 101) if page == 1 else range(101, 103))
            ],
        )

    proxy = GitLabProxyClient(
        base_url="http://proxy.test",
        hmac_key=_HMAC,
        transport=httpx.ASGITransport(app=_app(tmp_path, handler)),
    )

    merge_requests = await proxy.list_issue_related_merge_requests(_PROJECT_ID, 7)

    assert [merge_request.iid for merge_request in merge_requests] == list(range(1, 103))
    assert requested_pages == ["1", "2"]


async def test_gitlab_proxy_client_posts_issue_note_to_typed_api_route(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        assert request.headers["PRIVATE-TOKEN"] == _GITLAB_TOKEN
        return httpx.Response(
            201,
            json={
                "id": 44,
                "author": {"username": "robomp"},
                "body": "implemented",
                "created_at": "2026-01-01T00:00:00Z",
                "system": False,
                "internal": False,
            },
        )

    app = _app(tmp_path, handler)
    proxy = GitLabProxyClient(
        base_url="http://proxy.test",
        hmac_key=_HMAC,
        transport=httpx.ASGITransport(app=app),
    )
    note = await proxy.post_issue_note(_PROJECT_ID, 9, "implemented")

    assert note.id == 44
    assert seen == {
        "method": "POST",
        "path": f"/api/v4/projects/{_PROJECT_ID}/issues/9/notes",
        "body": {"body": "implemented"},
    }


async def test_gitlab_issue_note_error_redacts_proxy_token(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": f"invalid token {_GITLAB_TOKEN}"})

    app = _app(tmp_path, handler)
    raw_body = b'{"body":"hello"}'
    path = f"/gl/v1/projects/{_PROJECT_ID}/issues/9/notes"
    async with await _client(app) as client:
        response = await client.post(
            path,
            content=raw_body,
            headers={**_headers("POST", path, raw_body), "Content-Type": "application/json"},
        )
    assert response.status_code == 401
    assert _GITLAB_TOKEN not in response.text
    assert "PRIVATE-TOKEN" not in response.text
    assert "********" in response.text


def test_trusted_gitlab_clone_requires_configured_host_and_api_path() -> None:
    trusted = GitLabProjectInfo(
        id=_PROJECT_ID,
        path_with_namespace="group/subgroup/widget",
        default_branch="main",
        http_url_to_repo=f"{_BASE_URL}/group/subgroup/widget.git",
        visibility="private",
        web_url=f"{_BASE_URL}/group/subgroup/widget",
    )
    assert _trusted_gitlab_clone_url(_BASE_URL, trusted, project_id=_PROJECT_ID) == trusted.http_url_to_repo

    wrong_host = GitLabProjectInfo(
        id=_PROJECT_ID,
        path_with_namespace="group/subgroup/widget",
        default_branch="main",
        http_url_to_repo="https://evil.example/group/subgroup/widget.git",
        visibility="private",
        web_url=f"{_BASE_URL}/group/subgroup/widget",
    )
    wrong_path = GitLabProjectInfo(
        id=_PROJECT_ID,
        path_with_namespace="group/subgroup/widget",
        default_branch="main",
        http_url_to_repo=f"{_BASE_URL}/group/other.git",
        visibility="private",
        web_url=f"{_BASE_URL}/group/subgroup/widget",
    )
    with pytest.raises(HTTPException, match="untrusted clone URL"):
        _trusted_gitlab_clone_url(_BASE_URL, wrong_host, project_id=_PROJECT_ID)
    with pytest.raises(HTTPException, match="untrusted clone URL"):
        _trusted_gitlab_clone_url(_BASE_URL, wrong_path, project_id=_PROJECT_ID)


def test_gitlab_proxy_and_sandbox_share_canonical_pool_path(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    project = GitLabProjectInfo(
        id=_PROJECT_ID,
        path_with_namespace="ica/server",
        default_branch="main",
        http_url_to_repo=f"{_BASE_URL}/ica/server.git",
        visibility="private",
        web_url=f"{_BASE_URL}/ica/server",
    )
    transport = GitLabProxyGitTransport(
        project_id=_PROJECT_ID,
        base_url="http://proxy.test",
        hmac_key=_HMAC,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
    )
    sandbox = SandboxManager(cfg.workspace_root, transport=transport)

    expected = sandbox.pool_path(
        project.path_with_namespace,
        instance_id="gitlab-zingplay",
        repository_id=str(_PROJECT_ID),
    )
    assert _gitlab_pool_dir(cfg, project) == expected


def test_gitlab_proxy_transport_sends_only_typed_project_route() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    transport = GitLabProxyGitTransport(
        project_id=_PROJECT_ID,
        base_url="http://proxy.test",
        hmac_key=_HMAC,
        transport=httpx.MockTransport(handler),
    )
    transport.clone_pool(
        repo="group/widget",
        clone_url="https://attacker.example/group/widget.git",
        default_branch="attacker-branch",
        target=Path("ignored"),
    )

    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == f"/gl/v1/projects/{_PROJECT_ID}/git/clone"
    assert json.loads(request.content) == {}
    assert _GITLAB_TOKEN not in request.content.decode("utf-8")
    signature = request.headers[HEADER_SIGNATURE]
    timestamp = request.headers[HEADER_TIMESTAMP]
    assert verify(
        method="POST",
        path=request.url.path,
        body=request.content,
        timestamp=timestamp,
        signature=signature,
        key=_HMAC.encode("utf-8"),
    ).ok


def test_gitlab_proxy_clone_does_not_retry_a_lost_response() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("clone response lost", request=request)

    transport = GitLabProxyGitTransport(
        project_id=_PROJECT_ID,
        base_url="http://proxy.test",
        hmac_key=_HMAC,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.ReadTimeout, match="clone response lost"):
        transport.clone_pool(
            repo="group/widget",
            clone_url=f"{_BASE_URL}/group/widget.git",
            default_branch="main",
            target=Path("ignored"),
        )

    assert [request.url.path for request in attempts] == [f"/gl/v1/projects/{_PROJECT_ID}/git/clone"]


async def test_bad_hmac_header_is_not_accepted_by_gitlab_route(tmp_path: Path) -> None:
    app = _app(tmp_path, lambda _: httpx.Response(200, json=_project_payload()))
    path = f"/gl/v1/projects/{_PROJECT_ID}"
    async with await _client(app) as client:
        response = await client.get(
            path,
            headers={HEADER_TIMESTAMP: str(int(time.time())), HEADER_SIGNATURE: "0" * 64},
        )
    assert response.status_code == 401
