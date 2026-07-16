import type { RoutingFlow, RoutingTarget, StatusResponse } from "./types";
import { buildWorkItems, type WorkItem } from "./work-items";

const ROUTING_STATE_LABELS: Readonly<Record<RoutingTarget["state"], string>> = {
  planned: "creating child",
  queued: "queued",
  running: "running",
  done: "done",
  failed: "failed",
  skipped: "skipped",
};

export interface PipelineView {
  flows: RoutingFlow[];
  items: WorkItem[];
}

export function routingTargetStatus(target: RoutingTarget): string {
  if (target.state === "planned") return ROUTING_STATE_LABELS.planned;
  if (target.task_kind === "triage_issue") {
    if (target.state === "queued") return "implementation queued";
    if (target.state === "running") return "implementing";
    if (target.state === "done") return "implementation finished";
  }
  if (target.mode === "recommend" && target.state === "done") {
    return "awaiting maintainer";
  }
  if (target.state === "done") return "routed";
  return ROUTING_STATE_LABELS[target.state];
}

// The status endpoint already returns flows newest-first and targets in numeric
// project order. Keep that ordering intact while removing child issues from the
// ordinary pipeline so each routed task has one visual home.
export function buildPipelineView(status: StatusResponse): PipelineView {
  const routedKeys = new Set<string>();
  const routedRefs = new Set<string>();

  for (const flow of status.routing_flows) {
    for (const target of flow.targets) {
      if (target.canonical_key) routedKeys.add(target.canonical_key);
      if (target.repo && target.number != null)
        routedRefs.add(`${target.repo}#${target.number}`);
    }
  }

  return {
    flows: status.routing_flows,
    items: buildWorkItems(status).filter(
      (item) =>
        !routedKeys.has(item.key) &&
        !(item.ref && routedRefs.has(`${item.ref.repo}#${item.ref.number}`)),
    ),
  };
}
