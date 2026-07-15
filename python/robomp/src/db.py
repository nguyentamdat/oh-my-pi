"""SQLite-backed durable event queue + bot state."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

EventState = Literal["queued", "running", "done", "failed", "skipped"]
INACTIVE_EVENT_STATES: tuple[EventState, ...] = ("done", "failed", "skipped")

DEFAULT_GITHUB_INSTANCE = "github-main"

IssueState = Literal[
    "new",
    "reproducing",
    "fixing",
    "reviewing",
    "opened",
    "merged",
    "closed",
    "needs_info",
    "abandoned",
]

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
  instance_id    TEXT NOT NULL,
  delivery_id   TEXT NOT NULL,
  event_type    TEXT NOT NULL,
  canonical_event TEXT,
  task_kind     TEXT,
  repo          TEXT,
  repository_id TEXT,
  item_kind     TEXT,
  item_number   INTEGER,
  canonical_key TEXT,
  issue_key     TEXT,
  payload_json  TEXT NOT NULL,
  received_at   TEXT NOT NULL,
  state         TEXT NOT NULL
    CHECK (state IN ('queued','running','done','failed','skipped')),
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  started_at    TEXT,
  finished_at   TEXT,
  model         TEXT,
  available_at  TEXT,
  PRIMARY KEY (instance_id, delivery_id)
);

CREATE INDEX IF NOT EXISTS events_state_received
  ON events(state, received_at);


CREATE INDEX IF NOT EXISTS events_issue_state
  ON events(issue_key, state);


CREATE TABLE IF NOT EXISTS routing_lineage (
  source_canonical_key TEXT PRIMARY KEY,
  target_canonical_key TEXT NOT NULL,
  created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS routing_lineage_target_canonical_key
  ON routing_lineage(target_canonical_key);

CREATE TABLE IF NOT EXISTS routing_intents (
  source_canonical_key TEXT PRIMARY KEY,
  target_project_id   TEXT NOT NULL,
  target_canonical_key TEXT,
  created_at           TEXT NOT NULL,
  completed_at         TEXT
);
CREATE INDEX IF NOT EXISTS routing_intents_incomplete
  ON routing_intents(created_at)
  WHERE target_canonical_key IS NULL;

CREATE TABLE IF NOT EXISTS routing_children (
  source_canonical_key TEXT NOT NULL,
  target_project_id    TEXT NOT NULL,
  mode                 TEXT NOT NULL,
  idempotency_token   TEXT NOT NULL,
  target_canonical_key TEXT,
  target_delivery_id   TEXT,
  created_at           TEXT NOT NULL,
  completed_at         TEXT,
  PRIMARY KEY (source_canonical_key, target_project_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS routing_children_target_canonical_key
  ON routing_children(target_canonical_key)
  WHERE target_canonical_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS routing_children_incomplete
  ON routing_children(created_at)
  WHERE target_canonical_key IS NULL;

CREATE TABLE IF NOT EXISTS routing_decisions (
  instance_id          TEXT NOT NULL,
  delivery_id          TEXT NOT NULL,
  source_canonical_key TEXT NOT NULL,
  candidates_json      TEXT NOT NULL,
  selected_target_key  TEXT,
  selected_project_id  TEXT,
  explicit             INTEGER NOT NULL CHECK (explicit IN (0, 1)),
  action               TEXT NOT NULL,
  mode                 TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  PRIMARY KEY (instance_id, delivery_id)
);
CREATE INDEX IF NOT EXISTS routing_decisions_source_created
  ON routing_decisions(source_canonical_key, created_at);
CREATE TABLE IF NOT EXISTS issues (
  key            TEXT PRIMARY KEY,
  repo           TEXT NOT NULL,
  number         INTEGER NOT NULL,
  branch         TEXT,
  session_dir    TEXT,
  pr_number      INTEGER,
  state          TEXT NOT NULL,
  classification TEXT,         -- bug|enhancement|question|proposal|documentation|invalid|duplicate
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_key     TEXT NOT NULL,
  tool          TEXT NOT NULL,
  args_json     TEXT NOT NULL,
  result_json   TEXT,
  error         TEXT,
  ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_calls_issue ON tool_calls(issue_key, ts);

CREATE TABLE IF NOT EXISTS pr_review_comments (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_key   TEXT NOT NULL,
  path        TEXT NOT NULL,
  line        INTEGER NOT NULL,
  side        TEXT NOT NULL DEFAULT 'RIGHT',
  start_line  INTEGER,
  start_side  TEXT,
  body        TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pr_review_comments_key
  ON pr_review_comments(issue_key);

CREATE TABLE IF NOT EXISTS submissions (
  delivery_id   TEXT PRIMARY KEY,
  login         TEXT NOT NULL,
  repo          TEXT,
  ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS submissions_login_ts ON submissions(login, ts);

CREATE TABLE IF NOT EXISTS pending_closures (
  issue_key     TEXT PRIMARY KEY,
  repo          TEXT NOT NULL,
  number        INTEGER NOT NULL,
  comment_id    INTEGER NOT NULL,
  issue_author  TEXT NOT NULL,
  close_at      TEXT NOT NULL,
  state         TEXT NOT NULL CHECK (state IN ('pending','claimed','closed','cancelled')),
  cancel_reason TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_closures_state_close_at
  ON pending_closures(state, close_at);
"""


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _utc_after(seconds: float) -> str:
    """UTC timestamp `seconds` in the future, same sortable format as `_utcnow`."""
    return (datetime.now(UTC) + timedelta(seconds=max(seconds, 0.0))).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def iso_seconds_ago(seconds: float) -> str:
    """ISO-UTC timestamp for `seconds` ago, matching the format `_utcnow` writes."""
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(slots=True, frozen=True)
class EventRow:
    delivery_id: str
    event_type: str
    repo: str | None
    issue_key: str | None
    payload: dict[str, Any]
    received_at: str
    state: EventState
    attempts: int
    last_error: str | None
    instance_id: str = DEFAULT_GITHUB_INSTANCE
    repository_id: str | None = None
    item_kind: str | None = None
    item_number: int | None = None
    canonical_key: str | None = None
    canonical_event: str | None = None
    task_kind: str | None = None

    @property
    def queue_key(self) -> str:
        return self.canonical_key or self.issue_key or f"{self.instance_id}:{self.delivery_id}"

    @property
    def dispatch_id(self) -> str:
        """Cancellation key; legacy GitHub deliveries retain their public ID."""
        if self.instance_id == DEFAULT_GITHUB_INSTANCE:
            return self.delivery_id
        return f"{self.instance_id}:{self.delivery_id}"


@dataclass(slots=True, frozen=True)
class RoutingLineageRow:
    """A durable canonical identity transition created by routing."""

    source_canonical_key: str
    target_canonical_key: str
    created_at: str


@dataclass(slots=True, frozen=True)
class RoutingIntentRow:
    """A durable pre-move routing intent, optionally completed after the move."""

    source_canonical_key: str
    target_project_id: str
    target_canonical_key: str | None
    created_at: str
    completed_at: str | None


@dataclass(slots=True, frozen=True)
class RoutingChildRow:
    """One durable child issue created by a multi-project route."""

    source_canonical_key: str
    target_project_id: str
    idempotency_token: str
    mode: str
    target_canonical_key: str | None
    target_delivery_id: str | None
    created_at: str
    completed_at: str | None


@dataclass(slots=True, frozen=True)
class RoutingDecisionRow:
    """One auditable routing recommendation or explicit route decision."""

    instance_id: str
    delivery_id: str
    source_canonical_key: str
    candidates: list[dict[str, Any]]
    selected_target_key: str | None
    selected_project_id: str | None
    explicit: bool
    action: str
    mode: str
    created_at: str


@dataclass(slots=True, frozen=True)
class IssueRow:
    key: str
    repo: str
    number: int
    branch: str | None
    session_dir: str | None
    pr_number: int | None
    state: IssueState
    updated_at: str
    classification: str | None = None


@dataclass(slots=True, frozen=True)
class StagedReviewComment:
    id: int
    issue_key: str
    path: str
    line: int
    side: str
    body: str
    created_at: str
    start_line: int | None = None
    start_side: str | None = None


