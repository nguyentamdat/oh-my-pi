"""Focused GitLab v4 client tests against httpx.MockTransport."""

from __future__ import annotations

import json
import secrets

import httpx
import pytest

from robomp.gitlab_client import GitLabClient

PROJECT_ID = 356


def _client(handler) -> GitLabClient:
    return GitLabClient(
        "https://gitlab.zingplay.com",
        secrets.token_urlsafe(),
        allowed_project_ids=frozenset({PROJECT_ID}),
        transport=httpx.MockTransport(handler),
    )


def _issue(iid: int = 42, labels: list[str] | None = None) -> dict[str, object]:
    return {
        "iid": iid,
        "title": "Crash on start",
        "description": "Steps",
        "state": "opened",
        "author": {"username": "alice"},
        "labels": labels or ["bug"],
        "web_url": "https://gitlab.zingplay.com/ica/server/-/issues/42",
    }


@pytest.mark.asyncio
async def test_project_issue_note_and_write_routes_use_numeric_project_id() -> None:
    calls: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        assert request.headers["PRIVATE-TOKEN"]
        match request.method, request.url.path:
            case "GET", "/api/v4/projects/356":
                return httpx.Response(
                    200,
                    json={
                        "id": PROJECT_ID,
                        "path_with_namespace": "ica/server",
                        "default_branch": "main",
                        "http_url_to_repo": "https://gitlab.zingplay.com/ica/server.git",
                        "visibility": "private",
                        "web_url": "https://gitlab.zingplay.com/ica/server",
                    },
                )
            case "GET", "/api/v4/user":
                return httpx.Response(200, json={"id": 9, "username": "robomp", "name": "RoboOMP"})
            case "GET", "/api/v4/projects/356/issues/42":
                return httpx.Response(200, json=_issue())
            case "GET", "/api/v4/projects/356/issues/42/related_merge_requests":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "iid": 13,
                            "title": "Fix crash",
                            "description": "Generated change",
                            "state": "opened",
                            "author": {"username": "robomp"},
                            "source_branch": "robomp/issue-42",
                            "target_branch": "main",
                            "web_url": "https://gitlab.zingplay.com/ica/server/-/merge_requests/13",
                        }
                    ],
                )
            case "GET", "/api/v4/projects/356/issues/42/closed_by":
                assert dict(request.url.params) == {"per_page": "100", "page": "1"}
                return httpx.Response(
                    200,
                    json=[
                        {
                            "iid": 14,
                            "title": "Closing fix",
                            "description": "Closes issue",
                            "state": "opened",
                            "author": {"username": "robomp"},
                            "source_branch": "robomp/issue-42",
                            "target_branch": "main",
                            "web_url": "https://gitlab.zingplay.com/ica/server/-/merge_requests/14",
                        }
                    ],
                )
            case "GET", "/api/v4/projects/356/issues/42/notes":
                assert request.url.params["per_page"] == "100"
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 99,
                            "author": {"username": "bob"},
                            "body": "Please fix",
                            "created_at": "2026-01-01T00:00:00Z",
                            "system": False,
                            "internal": False,
                        }
                    ],
                )
            case "POST", "/api/v4/projects/356/issues/42/notes":
                assert body == {"body": "Working on it"}
                return httpx.Response(201, json={"id": 100, "author": {"username": "robomp"}, "body": body["body"]})
            case "PUT", "/api/v4/projects/356/issues/42" if body == {"labels": "bug,triaged"}:
                return httpx.Response(200, json=_issue(labels=["bug", "triaged"]))
            case "PUT", "/api/v4/projects/356/issues/42" if body == {"add_labels": "triaged"}:
                return httpx.Response(200, json=_issue(labels=["bug", "triaged"]))
            case "PUT", "/api/v4/projects/356/issues/42" if body == {"state_event": "close"}:
                issue = _issue()
                issue["state"] = "closed"
                return httpx.Response(200, json=issue)
            case "POST", "/api/v4/projects/356/merge_requests":
                assert body == {
                    "source_branch": "robomp/issue-42",
                    "target_branch": "main",
                    "title": "Fix crash",
                    "description": "Generated change",
                }
                return httpx.Response(
                    201,
                    json={
                        "iid": 13,
                        "title": body["title"],
                        "description": body["description"],
                        "state": "opened",
                        "author": {"username": "robomp"},
                        "source_branch": body["source_branch"],
                        "target_branch": body["target_branch"],
                        "web_url": "https://gitlab.zingplay.com/ica/server/-/merge_requests/13",
                    },
                )
            case _:
                raise AssertionError(f"unexpected route {request.method} {request.url.path}")

    client = _client(handler)
    project = await client.get_project(PROJECT_ID)
    bot = await client.get_authenticated_user()
    issue = await client.get_issue(PROJECT_ID, 42)
    related_merge_requests = await client.list_issue_related_merge_requests(PROJECT_ID, 42)
    closing_merge_requests = await client.list_issue_closed_by_merge_requests(PROJECT_ID, 42)
    notes = await client.list_issue_notes(PROJECT_ID, 42)
    posted = await client.post_issue_note(PROJECT_ID, 42, "Working on it")
    updated = await client.update_issue_labels(PROJECT_ID, 42, ["bug", "triaged"])
    added = await client.add_issue_labels(PROJECT_ID, 42, ["triaged"])
    closed = await client.close_issue(PROJECT_ID, 42)
    merge_request = await client.create_merge_request(
        PROJECT_ID,
        source_branch="robomp/issue-42",
        target_branch="main",
        title="Fix crash",
        description="Generated change",
    )

    assert project.visibility == "private"
    assert bot.username == "robomp"
    assert issue.iid == 42
    assert notes[0].author == "bob"
    assert [merge_request.iid for merge_request in related_merge_requests] == [13]
    assert [merge_request.iid for merge_request in closing_merge_requests] == [14]
    assert posted.id == 100
    assert updated.labels == ("bug", "triaged")
    assert added.labels == ("bug", "triaged")
    assert closed.state == "closed"
    assert merge_request.iid == 13
    assert [path for _, path, _ in calls] == [
        "/api/v4/projects/356",
        "/api/v4/user",
        "/api/v4/projects/356/issues/42",
        "/api/v4/projects/356/issues/42/related_merge_requests",
        "/api/v4/projects/356/issues/42/closed_by",
        "/api/v4/projects/356/issues/42/notes",
        "/api/v4/projects/356/issues/42/notes",
        "/api/v4/projects/356/issues/42",
        "/api/v4/projects/356/issues/42",
        "/api/v4/projects/356/issues/42",
        "/api/v4/projects/356/merge_requests",
    ]


