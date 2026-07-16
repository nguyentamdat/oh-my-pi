import { describe, expect, test } from "bun:test";

import { buildPipelineView, routingTargetStatus } from "./routing-flows";
import type {
  EventState,
  RoutingFlow,
  RoutingTarget,
  StatusResponse,
} from "./types";

const ZERO_COUNTS: Record<EventState, number> = {
  queued: 0,
  running: 0,
  done: 0,
  failed: 0,
  skipped: 0,
};

function target(overrides: Partial<RoutingTarget>): RoutingTarget {
  return {
    key: null,
    project_id: "356",
    mode: "auto_implement",
    canonical_key: null,
    repo: null,
    number: null,
    url: null,
    delivery_id: null,
    task_kind: null,
    state: "planned",
    attempts: 0,
    last_error: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

function flow(overrides: Partial<RoutingFlow>): RoutingFlow {
  return {
    source_key: "gitlab:2080:issue:7",
    source_repo: "triage",
    source_number: 7,
    source_url: "https://gitlab.example.test/triage/-/issues/7",
    action: "implementation_queued",
    explicit: false,
    created_at: "2026-07-15T12:00:00Z",
    targets: [],
    ...overrides,
  };
}

function status(routingFlows: RoutingFlow[]): StatusResponse {
  return {
    runtime: {
      bot_login: "robomp",
      repo_allowlist: [],
      max_concurrency: 2,
      model: "test-model",
      thinking_level: "low",
      uptime_seconds: 1,
    },
    event_counts: ZERO_COUNTS,
    issue_event_counts: ZERO_COUNTS,
    running_events: [],
    inflight: [],
    issues: [
      {
        key: "gitlab:356:issue:21",
        repo: "products/client",
        number: 21,
        branch: null,
        pr_number: null,
        state: "new",
        classification: "bug",
        updated_at: "2026-07-15T12:01:00Z",
        latest_event: null,
      },
      {
        key: "gitlab:357:issue:22",
        repo: "products/server",
        number: 22,
        branch: null,
        pr_number: null,
        state: "fixing",
        classification: "bug",
        updated_at: "2026-07-15T12:02:00Z",
        latest_event: null,
      },
      {
        key: "github:other:issue:9",
        repo: "other/standalone",
        number: 9,
        branch: null,
        pr_number: null,
        state: "new",
        classification: "bug",
        updated_at: "2026-07-15T12:03:00Z",
        latest_event: null,
      },
    ],
    recent_events: [],
    routing_flows: routingFlows,
  };
}

describe("routing flow presentation", () => {
  test("keeps source and target order, labels states, and removes duplicate child cards", () => {
    const newest = flow({
      targets: [
        target({
          key: "client",
          project_id: "356",
          canonical_key: "gitlab:356:issue:21",
          repo: "products/client",
          number: 21,
          url: "https://gitlab.example.test/products/client/-/issues/21",
          state: "queued",
        }),
        target({
          key: "server",
          project_id: "357",
          canonical_key: "gitlab:357:issue:22",
          repo: "products/server",
          number: 22,
          url: "https://gitlab.example.test/products/server/-/issues/22",
          delivery_id: "server-delivery",
          task_kind: "triage_issue",
          state: "running",
          attempts: 1,
          started_at: "2026-07-15T12:02:00Z",
        }),
      ],
    });
    const older = flow({
      source_key: "gitlab:2080:issue:6",
      source_number: 6,
      source_url: "https://gitlab.example.test/triage/-/issues/6",
      created_at: "2026-07-15T11:00:00Z",
    });

    const view = buildPipelineView(status([newest, older]));

    expect(view.flows.map((item) => item.source_number)).toEqual([7, 6]);
    expect(
      view.flows[0]?.targets.map((item) => [item.key, item.project_id]),
    ).toEqual([
      ["client", "356"],
      ["server", "357"],
    ]);
    expect([
      routingTargetStatus(target({ state: "planned" })),
      routingTargetStatus(
        target({
          mode: "recommend",
          task_kind: "routing_complete",
          state: "done",
        }),
      ),
      routingTargetStatus(
        target({ task_kind: "triage_issue", state: "queued" }),
      ),
      routingTargetStatus(
        target({ task_kind: "triage_issue", state: "running" }),
      ),
      routingTargetStatus(target({ task_kind: "triage_issue", state: "done" })),
    ]).toEqual([
      "creating child",
      "awaiting maintainer",
      "implementation queued",
      "implementing",
      "implementation finished",
    ]);
    expect(view.items.map((item) => item.key)).toEqual([
      "github:other:issue:9",
    ]);
  });
});
