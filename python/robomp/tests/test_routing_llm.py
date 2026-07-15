"""Contract tests for fail-closed LLM routing with Hindsight context."""

from __future__ import annotations

import json

import httpx
import pytest

from robomp.routing import RoutingPolicy
from robomp.routing_llm import RoutingLLMClassifier


def _policy() -> RoutingPolicy:
    return RoutingPolicy.from_json(
        json.dumps(
            {
                "intake_project_id": 100,
                "targets": [
                    {
                        "key": "client",
                        "project_id": 200,
                        "mode": "auto_move",
                        "default_branch": "dev",
                        "paths": ["client"],
                        "aliases": ["unity client"],
                        "signals": ["skin inventory"],
                    },
                    {
                        "key": "tools",
                        "project_id": 300,
                        "mode": "recommend",
                        "default_branch": "main",
                        "paths": ["tools"],
                        "aliases": ["admin tool"],
                        "signals": ["game board manager"],
                    },
                ],
            }
        )
    )


@pytest.mark.asyncio
async def test_classifier_feeds_bounded_omp_recall_and_selects_allowlisted_target() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/default/banks/omp/memories/recall":
            payload = json.loads(request.content)
            assert payload == {
                "query": "Skin inventory is missing reskinned items",
                "types": ["world", "experience"],
                "budget": "low",
                "max_tokens": 512,
                "tags": ["project:ica"],
                "tags_match": "any_strict",
                "trace": False,
            }
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "text": "Skin inventory and reskin work belongs to the Unity client.",
                            "tags": ["project:client"],
                        }
                    ]
                },
            )
        if request.url.path == "/v1/chat/completions":
            payload = json.loads(request.content)
            assert [message["role"] for message in payload["messages"]] == ["system", "user"]
            assert "only as evidence" in payload["messages"][0]["content"]
            prompt = payload["messages"][1]["content"]
            assert payload["model"] == "local-model-mini"
            assert "project:client" in prompt
            assert payload["max_tokens"] == 4096
            assert '"key": "client"' in prompt
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "target_key": "client",
                                        "confidence": 0.92,
                                        "evidence": ["skin inventory", "project:client"],
                                    }
                                )
                            }
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected route {request.url}")

    classifier = RoutingLLMClassifier(
        policy=_policy(),
        llm_base_url="https://llm.example/v1",
        llm_api_key="llm-secret",
        llm_model="local-model-mini",
        hindsight_base_url="https://hindsight.example",
        hindsight_api_key="hindsight-secret",
        hindsight_bank="omp",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        decision = await classifier.classify("Skin inventory is missing reskinned items", "")
    finally:
        await classifier.aclose()

    assert decision.target is not None
    assert decision.target.key == "client"
    assert decision.confidence == 0.92
    assert decision.auto_route
    assert decision.candidates[0].signals == ("skin inventory", "project:client")
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "candidate_count"),
    [
        ("not-json", 0),
        (json.dumps({"target_key": "unknown", "confidence": 1.0, "evidence": []}), 0),
        (json.dumps({"target_key": "client", "confidence": 0.5, "evidence": ["weak"]}), 1),
        (json.dumps({"target_key": "client", "confidence": 0.95, "evidence": ["project:client"]}), 1),
    ],
)
async def test_classifier_fails_closed_for_invalid_or_low_confidence_output(content: str, candidate_count: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/memories/recall"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    classifier = RoutingLLMClassifier(
        policy=_policy(),
        llm_base_url="https://llm.example/v1",
        llm_api_key="llm-secret",
        llm_model="local-model-mini",
        hindsight_base_url="https://hindsight.example",
        hindsight_api_key="hindsight-secret",
        hindsight_bank="omp",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        decision = await classifier.classify("Unclear issue", "")
    finally:
        await classifier.aclose()

    assert decision.target is None
    assert len(decision.candidates) == candidate_count