def _event_row_from_db_row(row: sqlite3.Row) -> EventRow:
    return EventRow(
        delivery_id=row["delivery_id"],
        event_type=row["event_type"],
        repo=row["repo"],
        issue_key=row["issue_key"],
        payload=json.loads(row["payload_json"]),
        received_at=row["received_at"],
        state=row["state"],
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        instance_id=row["instance_id"],
        repository_id=row["repository_id"],
        item_kind=row["item_kind"],
        item_number=row["item_number"],
        canonical_key=row["canonical_key"],
        canonical_event=row["canonical_event"],
        task_kind=row["task_kind"],
    )


@dataclass(slots=True, frozen=True)
class SubmissionAdmission:
    accepted: bool
    duplicate: bool
    used: int


PendingClosureState = Literal["pending", "claimed", "closed", "cancelled"]


@dataclass(slots=True, frozen=True)
class PendingClosureRow:
    issue_key: str
    repo: str
    number: int
    comment_id: int
    issue_author: str
    close_at: str
    state: PendingClosureState
    cancel_reason: str | None
    created_at: str
    updated_at: str


def _pending_closure_from_row(row: sqlite3.Row) -> PendingClosureRow:
    return PendingClosureRow(
        issue_key=row["issue_key"],
        repo=row["repo"],
        number=int(row["number"]),
        comment_id=int(row["comment_id"]),
        issue_author=row["issue_author"],
        close_at=row["close_at"],
        state=row["state"],
        cancel_reason=row["cancel_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def issue_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def canonical_item_key(instance_id: str, repository_id: str, item_kind: str, number: int) -> str:
    """Stable work-item identity shared by persistence and queue serialization."""
    return f"{instance_id}:{repository_id}:{item_kind}:{number}"


def _legacy_event_metadata(
    event_type: str,
    payload: Mapping[str, Any],
    repo: str | None,
    legacy_issue_key: str | None,
) -> tuple[str | None, str | None, int | None, str | None, str | None]:
    """Infer canonical metadata for pre-forge GitHub rows and callers."""
    repository = payload.get("repository")
    repository_data = repository if isinstance(repository, Mapping) else {}
    raw_repository_id = repository_data.get("id")
    repository_id = str(raw_repository_id) if raw_repository_id not in (None, "") else repo

    action = str(payload.get("action") or "")
    issue = payload.get("issue")
    issue_data = issue if isinstance(issue, Mapping) else {}
    pull_request = payload.get("pull_request")
    pull_request_data = pull_request if isinstance(pull_request, Mapping) else {}

    task_kind: str | None = None
    canonical_event: str | None = None
    item_kind = "issue"
    if event_type == "issues":
        if action == "opened":
            task_kind, canonical_event = "triage_issue", "issue.opened"
        elif action == "closed":
            task_kind, canonical_event = "cleanup_workspace", "issue.closed"
    elif event_type == "issue_comment" and action == "created":
        task_kind = "handle_pr_conversation" if "pull_request" in issue_data else "handle_comment"
        canonical_event = "issue.comment.created"
    elif event_type == "pull_request":
        item_kind = "change"
        if action in {"opened", "reopened", "ready_for_review", "labeled"}:
            task_kind, canonical_event = "review_change", "change.opened"
        elif action == "closed":
            task_kind = "cleanup_workspace"
            canonical_event = "change.merged" if bool(pull_request_data.get("merged")) else "change.closed"
    elif event_type == "pull_request_review_comment" and action == "created":
        task_kind, canonical_event = "handle_review", "change.review.created"

    number: int | None = None
    if legacy_issue_key:
        _, separator, raw_number = legacy_issue_key.rpartition("#")
        if separator:
            try:
                number = int(raw_number)
            except ValueError:
                pass
    if number is None:
        candidate = pull_request_data.get("number") or issue_data.get("number")
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            number = candidate
    return repository_id, item_kind, number, task_kind, canonical_event


class Database:
    """Thread-safe sqlite wrapper. One connection per thread via locks."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    @staticmethod
    def _create_events_v2(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE events_v2 (
              instance_id TEXT NOT NULL,
              delivery_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              canonical_event TEXT,
              task_kind TEXT,
              repo TEXT,
              repository_id TEXT,
              item_kind TEXT,
              item_number INTEGER,
              canonical_key TEXT,
              issue_key TEXT,
              payload_json TEXT NOT NULL,
              received_at TEXT NOT NULL,
              state TEXT NOT NULL
                CHECK (state IN ('queued','running','done','failed','skipped')),
              attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              started_at TEXT,
              finished_at TEXT,
              model TEXT,
              available_at TEXT,
              PRIMARY KEY (instance_id, delivery_id)
            )
            """
        )

    def _migrate_events(self) -> None:
        info = self._conn.execute("PRAGMA table_info(events)").fetchall()
        columns = {row[1] for row in info}
        primary_key = [row[1] for row in sorted((row for row in info if row[5]), key=lambda row: row[5])]
        required = {
            "instance_id",
            "repository_id",
            "item_kind",
            "item_number",
            "canonical_key",
            "canonical_event",
            "task_kind",
            "model",
            "available_at",
        }
        if primary_key == ["instance_id", "delivery_id"] and required <= columns:
            return

        with self._txn() as conn:
            rows = conn.execute("SELECT * FROM events").fetchall()
            conn.execute("DROP TABLE IF EXISTS events_v2")
            self._create_events_v2(conn)
            for row in rows:
                names = set(row.keys())
                payload = json.loads(row["payload_json"])
                legacy_key = row["issue_key"] if "issue_key" in names else None
                repo = row["repo"] if "repo" in names else None
                repository_id, item_kind, item_number, task_kind, canonical_event = _legacy_event_metadata(
                    row["event_type"], payload, repo, legacy_key
                )
                instance_id = (row["instance_id"] if "instance_id" in names else None) or DEFAULT_GITHUB_INSTANCE
                if "repository_id" in names and row["repository_id"]:
                    repository_id = row["repository_id"]
                if "item_kind" in names and row["item_kind"]:
                    item_kind = row["item_kind"]
                if "item_number" in names and row["item_number"] is not None:
                    item_number = int(row["item_number"])
                canonical_key = row["canonical_key"] if "canonical_key" in names else None
                if canonical_key is None and repository_id and item_kind and item_number is not None:
                    canonical_key = canonical_item_key(instance_id, repository_id, item_kind, item_number)
                conn.execute(
                    """
                    INSERT INTO events_v2 (
                      instance_id, delivery_id, event_type, canonical_event, task_kind,
                      repo, repository_id, item_kind, item_number, canonical_key, issue_key,
                      payload_json, received_at, state, attempts, last_error, started_at,
                      finished_at, model, available_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        instance_id,
                        row["delivery_id"],
                        row["event_type"],
                        (row["canonical_event"] if "canonical_event" in names else None) or canonical_event,
                        (row["task_kind"] if "task_kind" in names else None) or task_kind,
                        repo,
                        repository_id,
                        item_kind,
                        item_number,
                        canonical_key,
                        legacy_key,
                        row["payload_json"],
                        row["received_at"],
                        row["state"],
                        row["attempts"] if "attempts" in names else 0,
                        row["last_error"] if "last_error" in names else None,
                        row["started_at"] if "started_at" in names else None,
                        row["finished_at"] if "finished_at" in names else None,
                        row["model"] if "model" in names else None,
                        row["available_at"] if "available_at" in names else None,
                    ),
                )
            conn.execute("DROP TABLE events")
            conn.execute("ALTER TABLE events_v2 RENAME TO events")

        self._conn.execute("CREATE INDEX IF NOT EXISTS events_state_received ON events(state, received_at)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS events_item_state ON events(canonical_key, state)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS events_issue_state ON events(issue_key, state)")

    def _migrate(self) -> None:
        # SQLite-friendly forward migrations. Each is idempotent.
        issue_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(issues)").fetchall()}
        if "classification" not in issue_cols:
            self._conn.execute("ALTER TABLE issues ADD COLUMN classification TEXT")
        routing_child_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(routing_children)").fetchall()}
        if "idempotency_token" not in routing_child_cols:
            self._conn.execute("ALTER TABLE routing_children ADD COLUMN idempotency_token TEXT")
        incomplete_tokens = self._conn.execute(
            "SELECT source_canonical_key, target_project_id FROM routing_children WHERE idempotency_token IS NULL"
        ).fetchall()
        for row in incomplete_tokens:
            self._conn.execute(
                """
                UPDATE routing_children
                SET idempotency_token=?
                WHERE source_canonical_key=? AND target_project_id=?
                """,
                (
                    f"legacy:{row['source_canonical_key']}:{row['target_project_id']}",
                    row["source_canonical_key"],
                    row["target_project_id"],
                ),
            )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS routing_children_idempotency_token "
            "ON routing_children(idempotency_token)"
        )
        self._migrate_events()
        self._conn.execute("CREATE INDEX IF NOT EXISTS events_item_state ON events(canonical_key, state)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    # ---- events ----
    def record_event(
        self,
        *,
        delivery_id: str,
        event_type: str,
        repo: str | None,
        issue_key: str | None,
        payload: Mapping[str, Any],
        state: EventState = "queued",
        last_error: str | None = None,
        instance_id: str = DEFAULT_GITHUB_INSTANCE,
        repository_id: str | None = None,
        item_kind: str | None = None,
        item_number: int | None = None,
        canonical_key: str | None = None,
        canonical_event: str | None = None,
        task_kind: str | None = None,
    ) -> bool:
        """Insert a webhook event, deduplicated within its forge instance."""
        inferred = _legacy_event_metadata(event_type, payload, repo, issue_key)
        repository_id = repository_id or inferred[0]
        item_kind = item_kind or inferred[1]
        item_number = item_number if item_number is not None else inferred[2]
        task_kind = task_kind or inferred[3]
        canonical_event = canonical_event or inferred[4]
        if canonical_key is None and repository_id and item_kind and item_number is not None:
            canonical_key = canonical_item_key(instance_id, repository_id, item_kind, item_number)
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO events (
                  instance_id, delivery_id, event_type, canonical_event, task_kind,
                  repo, repository_id, item_kind, item_number, canonical_key, issue_key,
                  payload_json, received_at, state, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    delivery_id,
                    event_type,
                    canonical_event,
                    task_kind,
                    repo,
                    repository_id,
                    item_kind,
                    item_number,
                    canonical_key,
                    issue_key,
                    json.dumps(payload, separators=(",", ":")),
                    now,
                    state,
                    last_error,
                ),
            )
            return cur.rowcount > 0

    def claim_next_event(self) -> EventRow | None:
        """Atomically dequeue one canonically-unblocked queued event."""
        with self._txn() as conn:
            now = _utcnow()
            row = conn.execute(
                """
                SELECT queued.instance_id, queued.delivery_id, queued.event_type,
                       queued.canonical_event, queued.task_kind, queued.repo,
                       queued.repository_id, queued.item_kind, queued.item_number,
                       queued.canonical_key, queued.issue_key, queued.payload_json,
                       queued.received_at, queued.state, queued.attempts, queued.last_error
                FROM events AS queued
                WHERE queued.state = 'queued'
                  AND (queued.available_at IS NULL OR queued.available_at <= ?)
                  AND (
                    COALESCE(queued.canonical_key, queued.issue_key) IS NULL
                    OR NOT EXISTS (
                      SELECT 1
                      FROM events AS running
                      WHERE running.state = 'running'
                        AND COALESCE(running.canonical_key, running.issue_key)
                            = COALESCE(queued.canonical_key, queued.issue_key)
                    )
                  )
                ORDER BY queued.received_at, queued.rowid
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE events
                SET state='running', attempts=attempts+1, started_at=?
                WHERE instance_id=? AND delivery_id=?
                """,
                (now, row["instance_id"], row["delivery_id"]),
            )
            return EventRow(
                delivery_id=row["delivery_id"],
                event_type=row["event_type"],
                repo=row["repo"],
                issue_key=row["issue_key"],
                payload=json.loads(row["payload_json"]),
                received_at=row["received_at"],
                state="running",
                attempts=int(row["attempts"]) + 1,
                last_error=row["last_error"],
                instance_id=row["instance_id"],
                repository_id=row["repository_id"],
                item_kind=row["item_kind"],
                item_number=row["item_number"],
                canonical_key=row["canonical_key"],
                canonical_event=row["canonical_event"],
                task_kind=row["task_kind"],
            )

    def mark_event(
        self,
        delivery_id: str,
        state: EventState,
        *,
        error: str | None = None,
        instance_id: str = DEFAULT_GITHUB_INSTANCE,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE events SET state=?, last_error=?, finished_at=?
                WHERE instance_id=? AND delivery_id=?
                """,
                (state, error, _utcnow(), instance_id, delivery_id),
            )

    def set_event_model(
        self,
        delivery_id: str,
        model: str,
        *,
        instance_id: str = DEFAULT_GITHUB_INSTANCE,
    ) -> None:
        """Persist the model picked for one forge-instance delivery."""
        with self._lock:
            self._conn.execute(
                "UPDATE events SET model=? WHERE instance_id=? AND delivery_id=?",
                (model, instance_id, delivery_id),
            )

    def reset_stuck_running(self) -> int:
        """Recover events that were running at shutdown."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE events SET state='queued', available_at=NULL WHERE state='running'",
            )
            return cur.rowcount

    def list_events(self, *, limit: int = 50) -> list[EventRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT instance_id, delivery_id, event_type, canonical_event, task_kind,
                       repo, repository_id, item_kind, item_number, canonical_key,
                       issue_key, payload_json, received_at, state, attempts, last_error
                FROM events
                ORDER BY received_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_event_row_from_db_row(row) for row in rows]

    def remove_event(self, delivery_id: str, *, instance_id: str = DEFAULT_GITHUB_INSTANCE) -> None:
        """Hard-delete one forge-instance delivery."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM events WHERE instance_id=? AND delivery_id=?",
                (instance_id, delivery_id),
            )

    def replace_event_if_state_in(
        self,
        *,
        delivery_id: str,
        event_type: str,
        repo: str | None,
        issue_key: str | None,
        payload: Mapping[str, Any],
        state: EventState = "queued",
        allowed_existing_states: tuple[EventState, ...],
        instance_id: str = DEFAULT_GITHUB_INSTANCE,
    ) -> bool:
        """Replace one inactive forge-instance delivery atomically."""
        repository_id, item_kind, item_number, task_kind, canonical_event = _legacy_event_metadata(
            event_type, payload, repo, issue_key
        )
        canonical_key = (
            canonical_item_key(instance_id, repository_id, item_kind, item_number)
            if repository_id and item_kind and item_number is not None
            else None
        )
        now = _utcnow()
        with self._txn() as conn:
            row = conn.execute(
                "SELECT state FROM events WHERE instance_id=? AND delivery_id=?",
                (instance_id, delivery_id),
            ).fetchone()
            if row is not None:
                if row["state"] not in allowed_existing_states:
                    return False
                conn.execute(
                    "DELETE FROM events WHERE instance_id=? AND delivery_id=?",
                    (instance_id, delivery_id),
                )
            conn.execute(
                """
                INSERT INTO events (
                  instance_id, delivery_id, event_type, canonical_event, task_kind,
                  repo, repository_id, item_kind, item_number, canonical_key, issue_key,
                  payload_json, received_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    delivery_id,
                    event_type,
                    canonical_event,
                    task_kind,
                    repo,
                    repository_id,
                    item_kind,
                    item_number,
                    canonical_key,
                    issue_key,
                    json.dumps(payload, separators=(",", ":")),
                    now,
                    state,
                ),
            )
            return True

    def latest_event_for_issue(self, key: str, *, include_skipped: bool = False) -> EventRow | None:
        """Return the newest event for an issue.

        By default this ignores `skipped` rows. Those are usually webhook noise
        (`issues.labeled ignored`, bot/self comments) and must not hide the last
        real processing run when the dashboard retries a failed issue.
        """
        state_filter = "" if include_skipped else "AND state <> 'skipped'"
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT instance_id, delivery_id, event_type, canonical_event, task_kind,
                       repo, repository_id, item_kind, item_number, canonical_key,
                       issue_key, payload_json, received_at, state, attempts, last_error
                FROM events
                WHERE issue_key = ?
                  {state_filter}
                ORDER BY received_at DESC, rowid DESC
                LIMIT 1
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        return _event_row_from_db_row(row)

    def latest_events_for_issues(
        self,
        keys: Iterable[str],
        *,
        include_skipped: bool = False,
    ) -> dict[str, EventRow]:
        """Return newest event rows keyed by issue key for a bounded issue set."""
        unique = tuple({k for k in keys if k})
        if not unique:
            return {}
        state_filter = "" if include_skipped else "AND state <> 'skipped'"
        out: dict[str, EventRow] = {}
        with self._lock:
            for start in range(0, len(unique), 500):
                batch = unique[start : start + 500]
                placeholders = ",".join("?" * len(batch))
                rows = self._conn.execute(
                    f"""
                    SELECT instance_id, delivery_id, event_type, canonical_event, task_kind,
                           repo, repository_id, item_kind, item_number, canonical_key,
                           issue_key, payload_json, received_at, state, attempts, last_error
                    FROM events
                    WHERE issue_key IN ({placeholders})
                      {state_filter}
                    ORDER BY issue_key ASC, received_at DESC, rowid DESC
                    """,
                    batch,
                ).fetchall()
                for row in rows:
                    issue = row["issue_key"]
                    if issue not in out:
                        out[issue] = _event_row_from_db_row(row)
        return out

    def event_state_counts(self) -> dict[str, int]:
        """Return current row counts per event state, including states with zero rows."""
        with self._lock:
            rows = self._conn.execute("SELECT state, COUNT(*) AS n FROM events GROUP BY state").fetchall()
        counts: dict[str, int] = dict.fromkeys(("queued", "running", "done", "failed", "skipped"), 0)
        for row in rows:
            counts[row["state"]] = int(row["n"])
        return counts

    def latest_issue_event_state_counts(self) -> dict[str, int]:
        """Count each issue by its newest non-skipped event state.

        This is the dashboard's "current issue event" view: a later successful
        run clears an older failure for that issue, and ignored webhook noise
        does not make a failed issue look skipped.
        """
        counts: dict[str, int] = dict.fromkeys(("queued", "running", "done", "failed", "skipped"), 0)
        seen: set[str] = set()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT issue_key, state
                FROM events
                WHERE issue_key IS NOT NULL
                  AND state <> 'skipped'
                ORDER BY issue_key ASC, received_at DESC, rowid DESC
                """
            ).fetchall()
        for row in rows:
            key = row["issue_key"]
            if key in seen:
                continue
            seen.add(key)
            counts[row["state"]] += 1
        return counts

    def list_running_events(self) -> list[dict[str, Any]]:
        """Snapshot of currently-running events.

        Returns elapsed-time inputs (`started_at`) plus per-run telemetry:
        - `model`: the omp model the worker picked for this run, set after
          `pick_model()` so it reflects the actual pool selection.
        - `last_tool` / `last_tool_ts`: the most recent host-tool call audited
          on the same `issue_key` since `started_at`. Scoping by start time
          prevents stale entries from a prior run on the same issue leaking
          into the dashboard before this run has emitted any tool calls.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT e.instance_id, e.delivery_id, e.event_type, e.canonical_event,
                       e.task_kind, e.repo, e.repository_id, e.item_kind, e.item_number,
                       e.canonical_key, e.issue_key, e.received_at, e.started_at, e.attempts, e.model,
                       (SELECT tool FROM tool_calls
                          WHERE issue_key = e.issue_key AND ts >= e.started_at
                          ORDER BY ts DESC LIMIT 1) AS last_tool,
                       (SELECT ts FROM tool_calls
                          WHERE issue_key = e.issue_key AND ts >= e.started_at
                          ORDER BY ts DESC LIMIT 1) AS last_tool_ts
                FROM events e
                WHERE e.state = 'running'
                ORDER BY COALESCE(e.started_at, e.received_at)
                """
            ).fetchall()
        return [
            {
                "delivery_id": r["delivery_id"],
                "event_type": r["event_type"],
                "repo": r["repo"],
                "issue_key": r["issue_key"],
                "received_at": r["received_at"],
                "started_at": r["started_at"],
                "attempts": int(r["attempts"]),
                "model": r["model"],
                "last_tool": r["last_tool"],
                "last_tool_ts": r["last_tool_ts"],
            }
            for r in rows
        ]

    def get_event(
        self,
        delivery_id: str,
        *,
        instance_id: str = DEFAULT_GITHUB_INSTANCE,
    ) -> EventRow | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT instance_id, delivery_id, event_type, canonical_event, task_kind,
                       repo, repository_id, item_kind, item_number, canonical_key,
                       issue_key, payload_json, received_at, state, attempts, last_error
                FROM events
                WHERE instance_id=? AND delivery_id=?
                """,
                (instance_id, delivery_id),
            ).fetchone()
        return _event_row_from_db_row(row) if row is not None else None

    def has_activated_item(self, canonical_key: str) -> bool:
        """Return whether a canonical item has an accepted activation event."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                FROM events
                WHERE canonical_key=?
                  AND task_kind IN ('triage_issue', 'review_change')
                  AND state <> 'skipped'
                LIMIT 1
                """,
                (canonical_key,),
            ).fetchone()
        return row is not None

    def resolve_routing_lineage(self, source_canonical_key: str) -> RoutingLineageRow | None:
        """Resolve the target identity previously assigned to a source identity."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT source_canonical_key, target_canonical_key, created_at
                FROM routing_lineage
                WHERE source_canonical_key=?
                """,
                (source_canonical_key,),
            ).fetchone()
        if row is None:
            return None
        return RoutingLineageRow(
            source_canonical_key=row["source_canonical_key"],
            target_canonical_key=row["target_canonical_key"],
            created_at=row["created_at"],
        )

    def resolve_routed_target(self, target_canonical_key: str) -> RoutingLineageRow | None:
        """Return one lineage that produced this target identity, if any."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT source_canonical_key, target_canonical_key, created_at
                FROM routing_lineage
                WHERE target_canonical_key=?
                ORDER BY created_at, source_canonical_key
                LIMIT 1
                """,
                (target_canonical_key,),
            ).fetchone()
        if row is None:
            return None
        return RoutingLineageRow(
            source_canonical_key=row["source_canonical_key"],
            target_canonical_key=row["target_canonical_key"],
            created_at=row["created_at"],
        )

    def is_routed_target(self, target_canonical_key: str) -> bool:
        """Whether ingress/dispatch should treat a canonical target as routed."""
        if self.resolve_routed_target(target_canonical_key) is not None:
            return True
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM routing_children
                WHERE target_canonical_key=?
                LIMIT 1
                """,
                (target_canonical_key,),
            ).fetchone()
        return row is not None

    def has_synthetic_routing_event(self, target_canonical_key: str) -> bool:
        """Whether a routed target has its durable synthetic activation event."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM events
                WHERE canonical_key=? AND delivery_id LIKE 'route:%' AND task_kind='triage_issue'
                LIMIT 1
                """,
                (target_canonical_key,),
            ).fetchone()
        return row is not None

    def _persist_routing_lineage_event(
        self,
        conn: sqlite3.Connection,
        *,
        source_canonical_key: str,
        target_canonical_key: str,
        target_delivery_id: str,
        target_event_type: str,
        target_repo: str | None,
        target_issue_key: str | None,
        target_payload: Mapping[str, Any],
        target_repository_id: str,
        target_item_kind: str,
        target_item_number: int,
        target_canonical_event: str | None,
        target_task_kind: str | None,
        target_instance_id: str,
        now: str,
    ) -> RoutingLineageRow:
        existing = conn.execute(
            """
            SELECT source_canonical_key, target_canonical_key, created_at
            FROM routing_lineage
            WHERE source_canonical_key=?
            """,
            (source_canonical_key,),
        ).fetchone()
        if existing is not None:
            if existing["target_canonical_key"] != target_canonical_key:
                raise ValueError("routing lineage conflict: source canonical key already has a different target")
            lineage = RoutingLineageRow(
                source_canonical_key=existing["source_canonical_key"],
                target_canonical_key=existing["target_canonical_key"],
                created_at=existing["created_at"],
            )
        else:
            conn.execute(
                """
                INSERT INTO routing_lineage (
                  source_canonical_key, target_canonical_key, created_at
                ) VALUES (?, ?, ?)
                """,
                (source_canonical_key, target_canonical_key, now),
            )
            lineage = RoutingLineageRow(
                source_canonical_key=source_canonical_key,
                target_canonical_key=target_canonical_key,
                created_at=now,
            )

        conn.execute(
            """
            INSERT OR IGNORE INTO events (
              instance_id, delivery_id, event_type, canonical_event, task_kind,
              repo, repository_id, item_kind, item_number, canonical_key, issue_key,
              payload_json, received_at, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')
            """,
            (
                target_instance_id,
                target_delivery_id,
                target_event_type,
                target_canonical_event,
                target_task_kind,
                target_repo,
                target_repository_id,
                target_item_kind,
                target_item_number,
                target_canonical_key,
                target_issue_key,
                json.dumps(target_payload, separators=(",", ":")),
                now,
            ),
        )
        return lineage

    def record_routing_lineage_event(
        self,
        *,
        source_canonical_key: str,
        target_delivery_id: str,
        target_event_type: str,
        target_repo: str | None,
        target_issue_key: str | None,
        target_payload: Mapping[str, Any],
        target_repository_id: str,
        target_item_kind: str,
        target_item_number: int,
        target_canonical_event: str | None = None,
        target_task_kind: str | None = None,
        target_instance_id: str = DEFAULT_GITHUB_INSTANCE,
    ) -> RoutingLineageRow:
        """Atomically persist a route and its exact synthetic queued event."""
        if not source_canonical_key:
            raise ValueError("source_canonical_key is required")
        if not target_instance_id or not target_repository_id or not target_item_kind:
            raise ValueError("target canonical identity fields are required")
        if isinstance(target_item_number, bool) or not isinstance(target_item_number, int):
            raise ValueError("target_item_number must be an integer")
        target_canonical_key = canonical_item_key(
            target_instance_id,
            target_repository_id,
            target_item_kind,
            target_item_number,
        )
        with self._txn() as conn:
            return self._persist_routing_lineage_event(
                conn,
                source_canonical_key=source_canonical_key,
                target_canonical_key=target_canonical_key,
                target_delivery_id=target_delivery_id,
                target_event_type=target_event_type,
                target_repo=target_repo,
                target_issue_key=target_issue_key,
                target_payload=target_payload,
                target_repository_id=target_repository_id,
                target_item_kind=target_item_kind,
                target_item_number=target_item_number,
                target_canonical_event=target_canonical_event,
                target_task_kind=target_task_kind,
                target_instance_id=target_instance_id,
                now=_utcnow(),
            )

    def begin_routing_intent(
        self,
        source_canonical_key: str,
        target_project_id: str | int,
    ) -> RoutingIntentRow:
        """Durably record a planned target project before performing a move."""
        if not source_canonical_key:
            raise ValueError("source_canonical_key is required")
        if isinstance(target_project_id, bool):
            raise ValueError("target_project_id must be a string or integer")
        normalized_target_project_id = str(target_project_id)
        if not normalized_target_project_id:
            raise ValueError("target_project_id is required")
        now = _utcnow()
        with self._txn() as conn:
            existing = conn.execute(
                """
                SELECT source_canonical_key, target_project_id, target_canonical_key, created_at, completed_at
                FROM routing_intents
                WHERE source_canonical_key=?
                """,
                (source_canonical_key,),
            ).fetchone()
            if existing is not None:
                if existing["target_project_id"] != normalized_target_project_id:
                    raise ValueError("routing intent conflict: source canonical key already has a different target")
                return RoutingIntentRow(
                    source_canonical_key=existing["source_canonical_key"],
                    target_project_id=existing["target_project_id"],
                    target_canonical_key=existing["target_canonical_key"],
                    created_at=existing["created_at"],
                    completed_at=existing["completed_at"],
                )
            conn.execute(
                """
                INSERT INTO routing_intents (
                  source_canonical_key, target_project_id, created_at
                ) VALUES (?, ?, ?)
                """,
                (source_canonical_key, normalized_target_project_id, now),
            )
        return RoutingIntentRow(
            source_canonical_key=source_canonical_key,
            target_project_id=normalized_target_project_id,
            target_canonical_key=None,
            created_at=now,
            completed_at=None,
        )

    def get_incomplete_routing_intent(self, source_canonical_key: str) -> RoutingIntentRow | None:
        """Return the pre-move intent that still needs post-move completion."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT source_canonical_key, target_project_id, target_canonical_key, created_at, completed_at
                FROM routing_intents
                WHERE source_canonical_key=? AND target_canonical_key IS NULL
                """,
                (source_canonical_key,),
            ).fetchone()
        if row is None:
            return None
        return RoutingIntentRow(
            source_canonical_key=row["source_canonical_key"],
            target_project_id=row["target_project_id"],
            target_canonical_key=row["target_canonical_key"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    def complete_routing_intent_event(
        self,
        *,
        source_canonical_key: str,
        target_delivery_id: str,
        target_event_type: str,
        target_repo: str | None,
        target_issue_key: str | None,
        target_payload: Mapping[str, Any],
        target_repository_id: str,
        target_item_kind: str,
        target_item_number: int,
        target_canonical_event: str | None = None,
        target_task_kind: str | None = None,
        target_instance_id: str = DEFAULT_GITHUB_INSTANCE,
    ) -> RoutingLineageRow:
        """Complete a pre-move intent with the moved target and queued event."""
        if not source_canonical_key:
            raise ValueError("source_canonical_key is required")
        if not target_instance_id or not target_repository_id or not target_item_kind:
            raise ValueError("target canonical identity fields are required")
        if isinstance(target_item_number, bool) or not isinstance(target_item_number, int):
            raise ValueError("target_item_number must be an integer")
        target_canonical_key = canonical_item_key(
            target_instance_id,
            target_repository_id,
            target_item_kind,
            target_item_number,
        )
        now = _utcnow()
        with self._txn() as conn:
            intent = conn.execute(
                """
                SELECT source_canonical_key, target_project_id, target_canonical_key, created_at, completed_at
                FROM routing_intents
                WHERE source_canonical_key=?
                """,
                (source_canonical_key,),
            ).fetchone()
            if intent is None:
                raise ValueError("routing intent not found")
            if intent["target_project_id"] != target_repository_id:
                raise ValueError("routing intent conflict: completed target project differs from planned target")
            if intent["target_canonical_key"] is not None:
                if intent["target_canonical_key"] != target_canonical_key:
                    raise ValueError("routing intent conflict: source canonical key already has a different target")
            else:
                conn.execute(
                    """
                    UPDATE routing_intents
                    SET target_canonical_key=?, completed_at=?
                    WHERE source_canonical_key=?
                    """,
                    (target_canonical_key, now, source_canonical_key),
                )
            return self._persist_routing_lineage_event(
                conn,
                source_canonical_key=source_canonical_key,
                target_canonical_key=target_canonical_key,
                target_delivery_id=target_delivery_id,
                target_event_type=target_event_type,
                target_repo=target_repo,
                target_issue_key=target_issue_key,
                target_payload=target_payload,
                target_repository_id=target_repository_id,
                target_item_kind=target_item_kind,
                target_item_number=target_item_number,
                target_canonical_event=target_canonical_event,
                target_task_kind=target_task_kind,
                target_instance_id=target_instance_id,
                now=now,
            )

    def plan_routing_children(
        self,
        source_canonical_key: str,
        targets: Iterable[tuple[str | int, str]],
    ) -> list[RoutingChildRow]:
        """Atomically persist the complete child target set before remote creates."""
        if not source_canonical_key:
            raise ValueError("routing child source is required")
        planned: dict[str, str] = {}
        for target_project_id, mode in targets:
            if isinstance(target_project_id, bool) or not mode:
                raise ValueError("routing child target project and mode are required")
            project_id = str(target_project_id)
            if not project_id or project_id in planned:
                raise ValueError("routing child targets must be non-empty and unique")
            planned[project_id] = mode
        if not planned:
            raise ValueError("at least one routing child target is required")
        now = _utcnow()
        with self._txn() as conn:
            rows = conn.execute(
                """
                SELECT source_canonical_key, target_project_id, mode, idempotency_token,
                       target_canonical_key, target_delivery_id, created_at, completed_at
                FROM routing_children
                WHERE source_canonical_key=?
                ORDER BY target_project_id
                """,
                (source_canonical_key,),
            ).fetchall()
            if rows:
                existing = {row["target_project_id"]: row["mode"] for row in rows}
                if existing != planned:
                    raise ValueError("routing child plan conflicts with the existing target set")
            else:
                conn.executemany(
                    """
                    INSERT INTO routing_children (
                      source_canonical_key, target_project_id, mode, idempotency_token, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (source_canonical_key, project_id, mode, secrets.token_hex(24), now)
                        for project_id, mode in planned.items()
                    ),
                )
                rows = conn.execute(
                    """
                    SELECT source_canonical_key, target_project_id, mode, idempotency_token,
                           target_canonical_key, target_delivery_id, created_at, completed_at
                    FROM routing_children
                    WHERE source_canonical_key=?
                    ORDER BY target_project_id
                    """,
                    (source_canonical_key,),
                ).fetchall()
        return [RoutingChildRow(**dict(row)) for row in rows]

    def list_routing_children(self, source_canonical_key: str) -> list[RoutingChildRow]:
        """Return every planned or completed child for a source issue."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT source_canonical_key, target_project_id, mode, idempotency_token,
                       target_canonical_key, target_delivery_id, created_at, completed_at
                FROM routing_children
                WHERE source_canonical_key=?
                ORDER BY target_project_id
                """,
                (source_canonical_key,),
            ).fetchall()
        return [RoutingChildRow(**dict(row)) for row in rows]

    def complete_routing_child_event(
        self,
        *,
        source_canonical_key: str,
        target_project_id: str | int,
        target_delivery_id: str,
        target_event_type: str,
        target_repo: str | None,
        target_issue_key: str | None,
        target_payload: Mapping[str, Any],
        target_item_kind: str,
        target_item_number: int,
        target_canonical_event: str | None = None,
        target_task_kind: str | None = None,
        target_instance_id: str = DEFAULT_GITHUB_INSTANCE,
    ) -> RoutingChildRow:
        """Complete one child and atomically queue its synthetic target event."""
        project_id = str(target_project_id)
        target_canonical_key = canonical_item_key(
            target_instance_id,
            project_id,
            target_item_kind,
            target_item_number,
        )
        now = _utcnow()
        with self._txn() as conn:
            row = conn.execute(
                """
                SELECT source_canonical_key, target_project_id, mode, idempotency_token,
                       target_canonical_key, target_delivery_id, created_at, completed_at
                FROM routing_children
                WHERE source_canonical_key=? AND target_project_id=?
                """,
                (source_canonical_key, project_id),
            ).fetchone()
            if row is None:
                raise ValueError("routing child intent not found")
            if row["target_canonical_key"] not in (None, target_canonical_key):
                raise ValueError("routing child conflict: target identity changed")
            if row["target_delivery_id"] not in (None, target_delivery_id):
                raise ValueError("routing child conflict: target delivery changed")
            existing_activation = None
            if target_task_kind == "triage_issue":
                existing_activation = conn.execute(
                    """
                    SELECT delivery_id FROM events
                    WHERE canonical_key=?
                      AND task_kind='triage_issue'
                      AND state <> 'skipped'
                      AND delivery_id NOT LIKE 'route:%'
                    ORDER BY received_at, delivery_id
                    LIMIT 1
                    """,
                    (target_canonical_key,),
                ).fetchone()
            synthetic_state = "skipped" if existing_activation is not None else "queued"
            synthetic_error = (
                f"activation already owned by delivery {existing_activation['delivery_id']}"
                if existing_activation is not None
                else None
            )
            conn.execute(
                """
                UPDATE routing_children
                SET target_canonical_key=?, target_delivery_id=?, completed_at=?
                WHERE source_canonical_key=? AND target_project_id=?
                """,
                (target_canonical_key, target_delivery_id, now, source_canonical_key, project_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO events (
                  instance_id, delivery_id, event_type, canonical_event, task_kind,
                  repo, repository_id, item_kind, item_number, canonical_key, issue_key,
                  payload_json, received_at, state, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_instance_id,
                    target_delivery_id,
                    target_event_type,
                    target_canonical_event,
                    target_task_kind,
                    target_repo,
                    project_id,
                    target_item_kind,
                    target_item_number,
                    target_canonical_key,
                    target_issue_key,
                    json.dumps(target_payload, separators=(",", ":")),
                    now,
                    synthetic_state,
                    synthetic_error,
                ),
            )
            completed = conn.execute(
                """
                SELECT source_canonical_key, target_project_id, mode, idempotency_token,
                       target_canonical_key, target_delivery_id, created_at, completed_at
                FROM routing_children
                WHERE source_canonical_key=? AND target_project_id=?
                """,
                (source_canonical_key, project_id),
            ).fetchone()
        assert completed is not None
        return RoutingChildRow(**dict(completed))

    def record_routing_decision(
        self,
        *,
        instance_id: str,
        delivery_id: str,
        source_canonical_key: str,
        ranked_candidates: Iterable[Mapping[str, Any]],
        selected_target_key: str | None,
        selected_project_id: str | int | None,
        explicit: bool,
        action: str,
        mode: str,
    ) -> RoutingDecisionRow:
        """Persist one delivery's auditable routing outcome without routing imports."""
        if not instance_id or not delivery_id or not source_canonical_key:
            raise ValueError("routing decision identity fields are required")
        if not action or not mode:
            raise ValueError("routing decision action and mode are required")
        if isinstance(explicit, bool) is False:
            raise ValueError("explicit must be a boolean")
        if isinstance(selected_project_id, bool):
            raise ValueError("selected_project_id must be a string, integer, or None")
        candidates_json = json.dumps([dict(candidate) for candidate in ranked_candidates], separators=(",", ":"))
        normalized_project_id = None if selected_project_id is None else str(selected_project_id)
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO routing_decisions (
                  instance_id, delivery_id, source_canonical_key, candidates_json,
                  selected_target_key, selected_project_id, explicit, action, mode, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id, delivery_id) DO UPDATE SET
                  source_canonical_key=excluded.source_canonical_key,
                  candidates_json=excluded.candidates_json,
                  selected_target_key=excluded.selected_target_key,
                  selected_project_id=excluded.selected_project_id,
                  explicit=excluded.explicit,
                  action=excluded.action,
                  mode=excluded.mode
                """,
                (
                    instance_id,
                    delivery_id,
                    source_canonical_key,
                    candidates_json,
                    selected_target_key,
                    normalized_project_id,
                    int(explicit),
                    action,
                    mode,
                    now,
                ),
            )
            row = self._conn.execute(
                """
                SELECT instance_id, delivery_id, source_canonical_key, candidates_json,
                       selected_target_key, selected_project_id, explicit, action, mode, created_at
                FROM routing_decisions
                WHERE instance_id=? AND delivery_id=?
                """,
                (instance_id, delivery_id),
            ).fetchone()
        assert row is not None
        return RoutingDecisionRow(
            instance_id=row["instance_id"],
            delivery_id=row["delivery_id"],
            source_canonical_key=row["source_canonical_key"],
            candidates=json.loads(row["candidates_json"]),
            selected_target_key=row["selected_target_key"],
            selected_project_id=row["selected_project_id"],
            explicit=bool(row["explicit"]),
            action=row["action"],
            mode=row["mode"],
            created_at=row["created_at"],
        )

    def list_routing_decisions(
        self,
        source_canonical_key: str,
        *,
        limit: int = 50,
    ) -> list[RoutingDecisionRow]:
        """Return an ordered, bounded audit trail for one source identity."""
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT instance_id, delivery_id, source_canonical_key, candidates_json,
                       selected_target_key, selected_project_id, explicit, action, mode, created_at
                FROM routing_decisions
                WHERE source_canonical_key=?
                ORDER BY created_at, rowid
                LIMIT ?
                """,
                (source_canonical_key, limit),
            ).fetchall()
        return [
            RoutingDecisionRow(
                instance_id=row["instance_id"],
                delivery_id=row["delivery_id"],
                source_canonical_key=row["source_canonical_key"],
                candidates=json.loads(row["candidates_json"]),
                selected_target_key=row["selected_target_key"],
                selected_project_id=row["selected_project_id"],
                explicit=bool(row["explicit"]),
                action=row["action"],
                mode=row["mode"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def has_authorized_impl_event(self, issue_key: str) -> bool:
        """Return whether a non-skipped event on this issue carried implementation authorization."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT payload_json
                FROM events
                WHERE issue_key = ?
                  AND state <> 'skipped'
                ORDER BY received_at DESC
                """,
                (issue_key,),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            directive = payload.get("_robomp_directive")
            if isinstance(directive, dict) and directive.get("authorizes_impl") is True:
                return True
        return False

    def requeue_event(
        self,
        delivery_id: str,
        *,
        from_states: tuple[EventState, ...] | None = None,
        instance_id: str = DEFAULT_GITHUB_INSTANCE,
    ) -> bool:
        """Move one forge-instance event back to queued."""
        with self._lock:
            if from_states is None:
                cur = self._conn.execute(
                    """
                    UPDATE events SET state='queued', available_at=NULL
                    WHERE instance_id=? AND delivery_id=?
                    """,
                    (instance_id, delivery_id),
                )
            elif not from_states:
                return False
            else:
                placeholders = ",".join("?" for _ in from_states)
                cur = self._conn.execute(
                    f"""
                    UPDATE events SET state='queued', available_at=NULL
                    WHERE instance_id=? AND delivery_id=? AND state IN ({placeholders})
                    """,
                    (instance_id, delivery_id, *from_states),
                )
            return cur.rowcount > 0

    def schedule_retry(
        self,
        delivery_id: str,
        *,
        delay_seconds: float,
        error: str | None = None,
        instance_id: str = DEFAULT_GITHUB_INSTANCE,
    ) -> bool:
        """Schedule one forge-instance delivery for retry."""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE events
                SET state='queued', last_error=?, available_at=?, finished_at=NULL
                WHERE instance_id=? AND delivery_id=? AND state IN ('running','failed')
                """,
                (error, _utc_after(delay_seconds), instance_id, delivery_id),
            )
            return cur.rowcount > 0

    # ---- issues ----
    def upsert_issue(
        self,
        *,
        key: str,
        repo: str,
        number: int,
        state: IssueState,
        branch: str | None = None,
        session_dir: str | None = None,
        pr_number: int | None = None,
    ) -> IssueRow:
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO issues (key, repo, number, branch, session_dir, pr_number, state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  branch = COALESCE(excluded.branch, issues.branch),
                  session_dir = COALESCE(excluded.session_dir, issues.session_dir),
                  pr_number = COALESCE(excluded.pr_number, issues.pr_number),
                  state = excluded.state,
                  updated_at = excluded.updated_at
                """,
                (key, repo, number, branch, session_dir, pr_number, state, now),
            )
        got = self.get_issue(key)
        assert got is not None
        return got

    def set_issue_state(self, key: str, state: IssueState) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE issues SET state=?, updated_at=? WHERE key=?",
                (state, _utcnow(), key),
            )

    def set_issue_pr(self, key: str, pr_number: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE issues SET pr_number=?, updated_at=? WHERE key=?",
                (pr_number, _utcnow(), key),
            )

    def set_issue_classification(self, key: str, classification: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE issues SET classification=?, updated_at=? WHERE key=?",
                (classification, _utcnow(), key),
            )

    def set_issue_branch(self, key: str, branch: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE issues SET branch=?, updated_at=? WHERE key=?",
                (branch, _utcnow(), key),
            )

    def get_issue(self, key: str) -> IssueRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, repo, number, branch, session_dir, pr_number, state, classification, updated_at FROM issues WHERE key=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return IssueRow(
            key=row["key"],
            repo=row["repo"],
            number=int(row["number"]),
            branch=row["branch"],
            session_dir=row["session_dir"],
            pr_number=int(row["pr_number"]) if row["pr_number"] is not None else None,
            state=row["state"],
            updated_at=row["updated_at"],
            classification=row["classification"],
        )

    def find_issue_by_pr(self, repo: str, pr_number: int) -> IssueRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, repo, number, branch, session_dir, pr_number, state, classification, updated_at FROM issues WHERE repo=? AND pr_number=?",
                (repo, pr_number),
            ).fetchone()
        if row is None:
            return None
        return IssueRow(
            key=row["key"],
            repo=row["repo"],
            number=int(row["number"]),
            branch=row["branch"],
            session_dir=row["session_dir"],
            pr_number=int(row["pr_number"]),
            state=row["state"],
            updated_at=row["updated_at"],
            classification=row["classification"],
        )

    def find_issue_by_branch(self, repo: str, branch: str) -> IssueRow | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT key, repo, number, branch, session_dir, pr_number, state, classification, updated_at
                FROM issues
                WHERE repo=? AND branch=?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (repo, branch),
            ).fetchone()
        if row is None:
            return None
        return IssueRow(
            key=row["key"],
            repo=row["repo"],
            number=int(row["number"]),
            branch=row["branch"],
            session_dir=row["session_dir"],
            pr_number=int(row["pr_number"]) if row["pr_number"] is not None else None,
            state=row["state"],
            updated_at=row["updated_at"],
            classification=row["classification"],
        )

    def list_issues(self, limit: int = 100) -> list[IssueRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, repo, number, branch, session_dir, pr_number, state, classification, updated_at FROM issues ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            IssueRow(
                key=r["key"],
                repo=r["repo"],
                number=int(r["number"]),
                branch=r["branch"],
                session_dir=r["session_dir"],
                pr_number=int(r["pr_number"]) if r["pr_number"] is not None else None,
                state=r["state"],
                updated_at=r["updated_at"],
                classification=r["classification"],
            )
            for r in rows
        ]

    def processed_issue_keys(self, keys: Iterable[str]) -> set[str]:
        """Return the subset of `keys` that have a row in the `issues` table.

        Membership in `issues` means robomp has at minimum upserted state for the
        issue — i.e. it has been picked up by the dispatcher at least once. Used
        by the browse panel to hide issues we've already started on.
        """
        unique = tuple({k for k in keys if k})
        if not unique:
            return set()
        # SQLite parameter limit is 999 by default; chunk to stay well under it.
        out: set[str] = set()
        with self._lock:
            for start in range(0, len(unique), 500):
                batch = unique[start : start + 500]
                placeholders = ",".join("?" * len(batch))
                rows = self._conn.execute(
                    f"SELECT key FROM issues WHERE key IN ({placeholders})",
                    batch,
                ).fetchall()
                out.update(r["key"] for r in rows)
        return out

    # ---- tool_calls ----
    def log_tool_call(
        self,
        *,
        issue_key: str,
        tool: str,
        args: Mapping[str, Any],
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO tool_calls (issue_key, tool, args_json, result_json, error, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    issue_key,
                    tool,
                    json.dumps(args, separators=(",", ":"), default=str),
                    json.dumps(result, separators=(",", ":"), default=str) if result is not None else None,
                    error,
                    _utcnow(),
                ),
            )
            return int(cur.lastrowid or 0)

    def has_successful_tool_call(self, issue_key: str, tool: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                FROM tool_calls
                WHERE issue_key=? AND tool=? AND error IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (issue_key, tool),
            ).fetchone()
        return row is not None

    # ---- PR review comment staging ----
    def stage_review_comment(
        self,
        *,
        issue_key: str,
        path: str,
        line: int,
        body: str,
        side: str = "RIGHT",
        start_line: int | None = None,
        start_side: str | None = None,
    ) -> StagedReviewComment:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO pr_review_comments
                  (issue_key, path, line, side, start_line, start_side, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (issue_key, path, line, side, start_line, start_side, body, _utcnow()),
            )
            row = self._conn.execute(
                """
                SELECT id, issue_key, path, line, side, start_line, start_side, body, created_at
                FROM pr_review_comments
                WHERE id=?
                """,
                (int(cur.lastrowid or 0),),
            ).fetchone()
        assert row is not None
        return StagedReviewComment(
            id=int(row["id"]),
            issue_key=row["issue_key"],
            path=row["path"],
            line=int(row["line"]),
            side=row["side"],
            body=row["body"],
            created_at=row["created_at"],
            start_line=int(row["start_line"]) if row["start_line"] is not None else None,
            start_side=row["start_side"],
        )

    def list_staged_review_comments(self, issue_key: str) -> list[StagedReviewComment]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, issue_key, path, line, side, start_line, start_side, body, created_at
                FROM pr_review_comments
                WHERE issue_key=?
                ORDER BY id
                """,
                (issue_key,),
            ).fetchall()
        return [
            StagedReviewComment(
                id=int(row["id"]),
                issue_key=row["issue_key"],
                path=row["path"],
                line=int(row["line"]),
                side=row["side"],
                body=row["body"],
                created_at=row["created_at"],
                start_line=int(row["start_line"]) if row["start_line"] is not None else None,
                start_side=row["start_side"],
            )
            for row in rows
        ]

    def clear_staged_review_comments(self, issue_key: str) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM pr_review_comments WHERE issue_key=?", (issue_key,))
            return int(cur.rowcount or 0)

    # ---- submissions (per-user rate limiting) ----
    def admit_submission(
        self,
        *,
        delivery_id: str,
        login: str,
        repo: str | None,
        since: str,
        cap: int | None,
    ) -> SubmissionAdmission:
        """Atomically check a submitter's rolling cap and record this delivery.

        Duplicate delivery ids are accepted without inserting a second row, so a
        webhook retry remains idempotent even after the submitter reaches the cap.
        `used` is the matching submission count after acceptance, or the count
        that caused rejection when `accepted` is False.
        """
        normalized_login = login.lower()
        with self._txn() as conn:
            existing = conn.execute(
                "SELECT 1 FROM submissions WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            if existing is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM submissions WHERE login=? AND ts>=?",
                    (normalized_login, since),
                ).fetchone()
                return SubmissionAdmission(
                    accepted=True,
                    duplicate=True,
                    used=int(row["n"]) if row is not None else 0,
                )

            row = conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE login=? AND ts>=?",
                (normalized_login, since),
            ).fetchone()
            used = int(row["n"]) if row is not None else 0
            if cap is not None and used >= cap:
                return SubmissionAdmission(accepted=False, duplicate=False, used=used)

            conn.execute(
                "INSERT INTO submissions (delivery_id, login, repo, ts) VALUES (?, ?, ?, ?)",
                (delivery_id, normalized_login, repo, _utcnow()),
            )
            return SubmissionAdmission(accepted=True, duplicate=False, used=used + 1)

    def record_submission(
        self,
        *,
        delivery_id: str,
        login: str,
        repo: str | None,
    ) -> bool:
        """Idempotently log a queue-worthy submission by `login`.

        Returns False if the delivery_id was already recorded (webhook retry).
        """
        now = _utcnow()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO submissions (delivery_id, login, repo, ts) VALUES (?, ?, ?, ?)",
                (delivery_id, login.lower(), repo, now),
            )
            return cur.rowcount > 0

    def count_submissions_since(self, login: str, since: str) -> int:
        """Count submissions by `login` (case-insensitive) with ts >= `since`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM submissions WHERE login=? AND ts>=?",
                (login.lower(), since),
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    # ---- pending_closures ----
    def upsert_pending_closure(
        self,
        *,
        issue_key: str,
        repo: str,
        number: int,
        comment_id: int,
        issue_author: str,
        close_at: str,
    ) -> None:
        """Schedule (or reschedule) a question issue to auto-close.

        A follow-up bot answer on the same issue overwrites the prior schedule:
        we always watch the latest comment and can roll the close_at forward.
        Resets state to `pending` and clears any prior cancel_reason so a row
        previously closed/cancelled becomes a live schedule again.
        """
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pending_closures
                  (issue_key, repo, number, comment_id, issue_author, close_at,
                   state, cancel_reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                ON CONFLICT(issue_key) DO UPDATE SET
                  repo = excluded.repo,
                  number = excluded.number,
                  comment_id = excluded.comment_id,
                  issue_author = excluded.issue_author,
                  close_at = excluded.close_at,
                  state = 'pending',
                  cancel_reason = NULL,
                  updated_at = excluded.updated_at
                """,
                (issue_key, repo, number, comment_id, issue_author.lower(), close_at, now, now),
            )

    def claim_due_closures(self, *, now: str, limit: int = 50) -> list[PendingClosureRow]:
        """Atomically flip due `pending` rows to `claimed` and return them.

        Atomic claim prevents two scheduler ticks (or a tick racing a
        cancellation) from acting on the same row twice. Caller is responsible
        for finalizing each claimed row via `finalize_closure` or returning
        it to `pending` via `requeue_claimed_closure` after a transient error.
        """
        with self._txn() as conn:
            rows = conn.execute(
                """
                UPDATE pending_closures
                SET state = 'claimed', updated_at = ?
                WHERE issue_key IN (
                  SELECT issue_key FROM pending_closures
                  WHERE state = 'pending' AND close_at <= ?
                  ORDER BY close_at
                  LIMIT ?
                )
                RETURNING issue_key, repo, number, comment_id, issue_author,
                          close_at, state, cancel_reason, created_at, updated_at
                """,
                (now, now, int(limit)),
            ).fetchall()
        return [_pending_closure_from_row(row) for row in rows]

    def finalize_closure(
        self,
        issue_key: str,
        *,
        state: PendingClosureState,
        reason: str | None,
    ) -> None:
        """Mark a claimed row terminal (`closed` / `cancelled`)."""
        if state not in ("closed", "cancelled"):
            raise ValueError(f"finalize_closure: invalid terminal state {state!r}")
        with self._lock:
            self._conn.execute(
                """
                UPDATE pending_closures
                SET state = ?, cancel_reason = ?, updated_at = ?
                WHERE issue_key = ?
                """,
                (state, reason, _utcnow(), issue_key),
            )

    def requeue_claimed_closure(self, issue_key: str) -> bool:
        """Return a `claimed` row to `pending` so the next tick retries it.

        Used by the scheduler when a transient GitHub error prevents the
        close from completing. Only flips `claimed -> pending`; rows in any
        other state are left untouched.
        """
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE pending_closures
                SET state = 'pending', updated_at = ?
                WHERE issue_key = ? AND state = 'claimed'
                """,
                (_utcnow(), issue_key),
            )
            return cur.rowcount > 0

    def cancel_pending_closure(self, issue_key: str, *, reason: str) -> bool:
        """Cancel a scheduled close. No-op when state is not `pending`.

        A row already `claimed` is left for the scheduler tick that owns it
        to finalize — racing a cancel against a claim must not double-write
        the row's terminal state.
        """
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE pending_closures
                SET state = 'cancelled', cancel_reason = ?, updated_at = ?
                WHERE issue_key = ? AND state = 'pending'
                """,
                (reason, _utcnow(), issue_key),
            )
            return cur.rowcount > 0

    def get_pending_closure(self, issue_key: str) -> PendingClosureRow | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT issue_key, repo, number, comment_id, issue_author,
                       close_at, state, cancel_reason, created_at, updated_at
                FROM pending_closures WHERE issue_key = ?
                """,
                (issue_key,),
            ).fetchone()
        return _pending_closure_from_row(row) if row is not None else None


_DB_SINGLETON: Database | None = None
_DB_LOCK = threading.Lock()


def get_database(path: Path) -> Database:
    global _DB_SINGLETON
    with _DB_LOCK:
        if _DB_SINGLETON is None or _DB_SINGLETON.path != path:
            if _DB_SINGLETON is not None:
                _DB_SINGLETON.close()
            _DB_SINGLETON = Database(path)
        return _DB_SINGLETON


def close_database() -> None:
    global _DB_SINGLETON
    with _DB_LOCK:
        if _DB_SINGLETON is not None:
            _DB_SINGLETON.close()
            _DB_SINGLETON = None
