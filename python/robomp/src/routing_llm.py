"""Fail-closed GitLab issue classification with Hindsight recall."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from functools import cache
from importlib import resources
from typing import Any
from urllib.parse import quote

import httpx

from robomp.persona import render
from robomp.routing import AUTO_ROUTE_CONFIDENCE, RouteCandidate, RouteDecision, RoutingPolicy

log = logging.getLogger(__name__)

_MAX_ISSUE_CHARS = 4_000
_MAX_RECALL_RESULTS = 5
_MAX_RECALL_TEXT_CHARS = 500


@cache
def _prompt_template() -> str:
    return resources.files("robomp.prompts").joinpath("routing_classifier.md").read_text(encoding="utf-8")


@cache
def _system_prompt() -> str:
    return resources.files("robomp.prompts").joinpath("routing_classifier_system.md").read_text(encoding="utf-8")


class RoutingLLMClassifier:
    """Classify one allowlisted routing target; invalid output means no route."""

    def __init__(
        self,
        *,
        policy: RoutingPolicy,
        llm_base_url: str,
        llm_api_key: str,
        llm_model: str,
        hindsight_base_url: str,
        hindsight_api_key: str,
        hindsight_bank: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._policy = policy
        self._llm_base_url = llm_base_url.rstrip("/")
        self._llm_api_key = llm_api_key
        self._llm_model = llm_model
        self._hindsight_base_url = hindsight_base_url.rstrip("/")
        self._hindsight_api_key = hindsight_api_key
        self._hindsight_bank = hindsight_bank
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def classify(self, title: str, body: str, paths: Sequence[str] = ()) -> RouteDecision:
        recall = await self._recall(title, body)
        prompt = render(
            _prompt_template(),
            {
                "targets_json": json.dumps(
                    [
                        {
                            "key": target.key,
                            "paths": target.paths,
                            "aliases": target.aliases,
                            "signals": target.signals,
                        }
                        for target in self._policy.targets
                    ],
                    ensure_ascii=False,
                ),
                "issue_json": json.dumps(
                    {"title": title[:_MAX_ISSUE_CHARS], "body": body[:_MAX_ISSUE_CHARS], "paths": list(paths[:10])},
                    ensure_ascii=False,
                ),
                "recall_json": json.dumps(recall, ensure_ascii=False),
            },
        )
        try:
            response = await self._client.post(
                f"{self._llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._llm_api_key}"},
                json={
                    "model": self._llm_model,
                    "temperature": 0,
                    "max_tokens": 4096,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": _system_prompt()},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            response.raise_for_status()
            return self._parse(response.json(), title, body)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            log.warning("Routing LLM classification failed closed: %s", exc)
            return RouteDecision(target=None, confidence=0.0, candidates=())

    async def _recall(self, title: str, body: str) -> list[dict[str, Any]]:
        query = "\n".join(part.strip() for part in (title, body) if part.strip())[:_MAX_ISSUE_CHARS]
        if not query:
            return []
        try:
            response = await self._client.post(
                f"{self._hindsight_base_url}/v1/default/banks/{quote(self._hindsight_bank, safe='')}/memories/recall",
                headers={"Authorization": f"Bearer {self._hindsight_api_key}"},
                json={
                    "query": query,
                    "types": ["world", "experience"],
                    "budget": "low",
                    "max_tokens": 512,
                    "trace": False,
                },
            )
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results") if isinstance(payload, Mapping) else None
            if not isinstance(results, list):
                raise ValueError("Hindsight recall response has no results list")
            recalled: list[dict[str, Any]] = []
            for item in results[:_MAX_RECALL_RESULTS]:
                if not isinstance(item, Mapping) or not isinstance(item.get("text"), str):
                    continue
                tags = item.get("tags")
                recalled.append(
                    {
                        "text": item["text"][:_MAX_RECALL_TEXT_CHARS],
                        "tags": [tag for tag in tags[:5] if isinstance(tag, str)] if isinstance(tags, list) else [],
                    }
                )
            return recalled
        except (httpx.HTTPError, TypeError, ValueError, json.JSONDecodeError) as exc:
            log.warning("Hindsight routing recall unavailable: %s", exc)
            return []

    def _parse(self, payload: object, title: str, body: str) -> RouteDecision:
        if not isinstance(payload, Mapping):
            raise ValueError("routing response must be an object")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ValueError("routing response has no choice")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str):
            raise ValueError("routing response has no content")
        result = json.loads(content)
        if not isinstance(result, Mapping):
            raise ValueError("routing content must be an object")
        target_key = result.get("target_key")
        confidence = result.get("confidence")
        if target_key is None:
            return RouteDecision(target=None, confidence=0.0, candidates=())
        target = next((candidate for candidate in self._policy.targets if candidate.key == target_key), None)
        if target is None:
            raise ValueError("routing response selected an unknown target")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("routing response has invalid confidence")
        raw_evidence = result.get("evidence")
        evidence = (
            tuple(item[:200] for item in raw_evidence[:3] if isinstance(item, str) and item.strip())
            if isinstance(raw_evidence, list)
            else ()
        )
        candidate = RouteCandidate(
            target=target,
            score=round(float(confidence) * 100),
            confidence=float(confidence),
            paths=(),
            aliases=(),
            signals=evidence,
        )
        issue_text = f"{title}\n{body}".casefold()
        grounded = any(item.casefold() in issue_text for item in evidence)
        selected = target if confidence >= AUTO_ROUTE_CONFIDENCE and grounded else None
        return RouteDecision(target=selected, confidence=float(confidence), candidates=(candidate,))
