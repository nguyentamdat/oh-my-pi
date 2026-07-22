"""gh-proxy FastAPI app: HMAC-gated GitHub REST + git proxy.

Robomp calls every endpoint with HMAC headers (see `robomp.proxy_hmac`).
Authenticated requests dispatch to a single `GitHubClient` instance holding
the PAT, or to `robomp.git_ops` for git transport. The PAT never leaves
this process.

Endpoint payloads are deliberately typed (no generic GitHub passthrough):
each one names exactly one operation robomp performs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

from robomp.config import Settings
from robomp.git_ops import (
    GitCommandError,
    HeadDriftError,
)
from robomp.git_ops import (
    clone as git_clone,
)
from robomp.git_ops import (
    fetch_pr_head as git_fetch_pr_head,
)
from robomp.git_ops import (
    fetch_prune as git_fetch_prune,
)
from robomp.git_ops import (
    fetch_ref as git_fetch_ref,
)
from robomp.git_ops import (
    push as git_push,
)
from robomp.github_client import GitHubClient, GitHubError
from robomp.gitlab_client import (
    GitLabClient,
    GitLabError,
    GitLabIssueInfo,
    GitLabMergeRequestInfo,
    GitLabProjectInfo,
    GitLabUserInfo,
)
from robomp.proxy_hmac import HEADER_SIGNATURE, HEADER_TIMESTAMP, verify
from robomp.sandbox import _safe_directory_env, _slot_subprocess_kwargs
from robomp.sandbox import workspace_key as compute_workspace_key

log = logging.getLogger(__name__)


def _serialize(obj: Any) -> Any:
    """Best-effort serializer for dataclasses + tuples → JSON-safe payload."""
    if hasattr(obj, "__dataclass_fields__"):
        data = asdict(obj)
        return {k: _serialize(v) for k, v in data.items()}
    if isinstance(obj, tuple):
        return [_serialize(v) for v in obj]
    if isinstance(obj, list):
        return [_serialize(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


def _gh_error_response(exc: GitHubError) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "kind": "github",
                "status": exc.status,
                "message": exc.message,
                "retry_after": exc.retry_after,
            }
        },
        status_code=exc.status,
    )


def _configured_gitlab_tokens(cfg: Settings) -> tuple[str, ...]:
    """Return every configured GitLab credential for error scrubbing only."""
    tokens: list[str] = []
    for secret in (cfg.gitlab_token, cfg.gitlab_routing_token):
        if secret is not None:
            tokens.append(secret.get_secret_value())
    tokens.extend(secret.get_secret_value() for secret in cfg.gitlab_project_tokens.values())
    return tuple(sorted(dict.fromkeys(tokens), key=len, reverse=True))


def _scrub_gitlab_tokens(message: str, cfg: Settings) -> str:
    for token in _configured_gitlab_tokens(cfg):
        message = message.replace(token, "********")
    return message


def _gitlab_error_response(exc: GitLabError, *, settings: Settings) -> JSONResponse:
    """Return an upstream GitLab failure without reflecting any configured PAT."""
    return JSONResponse(
        {
            "error": {
                "kind": "gitlab",
                "status": exc.status,
                "message": _scrub_gitlab_tokens(exc.message, settings),
            }
        },
        status_code=exc.status,
    )


def _git_error_response(
    exc: GitCommandError,
    *,
    head_drift: bool = False,
    settings: Settings | None = None,
) -> JSONResponse:
    scrub = (lambda value: _scrub_gitlab_tokens(value, settings)) if settings is not None else (lambda value: value)
    payload: dict[str, Any] = {
        "error": {
            "kind": "head_drift" if head_drift else "git",
            "returncode": exc.returncode,
            "cmd": [scrub(str(part)) for part in exc.cmd],
            "stdout": scrub(exc.stdout),
            "stderr": scrub(exc.stderr),
        }
    }
    # 409 for head drift (concurrent commit detected); 502 for everything else.
    return JSONResponse(payload, status_code=409 if head_drift else 502)


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise HTTPException(400, f"missing/invalid '{field}'")
    return value


def _require_int(value: Any, field: str) -> int:
    if not isinstance(value, int):
        raise HTTPException(400, f"missing/invalid '{field}'")
    return value


def _require_positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise HTTPException(400, f"missing/invalid '{field}'")
    return value


def _require_gitlab_project_id(cfg: Settings, value: Any) -> int:
    project_id = _require_positive_int(value, "project_id")
    if project_id not in cfg.gitlab_project_ids:
        raise HTTPException(403, "GitLab project is not allowlisted")
    return project_id


_SAFE_REF_BODY_RE = re.compile(r"[A-Za-z0-9._/-]+")


def _require_fetch_ref(value: Any) -> str:
    """Validate the base-branch ref for `/gh/v1/git/fetch_ref`.

    The orchestrator only ever fetches a branch — a bare name (`main`,
    `farm/x/y`, `alice/fix-parser`) or `refs/heads/<name>`. Reject anything
    `git_ops._branch_refspec` would otherwise pass verbatim into the fetch
    refspec: `:` (refspec injection — write arbitrary refs in the shared
    pool), a leading `-` (argv option injection), and (via the charset) `*`
    `+` `~` `^` `@` `?` `[` `\\`, whitespace, and control bytes; plus the
    git-invalid `..` / `//` / leading-or-trailing `/` / trailing `.`|`.lock`
    forms. Normal slashy branch names still pass.
    """
    ref = _require_str(value, "ref")
    body = ref.removeprefix("refs/heads/")
    if (
        not body
        or ref.startswith("-")
        or body.startswith("/")
        or body.endswith(("/", ".", ".lock"))
        or "//" in body
        or ".." in body
        or not _SAFE_REF_BODY_RE.fullmatch(body)
    ):
        raise HTTPException(400, "invalid ref")
    return ref


def _optional_slot_uid(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not (0 < value < 65536):
        raise HTTPException(400, "missing/invalid 'slot_uid'")
    return value


def _optional_str_list(value: Any, field: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise HTTPException(400, f"invalid '{field}': must be array of strings")
    return list(value)


def _require_review_comments(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(400, "missing/invalid 'comments'")
    comments: list[dict[str, Any]] = []
    for idx, item in enumerate(value):
        if not isinstance(item, dict):
            raise HTTPException(400, f"comments[{idx}] must be an object")
        path = _require_str(item.get("path"), f"comments[{idx}].path")
        line = _require_int(item.get("line"), f"comments[{idx}].line")
        body = _require_str(item.get("body"), f"comments[{idx}].body")
        side = str(item.get("side") or "RIGHT")
        if side not in ("RIGHT", "LEFT"):
            raise HTTPException(400, f"comments[{idx}].side must be RIGHT or LEFT")
        comment: dict[str, Any] = {"path": path, "line": line, "side": side, "body": body}
        start_line = item.get("start_line")
        if start_line is not None:
            comment["start_line"] = _require_int(start_line, f"comments[{idx}].start_line")
        start_side = item.get("start_side")
        if start_side is not None:
            start_side_str = _require_str(start_side, f"comments[{idx}].start_side")
            if start_side_str not in ("RIGHT", "LEFT"):
                raise HTTPException(400, f"comments[{idx}].start_side must be RIGHT or LEFT")
            comment["start_side"] = start_side_str
        comments.append(comment)
    return comments


def _pool_dir(cfg: Settings, repo: str) -> Path:
    _validate_repo_name(repo)
    return Path(cfg.workspace_root) / "_pool" / repo.replace("/", "__")


def _workspace_repo_dir(cfg: Settings, workspace_key: str) -> Path:
    # Defense-in-depth: workspace_key is constructed by `sandbox.workspace_key`
    # as `<repo_with_underscores>__<number>`. Reject anything outside that shape.
    if "/" in workspace_key or workspace_key.startswith(".") or ".." in workspace_key:
        raise HTTPException(400, f"invalid workspace_key {workspace_key!r}")
    return Path(cfg.workspace_root) / workspace_key / "repo"


def _resolve_token(cfg: Settings) -> str:
    if cfg.github_token is None:
        # Will already have been caught at startup, but stay defensive.
        raise HTTPException(500, "gh-proxy: GITHUB_TOKEN not configured")
    return cfg.github_token.get_secret_value()


def _resolve_hmac_key(cfg: Settings) -> bytes:
    if cfg.gh_proxy_hmac_key is None:
        raise HTTPException(500, "gh-proxy: ROBOMP_GH_PROXY_HMAC_KEY not configured")
    return cfg.gh_proxy_hmac_key.get_secret_value().encode("utf-8")


def _resolve_gitlab_project_token(cfg: Settings, project_id: int) -> str:
    if cfg.gitlab_project_tokens_configured:
        secret = cfg.gitlab_project_tokens.get(project_id)
        if secret is None:
            raise HTTPException(503, "GitLab credentials for project are not configured")
        return secret.get_secret_value()
    if cfg.gitlab_token is None:
        raise HTTPException(503, "GitLab proxy is not configured")
    return cfg.gitlab_token.get_secret_value()


def _resolve_gitlab_routing_token(cfg: Settings) -> str:
    secret = cfg.gitlab_routing_token or cfg.gitlab_token
    if secret is None:
        raise HTTPException(503, "GitLab routing credentials are not configured")
    return secret.get_secret_value()


_ORIGIN_READ_TIMEOUT_SECONDS = 5.0


_REMOTE_HELPER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*::")
_FORBIDDEN_URL_BYTES_RE = re.compile(r"[\x00-\x1f\x7f]|%(?:00|0a|0d)", re.IGNORECASE)
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]+$")
_GIT_PROBE_SCRUBBED_ENV_KEYS = (
    "ROBOMP_GIT_HTTP_AUTH",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_WEBHOOK_SECRET",
    "ROBOMP_GITLAB_TOKEN",
    "ROBOMP_GITLAB_ROUTING_TOKEN",
    "ROBOMP_GITLAB_PROJECT_TOKENS_JSON",
    "ROBOMP_GITLAB_WEBHOOK_SECRET",
    "ROBOMP_REPLAY_TOKEN",
    "ROBOMP_GH_PROXY_HMAC_KEY",
)


@dataclass(slots=True, frozen=True)
class _RemoteAuth:
    url: str
    token: str | None
    auth_url: str | None


@dataclass(slots=True, frozen=True)
class _GitLabInstance:
    """Canonical HTTPS GitLab origin plus its optional installation prefix."""

    host: str
    port: int
    root: str
    path_prefix: str


def _gitlab_instance(base_url: str) -> _GitLabInstance:
    """Parse a configured GitLab base URL into a canonical clone origin."""
    parsed = urlparse(base_url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(500, "invalid configured GitLab base URL")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise HTTPException(500, "invalid configured GitLab base URL") from exc
    prefix = parsed.path.rstrip("/")
    if prefix.endswith("/api/v4"):
        prefix = prefix[: -len("/api/v4")]
    if prefix and not prefix.startswith("/"):
        raise HTTPException(500, "invalid configured GitLab base URL")
    host = parsed.hostname.lower()
    authority = host if port == 443 else f"{host}:{port}"
    return _GitLabInstance(
        host=host,
        port=port,
        root=f"https://{authority}{prefix}",
        path_prefix=prefix,
    )


def _gitlab_project_path(project: GitLabProjectInfo) -> str:
    """Return a safely encoded path from GitLab's trusted project metadata."""
    parts = project.path_with_namespace.split("/")
    if not parts or any(not part or part in (".", "..") for part in parts):
        raise HTTPException(502, "GitLab project returned an invalid path")
    if any(_FORBIDDEN_URL_BYTES_RE.search(part) for part in parts):
        raise HTTPException(502, "GitLab project returned an invalid path")
    return "/".join(quote(part, safe="-._~") for part in parts)


