"""Focused contract tests for deterministic cross-project routing."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from robomp.config import Settings, reset_settings_cache
from robomp.routing import AUTO_ROUTE_CONFIDENCE, RoutingMode, RoutingPolicy


def _policy_data() -> dict[str, object]:
    return {
        "intake_project_id": 100,
        "targets": [
            {
                "key": "web",
                "project_id": 200,
                "mode": "auto_move",
                "default_branch": "main",
                "paths": ["apps/web"],
                "aliases": ["web platform"],
                "signals": ["browser client"],
            },
            {
                "key": "mobile",
                "project_id": 300,
                "mode": "recommend",
                "default_branch": "main",
                "paths": ["apps/mobile"],
                "aliases": ["mobile platform"],
                "signals": ["android client"],
            },
        ],
    }


def _policy() -> RoutingPolicy:
    return RoutingPolicy.from_json(json.dumps(_policy_data()))


def test_classify_explicit_human_route_label() -> None:
    decision = _policy().classify("Unrelated title", "No routing hints", ["route::mobile"])

    assert decision.target is not None
    assert decision.target.key == "mobile"
    assert decision.mode is RoutingMode.RECOMMEND
    assert decision.confidence == 1.0
    assert decision.explicit
    assert not decision.auto_route


def test_classify_unique_strong_phrase_exceeds_auto_threshold() -> None:
    decision = _policy().classify("Browser-client rendering error", "The WEB platform cannot load.")

    assert decision.target is not None
    assert decision.target.key == "web"
    assert decision.confidence > AUTO_ROUTE_CONFIDENCE
    assert decision.auto_route
    assert decision.candidates[0].aliases == ("web platform",)
    assert decision.candidates[0].signals == ("browser client",)


def test_classify_path_matching_uses_component_boundaries() -> None:
    policy = RoutingPolicy.from_json(
        json.dumps(
            {
                "intake_project_id": 100,
                "targets": [
                    {
                        "key": "foo",
                        "project_id": 200,
                        "mode": "auto_move",
                        "default_branch": "main",
                        "paths": ["libs/foo"],
                        "aliases": ["foo library"],
                        "signals": ["foo module"],
                    },
                    {
                        "key": "foobar",
                        "project_id": 300,
                        "mode": "auto_move",
                        "default_branch": "main",
                        "paths": ["libs/foobar"],
                        "aliases": ["foobar library"],
                        "signals": ["foobar module"],
                    },
                ],
            }
        )
    )

    decision = policy.classify("Dependency update", paths=["libs/foobar/adapter.py"])

    assert decision.target is not None
    assert decision.target.key == "foobar"
    assert [candidate.key for candidate in decision.candidates] == ["foobar"]
    assert decision.candidates[0].paths == ("libs/foobar",)


def test_classify_cross_project_text_is_ambiguous() -> None:
    decision = _policy().classify("web platform and mobile platform integration")

    assert decision.target is None
    assert decision.ambiguous
    assert [candidate.key for candidate in decision.candidates] == ["mobile", "web"]
    assert decision.confidence > AUTO_ROUTE_CONFIDENCE


def test_classify_returns_no_match_without_policy_evidence() -> None:
    decision = _policy().classify("Question about release dates", "No product vocabulary here.")

    assert decision.target is None
    assert not decision.ambiguous
    assert decision.confidence == 0.0
    assert decision.candidates == ()


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        json.dumps({"intake_project_id": 100, "targets": []}),
    ],
)
def test_policy_rejects_malformed_json(raw: str) -> None:
    with pytest.raises(ValueError):
        RoutingPolicy.from_json(raw)


def test_policy_rejects_duplicate_target_key_and_project_id() -> None:
    duplicate_key = deepcopy(_policy_data())
    duplicate_key["targets"].append(  # type: ignore[index,union-attr]
        {
            "key": "web",
            "project_id": 400,
            "mode": "recommend",
            "default_branch": "main",
            "paths": ["other"],
            "aliases": ["other project"],
            "signals": ["other signal"],
        }
    )
    with pytest.raises(ValueError, match="keys must be unique"):
        RoutingPolicy.from_json(json.dumps(duplicate_key))

    duplicate_project = deepcopy(_policy_data())
    duplicate_project["targets"][1]["project_id"] = 200  # type: ignore[index]
    with pytest.raises(ValueError, match="project IDs must be unique"):
        RoutingPolicy.from_json(json.dumps(duplicate_project))


def test_policy_rejects_intake_as_target_and_invalid_target_values() -> None:
    intake_target = deepcopy(_policy_data())
    intake_target["targets"][0]["project_id"] = 100  # type: ignore[index]
    with pytest.raises(ValueError, match="must not be a route target"):
        RoutingPolicy.from_json(json.dumps(intake_target))

    invalid_target = deepcopy(_policy_data())
    invalid_target["targets"][0]["aliases"] = [""]  # type: ignore[index]
    with pytest.raises(ValueError, match="non-empty text"):
        RoutingPolicy.from_json(json.dumps(invalid_target))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("mode", "move", "invalid mode"),
        ("project_id", 0, "positive integer"),
        ("default_branch", "release branch", "invalid default_branch"),
        ("paths", ["../outside"], "invalid path"),
        ("signals", [], "signals must be a non-empty array"),
    ],
)
def test_policy_validates_mode_branch_paths_and_signals(field: str, value: object, message: str) -> None:
    invalid_target = deepcopy(_policy_data())
    invalid_target["targets"][0][field] = value  # type: ignore[index]

    with pytest.raises(ValueError, match=message):
        RoutingPolicy.from_json(json.dumps(invalid_target))


def test_settings_parses_policy_and_rejects_malformed_policy(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROBOMP_GITLAB_BASE_URL", "https://gitlab.example.com")
    monkeypatch.setenv("ROBOMP_GITLAB_WEBHOOK_SECRET", "routing-webhook-secret")
    monkeypatch.setenv("ROBOMP_GITLAB_BOT_LOGIN", "routing-bot")
    monkeypatch.setenv("ROBOMP_GITLAB_PROJECT_IDS", "100,200,300")
    monkeypatch.setenv("ROBOMP_GITLAB_ROUTING_POLICY", json.dumps(_policy_data()))
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]

    assert cfg.gitlab_routing_policy is not None
    assert cfg.gitlab_routing_policy.intake_project_id == 100

    monkeypatch.setenv("ROBOMP_GITLAB_ROUTING_POLICY", "not-json")
    reset_settings_cache()
    with pytest.raises(ValidationError, match="invalid routing policy JSON"):
        Settings()  # type: ignore[call-arg]


def test_settings_rejects_routing_projects_outside_ingress_allowlist(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROBOMP_GITLAB_BASE_URL", "https://gitlab.example.com")
    monkeypatch.setenv("ROBOMP_GITLAB_WEBHOOK_SECRET", "routing-webhook-secret")
    monkeypatch.setenv("ROBOMP_GITLAB_BOT_LOGIN", "routing-bot")
    monkeypatch.setenv("ROBOMP_GITLAB_ROUTING_POLICY", json.dumps(_policy_data()))
    monkeypatch.setenv("ROBOMP_GITLAB_PROJECT_IDS", "100,200")
    reset_settings_cache()

    with pytest.raises(ValidationError, match=r"missing \[300\]"):
        Settings()  # type: ignore[call-arg]
