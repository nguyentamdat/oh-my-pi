"""Dispatch action -> task mapping in WorkerPool._dispatch.

Regression guard for the route<->dispatch contract: `github_events.route`
queues a `pull_request.labeled` event as a `review_pr` task in `vouched_label`
mode, so `_dispatch` MUST invoke `tasks.review_pr` for that action. It
previously only handled `opened/reopened/ready_for_review`, so every vouched
PR fell through to the no-op branch and was silently marked `done`.
"""

from __future__ import annotations

import pytest

from robomp import tasks
from robomp.config import Settings
from robomp.db import Database, EventRow
from robomp.forge import Actor, ForgeEvent, RepositoryRef, WorkItemRef
from robomp.queue import ForgeRuntime, WorkerPool
from robomp.slot_pool import SlotPool


class _StubGitHub:
    """Sentinel; dispatch tests stub out the task body."""


class _StubSandbox:
    natives_cache = None


class _StubGitTransport:
    pass


def _make_pool(
    settings: Settings,
    db: Database,
    *,
    forge_runtime: ForgeRuntime | None = None,
) -> WorkerPool:
    factories = {"gitlab-zingplay": lambda _row: forge_runtime} if forge_runtime is not None else None
    return WorkerPool(
        settings=settings,
        db=db,
        github=_StubGitHub(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        git_transport=_StubGitTransport(),  # type: ignore[arg-type]
        slot_pool=SlotPool(),
        forge_runtime_factories=factories,  # type: ignore[arg-type]
    )


def _pr_row(action: str, *, delivery: str = "pr1") -> EventRow:
    return EventRow(
        delivery_id=delivery,
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#7",
        payload={"action": action, "pull_request": {"number": 7}},
        received_at="2026-01-01T00:00:00Z",
        state="running",
        attempts=1,
        last_error=None,
    )


@pytest.mark.parametrize("action", ["opened", "reopened", "ready_for_review", "labeled"])
@pytest.mark.asyncio
async def test_dispatch_routes_pr_review_actions_to_review_pr(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """Every PR action `route` can queue for review MUST reach `tasks.review_pr`.

    `labeled` is the vouched-label trigger; the others are the `open` trigger.
    """
    seen: list[str] = []

    async def fake_review_pr(*, payload, **_kwargs) -> None:
        seen.append(str(payload.get("action")))

    monkeypatch.setattr(tasks, "review_pr", fake_review_pr)

    await _make_pool(settings, db)._dispatch(_pr_row(action))  # noqa: SLF001

    assert seen == [action]


@pytest.mark.asyncio
async def test_dispatch_prefers_persisted_canonical_task_kind(
    settings: Settings,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def fake_triage_issue(*, forge_event, **_kwargs) -> None:
        seen.append(forge_event.item.key)

    monkeypatch.setattr(tasks, "triage_issue", fake_triage_issue)
    event = ForgeEvent(
        delivery_id="gitlab-1",
        source_event="Issue Hook",
        event="issue.opened",
        task_kind="triage_issue",
        item=WorkItemRef(
            repository=RepositoryRef(
                instance_id="gitlab-zingplay",
                remote_id="356",
                full_name="ica/server",
            ),
            kind="issue",
            number=42,
        ),
        actor=Actor(remote_id="7", login="alice"),
        labels=("roboomp",),
    )
    row = EventRow(
        delivery_id=event.delivery_id,
        event_type=event.source_event,
        repo="ica/server",
        issue_key=event.item.key,
        payload=event.to_dict(),
        received_at="2026-01-01T00:00:00Z",
        state="running",
        attempts=1,
        last_error=None,
        instance_id="gitlab-zingplay",
        repository_id="356",
        item_kind="issue",
        item_number=42,
        canonical_key=event.item.key,
        canonical_event=event.event,
        task_kind=event.task_kind,
    )
    runtime = ForgeRuntime(
        backend=_StubGitHub(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        git_transport=_StubGitTransport(),  # type: ignore[arg-type]
    )

    await _make_pool(settings, db, forge_runtime=runtime)._dispatch(row)  # noqa: SLF001

    assert seen == ["gitlab-zingplay:356:issue:42"]


@pytest.mark.asyncio
async def test_dispatch_pr_synchronize_is_noop(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actions `route` never queues for review must NOT spawn a review task."""
    called = False

    async def fake_review_pr(**_kwargs) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(tasks, "review_pr", fake_review_pr)

    await _make_pool(settings, db)._dispatch(_pr_row("synchronize"))  # noqa: SLF001

    assert called is False
