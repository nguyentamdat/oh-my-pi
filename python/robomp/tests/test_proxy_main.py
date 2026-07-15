from __future__ import annotations

from click.testing import CliRunner

from robomp.config import load_proxy_settings
from robomp.proxy import __main__ as proxy_main


def test_proxy_cli_starts_with_routing_and_project_tokens_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ROBOMP_GH_PROXY_HMAC_KEY", "hmac-key-" + "a" * 32)
    monkeypatch.setenv("ROBOMP_GITLAB_BASE_URL", "https://gitlab.example.com")
    monkeypatch.setenv("ROBOMP_GITLAB_PROJECT_IDS", "2080,357")
    monkeypatch.setenv("ROBOMP_GITLAB_ROUTING_TOKEN", "routing-token")
    monkeypatch.setenv(
        "ROBOMP_GITLAB_PROJECT_TOKENS_JSON",
        '{"2080":"intake-token","357":"protocol-token"}',
    )
    monkeypatch.setenv("ROBOMP_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("ROBOMP_SQLITE_PATH", str(tmp_path / "robomp.sqlite"))
    monkeypatch.setenv("ROBOMP_LOG_DIR", str(tmp_path / "logs"))
    cfg = load_proxy_settings()
    app = object()
    started: list[object] = []
    monkeypatch.setattr(proxy_main, "_settings_or_die", lambda: cfg)
    monkeypatch.setattr(proxy_main, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(proxy_main, "create_proxy_app", lambda _cfg: app)
    monkeypatch.setattr(proxy_main.uvicorn, "run", lambda candidate, **_kwargs: started.append(candidate))

    result = CliRunner().invoke(proxy_main.main, ["serve"])

    assert result.exit_code == 0, result.output
    assert started == [app]
