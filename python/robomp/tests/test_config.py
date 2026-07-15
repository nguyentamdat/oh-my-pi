from __future__ import annotations

import pytest
from pydantic import ValidationError

from robomp.config import Settings, load_proxy_settings, reset_settings_cache


def test_settings_load_from_env(env: dict[str, str]) -> None:
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.bot_login == "robomp-bot"
    assert cfg.repo_allowlist == frozenset({"octo/widget"})
    assert cfg.allows("octo/widget")
    assert cfg.allows("Octo/Widget")
    assert not cfg.allows("other/widget")


def test_settings_missing_required(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    """Empty out every credential source: validator MUST trip the
    'no GitHub access configured' branch. The `env` fixture keeps the other
    required fields satisfied so we isolate the credential-validator path."""
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("ROBOMP_GH_PROXY_URL", "")
    monkeypatch.setenv("ROBOMP_GH_PROXY_HMAC_KEY", "")
    reset_settings_cache()
    with pytest.raises(ValidationError, match="no GitHub access configured"):
        Settings()  # type: ignore[call-arg]


def test_orchestrator_mode_loads_proxy_config(env: dict[str, str]) -> None:
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.github_token is None
    assert cfg.gh_proxy_url == "http://gh-proxy.invalid:8081"
    assert cfg.gh_proxy_hmac_key is not None
    assert cfg.gh_proxy_hmac_key.get_secret_value().startswith("test-hmac-key")


def test_rejects_token_and_proxy_together(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    reset_settings_cache()
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_rejects_proxy_url_without_key(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_GH_PROXY_HMAC_KEY", "")
    reset_settings_cache()
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_proxy_mode_loads_pat(proxy_env: dict[str, str]) -> None:
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.github_token is not None
    assert cfg.github_token.get_secret_value() == "ghp_test_token_value_xxxxxxxxxxxxxxxx"
    assert cfg.gh_proxy_url is None
    assert cfg.gh_proxy_hmac_key is None


def test_allowlist_csv_parsing(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_REPO_ALLOWLIST", "  alpha/one ,beta/two, ,gamma/three ")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.repo_allowlist == frozenset({"alpha/one", "beta/two", "gamma/three"})


def test_gitlab_config_parses_project_ids_without_exposing_secret(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
) -> None:
    monkeypatch.setenv("ROBOMP_GITLAB_BASE_URL", "https://gitlab.zingplay.com/")
    monkeypatch.setenv("ROBOMP_GITLAB_WEBHOOK_SECRET", "test-gitlab-secret")
    monkeypatch.setenv("ROBOMP_GITLAB_PROJECT_IDS", "356, 357,356")
    monkeypatch.setenv("ROBOMP_GITLAB_BOT_LOGIN", "@RoboMP")
    cfg = Settings()  # type: ignore[call-arg]

    assert cfg.gitlab_enabled
    assert cfg.gitlab_base_url == "https://gitlab.zingplay.com"
    assert cfg.gitlab_project_ids == frozenset({356, 357})
    assert cfg.gitlab_bot_login == "robomp"
    assert "test-gitlab-secret" not in repr(cfg)
    assert cfg.gitlab_trigger_label == "roboomp"


def test_gitlab_proxy_token_is_rejected_by_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
) -> None:
    monkeypatch.setenv("ROBOMP_GITLAB_TOKEN", "glpat-never-in-orchestrator")
    reset_settings_cache()
    with pytest.raises(ValidationError, match="proxy-only"):
        Settings()  # type: ignore[call-arg]


def test_proxy_loader_enables_gitlab_only_with_proxy_token(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.setenv("ROBOMP_GITLAB_TOKEN", "glpat_proxy_only")
    monkeypatch.setenv("ROBOMP_GITLAB_BASE_URL", "https://gitlab.example.test/")
    monkeypatch.setenv("ROBOMP_GITLAB_PROJECT_IDS", "356")
    cfg = load_proxy_settings()

    assert cfg.github_token is None
    assert cfg.gitlab_proxy_enabled
    assert cfg.gitlab_token is not None
    assert cfg.gitlab_token.get_secret_value() == "glpat_proxy_only"
    assert cfg.gitlab_base_url == "https://gitlab.example.test"
    assert cfg.gitlab_project_ids == frozenset({356})


def test_gitlab_config_rejects_partial_credentials(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
) -> None:
    monkeypatch.setenv("ROBOMP_GITLAB_BASE_URL", "https://gitlab.zingplay.com")
    with pytest.raises(ValueError, match="must all be set together"):
        Settings()  # type: ignore[call-arg]


def test_gitlab_config_requires_bot_login(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
) -> None:
    monkeypatch.setenv("ROBOMP_GITLAB_BASE_URL", "https://gitlab.zingplay.com")
    monkeypatch.setenv("ROBOMP_GITLAB_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("ROBOMP_GITLAB_PROJECT_IDS", "356")
    monkeypatch.delenv("ROBOMP_GITLAB_BOT_LOGIN", raising=False)
    with pytest.raises(ValueError, match="ROBOMP_GITLAB_BOT_LOGIN"):
        Settings()  # type: ignore[call-arg]


def test_orchestrator_config_allows_gitlab_without_github_ingress(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "")
    monkeypatch.setenv("ROBOMP_BOT_LOGIN", "")
    monkeypatch.setenv("ROBOMP_REPO_ALLOWLIST", "")
    monkeypatch.setenv("ROBOMP_GH_PROXY_URL", "http://proxy.test")
    monkeypatch.setenv("ROBOMP_GH_PROXY_HMAC_KEY", "h" * 32)
    monkeypatch.setenv("ROBOMP_GITLAB_BASE_URL", "https://gitlab.zingplay.com")
    monkeypatch.setenv("ROBOMP_GITLAB_WEBHOOK_SECRET", "gitlab-secret")
    monkeypatch.setenv("ROBOMP_GITLAB_PROJECT_IDS", "356")
    monkeypatch.setenv("ROBOMP_GITLAB_BOT_LOGIN", "roboomp")

    cfg = Settings()  # type: ignore[call-arg]

    assert cfg.github_webhook_secret is None
    assert cfg.bot_login == ""
    assert cfg.repo_allowlist == frozenset()
    assert cfg.gitlab_enabled


def test_blank_replay_token_treated_as_disabled(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_REPLAY_TOKEN", "")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.replay_token is None


def test_whitespace_replay_token_treated_as_disabled(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_REPLAY_TOKEN", "   ")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.replay_token is None


def test_real_replay_token_preserved(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_REPLAY_TOKEN", "abc")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.replay_token is not None
    assert cfg.replay_token.get_secret_value() == "abc"


def test_blank_bot_login_rejected(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_BOT_LOGIN", "   ")
    reset_settings_cache()
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "raw_login",
    [
        "roboomp",
        " @roboomp ",
        " @ROBOOMP ",
        "roboomp[bot]",
        "@roboomp[bot]",
        " @ROBOOMP[BOT] ",
    ],
)
def test_bot_login_normalizes_mention_case_and_app_suffix(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], raw_login: str
) -> None:
    monkeypatch.setenv("ROBOMP_BOT_LOGIN", raw_login)
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.bot_login == "roboomp"


def test_maintainer_logins_normalize_csv_entries(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_MAINTAINER_LOGINS", " can1357, @ROBOOMP , @Alice[bot] ,, ")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.maintainer_logins == frozenset({"can1357", "roboomp", "alice"})


@pytest.mark.parametrize(
    ("raw_login", "expected"),
    [
        ("roboomp", "roboomp"),
        (" @roboomp ", "roboomp"),
        (" @ROBOOMP ", "roboomp"),
        ("roboomp[bot]", "roboomp"),
        ("@roboomp[bot]", "roboomp"),
        (" @ROBOOMP[BOT] ", "roboomp"),
    ],
)
def test_maintainer_logins_common_entry_forms(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], raw_login: str, expected: str
) -> None:
    monkeypatch.setenv("ROBOMP_MAINTAINER_LOGINS", raw_login)
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.maintainer_logins == frozenset({expected})


def test_model_pool_single(env: dict[str, str]) -> None:
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.model_pool == (cfg.model,)
    assert cfg.pick_model() == cfg.model


def test_model_pool_csv_parses(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv(
        "ROBOMP_MODEL",
        " codex/gpt-5.4 , anthropic/claude-sonnet-4-6 ,, anthropic/claude-opus-4-7 ",
    )
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.model_pool == (
        "codex/gpt-5.4",
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-opus-4-7",
    )


def test_pick_model_covers_full_pool(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    """With a 3-item pool and 500 picks, each option appears at least once."""
    monkeypatch.setenv("ROBOMP_MODEL", "a,b,c")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    seen = {cfg.pick_model() for _ in range(500)}
    assert seen == {"a", "b", "c"}


def test_max_concurrency_default_is_8(env: dict[str, str]) -> None:
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.max_concurrency == 8


def test_task_timeout_hard_grace_env_parses(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    monkeypatch.setenv("ROBOMP_TASK_TIMEOUT_HARD_GRACE_SECONDS", "12.5")
    reset_settings_cache()
    cfg = Settings()  # type: ignore[call-arg]
    assert cfg.task_timeout_hard_grace_seconds == 12.5