def _trusted_gitlab_clone_url(base_url: str, project: GitLabProjectInfo, *, project_id: int) -> str:
    """Accept only the configured HTTPS origin and the API-returned project path.

    The request body never supplies a Git URL. The configured base URL defines
    the host and installation prefix; `path_with_namespace` from the selected
    API project defines the sole permissible repository path.
    """
    if project.id != project_id:
        raise HTTPException(502, "GitLab project metadata ID mismatch")
    instance = _gitlab_instance(base_url)
    project_path = _gitlab_project_path(project)
    expected_path = f"{instance.path_prefix}/{project_path}.git"
    raw = project.http_url_to_repo.strip()
    parsed = urlparse(raw)
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise HTTPException(502, "GitLab project returned an invalid clone URL") from exc
    if (
        raw != project.http_url_to_repo
        or (parsed.scheme or "").lower() != "https"
        or parsed.username
        or parsed.password
        or (parsed.hostname or "").lower() != instance.host
        or port != instance.port
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.path != expected_path
    ):
        raise HTTPException(502, "GitLab project returned an untrusted clone URL")
    return f"{instance.root}/{project_path}.git"


def _gitlab_pool_dir(cfg: Settings, project: GitLabProjectInfo) -> Path:
    instance = re.sub(r"[^A-Za-z0-9._-]+", "__", cfg.gitlab_instance_id)
    return Path(cfg.workspace_root) / "_pool" / f"{instance}__{project.id}"


def _validate_repo_name(repo: str) -> None:
    if not _GITHUB_REPO_RE.fullmatch(repo) or "/.." in repo or "../" in repo:
        raise HTTPException(400, f"invalid repo {repo!r}")


def _github_url_for_repo(repo: str) -> str:
    _validate_repo_name(repo)
    return f"https://github.com/{repo}.git"


