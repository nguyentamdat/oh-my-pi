# roboomp

Self-hosted GitHub triage bot. Drives [`omp --mode rpc`](https://github.com/can1357/oh-my-pi)
as a subprocess against a per-issue git worktree, then writes back to GitHub
through a sidecar that holds the PAT.

On `issues.opened` in an allowlisted repo it classifies the issue, labels it,
and branches:

- `bug` / `documentation` → reproduce, fix on a fresh branch, open a PR whose
  body has `## Repro` / `## Cause` / `## Fix` / `## Verification` and
  `Fixes #N`.
- `question` → one comment, suffixed with a 👎-to-keep-open prompt; if the
  issue author doesn't react 👎 within `ROBOMP_QUESTION_AUTOCLOSE_HOURS`
  (default 4), the issue auto-closes as `state_reason=completed`. A follow-up
  comment or external close cancels the schedule synchronously.
- `enhancement` / `proposal` → one comment, no PR.
- `invalid` / `duplicate` → one brief comment.

Follow-up issue comments and PR review comments resume the same omp session
(`--continue` against the persisted JSONL transcript). On orchestrator
restart, in-flight events are re-queued and resume the same way.

## Architecture

Two containers, one trust boundary:

- **robomp** — FastAPI + sqlite event queue + `WorkerPool` running `omp` in
  per-issue worktrees under `/data/workspaces/`. Holds the HMAC key, never
  the PAT.
- **gh-proxy** — sibling on an `internal: true` network. Holds `GITHUB_TOKEN`,
  verifies HMAC-signed requests from robomp, executes REST + `git push`.
  Only egress to `api.github.com`.

Flow: webhook → HMAC verify → `github_events.route` → sqlite `events`
(dedup on `X-GitHub-Delivery`) → `WorkerPool` claims under
`BEGIN IMMEDIATE` with an in-process `_inflight` set per `(owner, repo, n)`
→ `sandbox.ensure_workspace` produces a worktree on `farm/<8hex>/<slug>`
→ `worker.run_task` spawns `omp --mode rpc` with `cwd=worktree`,
persistent `session_dir`, model randomly drawn from `ROBOMP_MODEL` (CSV).

The agent uses omp's built-in tools (`read`/`edit`/`bash`/`lsp`, scoped to
the worktree) plus the host tools in `src/host_tools.py` — the
exclusive surface for GitHub writes. Every host-tool invocation is audited
into the `tool_calls` table with credential-redacted args and results.

## Setup

