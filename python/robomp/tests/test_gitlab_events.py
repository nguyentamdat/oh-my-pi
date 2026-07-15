"""Focused GitLab legacy-webhook adapter tests."""

from __future__ import annotations

import hashlib
import secrets

import pytest

from robomp.forge import ForgeEvent, ForgeInstance
from robomp.gitlab_events import GitLabWebhookAdapter

PROJECT_ID = 356


def _adapter(token: str) -> GitLabWebhookAdapter:
    return GitLabWebhookAdapter(
        ForgeInstance(
            id="zingplay",
            kind="gitlab",
            base_url="https://gitlab.zingplay.com",
            api_url="https://gitlab.zingplay.com/api/v4",
        ),
        webhook_token=token,
        allowed_project_ids=frozenset({PROJECT_ID}),
        bot_username="robomp",
        trigger_label="roboomp",
    )


def _project() -> dict[str, object]:
    return {"id": PROJECT_ID, "path_with_namespace": "ica/server"}


def _issue_payload(action: str = "open") -> dict[str, object]:
    return {
        "object_kind": "issue",
        "project": _project(),
        "user": {"id": 7, "username": "alice"},
        "object_attributes": {
            "iid": 42,
            "action": action,
            "title": "Crash on start",
            "description": "Steps",
        },
        "labels": [{"title": "bug"}, {"title": "roboomp"}],
    }


def _note_payload(noteable_type: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "object_kind": "note",
        "project": _project(),
        "user": {"id": 8, "username": "bob"},
        "object_attributes": {"id": 99, "action": "create", "noteable_type": noteable_type, "note": "Please fix"},
    }
    if noteable_type == "Issue":
        payload["issue"] = {"iid": 42}
    else:
        payload["merge_request"] = {"iid": 12}
    return payload


def test_verify_accepts_valid_token_and_uses_raw_body_digest() -> None:
    token = secrets.token_urlsafe()
    body = b'{"object_kind":"issue"}'
    assert _adapter(token).verify({"X-Gitlab-Token": token}, body) == hashlib.sha256(body).hexdigest()


def test_verify_prefers_idempotency_key_over_event_uuid() -> None:
    token = secrets.token_urlsafe()
    headers = {
        "X-Gitlab-Token": token,
        "Idempotency-Key": "idempotency-42",
        "X-Gitlab-Event-UUID": "event-42",
        "X-Gitlab-Webhook-UUID": "shared-hook",
    }
    assert _adapter(token).verify(headers, b"{}") == "idempotency-42"


def test_verify_rejects_invalid_token() -> None:
    token = secrets.token_urlsafe()
    with pytest.raises(PermissionError):
        _adapter(token).verify({"X-Gitlab-Token": f"{token}.invalid"}, b"{}")


def test_normalize_allowlisted_issue_opened() -> None:
    payload = _issue_payload()
    event = _adapter(secrets.token_urlsafe()).normalize(
        delivery_id="d1", headers={"X-Gitlab-Event": "Issue Hook"}, payload=payload
    )
    assert event is not None
    assert (event.event, event.task_kind, event.item.key) == ("issue.opened", "triage_issue", "zingplay:356:issue:42")
    assert event.labels == ("bug", "roboomp")
    assert event.source_payload is payload
    assert ForgeEvent.from_dict(event.to_dict()) == event


def test_normalize_ignores_unlabeled_issue_open() -> None:
    payload = _issue_payload()
    payload["labels"] = [{"title": "bug"}]
    assert _adapter(secrets.token_urlsafe()).normalize(delivery_id="d0", headers={}, payload=payload) is None


def test_normalize_label_added_update_once() -> None:
    payload = _issue_payload("update")
    payload["changes"] = {
        "labels": {
            "previous": [{"title": "bug"}],
            "current": [{"title": "bug"}, {"title": "roboomp"}],
        }
    }
    event = _adapter(secrets.token_urlsafe()).normalize(delivery_id="d-update", headers={}, payload=payload)
    assert event is not None
    assert (event.event, event.task_kind) == ("issue.updated", "triage_issue")


def test_normalize_labeled_issue_reopened() -> None:
    event = _adapter(secrets.token_urlsafe()).normalize(
        delivery_id="d-reopen",
        headers={},
        payload=_issue_payload("reopen"),
    )
    assert event is not None
    assert (event.event, event.task_kind) == ("issue.reopened", "triage_issue")


def test_normalize_issue_closed_for_cleanup() -> None:
    event = _adapter(secrets.token_urlsafe()).normalize(delivery_id="d2", headers={}, payload=_issue_payload("close"))
    assert event is not None
    assert (event.event, event.task_kind) == ("issue.closed", "cleanup_workspace")


def test_normalize_issue_note() -> None:
    event = _adapter(secrets.token_urlsafe()).normalize(delivery_id="d3", headers={}, payload=_note_payload("Issue"))
    assert event is not None
    assert (event.event, event.task_kind, event.item.kind, event.item.number) == (
        "issue.comment.created",
        "handle_comment",
        "issue",
        42,
    )
    assert event.comment is not None and event.comment.body == "Please fix"


@pytest.mark.parametrize(
    ("system", "internal", "body"),
    [(True, False, "changed label"), (False, True, "secret"), (False, False, "  ")],
)
def test_normalize_ignores_non_public_or_empty_note(system: bool, internal: bool, body: str) -> None:
    payload = _note_payload("Issue")
    attributes = payload["object_attributes"]
    assert isinstance(attributes, dict)
    attributes["system"] = system
    attributes["internal"] = internal
    attributes["note"] = body
    assert _adapter(secrets.token_urlsafe()).normalize(delivery_id="noise", headers={}, payload=payload) is None


def test_normalize_rejects_gitlab_175_confidential_note_before_event_construction() -> None:
    payload = _note_payload("Issue")
    payload["event_type"] = "confidential_note"

    assert _adapter(secrets.token_urlsafe()).normalize(delivery_id="confidential", headers={}, payload=payload) is None


def test_normalize_merge_request_note_is_ignored_for_phase_a() -> None:
    assert (
        _adapter(secrets.token_urlsafe()).normalize(delivery_id="d4", headers={}, payload=_note_payload("MergeRequest"))
        is None
    )


def test_normalize_rejects_disallowed_project() -> None:
    payload = _issue_payload()
    payload["project"] = {"id": PROJECT_ID + 1, "path_with_namespace": "ica/server"}
    assert _adapter(secrets.token_urlsafe()).normalize(delivery_id="d5", headers={}, payload=payload) is None


def test_normalize_ignores_bot_event() -> None:
    payload = _issue_payload()
    payload["user"] = {"id": 9, "username": "robomp"}
    assert _adapter(secrets.token_urlsafe()).normalize(delivery_id="d6", headers={}, payload=payload) is None
