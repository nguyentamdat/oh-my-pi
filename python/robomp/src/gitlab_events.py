"""GitLab 17.5 legacy-webhook adapter."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

from robomp.forge import Actor, Comment, ForgeEvent, ForgeInstance, RepositoryRef, WorkItemRef


class GitLabWebhookAdapter:
    """Verify GitLab's legacy token and normalize supported project events."""

    def __init__(
        self,
        instance: ForgeInstance,
        *,
        webhook_token: str,
        allowed_project_ids: frozenset[int],
        bot_username: str,
        trigger_label: str,
    ) -> None:
        if instance.kind != "gitlab":
            raise ValueError("GitLab adapter requires a GitLab instance")
        self.instance = instance
        self._webhook_token = webhook_token
        self._allowed_project_ids = allowed_project_ids
        self._bot_username = bot_username
        self._trigger_label = trigger_label.casefold()

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> str:
        """Verify ``X-Gitlab-Token`` and return a retry-stable delivery id."""
        if not verify_token(self._webhook_token, _header(headers, "x-gitlab-token")):
            raise PermissionError("invalid GitLab webhook token")
        return (
            _header(headers, "idempotency-key")
            or _header(headers, "x-gitlab-event-uuid")
            or hashlib.sha256(raw_body).hexdigest()
        )

    def normalize(
        self,
        *,
        delivery_id: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
    ) -> ForgeEvent | None:
        """Return a canonical event, or ``None`` for ignored GitLab payloads."""
        if str(payload.get("event_type") or "").casefold() == "confidential_note":
            return None

        project = _mapping(payload.get("project"))
        project_id = project.get("id")
        if not _positive_int(project_id) or project_id not in self._allowed_project_ids:
            return None

        actor_data = _mapping(payload.get("user"))
        actor = _actor(actor_data)
        if _is_bot(actor_data, actor.login, self._bot_username):
            return None

        attributes = _mapping(payload.get("object_attributes"))
        source_event = _header(headers, "x-gitlab-event") or str(
            payload.get("object_kind") or payload.get("event_type") or ""
        )
        repository = RepositoryRef(
            instance_id=self.instance.id,
            remote_id=str(project_id),
            full_name=_string(project.get("path_with_namespace")),
        )
        object_kind = str(payload.get("object_kind") or payload.get("event_type") or "").lower()
        if object_kind == "issue":
            return _normalize_issue(
                delivery_id,
                source_event,
                payload,
                attributes,
                repository,
                actor,
                self._trigger_label,
            )
        if object_kind == "note":
            return _normalize_note(delivery_id, source_event, payload, attributes, repository, actor)
        return None


def verify_token(secret: str, token_header: str | None) -> bool:
    """Constant-time comparison for the GitLab legacy token header."""
    return bool(secret and isinstance(token_header, str) and hmac.compare_digest(secret, token_header))


def _normalize_issue(
    delivery_id: str,
    source_event: str,
    payload: Mapping[str, Any],
    attributes: Mapping[str, Any],
    repository: RepositoryRef,
    actor: Actor,
    trigger_label: str,
) -> ForgeEvent | None:
    iid = attributes.get("iid")
    if not _positive_int(iid):
        return None
    action = str(attributes.get("action") or "").lower()
    labels = _labels(payload.get("labels") or attributes.get("labels"))
    current_labels = {label.casefold() for label in labels}
    if action == "open" and trigger_label in current_labels:
        event, task = "issue.opened", "triage_issue"
    elif action == "update" and _label_added(payload, trigger_label):
        event, task = "issue.updated", "triage_issue"
    elif action == "reopen" and trigger_label in current_labels:
        event, task = "issue.reopened", "triage_issue"
    elif action == "close":
        event, task = "issue.closed", "cleanup_workspace"
    else:
        return None
    return ForgeEvent(
        delivery_id=delivery_id,
        source_event=source_event,
        event=event,
        task_kind=task,
        item=WorkItemRef(repository=repository, kind="issue", number=iid),
        actor=actor,
        title=_string(attributes.get("title")),
        body=_string(attributes.get("description")),
        labels=labels,
        source_payload=payload,
    )


def _normalize_note(
    delivery_id: str,
    source_event: str,
    payload: Mapping[str, Any],
    attributes: Mapping[str, Any],
    repository: RepositoryRef,
    actor: Actor,
) -> ForgeEvent | None:
    if str(attributes.get("action") or "").lower() != "create":
        return None
    note = _string(attributes.get("note")).strip()
    if attributes.get("system") is True or attributes.get("internal") is True or not note:
        return None
    noteable_type = str(attributes.get("noteable_type") or "").lower()
    if noteable_type == "issue":
        item_payload, kind, event, task = (
            _mapping(payload.get("issue")),
            "issue",
            "issue.comment.created",
            "handle_comment",
        )
    elif noteable_type == "mergerequest":
        return None
    else:
        return None
    iid = item_payload.get("iid")
    note_id = attributes.get("id")
    if not _positive_int(iid) or not _positive_int(note_id):
        return None
    return ForgeEvent(
        delivery_id=delivery_id,
        source_event=source_event,
        event=event,
        task_kind=task,
        item=WorkItemRef(repository=repository, kind=kind, number=iid),
        actor=actor,
        comment=Comment(
            remote_id=str(note_id),
            author=actor.login,
            body=note,
            created_at=_string(attributes.get("created_at")),
        ),
        source_payload=payload,
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.casefold() == name:
            return value
    return None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        label if isinstance(label, str) else _string(label.get("title") or label.get("name"))
        for label in value
        if isinstance(label, str) or isinstance(label, Mapping)
    )


def _label_added(payload: Mapping[str, Any], trigger_label: str) -> bool:
    labels_change = _mapping(_mapping(payload.get("changes")).get("labels"))
    previous = {label.casefold() for label in _labels(labels_change.get("previous"))}
    current = {label.casefold() for label in _labels(labels_change.get("current"))}
    return trigger_label not in previous and trigger_label in current


def _actor(user: Mapping[str, Any]) -> Actor:
    return Actor(remote_id=str(user.get("id") or ""), login=_string(user.get("username")))


def _is_bot(user: Mapping[str, Any], login: str, bot_username: str) -> bool:
    return bool(
        user.get("bot")
        or str(user.get("user_type") or "").casefold() == "bot"
        or login.casefold().endswith("[bot]")
        or (login and bot_username and login.casefold() == bot_username.casefold())
    )


__all__ = ["GitLabWebhookAdapter", "verify_token"]