Requires Docker Compose v2 and a LiteLLM-style proxy on the host that your
`~/.omp/agent/models.container.yml` points at (mounted into the container as `models.yml`; kept under a separate filename on the host so the host omp doesn't route through the gateway). roboomp lives inside the oh-my-pi
monorepo at `python/robomp/`; both the docker build context and the
`/work/pi` bind mount default to the parent monorepo (`../..`). Override
`PI_ROOT` only if you want a different oh-my-pi checkout backing the build
and runtime.

Bot account needs **Write** on every repo in `ROBOMP_REPO_ALLOWLIST`. A
fine-grained PAT with Contents / Issues / Pull requests RW + Metadata R is
enough.

```bash
cp .env.example .env
$EDITOR .env
openssl rand -hex 32              # ROBOMP_GH_PROXY_HMAC_KEY
openssl rand -hex 32              # GITHUB_WEBHOOK_SECRET

bun run pi:image                  # build oh-my-pi/pi:dev (one-time / on pi change)
bun run robomp:build && bun run robomp:up
curl -fsS http://localhost:8080/healthz
```

The bundled `docker-compose.yml` runs in gh-proxy mode by default. To run
the orchestrator directly with the PAT in-process (host CLI, tests),
comment out `ROBOMP_GH_PROXY_URL` / `ROBOMP_GH_PROXY_HMAC_KEY` and set
`GITHUB_TOKEN`. The two modes are mutually exclusive (`config.py`
rejects a `.env` setting both).

Build invalidation is bounded: editing roboomp Python touches only the
runtime layer; editing pi source rebuilds `oh-my-pi/pi:dev`, which
roboomp's `Dockerfile.robomp` extends via `FROM ${PI_BASE}`.

### Public URL

roboomp does not ship a tunnel. Cloudflare, smee, ngrok are all fine. The
recommended ingress rule exposes only configured webhook routes:
`/webhook/github` and/or `/webhook/gitlab-zingplay`. `/healthz`, `/events`,
`/issues`, `/replay`, and `/api/*` stay localhost-only.

### GitHub webhook

In *Settings → Webhooks*: payload URL `https://…/webhook/github`, content
type `application/json`, secret = `GITHUB_WEBHOOK_SECRET`, events =
*Issues, Issue comments, Pull requests, Pull request reviews, Pull
request review comments*. GitHub's `ping` should produce
`POST /webhook/github 202` within a second.

### GitLab project webhooks and intake routing

GitLab integration is keyed by immutable numeric project IDs. It supports:

- **Single-project mode:** omit `ROBOMP_GITLAB_ROUTING_POLICY`; an issue starts
  work only after it carries `ROBOMP_GITLAB_TRIGGER_LABEL`.
- **Routing mode:** configure one intake project plus explicit target projects.
  Deterministic policy evidence is augmented by bounded Hindsight recall and an
  OpenAI-compatible local classifier before any repository is cloned.

The orchestrator receives routing metadata plus only the two classifier API
keys; forge credentials remain proxy-only:

```dotenv
ROBOMP_GITLAB_BASE_URL=https://gitlab.example.com
ROBOMP_GITLAB_PROJECT_IDS=2080,356
ROBOMP_GITLAB_BOT_LOGIN=<legacy-primary-bot-login>
ROBOMP_GITLAB_BOT_LOGINS=<routing-bot>,<intake-bot>,<target-bot>
ROBOMP_GITLAB_TRIGGER_LABEL=roboomp
ROBOMP_GITLAB_WEBHOOK_SECRET=<random-webhook-secret>
ROBOMP_GITLAB_ROUTING_POLICY={"intake_project_id":2080,"targets":[{"key":"server","project_id":356,"mode":"auto_implement","default_branch":"main","paths":["server"],"aliases":["server"],"signals":["server runtime"]}]}
ROBOMP_ROUTING_LLM_BASE_URL=https://litellm.example.com/v1
ROBOMP_ROUTING_LLM_API_KEY=<local-model-api-key>
ROBOMP_ROUTING_LLM_MODEL=local-model-mini
ROBOMP_ROUTING_LLM_TIMEOUT_SECONDS=90
ROBOMP_HINDSIGHT_BASE_URL=http://hindsight:8888
ROBOMP_HINDSIGHT_API_KEY=<hindsight-tenant-api-key>
ROBOMP_HINDSIGHT_BANK=omp
```

Recall is untrusted context: it can rank policy targets but cannot supply the
issue quote required for an automatic route. Unknown targets, malformed JSON,
timeouts, and API failures fail closed to deterministic evidence or human
routing. The classifier may return multiple directly affected targets.

Every policy target declares one mode:

- `recommend`: keep the issue in intake, add `needs-routing` and
  `suggest::<target>`, and wait for a maintainer.
- `auto_move`: move a high-confidence issue, persist source-to-target lineage,
  and add `routed` without starting repository work.
- `auto_implement`: move first, then queue normal target-project triage only
  after the canonical target project and IID are durable.

`route::<target>` labels are deterministic maintainer overrides. One label
keeps the single-target behavior above. Multiple labels, or multiple classifier
targets at confidence 0.85 or higher, keep the intake issue in place and create
one idempotent linked child in every selected project:

- `recommend` children receive `routed` but remain dormant for a maintainer.
- `auto_move` children receive `routed` and a completed routing event.
- `auto_implement` children receive `routed`; their durable synthetic event
  adds the trigger label and queues normal target-project triage exactly once.

Before the first child is created, roboomp validates every target branch and
persists the complete target set. Retry recovery uses a random per-child marker,
does not duplicate children or source notes, and continues unfinished targets.
Single-target move recovery still resolves GitLab's global issue ID through
GraphQL and reads the issue through the allowlisted destination project; no
administrator API or `sudo` scope is required.

The credential-proxy—not the orchestrator—receives either the legacy token or
the routed credential set:

```dotenv
# Legacy single-token mode
ROBOMP_GITLAB_TOKEN=<project-or-group-access-token>

# Routed mode
ROBOMP_GITLAB_ROUTING_TOKEN=<group-access-token-for-move-and-recovery>
ROBOMP_GITLAB_PROJECT_TOKENS_JSON={"2080":"<intake-token>","356":"<target-token>"}
```

Use `api` and `write_repository` scopes with the minimum project/group role
needed. The routing token is used only for cross-project move/recovery. Each
project token is selected by exact immutable project ID for ordinary API and
Git operations. None of these token values may enter the orchestrator or agent
environment. List every token's authenticated username in
`ROBOMP_GITLAB_BOT_LOGINS`; their issue, label, move, and note webhooks are
ignored to prevent feedback loops.

Install the same **project hook** on the intake project and every allowlisted
target. A group hook is neither required nor used:

- URL: `https://<robomp-host>/webhook/<instance-id>`
- Secret token: same value as `ROBOMP_GITLAB_WEBHOOK_SECRET`
- Enable: **Issues events**, **Comments**
- SSL verification: enabled

GitLab uses `X-Gitlab-Token`; the receiver compares it in constant time.
Re-delivery of the same event UUID is idempotent. Validate routing with a
single-target recommendation, a single automatic move, and a multi-target
issue. The multi-target case must retain the intake issue, create exactly one
linked child per selected project, leave `recommend` children dormant, and
queue `auto_implement` children with task kind `triage_issue`.

### Configuration

See `.env.example` for the authoritative variable list. The shipped
`docker-compose.yml` uses per-service `environment:` allowlists rather
than `env_file:`, so `GITHUB_TOKEN` only reaches the gh-proxy container.

## CLI

The container entrypoint is `python -m robomp serve`. Other commands run
inside the running container:

```bash
docker compose exec robomp robomp triage  owner/repo#123   # synthesize an issues.opened and wait
docker compose exec robomp robomp replay  <delivery_id>    # re-enqueue a stored event and wait
docker compose exec robomp robomp status                   # dump issues table
docker compose exec robomp robomp cleanup owner/repo#123   # force workspace removal, state=abandoned
```

`bun run robomp:…` shortcuts in the root `package.json` cover the common
lifecycle commands (`robomp:dev`, `robomp:build`, `robomp:up`, `robomp:down`,
`robomp:logs`, `robomp:restart`, `robomp:reset`).

## Tests

```bash
pytest -x tests/                              # unit suite, no network
ROBOMP_INTEGRATION=1 pytest -x tests/test_worker_smoke.py
```

The integration test spawns a real `omp --mode rpc` against an
`httpx.MockTransport` GitHub and a local bare repo, so it needs `omp` on
`PATH`. `bun run test:py` runs the unit suite.

## Security posture

- `GITHUB_TOKEN` lives only in the gh-proxy container. The orchestrator
  refuses to start if it sees `GITHUB_TOKEN` in its own environment.
- Orchestrator → gh-proxy is HMAC-SHA256 signed with a ±30s skew window
  and constant-time compare.
- `git push` inside gh-proxy uses `git -c http.extraheader=…` with the
  token passed through an ephemeral process env var; the remote URL in
  `.git/config` stays token-free.
- gh-proxy has no host port. The `robomp_internal` network is
  `internal: true` (no ingress, no egress); gh-proxy joins `default`
  only to reach `api.github.com`.
- Agent subprocess env is scrubbed of `GITHUB_TOKEN` /
  `ROBOMP_GH_PROXY_HMAC_KEY` / friends via `worker._SCRUBBED_ENV_KEYS`.
- Webhook signatures: bad sig → `401` (so GitHub stops retrying), never
  `5xx`.
- `git` errors flow through `git_ops.GitCommandError` which redacts
  `https://user:pw@host` to `https://***@host` from argv, stdout, stderr
  before raising. `host_tools._audit` only records agent-supplied args.
- Pre-push gates (`forge_push_branch`): branch matches the workspace
  branch, working tree clean, every commit on
  `origin/<default>..HEAD` carries `ROBOMP_GIT_AUTHOR_NAME` +
  `ROBOMP_GIT_AUTHOR_EMAIL`. Commit messages carrying shell-literal
  `\n` escapes (agents quoting `git commit -m 'a\n\nb'`) are rewritten
  to real newlines — message-only, trees/identities/dates preserved.
- Pre-PR gates (`forge_open_change`): when the repo defines them, `bun run fix`
  runs first (any diff amended into the agent's HEAD commit — no
  standalone `style:` noise commits) and then
  `bun check`. A failing `bun check` returns to the agent as
  `RpcCommandError` for iteration.
- `forge_open_change` validates `## Repro` / `## Cause` / `## Fix` /
  `## Verification` headers and a `Fixes`/`Closes`/`Resolves #N`
  reference before opening.

## Operational notes

- **One PR per issue.** Follow-up events push amendments to the same
  `farm/<hex>/<slug>` branch.
- **No PR without a recorded repro.** Persona prompt requires
  `repro_record`; `mark_unable_to_reproduce` asks for missing details,
  marks the row `needs_info`, and resumes the same session on the next reply.
- **Crash recovery.** On startup, `db.reset_stuck_running()` flips
  `running` rows back to `queued`. Existing `<session_dir>/*.jsonl`
  triggers `--continue`. Drain bounded by
  `ROBOMP_SHUTDOWN_DRAIN_TIMEOUT_SECONDS` (25s) +
  `ROBOMP_SHUTDOWN_KILL_TIMEOUT_SECONDS` (5s); compose
  `stop_grace_period: 30s` covers both.
- **Logs.** Structured JSON on stdout, rotated to
  `/data/logs/robomp.log.jsonl`.
- **Inspection** (localhost only): `GET /events?limit=N`,
  `GET /issues?limit=N`, `GET /healthz`, `GET /readyz`, and the
  dashboard at `/`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `401 invalid signature` | `GITHUB_WEBHOOK_SECRET` mismatch with the repo webhook config. |
| Container exits with `PI_ROOT … missing` | `/work/pi` mount empty inside the container; on the host either run `docker compose` from `python/robomp/` so `PI_ROOT` defaults to `../..`, or export `PI_ROOT` to a valid oh-my-pi checkout. |
| `git push: Authentication required` | Bot PAT lacks push, or `ROBOMP_BOT_LOGIN` does not identify the PAT account's mention handle (production: `roboomp`, no `@`/`[bot]`). |
| `refusing to push: commit author identity mismatch` | Some commit not authored as `ROBOMP_GIT_AUTHOR_*`. The error lists the offending shas; `git commit --amend --reset-author --no-edit`. |
| `refusing to push: working tree is dirty` | Uncommitted agent edits. Or just call `forge_open_change`, which auto-commits `bun run fix` output. |
| `bun check failed before PR creation` | Fix the reported failure and retry `forge_open_change`. |
| `Failed to load pi_natives` | Wrong arch / missing native. `bun run pi:image` then `bun run robomp:build`. |
| `No API key found for <provider>` | `~/.omp/agent/models.container.yml` mount missing or provider id mismatch with `ROBOMP_MODEL`. |

## Layout

```
src/
  server.py          FastAPI app, /webhook/github, /events, /issues, /replay, dashboard at /
  github_events.py   verify_signature + route()
  queue.py           WorkerPool, dispatch loop, per-issue _inflight serialization
  tasks.py           triage_issue, handle_comment, handle_pr_conversation, handle_review, cleanup_workspace
  worker.py          synchronous omp RPC driver, prompt assembly, env scrubbing
  host_tools.py      classify_issue, set_issue_labels, forge_post_comment, repro_record,
                     forge_push_branch, forge_open_change, forge_request_review,
                     mark_unable_to_reproduce, abort_task, fetch_issue_thread
  sandbox.py         clone pool + worktree lifecycle
  github_client.py   typed httpx client; webhook payload parsing
  proxy_client.py    GitHubProxyClient + HMAC signer
  db.py              sqlite schema + DAOs
  config.py          pydantic Settings; mode-exclusive PAT vs gh-proxy validation
  cli.py             serve / triage / replay / status / cleanup
  prompts/           system_append.md + per-task kickoff templates
tests/               pytest unit suite + one ROBOMP_INTEGRATION=1 smoke test
web/                 vite + solid dashboard, built into src/static/
```

## License

MIT.