@pytest.mark.asyncio
async def test_move_issue_posts_target_project_and_returns_target_issue() -> None:
    target_project_id = 357
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == f"/api/v4/projects/{PROJECT_ID}/issues/42/move"
        assert json.loads(request.content) == {"to_project_id": target_project_id}
        return httpx.Response(200, json=_issue(iid=99))

    client = GitLabClient(
        "https://gitlab.zingplay.com",
        "glpat-test-token",
        allowed_project_ids=frozenset({PROJECT_ID, target_project_id}),
        transport=httpx.MockTransport(handler),
    )

    moved = await client.move_issue(PROJECT_ID, 42, target_project_id)

    assert moved.project_id == target_project_id
    assert moved.iid == 99
    with pytest.raises(ValueError, match="allowlist"):
        await client.move_issue(PROJECT_ID, 42, target_project_id + 1)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_resolve_moved_issue_reads_source_then_global_target() -> None:
    target_project_id = 357
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == f"/api/v4/projects/{PROJECT_ID}/issues/42":
            source = _issue()
            source.update({"id": 100, "moved_to_id": 200})
            return httpx.Response(200, json=source)
        if request.url.path == "/api/v4/issues/200":
            target = _issue(iid=99)
            target.update({"id": 200, "project_id": target_project_id})
            return httpx.Response(200, json=target)
        if request.url.path == f"/api/v4/projects/{PROJECT_ID}/issues/43":
            source = _issue(iid=43)
            source.update({"id": 101, "moved_to_id": None})
            return httpx.Response(200, json=source)
        raise AssertionError(f"unexpected route {request.url.path}")

    client = GitLabClient(
        "https://gitlab.zingplay.com",
        "glpat-test-token",
        allowed_project_ids=frozenset({PROJECT_ID, target_project_id}),
        transport=httpx.MockTransport(handler),
    )

    moved = await client.resolve_moved_issue(PROJECT_ID, 42, target_project_id)

    assert moved is not None
    assert moved.id == 200
    assert moved.project_id == target_project_id
    assert await client.resolve_moved_issue(PROJECT_ID, 43, target_project_id) is None
    assert calls == [
        f"/api/v4/projects/{PROJECT_ID}/issues/42",
        "/api/v4/issues/200",
        f"/api/v4/projects/{PROJECT_ID}/issues/43",
    ]


