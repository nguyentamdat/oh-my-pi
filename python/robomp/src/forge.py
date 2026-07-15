"""Forge-neutral webhook identities and queue events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol, cast

ForgeKind = Literal["github", "gitlab", "gitea"]
ItemKind = Literal["issue", "change"]
CanonicalEvent = Literal[
    "issue.opened",
    "issue.updated",
    "issue.closed",
    "issue.reopened",
    "issue.comment.created",
    "issue.routed",
    "change.opened",
    "change.closed",
    "change.merged",
    "change.comment.created",
    "change.review.created",
]
TaskKind = Literal[
    "triage_issue",
    "route_issue",
    "routing_complete",
    "handle_comment",
    "handle_pr_conversation",
    "review_change",
    "handle_review",
    "cleanup_workspace",
]
TrustLevel = Literal["unknown", "contributor", "maintainer", "owner", "bot"]


@dataclass(slots=True, frozen=True)
class ForgeInstance:
    id: str
    kind: ForgeKind
    base_url: str
    api_url: str


@dataclass(slots=True, frozen=True)
class RepositoryRef:
    instance_id: str
    remote_id: str
    full_name: str


@dataclass(slots=True, frozen=True)
class WorkItemRef:
    repository: RepositoryRef
    kind: ItemKind
    number: int

    @property
    def key(self) -> str:
        return f"{self.repository.instance_id}:{self.repository.remote_id}:{self.kind}:{self.number}"


@dataclass(slots=True, frozen=True)
class Actor:
    remote_id: str
    login: str
    trust: TrustLevel = "unknown"


@dataclass(slots=True, frozen=True)
class Comment:
    remote_id: str
    author: str
    body: str
    created_at: str = ""


@dataclass(slots=True, frozen=True)
class ForgeEvent:
    delivery_id: str
    source_event: str
    event: CanonicalEvent
    task_kind: TaskKind
    item: WorkItemRef
    actor: Actor
    title: str = ""
    body: str = ""
    labels: tuple[str, ...] = ()
    comment: Comment | None = None
    source_payload: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ForgeEvent:
        """Rebuild one normalized event from durable queue JSON."""
        item_data = _mapping(data.get("item"), "item")
        repo_data = _mapping(item_data.get("repository"), "item.repository")
        actor_data = _mapping(data.get("actor"), "actor")
        comment_data = data.get("comment")
        comment = _mapping(comment_data, "comment") if comment_data is not None else None
        source_payload = data.get("source_payload")
        if source_payload is not None and not isinstance(source_payload, Mapping):
            raise ValueError("source_payload must be an object")
        return cls(
            delivery_id=str(data["delivery_id"]),
            source_event=str(data["source_event"]),
            event=cast(CanonicalEvent, data["event"]),
            task_kind=cast(TaskKind, data["task_kind"]),
            item=WorkItemRef(
                repository=RepositoryRef(
                    instance_id=str(repo_data["instance_id"]),
                    remote_id=str(repo_data["remote_id"]),
                    full_name=str(repo_data["full_name"]),
                ),
                kind=cast(ItemKind, item_data["kind"]),
                number=int(item_data["number"]),
            ),
            actor=Actor(
                remote_id=str(actor_data["remote_id"]),
                login=str(actor_data["login"]),
                trust=cast(TrustLevel, actor_data.get("trust", "unknown")),
            ),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            labels=tuple(str(label) for label in data.get("labels") or ()),
            comment=(
                Comment(
                    remote_id=str(comment["remote_id"]),
                    author=str(comment["author"]),
                    body=str(comment["body"]),
                    created_at=str(comment.get("created_at") or ""),
                )
                if comment is not None
                else None
            ),
            source_payload=source_payload,
        )


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


class WebhookAdapter(Protocol):
    instance: ForgeInstance

    def verify(self, headers: Mapping[str, str], raw_body: bytes) -> str: ...

    def normalize(
        self,
        *,
        delivery_id: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
    ) -> ForgeEvent | None: ...


__all__ = [
    "Actor",
    "CanonicalEvent",
    "Comment",
    "ForgeEvent",
    "ForgeInstance",
    "ForgeKind",
    "ItemKind",
    "RepositoryRef",
    "TaskKind",
    "TrustLevel",
    "WebhookAdapter",
    "WorkItemRef",
]