def _git_probe_env(repo_dir: Path) -> dict[str, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}
    for key in _GIT_PROBE_SCRUBBED_ENV_KEYS:
        env.pop(key, None)
    env.update(_safe_directory_env(repo_dir))
    return env


def _read_remote_urls(repo_dir: Path, slot_uid: int | None = None, *, push: bool = False) -> list[str]:
    """Read every configured fetch URL or push URL for `origin` without contacting it."""
    env = _git_probe_env(repo_dir)
    slot_kwargs = _slot_subprocess_kwargs(slot_uid)
    selector = ["--push", "--all"] if push else ["--all"]
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", *selector, "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_ORIGIN_READ_TIMEOUT_SECONDS,
            env=env,
            **slot_kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "timeout reading origin url") from exc
    if proc.returncode != 0:
        # `git remote get-url` writes nothing useful to stdout on failure; do
        # NOT echo stderr to the client (may leak local paths). The proxy log
        # already captured the failure.
        log.warning("gh-proxy: failed to read origin url", extra={"repo_dir": str(repo_dir)})
        raise HTTPException(400, "could not read origin url for worktree")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _read_single_remote_url(repo_dir: Path, expected_repo: str, *, push: bool, slot_uid: int | None = None) -> str:
    urls = list(dict.fromkeys(_read_remote_urls(repo_dir, slot_uid=slot_uid, push=push)))
    if len(urls) != 1:
        kind = "push" if push else "fetch"
        log.warning(
            "gh-proxy: refusing git op — origin has ambiguous remote urls",
            extra={"expected_repo": expected_repo, "kind": kind, "count": len(urls)},
        )
        raise HTTPException(400, f"origin must have exactly one {kind} url")
    return urls[0]


