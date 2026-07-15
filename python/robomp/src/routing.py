"""Deterministic, policy-driven GitLab project routing.

The policy deliberately has no side effects: it only turns an issue's text and
labels into an explainable route recommendation.  Moving an issue or starting
work remains the responsibility of the caller.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

AUTO_ROUTE_CONFIDENCE = 0.85
_KEY_RE = re.compile(r"[a-z][a-z0-9_-]*")
_BRANCH_RE = re.compile(r"[^\s~^:?*\[\\]+")


class RoutingMode(StrEnum):
    """What an accepted route is allowed to do."""

    RECOMMEND = "recommend"
    AUTO_MOVE = "auto_move"
    AUTO_IMPLEMENT = "auto_implement"


@dataclass(frozen=True, slots=True)
class RouteTarget:
    """One allowlisted destination project in a routing policy."""

    key: str
    project_id: int
    mode: RoutingMode
    default_branch: str
    paths: tuple[str, ...]
    aliases: tuple[str, ...]
    signals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    """A target supported by explicit label, path, or text evidence."""

    target: RouteTarget
    score: int
    confidence: float
    paths: tuple[str, ...]
    aliases: tuple[str, ...]
    signals: tuple[str, ...]

    @property
    def key(self) -> str:
        return self.target.key

    @property
    def project_id(self) -> int:
        return self.target.project_id


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """The deterministic outcome of :meth:`RoutingPolicy.classify`.

    ``target`` is populated only when the highest-ranked candidate is unique
    and has at least strong evidence.  Ambiguous and weak outcomes retain their
    candidates so callers can present an auditable recommendation to a human.
    """

    target: RouteTarget | None
    confidence: float
    candidates: tuple[RouteCandidate, ...]
    explicit: bool = False

    @property
    def mode(self) -> RoutingMode | None:
        return self.target.mode if self.target is not None else None

    @property
    def ambiguous(self) -> bool:
        return self.target is None and bool(self.candidates)

    @property
    def auto_route(self) -> bool:
        """Whether classification is confident enough for an automatic action."""
        return (
            self.target is not None
            and self.target.mode is not RoutingMode.RECOMMEND
            and self.confidence >= AUTO_ROUTE_CONFIDENCE
        )


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """Immutable policy for routing one intake project to known targets."""

    intake_project_id: int
    targets: tuple[RouteTarget, ...]

    @classmethod
    def from_json(cls, raw: str | bytes | Mapping[str, Any]) -> RoutingPolicy:
        """Parse and strictly validate a routing policy JSON object.

        Expected shape::

            {
              "intake_project_id": 10,
              "targets": [{
                "key": "web",
                "project_id": 20,
                "mode": "auto_move",
                "default_branch": "main",
                "paths": ["src/web"],
                "aliases": ["web application"],
                "signals": ["browser"]
              }]
            }
        """
        data = _json_object(raw, "routing policy")
        _require_exact_keys(data, {"intake_project_id", "targets"}, "routing policy")
        intake_project_id = _positive_project_id(data["intake_project_id"], "intake_project_id")
        targets_data = data["targets"]
        if not isinstance(targets_data, list) or not targets_data:
            raise ValueError("routing policy targets must be a non-empty array")

        targets = tuple(_parse_target(item, index) for index, item in enumerate(targets_data))
        target_keys = [target.key for target in targets]
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("routing target keys must be unique")
        target_project_ids = [target.project_id for target in targets]
        if len(target_project_ids) != len(set(target_project_ids)):
            raise ValueError("routing target project IDs must be unique")
        if intake_project_id in target_project_ids:
            raise ValueError("routing intake project ID must not be a route target")
        return cls(intake_project_id=intake_project_id, targets=targets)

    def target_for_key(self, key: str) -> RouteTarget | None:
        """Return the canonical target for a policy key, case-insensitively."""
        normalized = key.strip().casefold()
        return next((target for target in self.targets if target.key == normalized), None)

    def classify(
        self,
        title: str,
        body: str = "",
        labels: Iterable[str] = (),
        paths: Iterable[str] = (),
    ) -> RouteDecision:
        """Classify issue content without probabilistic or order-dependent behavior.

        ``route::<key>`` labels take precedence. Repository-relative paths
        match only at a path-component boundary and are stronger evidence than
        generic aliases or signals. Text aliases and signals use normalized
        complete words/phrases; one-word text markers remain weak evidence.
        """
        explicit_targets = {
            target
            for label in labels
            if isinstance(label, str)
            for target in (self.target_for_key(_explicit_key(label)),)
            if target is not None
        }
        if explicit_targets:
            candidates = tuple(
                RouteCandidate(target, score=0, confidence=1.0, paths=(), aliases=(), signals=())
                for target in sorted(explicit_targets, key=lambda target: target.key)
            )
            if len(candidates) == 1:
                return RouteDecision(candidates[0].target, 1.0, candidates, explicit=True)
            return RouteDecision(None, 1.0, candidates, explicit=True)

        route_paths = tuple(
            normalized for path in paths if isinstance(path, str) and (normalized := _normalized_path(path)) is not None
        )
        words = _words(f"{title}\n{body}")
        candidates = tuple(
            candidate for target in self.targets if (candidate := _candidate(target, words, route_paths)) is not None
        )
        candidates = tuple(sorted(candidates, key=lambda candidate: (-candidate.score, candidate.key)))
        if not candidates:
            return RouteDecision(None, 0.0, ())

        best = candidates[0]
        tied = len(candidates) > 1 and candidates[1].score == best.score
        if best.confidence >= AUTO_ROUTE_CONFIDENCE and not tied:
            return RouteDecision(best.target, best.confidence, candidates)
        return RouteDecision(None, best.confidence, candidates)


def _parse_target(value: object, index: int) -> RouteTarget:
    target = _json_object(value, f"routing target at index {index}")
    _require_exact_keys(
        target,
        {"key", "project_id", "mode", "default_branch", "paths", "aliases", "signals"},
        f"routing target at index {index}",
    )
    key = target["key"]
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise ValueError(f"routing target at index {index} has an invalid key")
    try:
        mode = RoutingMode(target["mode"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"routing target {key!r} has an invalid mode") from exc
    default_branch = _branch(target["default_branch"], key)
    return RouteTarget(
        key=key,
        project_id=_positive_project_id(target["project_id"], f"routing target {key!r} project_id"),
        mode=mode,
        default_branch=default_branch,
        paths=_paths(target["paths"], key),
        aliases=_phrases(target["aliases"], key, "aliases"),
        signals=_phrases(target["signals"], key, "signals"),
    )


def _json_object(raw: object, name: str) -> Mapping[str, Any]:
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid {name} JSON") from exc
    if not isinstance(raw, Mapping):
        raise ValueError(f"{name} must be an object")
    return raw


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise ValueError(f"{name} fields are invalid ({'; '.join(details)})")


def _positive_project_id(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _branch(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"routing target {key!r} default_branch must be a string")
    branch = value.strip()
    if (
        not branch
        or branch.startswith("/")
        or branch.endswith("/")
        or ".." in branch
        or not _BRANCH_RE.fullmatch(branch)
    ):
        raise ValueError(f"routing target {key!r} has an invalid default_branch")
    return branch


def _paths(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"routing target {key!r} paths must be a non-empty array")
    paths: list[str] = []
    for path in value:
        if not isinstance(path, str):
            raise ValueError(f"routing target {key!r} paths must contain strings")
        cleaned = path.strip()
        parts = cleaned.split("/")
        if not cleaned or cleaned.startswith("/") or "\\" in cleaned or any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"routing target {key!r} has an invalid path")
        paths.append(cleaned)
    if len(paths) != len(set(paths)):
        raise ValueError(f"routing target {key!r} paths must be unique")
    return tuple(paths)


def _phrases(value: object, key: str, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"routing target {key!r} {field} must be a non-empty array")
    phrases: list[str] = []
    normalized: set[tuple[str, ...]] = set()
    for phrase in value:
        if not isinstance(phrase, str) or not (words := _words(phrase)):
            raise ValueError(f"routing target {key!r} {field} must contain non-empty text")
        if words in normalized:
            raise ValueError(f"routing target {key!r} {field} must be unique")
        normalized.add(words)
        phrases.append(" ".join(words))
    return tuple(phrases)


def _words(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE))


def _explicit_key(label: str) -> str:
    prefix = "route::"
    cleaned = label.strip().casefold()
    return cleaned[len(prefix) :] if cleaned.startswith(prefix) else ""


def _candidate(
    target: RouteTarget,
    words: tuple[str, ...],
    route_paths: tuple[str, ...],
) -> RouteCandidate | None:
    matched_paths = tuple(
        target_path for target_path in target.paths if any(_path_matches(target_path, path) for path in route_paths)
    )
    aliases = tuple(alias for alias in target.aliases if _contains_phrase(words, _words(alias)))
    signals = tuple(signal for signal in target.signals if _contains_phrase(words, _words(signal)))
    if not matched_paths and not aliases and not signals:
        return None
    # A path match identifies code owned by the target, so it outweighs text
    # evidence. One-token markers are deliberately weak; phrases contain at
    # least two normalized words and therefore contribute stronger evidence.
    text_score = sum(len(_words(marker)) for marker in dict.fromkeys((*aliases, *signals)))
    score = (4 * len(matched_paths)) + text_score
    confidence = min(0.95, 0.5 + (score * 0.2))
    return RouteCandidate(target, score, confidence, matched_paths, aliases, signals)


def _normalized_path(value: str) -> str | None:
    path = value.strip()
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith("/") or "\\" in path:
        return None
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return path


def _path_matches(target_path: str, changed_path: str) -> bool:
    return changed_path == target_path or changed_path.startswith(f"{target_path}/")


def _contains_phrase(words: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    width = len(phrase)
    return any(words[offset : offset + width] == phrase for offset in range(len(words) - width + 1))