@pytest.mark.asyncio
async def test_resolve_moved_issue_rejects_unexpected_target_project() -> None:
    expected_target_project_id = 357

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v4/projects/{PROJECT_ID}/issues/42":
            source = _issue()
            source.update({"id": 100, "moved_to_id": 200})
            return httpx.Response(200, json=source)
        if request.url.path == "/api/v4/issues/200":
            target = _issue(iid=99)
            target.update({"id": 200, "project_id": 358})
            return httpx.Response(200, json=target)
        raise AssertionError(f"unexpected route {request.url.path}")

    client = GitLabClient(
        "https://gitlab.zingplay.com",
        "glpat-test-token",
        allowed_project_ids=frozenset({PROJECT_ID, expected_target_project_id}),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ValueError, match="expected target"):
        await client.resolve_moved_issue(PROJECT_ID, 42, expected_target_project_id)


@pytest.mark.asyncio
async def test_client_rejects_project_outside_injected_allowlist() -> None:
    client = _client(lambda request: pytest.fail(f"unexpected request: {request.url}"))
    with pytest.raises(ValueError, match="allowlist"):
        await client.get_issue(PROJECT_ID + 1, 42)


@pytest.mark.asyncio
async def test_list_issue_notes_fetches_all_pages() -> None:
    requested_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_pages.append(request.url.params["page"])
        page = int(request.url.params["page"])
        notes = [
            {"id": note_id, "author": {"username": "alice"}, "body": str(note_id)}
            for note_id in (range(1, 101) if page == 1 else range(101, 103))
        ]
        return httpx.Response(200, json=notes)

    notes = await _client(handler).list_issue_notes(PROJECT_ID, 42)

    assert [note.id for note in notes] == list(range(1, 103))
    assert requested_pages == ["1", "2"]


@pytest.mark.asyncio
async def test_list_issue_related_merge_requests_fetches_all_pages() -> None:
    requested_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_pages.append(request.url.params["page"])
        page = int(request.url.params["page"])
        merge_requests = [
            {
                "iid": merge_request_id,
                "title": f"MR {merge_request_id}",
                "description": "",
                "state": "opened",
                "author": {"username": "robomp"},
                "source_branch": f"bot/{merge_request_id}",
                "target_branch": "main",
                "web_url": f"https://gitlab.zingplay.com/ica/server/-/merge_requests/{merge_request_id}",
            }
            for merge_request_id in (range(1, 101) if page == 1 else range(101, 103))
        ]
        return httpx.Response(200, json=merge_requests)

    merge_requests = await _client(handler).list_issue_related_merge_requests(PROJECT_ID, 42)

    assert [merge_request.iid for merge_request in merge_requests] == list(range(1, 103))
    assert requested_pages == ["1", "2"]


@pytest.mark.asyncio
async def test_list_issue_closed_by_merge_requests_rejects_malformed_body() -> None:
    client = _client(lambda _: httpx.Response(200, json=[{"iid": 1}, "not an object"]))

    with pytest.raises(ValueError, match="list of objects"):
        await client.list_issue_closed_by_merge_requests(PROJECT_ID, 42)


@pytest.mark.asyncio
async def test_list_issue_closed_by_merge_requests_fetches_all_pages() -> None:
    requested_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v4/projects/{PROJECT_ID}/issues/42/closed_by"
        requested_pages.append(request.url.params["page"])
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=[
                {
                    "iid": merge_request_id,
                    "title": f"Closing MR {merge_request_id}",
                    "description": "",
                    "state": "opened",
                    "author": {"username": "robomp"},
                    "source_branch": f"bot/{merge_request_id}",
                    "target_branch": "main",
                    "web_url": f"https://gitlab.zingplay.com/ica/server/-/merge_requests/{merge_request_id}",
                }
                for merge_request_id in (range(1, 101) if page == 1 else range(101, 103))
            ],
        )

    merge_requests = await _client(handler).list_issue_closed_by_merge_requests(PROJECT_ID, 42)

    assert [merge_request.iid for merge_request in merge_requests] == list(range(1, 103))
    assert requested_pages == ["1", "2"]