def _normalized_github_https_url(url: str, expected_repo: str) -> str:
    _validate_repo_name(expected_repo)
    parsed = urlparse(url)
    if (parsed.scheme or "").lower() != "https":
        raise HTTPException(400, f"remote url must be https://github.com/{expected_repo}[.git]")
    if parsed.username or parsed.password:
        raise HTTPException(400, "remote url must not contain embedded credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(400, "remote url has invalid port") from exc
    if port is not None:
        raise HTTPException(400, "remote url must not specify a port")
    if (parsed.hostname or "").lower() != "github.com":
        raise HTTPException(400, f"remote url host must be github.com for repo {expected_repo!r}")
    if parsed.params or parsed.query or parsed.fragment:
        raise HTTPException(400, "remote url must not contain params, query, or fragment")
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if path.lower() != expected_repo.lower():
        raise HTTPException(400, f"remote url does not match repo {expected_repo!r}")
    return _github_url_for_repo(expected_repo)


def _remote_auth_for_url(url: str, expected_repo: str, token: str) -> _RemoteAuth:
    raw = url.strip()
    if not raw or raw != url:
        raise HTTPException(400, "remote url must not be empty or padded")
    if _FORBIDDEN_URL_BYTES_RE.search(raw):
        raise HTTPException(400, "remote url contains forbidden control bytes")
    if raw.startswith("-"):
        raise HTTPException(400, "remote url must not start with '-'")
    if _REMOTE_HELPER_RE.match(raw):
        raise HTTPException(400, "git remote helper transports are disabled")
    scheme = (urlparse(raw).scheme or "").lower()
    if scheme in ("http", "https"):
        normalized = _normalized_github_https_url(raw, expected_repo)
        return _RemoteAuth(url=normalized, token=token, auth_url=normalized)
    return _RemoteAuth(url=raw, token=None, auth_url=None)


def _clone_remote_auth(clone_url: str, expected_repo: str, token: str) -> _RemoteAuth:
    try:
        return _remote_auth_for_url(clone_url, expected_repo, token)
    except HTTPException:
        log.warning(
            "gh-proxy: refusing clone — clone_url is not permitted",
            extra={"expected_repo": expected_repo},
        )
        raise


def _origin_remote_auth(
    repo_dir: Path,
    expected_repo: str,
    token: str,
    *,
    push: bool = False,
    slot_uid: int | None = None,
) -> _RemoteAuth:
    url = _read_single_remote_url(repo_dir, expected_repo, push=push, slot_uid=slot_uid)
    try:
        return _remote_auth_for_url(url, expected_repo, token)
    except HTTPException:
        log.warning(
            "gh-proxy: refusing git op — origin url is not permitted",
            extra={"expected_repo": expected_repo, "push": push},
        )
        raise


def _gitlab_remote_auth_for_url(
    url: str,
    *,
    base_url: str,
    project: GitLabProjectInfo,
    project_id: int,
    token: str,
) -> _RemoteAuth:
    """Validate an existing GitLab origin before scoping ephemeral auth to it."""
    raw = url.strip()
    if not raw or raw != url:
        raise HTTPException(400, "GitLab remote URL must not be empty or padded")
    if _FORBIDDEN_URL_BYTES_RE.search(raw):
        raise HTTPException(400, "GitLab remote URL contains forbidden control bytes")
    if raw.startswith("-") or _REMOTE_HELPER_RE.match(raw):
        raise HTTPException(400, "GitLab remote URL uses a forbidden transport")
    trusted_url = _trusted_gitlab_clone_url(base_url, project, project_id=project_id)
    if raw != trusted_url:
        raise HTTPException(400, "GitLab remote URL does not match trusted project metadata")
    return _RemoteAuth(url=trusted_url, token=token, auth_url=trusted_url)


def _gitlab_origin_remote_auth(
    repo_dir: Path,
    *,
    base_url: str,
    project: GitLabProjectInfo,
    project_id: int,
    token: str,
    push: bool = False,
    slot_uid: int | None = None,
) -> _RemoteAuth:
    url = _read_single_remote_url(
        repo_dir,
        project.path_with_namespace,
        push=push,
        slot_uid=slot_uid,
    )
    try:
        return _gitlab_remote_auth_for_url(
            url,
            base_url=base_url,
            project=project,
            project_id=project_id,
            token=token,
        )
    except HTTPException:
        log.warning(
            "gh-proxy: refusing GitLab git operation — origin URL is not permitted",
            extra={"project_id": project_id, "push": push},
        )
        raise


def create_proxy_app(settings: Settings) -> FastAPI:
    """Build the gh-proxy FastAPI app bound to `settings`."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.github_token is not None:
            app.state.github = GitHubClient(_resolve_token(settings))
        if settings.gitlab_proxy_enabled:
            assert settings.gitlab_base_url is not None
            project_clients: dict[int, GitLabClient] = {}
            if settings.gitlab_project_tokens_configured:
                project_tokens = settings.gitlab_project_tokens
                for project_id in settings.gitlab_project_ids:
                    secret = project_tokens.get(project_id)
                    if secret is not None:
                        project_clients[project_id] = GitLabClient(
                            settings.gitlab_base_url,
                            secret.get_secret_value(),
                            allowed_project_ids=frozenset({project_id}),
                        )
            elif settings.gitlab_token is not None:
                token = settings.gitlab_token.get_secret_value()
                project_clients = {
                    project_id: GitLabClient(
                        settings.gitlab_base_url,
                        token,
                        allowed_project_ids=frozenset({project_id}),
                    )
                    for project_id in settings.gitlab_project_ids
                }
            app.state.gitlab_by_project = project_clients
            if settings.gitlab_routing_token is not None or settings.gitlab_token is not None:
                app.state.gitlab_routing = GitLabClient(
                    settings.gitlab_base_url,
                    _resolve_gitlab_routing_token(settings),
                    allowed_project_ids=settings.gitlab_project_ids,
                )
        app.state.settings = settings
        yield

    app = FastAPI(title="robomp-gh-proxy", version="0.1.0", lifespan=lifespan)

    def _request_target(request: Request) -> str:
        """Canonical signing target: `path` plus raw query string if any.

        Binding the query into the HMAC stops an attacker from replaying a
        signed `/gh/v1/issue?repo=octo/widget&number=1` against
        `?repo=octo/widget&number=2`.
        """
        query = request.url.query
        return f"{request.url.path}?{query}" if query else request.url.path

    async def _read_body_capped(request: Request) -> bytes:
        """Read the request body with a hard byte cap.

        Checks `Content-Length` first (cheap reject before any read), then
        streams chunks via `request.stream()` with a running counter so a
        client that lies about (or omits) the header still can't get more
        than `max_bytes` into memory. We deliberately do NOT call
        `request.body()` first — that would buffer the full payload before
        auth checks ever run.
        """
        max_bytes = settings.gh_proxy_max_body_bytes
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                declared = int(cl)
            except ValueError as exc:
                raise HTTPException(400, "invalid content-length") from exc
            if declared > max_bytes:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "request body too large")
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "request body too large")
            chunks.append(chunk)
        body = b"".join(chunks)
        # Starlette's `request.body()` / `request.json()` re-read from
        # `request._body`. We consumed the stream above, so seed the cache
        # to keep downstream JSON parsing working without a second read.
        request._body = body  # type: ignore[attr-defined]
        return body

    async def _authenticate(request: Request) -> bytes:
        body = await _read_body_capped(request)
        ts = request.headers.get(HEADER_TIMESTAMP)
        sig = request.headers.get(HEADER_SIGNATURE)
        target = _request_target(request)
        result = verify(
            method=request.method,
            path=target,
            body=body,
            timestamp=ts,
            signature=sig,
            key=_resolve_hmac_key(settings),
        )
        if not result.ok:
            log.warning(
                "gh-proxy auth rejected",
                extra={"reason": result.reason, "path": request.url.path},
            )
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthenticated")
        return body

    def _github_client(request: Request) -> GitHubClient:
        client = getattr(request.app.state, "github", None)
        if not isinstance(client, GitHubClient):
            raise HTTPException(503, "GitHub proxy is not configured")
        return client

    def _gitlab_project_client(request: Request, project_id: int) -> GitLabClient:
        clients = getattr(request.app.state, "gitlab_by_project", None)
        if not isinstance(clients, dict):
            raise HTTPException(503, "GitLab proxy is not configured")
        client = clients.get(project_id)
        if not isinstance(client, GitLabClient):
            raise HTTPException(503, "GitLab credentials for project are not configured")
        return client

    def _gitlab_routing_client(request: Request) -> GitLabClient:
        client = getattr(request.app.state, "gitlab_routing", None)
        if not isinstance(client, GitLabClient):
            raise HTTPException(503, "GitLab routing credentials are not configured")
        return client

    # ---- meta ----
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ---- reads ----
    @app.get("/gh/v1/authenticated_login")
    async def authenticated_login(request: Request) -> dict[str, str]:
        await _authenticate(request)
        github: GitHubClient = _github_client(request)
        try:
            login = await github.get_authenticated_login()
        except GitHubError as exc:
            raise HTTPException(exc.status, exc.message) from exc
        return {"login": login}

    @app.get("/gh/v1/repo")
    async def get_repo(request: Request, repo: str) -> JSONResponse:
        await _authenticate(request)
        github: GitHubClient = _github_client(request)
        try:
            info = await github.get_repo(repo)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse(_serialize(info))

    @app.get("/gh/v1/issue")
    async def get_issue(request: Request, repo: str, number: int) -> JSONResponse:
        await _authenticate(request)
        github: GitHubClient = _github_client(request)
        try:
            info = await github.get_issue(repo, number)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse(_serialize(info))

    @app.get("/gh/v1/closing_prs")
    async def list_closing_prs(request: Request, repo: str, number: int) -> JSONResponse:
        await _authenticate(request)
        github: GitHubClient = _github_client(request)
        try:
            prs = await github.list_closing_pull_requests(repo, number)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"pr_numbers": list(prs)})

    @app.get("/gh/v1/pull_request")
    async def get_pull_request(request: Request, repo: str, number: int) -> JSONResponse:
        await _authenticate(request)
        github: GitHubClient = _github_client(request)
        try:
            info = await github.get_pull_request(repo, number)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse(_serialize(info))

    @app.get("/gh/v1/pr_files")
    async def list_pr_files(request: Request, repo: str, pr_number: int) -> JSONResponse:
        await _authenticate(request)
        github: GitHubClient = _github_client(request)
        try:
            items = await github.list_pr_files(repo, pr_number)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"items": [_serialize(item) for item in items]})

    @app.get("/gh/v1/issues")
    async def list_issues(request: Request, repo: str, state: str = "open", limit: int = 30) -> JSONResponse:
        await _authenticate(request)
        github: GitHubClient = _github_client(request)
        try:
            items = await github.list_issues(repo, state=state, limit=limit)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"items": [_serialize(s) for s in items]})

    @app.get("/gh/v1/search_issues")
    async def search_issues(request: Request, repo: str, q: str, limit: int = 10) -> JSONResponse:
        await _authenticate(request)
        github: GitHubClient = request.app.state.github
        try:
            items = await github.search_issues(repo, q, limit=limit)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"items": [_serialize(s) for s in items]})

    @app.get("/gh/v1/issue_index_entries")
    async def list_issue_index_entries(
        request: Request,
        repo: str,
        since: str | None = None,
        page: int = 1,
        per_page: int = 100,
    ) -> JSONResponse:
        await _authenticate(request)
        github: GitHubClient = request.app.state.github
        try:
            items = await github.list_issue_index_entries(repo, since=since, page=page, per_page=per_page)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"items": [_serialize(s) for s in items]})

    @app.get("/gh/v1/comments")
    async def list_comments(request: Request, repo: str, number: int) -> JSONResponse:
        await _authenticate(request)
        github: GitHubClient = _github_client(request)
        try:
            items = await github.list_comments(repo, number)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"items": [_serialize(c) for c in items]})

    @app.get("/gh/v1/review_comments")
    async def list_review_comments(request: Request, repo: str, pr_number: int) -> JSONResponse:
        await _authenticate(request)
        github: GitHubClient = _github_client(request)
        try:
            items = await github.list_review_comments(repo, pr_number)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"items": [_serialize(c) for c in items]})

    @app.get("/gh/v1/pr_reviews")
    async def list_pr_reviews(request: Request, repo: str, pr_number: int) -> JSONResponse:
        await _authenticate(request)
        github: GitHubClient = _github_client(request)
        try:
            items = await github.list_pr_reviews(repo, pr_number)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"items": [_serialize(r) for r in items]})

    # ---- writes ----
    async def _json_body(request: Request) -> dict[str, Any]:
        await _authenticate(request)
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(400, f"invalid json: {exc}") from exc
        if not isinstance(data, dict):
            raise HTTPException(400, "json body must be an object")
        return data

    @app.post("/gh/v1/post_comment")
    async def post_comment(request: Request) -> JSONResponse:
        data = await _json_body(request)
        repo = _require_str(data.get("repo"), "repo")
        number = _require_int(data.get("number"), "number")
        body = _require_str(data.get("body"), "body")
        github: GitHubClient = _github_client(request)
        try:
            info = await github.post_comment(repo, number, body)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse(_serialize(info))

    @app.post("/gh/v1/open_pull_request")
    async def open_pull_request(request: Request) -> JSONResponse:
        data = await _json_body(request)
        repo = _require_str(data.get("repo"), "repo")
        head = _require_str(data.get("head"), "head")
        base = _require_str(data.get("base"), "base")
        title = _require_str(data.get("title"), "title")
        body = _require_str(data.get("body"), "body")
        draft = bool(data.get("draft", False))
        mcm = bool(data.get("maintainer_can_modify", True))
        github: GitHubClient = _github_client(request)
        try:
            pr = await github.open_pull_request(
                repo=repo,
                head=head,
                base=base,
                title=title,
                body=body,
                draft=draft,
                maintainer_can_modify=mcm,
            )
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse(_serialize(pr))

    @app.post("/gh/v1/request_reviewers")
    async def request_reviewers(request: Request) -> JSONResponse:
        data = await _json_body(request)
        repo = _require_str(data.get("repo"), "repo")
        pr_number = _require_int(data.get("pr_number"), "pr_number")
        reviewers = _optional_str_list(data.get("reviewers"), "reviewers")
        team_reviewers = _optional_str_list(data.get("team_reviewers"), "team_reviewers")
        github: GitHubClient = _github_client(request)
        try:
            await github.request_reviewers(
                repo=repo,
                pr_number=pr_number,
                reviewers=reviewers,
                team_reviewers=team_reviewers,
            )
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"ok": True})

    @app.post("/gh/v1/add_issue_labels")
    async def add_issue_labels(request: Request) -> JSONResponse:
        data = await _json_body(request)
        repo = _require_str(data.get("repo"), "repo")
        number = _require_int(data.get("number"), "number")
        labels = _optional_str_list(data.get("labels"), "labels") or []
        github: GitHubClient = _github_client(request)
        try:
            applied = await github.add_issue_labels(repo, number, labels)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"labels": list(applied)})

    @app.post("/gh/v1/remove_issue_label")
    async def remove_issue_label(request: Request) -> JSONResponse:
        data = await _json_body(request)
        repo = _require_str(data.get("repo"), "repo")
        number = _require_int(data.get("number"), "number")
        label = _require_str(data.get("label"), "label")
        github: GitHubClient = _github_client(request)
        try:
            await github.remove_issue_label(repo, number, label)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"ok": True})

    @app.post("/gh/v1/submit_pr_review")
    async def submit_pr_review(request: Request) -> JSONResponse:
        data = await _json_body(request)
        repo = _require_str(data.get("repo"), "repo")
        pr_number = _require_int(data.get("pr_number"), "pr_number")
        body = _require_str(data.get("body"), "body")
        event = str(data.get("event") or "COMMENT")
        comments = _require_review_comments(data.get("comments"))
        github: GitHubClient = _github_client(request)
        try:
            review = await github.submit_pr_review(
                repo=repo,
                pr_number=pr_number,
                body=body,
                event=event,
                comments=comments,
            )
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse(_serialize(review))

    @app.post("/gh/v1/add_assignees")
    async def add_assignees(request: Request) -> JSONResponse:
        data = await _json_body(request)
        repo = _require_str(data.get("repo"), "repo")
        number = _require_int(data.get("number"), "number")
        assignees = _optional_str_list(data.get("assignees"), "assignees") or []
        github: GitHubClient = _github_client(request)
        try:
            await github.add_assignees(repo, number, assignees)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"ok": True})

    @app.get("/gh/v1/comment_reactions")
    async def list_comment_reactions(request: Request, repo: str, comment_id: int) -> JSONResponse:
        await _authenticate(request)
        github: GitHubClient = _github_client(request)
        try:
            reactions = await github.list_comment_reactions(repo, comment_id)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"items": [_serialize(r) for r in reactions]})

    @app.post("/gh/v1/close_issue")
    async def close_issue(request: Request) -> JSONResponse:
        data = await _json_body(request)
        repo = _require_str(data.get("repo"), "repo")
        number = _require_int(data.get("number"), "number")
        reason_raw = data.get("reason")
        reason = reason_raw if isinstance(reason_raw, str) and reason_raw else "completed"
        github: GitHubClient = _github_client(request)
        try:
            await github.close_issue(repo, number, reason=reason)
        except GitHubError as exc:
            return _gh_error_response(exc)
        return JSONResponse({"ok": True})

    # ---- GitLab REST ----
    #
    # GitLab routes deliberately include a numeric project ID in the path and
    # validate it before touching the upstream client. There is no generic
    # passthrough route: each operation is explicitly named and typed.
    @app.get("/gl/v1/authenticated_user")
    async def get_gitlab_authenticated_user(
        request: Request,
        project_id: int | None = None,
    ) -> JSONResponse:
        await _authenticate(request)
        if project_id is None:
            gitlab = _gitlab_routing_client(request)
        else:
            project_id = _require_gitlab_project_id(settings, project_id)
            gitlab = _gitlab_project_client(request, project_id)
        try:
            user: GitLabUserInfo = await gitlab.get_authenticated_user(project_id)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        return JSONResponse(_serialize(user))

    @app.get("/gl/v1/projects/{project_id}")
    async def get_gitlab_project(request: Request, project_id: int) -> JSONResponse:
        await _authenticate(request)
        project_id = _require_gitlab_project_id(settings, project_id)

        gitlab = _gitlab_project_client(request, project_id)
        try:
            project = await gitlab.get_project(project_id)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        trusted_clone = _trusted_gitlab_clone_url(settings.gitlab_base_url or "", project, project_id=project_id)
        payload = _serialize(project)
        payload["http_url_to_repo"] = trusted_clone
        return JSONResponse(payload)

    @app.post("/gl/v1/projects/{project_id}/issues")
    async def create_gitlab_issue(request: Request, project_id: int) -> JSONResponse:
        data = await _json_body(request)
        project_id = _require_gitlab_project_id(settings, project_id)
        title = _require_str(data.get("title"), "title")
        description = _require_str(data.get("description"), "description")
        labels = _optional_str_list(data.get("labels"), "labels")

        gitlab = _gitlab_project_client(request, project_id)
        try:
            issue: GitLabIssueInfo = await gitlab.create_issue(
                project_id,
                title=title,
                description=description,
                labels=labels,
            )
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        return JSONResponse(_serialize(issue))

    @app.get("/gl/v1/projects/{project_id}/issues/find_by_marker")
    async def find_gitlab_issue_by_marker(
        request: Request,
        project_id: int,
        marker: str | None = None,
    ) -> Response:
        await _authenticate(request)
        project_id = _require_gitlab_project_id(settings, project_id)
        marker = _require_str(marker, "marker")

        gitlab = _gitlab_project_client(request, project_id)
        try:
            issue = await gitlab.find_issue_by_marker(project_id, marker)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        if issue is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return JSONResponse(_serialize(issue))

    @app.get("/gl/v1/projects/{project_id}/issues/{iid}")
    async def get_gitlab_issue(request: Request, project_id: int, iid: int) -> JSONResponse:
        await _authenticate(request)
        project_id = _require_gitlab_project_id(settings, project_id)
        iid = _require_positive_int(iid, "iid")

        gitlab = _gitlab_project_client(request, project_id)
        try:
            issue = await gitlab.get_issue(project_id, iid)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        return JSONResponse(_serialize(issue))

    @app.get("/gl/v1/projects/{project_id}/issues/{iid}/related_merge_requests")
    async def list_gitlab_issue_related_merge_requests(
        request: Request,
        project_id: int,
        iid: int,
    ) -> JSONResponse:
        await _authenticate(request)
        project_id = _require_gitlab_project_id(settings, project_id)
        iid = _require_positive_int(iid, "iid")

        gitlab = _gitlab_project_client(request, project_id)
        try:
            merge_requests = await gitlab.list_issue_related_merge_requests(project_id, iid)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        return JSONResponse({"items": [_serialize(merge_request) for merge_request in merge_requests]})

    @app.get("/gl/v1/projects/{project_id}/issues/{iid}/closed_by")
    async def list_gitlab_issue_closed_by_merge_requests(
        request: Request,
        project_id: int,
        iid: int,
    ) -> JSONResponse:
        await _authenticate(request)
        project_id = _require_gitlab_project_id(settings, project_id)
        iid = _require_positive_int(iid, "iid")

        gitlab = _gitlab_project_client(request, project_id)
        try:
            merge_requests = await gitlab.list_issue_closed_by_merge_requests(project_id, iid)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        return JSONResponse({"items": [_serialize(merge_request) for merge_request in merge_requests]})

    @app.get("/gl/v1/projects/{project_id}/issues/{iid}/notes")
    async def list_gitlab_issue_notes(request: Request, project_id: int, iid: int) -> JSONResponse:
        await _authenticate(request)
        project_id = _require_gitlab_project_id(settings, project_id)
        iid = _require_positive_int(iid, "iid")

        gitlab = _gitlab_project_client(request, project_id)
        try:
            notes = await gitlab.list_issue_notes(project_id, iid)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        return JSONResponse({"items": [_serialize(note) for note in notes]})

    @app.post("/gl/v1/projects/{project_id}/issues/{iid}/notes")
    async def post_gitlab_issue_note(request: Request, project_id: int, iid: int) -> JSONResponse:
        data = await _json_body(request)
        project_id = _require_gitlab_project_id(settings, project_id)
        iid = _require_positive_int(iid, "iid")
        body = _require_str(data.get("body"), "body")

        gitlab = _gitlab_project_client(request, project_id)
        try:
            note = await gitlab.post_issue_note(project_id, iid, body)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        return JSONResponse(_serialize(note))

    @app.put("/gl/v1/projects/{project_id}/issues/{iid}/labels")
    async def update_gitlab_issue_labels(request: Request, project_id: int, iid: int) -> JSONResponse:
        data = await _json_body(request)
        project_id = _require_gitlab_project_id(settings, project_id)
        iid = _require_positive_int(iid, "iid")
        labels = _optional_str_list(data.get("labels"), "labels")
        if labels is None:
            raise HTTPException(400, "missing/invalid 'labels'")

        gitlab = _gitlab_project_client(request, project_id)
        try:
            issue = await gitlab.update_issue_labels(project_id, iid, labels)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        return JSONResponse(_serialize(issue))

    @app.post("/gl/v1/projects/{project_id}/issues/{iid}/labels/add")
    async def add_gitlab_issue_labels(request: Request, project_id: int, iid: int) -> JSONResponse:
        data = await _json_body(request)
        project_id = _require_gitlab_project_id(settings, project_id)
        iid = _require_positive_int(iid, "iid")
        labels = _optional_str_list(data.get("labels"), "labels")
        if labels is None:
            raise HTTPException(400, "missing/invalid 'labels'")

        gitlab = _gitlab_project_client(request, project_id)
        try:
            issue = await gitlab.add_issue_labels(project_id, iid, labels)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        return JSONResponse(_serialize(issue))

    @app.post("/gl/v1/projects/{project_id}/issues/{iid}/labels/remove")
    async def remove_gitlab_issue_labels(request: Request, project_id: int, iid: int) -> JSONResponse:
        data = await _json_body(request)
        project_id = _require_gitlab_project_id(settings, project_id)
        iid = _require_positive_int(iid, "iid")
        labels = _optional_str_list(data.get("labels"), "labels")
        if labels is None:
            raise HTTPException(400, "missing/invalid 'labels'")

        gitlab = _gitlab_project_client(request, project_id)
        try:
            issue = await gitlab.remove_issue_labels(project_id, iid, labels)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        return JSONResponse(_serialize(issue))

    @app.post("/gl/v1/projects/{project_id}/issues/{iid}/close")
    async def close_gitlab_issue(request: Request, project_id: int, iid: int) -> JSONResponse:
        data = await _json_body(request)
        if data:
            raise HTTPException(400, "GitLab close request body must be empty")
        project_id = _require_gitlab_project_id(settings, project_id)
        iid = _require_positive_int(iid, "iid")

        gitlab = _gitlab_project_client(request, project_id)
        try:
            issue = await gitlab.close_issue(project_id, iid)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        return JSONResponse(_serialize(issue))

    @app.post("/gl/v1/projects/{project_id}/issues/{iid}/move")
    async def move_gitlab_issue(request: Request, project_id: int, iid: int) -> JSONResponse:
        data = await _json_body(request)
        project_id = _require_gitlab_project_id(settings, project_id)
        iid = _require_positive_int(iid, "iid")
        to_project_id = _require_gitlab_project_id(settings, data.get("to_project_id"))
        gitlab = _gitlab_routing_client(request)
        try:
            issue = await gitlab.move_issue(project_id, iid, to_project_id)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        return JSONResponse(_serialize(issue))

    @app.post("/gl/v1/projects/{project_id}/issues/{iid}/resolve_moved")
    async def resolve_gitlab_moved_issue(request: Request, project_id: int, iid: int) -> Response:
        data = await _json_body(request)
        project_id = _require_gitlab_project_id(settings, project_id)
        iid = _require_positive_int(iid, "iid")
        expected_target_project_id = _require_gitlab_project_id(
            settings,
            data.get("expected_target_project_id"),
        )
        gitlab = _gitlab_routing_client(request)
        try:
            issue = await gitlab.resolve_moved_issue(project_id, iid, expected_target_project_id)
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        if issue is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return JSONResponse(_serialize(issue))

    @app.post("/gl/v1/projects/{project_id}/merge_requests")
    async def create_gitlab_merge_request(request: Request, project_id: int) -> JSONResponse:
        data = await _json_body(request)
        project_id = _require_gitlab_project_id(settings, project_id)
        source_branch = _require_str(data.get("source_branch"), "source_branch")
        target_branch = _require_str(data.get("target_branch"), "target_branch")
        title = _require_str(data.get("title"), "title")
        description = _require_str(data.get("description"), "description")

        gitlab = _gitlab_project_client(request, project_id)
        try:
            merge_request: GitLabMergeRequestInfo = await gitlab.create_merge_request(
                project_id,
                source_branch=source_branch,
                target_branch=target_branch,
                title=title,
                description=description,
            )
        except GitLabError as exc:
            return _gitlab_error_response(exc, settings=settings)
        return JSONResponse(_serialize(merge_request))

    # ---- git transport ----
    #
    # The underlying `robomp.git_ops` primitives are blocking `subprocess.run`
    # calls. Running them directly from an `async def` handler pins the
    # event loop until the subprocess returns; a hung git would freeze the
    # whole proxy. We bridge with `asyncio.to_thread` (work on a threadpool
    # worker) wrapped in `asyncio.wait_for` (hard wall-clock cap, returns
    # 504 on timeout). The subprocess itself can outlive the timeout — a
    # proper subprocess.kill plumbing would have to live inside
    # `git_ops._run_git`; flagged for follow-up.

    async def _run_git_op(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs),
                timeout=settings.gh_proxy_git_timeout_seconds,
            )
        except TimeoutError as exc:
            log.warning(
                "gh-proxy: git op exceeded timeout",
                extra={"op": fn.__name__, "timeout": settings.gh_proxy_git_timeout_seconds},
            )
            raise HTTPException(504, f"git {fn.__name__} timed out") from exc

    @app.post("/gh/v1/git/clone")
    async def git_clone_endpoint(request: Request) -> JSONResponse:
        data = await _json_body(request)
        repo = _require_str(data.get("repo"), "repo")
        clone_url = _require_str(data.get("clone_url"), "clone_url")
        default_branch = _require_str(data.get("default_branch"), "default_branch")
        remote = _clone_remote_auth(clone_url, repo, _resolve_token(settings))
        target = _pool_dir(settings, repo)
        try:
            await _run_git_op(
                git_clone,
                target,
                clone_url=remote.url,
                default_branch=default_branch,
                token=remote.token,
                auth_url=remote.auth_url,
            )
        except GitCommandError as exc:
            return _git_error_response(exc)
        return JSONResponse({"pool_dir": str(target)})

    @app.post("/gh/v1/git/fetch")
    async def git_fetch_endpoint(request: Request) -> JSONResponse:
        data = await _json_body(request)
        repo = _require_str(data.get("repo"), "repo")
        target = _pool_dir(settings, repo)
        remote = await asyncio.to_thread(_origin_remote_auth, target, repo, _resolve_token(settings))
        try:
            await _run_git_op(
                git_fetch_prune,
                target,
                token=remote.token,
                remote_url=remote.url,
                auth_url=remote.auth_url,
            )
        except GitCommandError as exc:
            return _git_error_response(exc)
        return JSONResponse({"pool_dir": str(target)})

    @app.post("/gh/v1/git/fetch_ref")
    async def git_fetch_ref_endpoint(request: Request) -> JSONResponse:
        data = await _json_body(request)
        repo = _require_str(data.get("repo"), "repo")
        ref = _require_fetch_ref(data.get("ref"))
        target = _pool_dir(settings, repo)
        remote = await asyncio.to_thread(_origin_remote_auth, target, repo, _resolve_token(settings))
        # fetch_ref is intentionally best-effort; never surfaces a 5xx.
        await _run_git_op(
            git_fetch_ref,
            target,
            ref,
            token=remote.token,
            remote_url=remote.url,
            auth_url=remote.auth_url,
            timeout=settings.gh_proxy_git_timeout_seconds,
        )
        return JSONResponse({"pool_dir": str(target)})

    @app.post("/gh/v1/git/fetch_pr_head")
    async def git_fetch_pr_head_endpoint(request: Request) -> JSONResponse:
        data = await _json_body(request)
        repo = _require_str(data.get("repo"), "repo")
        pr_number = _require_int(data.get("pr_number"), "pr_number")
        target = _pool_dir(settings, repo)
        remote = await asyncio.to_thread(_origin_remote_auth, target, repo, _resolve_token(settings))
        try:
            await _run_git_op(
                git_fetch_pr_head,
                target,
                pr_number,
                token=remote.token,
                remote_url=remote.url,
                auth_url=remote.auth_url,
            )
        except GitCommandError as exc:
            return _git_error_response(exc)
        return JSONResponse({"pool_dir": str(target)})

    @app.post("/gh/v1/git/push")
    async def git_push_endpoint(request: Request) -> JSONResponse:
        data = await _json_body(request)
        repo = _require_str(data.get("repo"), "repo")
        workspace_key = _require_str(data.get("workspace_key"), "workspace_key")
        branch = _require_str(data.get("branch"), "branch")
        expected_head = _require_str(data.get("expected_head"), "expected_head")
        slot_uid = _optional_slot_uid(data.get("slot_uid"))
        # Sanity-check workspace_key matches the repo claim.
        expected_prefix = repo.replace("/", "__") + "__"
        if not workspace_key.startswith(expected_prefix):
            raise HTTPException(400, "workspace_key does not match repo")
        repo_dir = _workspace_repo_dir(settings, workspace_key)
        if not repo_dir.is_dir():
            raise HTTPException(404, f"workspace not found: {workspace_key}")
        remote = await asyncio.to_thread(
            _origin_remote_auth,
            repo_dir,
            repo,
            _resolve_token(settings),
            push=True,
            slot_uid=slot_uid,
        )
        try:
            result = await _run_git_op(
                git_push,
                repo_dir,
                branch=branch,
                expected_head=expected_head,
                token=remote.token,
                remote_url=remote.url,
                auth_url=remote.auth_url,
                slot_uid=slot_uid,
            )
        except HeadDriftError as exc:
            return _git_error_response(exc, head_drift=True)
        except GitCommandError as exc:
            return _git_error_response(exc)
        return JSONResponse({"head": result.head, "branch": result.branch})

    # ---- GitLab git transport ----
    #
    # The only client-supplied GitLab identifier is the numeric project ID in
    # the route. Clone URL and default branch are re-resolved from GitLab's
    # project API response on every operation; they are never accepted from a
    # proxy request body.
    async def _gitlab_project_for_git(
        request: Request,
        project_id: int,
    ) -> tuple[GitLabClient, GitLabProjectInfo, str, str]:
        project_id = _require_gitlab_project_id(settings, project_id)
        base_url = settings.gitlab_base_url
        if base_url is None:
            raise HTTPException(503, "GitLab proxy is not configured")
        token = _resolve_gitlab_project_token(settings, project_id)
        gitlab = _gitlab_project_client(request, project_id)
        try:
            project = await gitlab.get_project(project_id)
        except GitLabError as exc:
            raise HTTPException(exc.status, _scrub_gitlab_tokens(exc.message, settings)) from exc
        # Validate ID and clone destination before the path is used for either
        # filesystem selection or token-scoped git authentication.
        _trusted_gitlab_clone_url(base_url, project, project_id=project_id)
        return gitlab, project, base_url, token

    @app.post("/gl/v1/projects/{project_id}/git/clone")
    async def gitlab_clone_endpoint(request: Request, project_id: int) -> JSONResponse:
        data = await _json_body(request)
        if data:
            raise HTTPException(400, "GitLab clone request body must be empty")
        _, project, base_url, token = await _gitlab_project_for_git(request, project_id)
        trusted_url = _trusted_gitlab_clone_url(base_url, project, project_id=project_id)
        default_branch = _require_fetch_ref(project.default_branch)
        target = _gitlab_pool_dir(settings, project)
        try:
            await _run_git_op(
                git_clone,
                target,
                clone_url=trusted_url,
                default_branch=default_branch,
                token=token,
                auth_url=trusted_url,
            )
        except GitCommandError as exc:
            return _git_error_response(exc, settings=settings)
        return JSONResponse({"pool_dir": str(target)})

    @app.post("/gl/v1/projects/{project_id}/git/fetch")
    async def gitlab_fetch_endpoint(request: Request, project_id: int) -> JSONResponse:
        data = await _json_body(request)
        if data:
            raise HTTPException(400, "GitLab fetch request body must be empty")
        _, project, base_url, token = await _gitlab_project_for_git(request, project_id)
        target = _gitlab_pool_dir(settings, project)
        remote = await asyncio.to_thread(
            _gitlab_origin_remote_auth,
            target,
            base_url=base_url,
            project=project,
            project_id=project_id,
            token=token,
        )
        try:
            await _run_git_op(
                git_fetch_prune,
                target,
                token=remote.token,
                remote_url=remote.url,
                auth_url=remote.auth_url,
            )
        except GitCommandError as exc:
            return _git_error_response(exc, settings=settings)
        return JSONResponse({"pool_dir": str(target)})

    @app.post("/gl/v1/projects/{project_id}/git/fetch_ref")
    async def gitlab_fetch_ref_endpoint(request: Request, project_id: int) -> JSONResponse:
        data = await _json_body(request)
        ref = _require_fetch_ref(data.get("ref"))
        _, project, base_url, token = await _gitlab_project_for_git(request, project_id)
        target = _gitlab_pool_dir(settings, project)
        remote = await asyncio.to_thread(
            _gitlab_origin_remote_auth,
            target,
            base_url=base_url,
            project=project,
            project_id=project_id,
            token=token,
        )
        # `git_fetch_ref` intentionally degrades a fetch failure into the
        # following checkout's more actionable error, matching GitHub mode.
        await _run_git_op(
            git_fetch_ref,
            target,
            ref,
            token=remote.token,
            remote_url=remote.url,
            auth_url=remote.auth_url,
            timeout=settings.gh_proxy_git_timeout_seconds,
        )
        return JSONResponse({"pool_dir": str(target)})

    @app.post("/gl/v1/projects/{project_id}/git/push")
    async def gitlab_push_endpoint(request: Request, project_id: int) -> JSONResponse:
        data = await _json_body(request)
        workspace_key = _require_str(data.get("workspace_key"), "workspace_key")
        branch = _require_str(data.get("branch"), "branch")
        expected_head = _require_str(data.get("expected_head"), "expected_head")
        slot_uid = _optional_slot_uid(data.get("slot_uid"))
        _, project, base_url, token = await _gitlab_project_for_git(request, project_id)
        instance = re.sub(r"[^A-Za-z0-9._-]+", "__", settings.gitlab_instance_id)
        expected_prefix = f"{instance}__{project_id}__"
        if not workspace_key.startswith(expected_prefix):
            raise HTTPException(400, "workspace_key does not match GitLab project")
        repo_dir = _workspace_repo_dir(settings, workspace_key)
        if not repo_dir.is_dir():
            raise HTTPException(404, f"workspace not found: {workspace_key}")
        remote = await asyncio.to_thread(
            _gitlab_origin_remote_auth,
            repo_dir,
            base_url=base_url,
            project=project,
            project_id=project_id,
            token=token,
            push=True,
            slot_uid=slot_uid,
        )
        try:
            result = await _run_git_op(
                git_push,
                repo_dir,
                branch=branch,
                expected_head=expected_head,
                token=remote.token,
                remote_url=remote.url,
                auth_url=remote.auth_url,
                slot_uid=slot_uid,
            )
        except HeadDriftError as exc:
            return _git_error_response(exc, head_drift=True, settings=settings)
        except GitCommandError as exc:
            return _git_error_response(exc, settings=settings)
        return JSONResponse({"head": result.head, "branch": result.branch})

    # Expose for tests
    app.state.workspace_key_fn = compute_workspace_key  # type: ignore[attr-defined]
    return app


__all__ = ["create_proxy_app"]
