"""Issue-intake routing without cloning a repository."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from robomp.db import Database
from robomp.forge import ForgeEvent, RepositoryRef, WorkItemRef
from robomp.gitlab_backend import GitLabBackend
from robomp.gitlab_client import GitLabIssueInfo
from robomp.routing import AUTO_ROUTE_CONFIDENCE, RouteDecision, RouteTarget, RoutingMode, RoutingPolicy
from robomp.routing_llm import RoutingLLMClassifier

_PATH_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")


class RouteAction(StrEnum):
    """Observable routing outcomes used by the queue and tests."""

    NOOP = "noop"
    NEEDS_HUMAN = "needs_human"
    RECOMMENDED = "recommended"
    MOVED = "moved"
    IMPLEMENTATION_QUEUED = "implementation_queued"


@dataclass(frozen=True, slots=True)
class RouteResult:
    action: RouteAction
    target: RouteTarget | None = None
    target_event_queued: bool = False


async def route_issue(
    *,
    event: ForgeEvent,
    policy: RoutingPolicy,
    db: Database,
    gitlab: GitLabBackend,
    classifier: RoutingLLMClassifier | None = None,
) -> RouteResult:
    """Classify one intake issue, then recommend, move, or enqueue implementation."""
    if event.item.repository.remote_id != str(policy.intake_project_id):
        raise ValueError("route_issue event is not from the configured intake project")
    if db.resolve_routing_lineage(event.item.key) is not None:
        return RouteResult(RouteAction.NOOP)

    incomplete = db.get_incomplete_routing_intent(event.item.key)
    if incomplete is not None:
        target = next(
            (candidate for candidate in policy.targets if candidate.project_id == int(incomplete.target_project_id)),
            None,
        )
        if target is None:
            raise RuntimeError("routing intent target is absent from the active policy")
        target_project = await gitlab.get_project(target.project_id)
        moved = await gitlab.resolve_moved_issue(
            policy.intake_project_id,
            event.item.number,
            target.project_id,
        )
        if moved is None:
            moved = await gitlab.move_issue(policy.intake_project_id, event.item.number, target.project_id)
        confidence, explicit = _recorded_decision_metadata(db, event.item.key, target)
        return _complete_move(
            db=db,
            event=event,
            target=target,
            target_project_path=target_project.path_with_namespace,
            moved=moved,
            confidence=confidence,
            explicit=explicit,
        )

    issue = await gitlab.get_issue(policy.intake_project_id, event.item.number)
    paths = _extract_paths(issue.title, issue.description)
    decision = policy.classify(issue.title, issue.description, issue.labels, paths)
    if classifier is not None and not decision.explicit:
        decision = await classifier.classify(issue.title, issue.description, paths)
    if decision.target is None:
        _record_decision(db, event, decision, RouteAction.NEEDS_HUMAN)
        await _recommend(
            gitlab,
            policy.intake_project_id,
            issue.iid,
            decision,
            require_human=True,
        )
        return RouteResult(RouteAction.NEEDS_HUMAN)

    target = decision.target
    if target.mode is RoutingMode.RECOMMEND or not decision.auto_route:
        _record_decision(db, event, decision, RouteAction.RECOMMENDED)
        await _recommend(
            gitlab,
            policy.intake_project_id,
            issue.iid,
            decision,
            require_human=True,
        )
        return RouteResult(RouteAction.RECOMMENDED, target)

    target_project = await gitlab.get_project(target.project_id)
    if target_project.default_branch != target.default_branch:
        _record_decision(db, event, decision, RouteAction.NEEDS_HUMAN)
        await _post_once(
            gitlab,
            policy.intake_project_id,
            issue.iid,
            (
                f"RoboOMP routing paused: `ica/{target.key}` currently reports default branch "
                f"`{target_project.default_branch}`, but policy requires `{target.default_branch}`. "
                "A maintainer must update the routing policy before this issue can move."
            ),
        )
        await gitlab.add_issue_labels(policy.intake_project_id, issue.iid, ["needs-routing"])
        return RouteResult(RouteAction.NEEDS_HUMAN, target)

    action = RouteAction.IMPLEMENTATION_QUEUED if target.mode is RoutingMode.AUTO_IMPLEMENT else RouteAction.MOVED
    _record_decision(db, event, decision, action)
    db.begin_routing_intent(event.item.key, target.project_id)
    moved = await gitlab.resolve_moved_issue(
        policy.intake_project_id,
        issue.iid,
        target.project_id,
    )
    if moved is None:
        moved = await gitlab.move_issue(policy.intake_project_id, issue.iid, target.project_id)
    return _complete_move(
        db=db,
        event=event,
        target=target,
        target_project_path=target_project.path_with_namespace,
        moved=moved,
        confidence=decision.confidence,
        explicit=decision.explicit,
    )


def _complete_move(
    *,
    db: Database,
    event: ForgeEvent,
    target: RouteTarget,
    target_project_path: str,
    moved: GitLabIssueInfo,
    confidence: float,
    explicit: bool,
) -> RouteResult:
    task_kind = "triage_issue" if target.mode is RoutingMode.AUTO_IMPLEMENT else "routing_complete"
    action = RouteAction.IMPLEMENTATION_QUEUED if target.mode is RoutingMode.AUTO_IMPLEMENT else RouteAction.MOVED
    target_labels = ("routed", "roboomp") if target.mode is RoutingMode.AUTO_IMPLEMENT else ("routed",)
    target_event = ForgeEvent(
        delivery_id=f"route:{event.delivery_id}:{target.project_id}:{moved.iid}",
        source_event="RoboOMP Route",
        event="issue.routed",
        task_kind=task_kind,
        item=WorkItemRef(
            repository=RepositoryRef(
                instance_id=event.item.repository.instance_id,
                remote_id=str(target.project_id),
                full_name=target_project_path,
            ),
            kind="issue",
            number=moved.iid,
        ),
        actor=event.actor,
        title=moved.title,
        body=moved.description,
        labels=tuple(dict.fromkeys((*moved.labels, *target_labels))),
        source_payload={
            "routing_source": event.item.key,
            "routing_target": target.key,
            "routing_target_path": target_project_path,
            "routing_mode": target.mode.value,
            "confidence": confidence,
            "explicit": explicit,
        },
    )
    db.complete_routing_intent_event(
        source_canonical_key=event.item.key,
        target_delivery_id=target_event.delivery_id,
        target_event_type=target_event.source_event,
        target_repo=target_event.item.repository.full_name,
        target_issue_key=target_event.item.key,
        target_payload=target_event.to_dict(),
        target_repository_id=target_event.item.repository.remote_id,
        target_item_kind=target_event.item.kind,
        target_item_number=target_event.item.number,
        target_canonical_event=target_event.event,
        target_task_kind=target_event.task_kind,
        target_instance_id=target_event.item.repository.instance_id,
    )
    return RouteResult(action, target, target_event_queued=True)


def _recorded_decision_metadata(
    db: Database,
    source_canonical_key: str,
    target: RouteTarget,
) -> tuple[float, bool]:
    decisions = db.list_routing_decisions(source_canonical_key)
    if not decisions:
        return AUTO_ROUTE_CONFIDENCE, False
    latest = decisions[-1]
    confidence = next(
        (
            float(candidate.get("confidence") or 0.0)
            for candidate in latest.candidates
            if str(candidate.get("project_id")) == str(target.project_id)
        ),
        AUTO_ROUTE_CONFIDENCE,
    )
    return confidence, latest.explicit


async def apply_target_routing(event: ForgeEvent, gitlab: GitLabBackend) -> None:
    """Apply retryable target labels and audit note before target dispatch."""
    project_id = int(event.item.repository.remote_id)
    target_labels = ["routed"]
    if event.task_kind == "triage_issue":
        target_labels.append("roboomp")
    await gitlab.add_issue_labels(project_id, event.item.number, target_labels)
    await gitlab.remove_issue_labels(project_id, event.item.number, ["needs-routing"])
    confidence = float(event.source_payload.get("confidence") or 0.0)
    target_path = str(event.source_payload.get("routing_target_path") or event.item.repository.full_name)
    mode = str(event.source_payload.get("routing_mode") or "")
    await _post_once(
        gitlab,
        project_id,
        event.item.number,
        (
            f"RoboOMP routed this issue from `ica/triage` to `{target_path}` "
            f"with confidence {confidence:.2f}. "
            + (
                "Implementation has been queued for this project."
                if mode == RoutingMode.AUTO_IMPLEMENT.value
                else "Implementation remains disabled by this project's routing policy."
            )
        ),
    )


def _record_decision(
    db: Database,
    event: ForgeEvent,
    decision: RouteDecision,
    action: RouteAction,
) -> None:
    selected = decision.target
    mode = selected.mode.value if selected is not None else "none"
    db.record_routing_decision(
        instance_id=event.item.repository.instance_id,
        delivery_id=event.delivery_id,
        source_canonical_key=event.item.key,
        ranked_candidates=(
            {
                "key": candidate.key,
                "project_id": candidate.project_id,
                "score": candidate.score,
                "confidence": candidate.confidence,
            }
            for candidate in decision.candidates
        ),
        selected_target_key=selected.key if selected is not None else None,
        selected_project_id=selected.project_id if selected is not None else None,
        explicit=decision.explicit,
        action=action.value,
        mode=mode,
    )


async def _recommend(
    gitlab: GitLabBackend,
    project_id: int,
    iid: int,
    decision: RouteDecision,
    *,
    require_human: bool,
) -> None:
    candidates = decision.candidates[:3]
    labels = ["needs-routing"] if require_human else []
    labels.extend(f"suggest::{candidate.key}" for candidate in candidates)
    if labels:
        await gitlab.add_issue_labels(project_id, iid, labels)
    if not candidates:
        body = (
            "RoboOMP could not identify an ICA project from the current issue content. "
            "Add a `route::<project>` label or include an affected component, class, package, or file path."
        )
    else:
        ranked = "\n".join(
            f"{index}. `ica/{candidate.key}` — confidence {candidate.confidence:.2f}"
            for index, candidate in enumerate(candidates, start=1)
        )
        body = (
            f"RoboOMP routing recommendation:\n\n{ranked}\n\n"
            "A maintainer must apply exactly one `route::<project>` label to confirm an ambiguous or "
            "recommend-only route."
        )
    await _post_once(gitlab, project_id, iid, body)


async def _post_once(gitlab: GitLabBackend, project_id: int, iid: int, body: str) -> None:
    notes = await gitlab.list_issue_notes(project_id, iid)
    if any(not note.system and note.body == body for note in notes):
        return
    await gitlab.post_issue_note(project_id, iid, body)


def _extract_paths(title: str, body: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_PATH_RE.findall(f"{title}\n{body}")))


__all__ = ["RouteAction", "RouteResult", "route_issue"]
