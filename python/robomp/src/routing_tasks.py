"""Issue-intake routing without cloning a repository."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from robomp.db import Database
from robomp.forge import ForgeEvent, RepositoryRef, WorkItemRef
from robomp.gitlab_backend import GitLabBackend
from robomp.gitlab_client import GitLabIssueInfo
from robomp.routing import AUTO_ROUTE_CONFIDENCE, RouteCandidate, RouteDecision, RouteTarget, RoutingMode, RoutingPolicy
from robomp.routing_llm import RoutingLLMClassifier

_PATH_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")


class RouteAction(StrEnum):
    """Observable routing outcomes used by the queue and tests."""

    NOOP = "noop"
    NEEDS_HUMAN = "needs_human"
    RECOMMENDED = "recommended"
    MOVED = "moved"
    IMPLEMENTATION_QUEUED = "implementation_queued"
    CHILDREN_QUEUED = "children_queued"


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
    existing_children = db.list_routing_children(event.item.key)
    if existing_children:
        targets = tuple(
            target
            for child in existing_children
            for target in policy.targets
            if target.project_id == int(child.target_project_id)
        )
        if len(targets) != len(existing_children):
            raise RuntimeError("routing child target is absent from the active policy")
        return await _fanout_children(
            db=db,
            event=event,
            issue=issue,
            targets=targets,
            gitlab=gitlab,
            decision=None,
        )
    paths = _extract_paths(issue.title, issue.description)
    decision = policy.classify(issue.title, issue.description, issue.labels, paths)
    if classifier is not None and not decision.explicit:
        contextual = await classifier.classify(issue.title, issue.description, paths)
        decision = _merge_decisions(decision, contextual)
    strong_candidates = tuple(
        candidate for candidate in decision.candidates if candidate.confidence >= AUTO_ROUTE_CONFIDENCE
    )
    fanout_candidates = ()
    if decision.explicit and len(decision.candidates) > 1:
        fanout_candidates = decision.candidates
    elif len(strong_candidates) > 1:
        fanout_candidates = strong_candidates
    if fanout_candidates:
        result = await _fanout_children(
            db=db,
            event=event,
            issue=issue,
            targets=tuple(candidate.target for candidate in fanout_candidates),
            gitlab=gitlab,
            decision=decision,
        )
        return result
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


def _build_target_event(
    *,
    event: ForgeEvent,
    target: RouteTarget,
    target_project_path: str,
    issue: GitLabIssueInfo,
    confidence: float,
    explicit: bool,
    fanout: bool,
) -> ForgeEvent:
    task_kind = "triage_issue" if target.mode is RoutingMode.AUTO_IMPLEMENT else "routing_complete"
    target_labels = ("routed", "roboomp") if target.mode is RoutingMode.AUTO_IMPLEMENT else ("routed",)
    return ForgeEvent(
        delivery_id=f"route:{event.delivery_id}:{target.project_id}:{issue.iid}",
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
            number=issue.iid,
        ),
        actor=event.actor,
        title=issue.title,
        body=issue.description,
        labels=tuple(dict.fromkeys((*issue.labels, *target_labels))),
        source_payload={
            "routing_source": event.item.key,
            "routing_target": target.key,
            "routing_target_path": target_project_path,
            "routing_mode": target.mode.value,
            "routing_fanout": fanout,
            "confidence": confidence,
            "explicit": explicit,
        },
    )


async def _fanout_children(
    *,
    db: Database,
    event: ForgeEvent,
    issue: GitLabIssueInfo,
    targets: tuple[RouteTarget, ...],
    gitlab: GitLabBackend,
    decision: RouteDecision | None,
) -> RouteResult:
    targets = tuple(sorted(targets, key=lambda target: target.project_id))
    existing_plan = {int(child.target_project_id): child for child in db.list_routing_children(event.item.key)}
    projects = {target.project_id: await gitlab.get_project(target.project_id) for target in targets}
    mismatches = [
        (target, projects[target.project_id].default_branch)
        for target in targets
        if target.project_id not in existing_plan
        and projects[target.project_id].default_branch != target.default_branch
    ]
    if mismatches:
        details = ", ".join(
            f"`{projects[target.project_id].path_with_namespace}` reports `{actual}`, "
            f"policy requires `{target.default_branch}`"
            for target, actual in mismatches
        )
        await gitlab.add_issue_labels(int(event.item.repository.remote_id), issue.iid, ["needs-routing"])
        await _post_once(
            gitlab,
            int(event.item.repository.remote_id),
            issue.iid,
            f"RoboOMP multi-project routing paused: {details}.",
        )
        return RouteResult(RouteAction.NEEDS_HUMAN)
    if decision is None:
        recorded = db.list_routing_decisions(event.item.key)
        if not recorded:
            raise RuntimeError("routing child plan has no recorded decision")
        latest = recorded[-1]
        candidates = tuple(
            RouteCandidate(
                target=target,
                score=round(confidence * 100),
                confidence=confidence,
                paths=(),
                aliases=(),
                signals=(),
            )
            for target in targets
            for confidence in (_recorded_decision_metadata(db, event.item.key, target)[0],)
        )
        decision = RouteDecision(
            target=None,
            confidence=max(candidate.confidence for candidate in candidates),
            candidates=candidates,
            explicit=latest.explicit,
        )
    _record_decision(db, event, decision, RouteAction.CHILDREN_QUEUED)
    confidence_by_project = {candidate.target.project_id: candidate.confidence for candidate in decision.candidates}
    explicit = decision.explicit
    planned = db.plan_routing_children(
        event.item.key,
        ((target.project_id, target.mode.value) for target in targets),
    )
    planned_by_project = {int(child.target_project_id): child for child in planned}
    children: list[tuple[RouteTarget, GitLabIssueInfo]] = []
    for target in targets:
        project = projects[target.project_id]
        child = planned_by_project[target.project_id]
        if child.idempotency_token.startswith("legacy:") and child.target_canonical_key is None:
            raise RuntimeError("legacy routing child requires manual ownership reconciliation")
        marker = f"<!-- roboomp-child:{child.idempotency_token} -->"
        if child.target_canonical_key is not None:
            child_issue = await gitlab.get_issue(
                target.project_id,
                int(child.target_canonical_key.rsplit(":", 1)[1]),
            )
            children.append((target, child_issue))
            continue
        child_issue = await gitlab.find_issue_by_marker(target.project_id, marker)
        if child_issue is None:
            child_issue = await gitlab.create_issue(
                target.project_id,
                title=issue.title,
                description=(f"Routed from {issue.web_url}\n\n{issue.description.strip()}\n\n{marker}"),
                labels=["routed"],
            )
        target_event = _build_target_event(
            event=event,
            target=target,
            target_project_path=project.path_with_namespace,
            issue=child_issue,
            confidence=confidence_by_project.get(target.project_id, AUTO_ROUTE_CONFIDENCE),
            explicit=explicit,
            fanout=True,
        )
        db.complete_routing_child_event(
            source_canonical_key=event.item.key,
            target_project_id=target.project_id,
            target_delivery_id=target_event.delivery_id,
            target_event_type=target_event.source_event,
            target_repo=target_event.item.repository.full_name,
            target_issue_key=target_event.item.key,
            target_payload=target_event.to_dict(),
            target_item_kind=target_event.item.kind,
            target_item_number=target_event.item.number,
            target_canonical_event=target_event.event,
            target_task_kind=target_event.task_kind,
            target_instance_id=target_event.item.repository.instance_id,
        )
        children.append((target, child_issue))

    await gitlab.add_issue_labels(int(event.item.repository.remote_id), issue.iid, ["routed"])
    stale_labels = [label for label in issue.labels if label == "needs-routing" or label.startswith("suggest::")]
    if stale_labels:
        await gitlab.remove_issue_labels(int(event.item.repository.remote_id), issue.iid, stale_labels)
    links = "\n".join(
        f"- [`{projects[target.project_id].path_with_namespace}#{child.iid}`]({child.web_url}) — `{target.mode.value}`"
        for target, child in children
    )
    await _post_once(
        gitlab,
        int(event.item.repository.remote_id),
        issue.iid,
        f"RoboOMP created linked child issues:\n\n{links}",
    )
    return RouteResult(RouteAction.CHILDREN_QUEUED, target_event_queued=True)


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


def _merge_decisions(deterministic: RouteDecision, contextual: RouteDecision) -> RouteDecision:
    if not contextual.candidates:
        return deterministic
    candidates_by_key: dict[str, RouteCandidate] = {candidate.key: candidate for candidate in deterministic.candidates}
    for candidate in contextual.candidates:
        existing = candidates_by_key.get(candidate.key)
        if existing is None or candidate.confidence > existing.confidence:
            candidates_by_key[candidate.key] = candidate
    candidates = tuple(sorted(candidates_by_key.values(), key=lambda candidate: (-candidate.confidence, candidate.key)))
    selected = tuple(candidate for candidate in candidates if candidate.confidence >= AUTO_ROUTE_CONFIDENCE)
    target = selected[0].target if len(selected) == 1 else None
    confidence = candidates[0].confidence if candidates else 0.0
    return RouteDecision(target=target, confidence=confidence, candidates=candidates)
