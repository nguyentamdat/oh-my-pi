import { createMemo, For, type JSX, Show } from "solid-js";

import { buildPipelineView } from "../routing-flows";
import { statusResource } from "../state";
import type { RoutingFlow } from "../types";
import type { WorkItem } from "../work-items";
import { IssueCard } from "./IssueCard";
import { RoutingFlows } from "./RoutingFlows";

// The unified Operations surface: every active work item as one lifecycle
// card. Subsumes the old running, failed, and active issue tables. Failed
// items sort first; running items show live agent activity. No GlassCard
// wrapper — the cards ARE the card layer (no nested cards).
export function Pipeline(): JSX.Element {
  const view = createMemo(() => {
    const status = statusResource();
    return status ? buildPipelineView(status) : null;
  });
  const flows = (): RoutingFlow[] => view()?.flows ?? [];
  const items = (): WorkItem[] => view()?.items ?? [];
  const hasWork = (): boolean => flows().length > 0 || items().length > 0;

  return (
    <Show when={view() !== null} fallback={<PipelineSkeleton />}>
      <Show when={hasWork()} fallback={<PipelineEmpty />}>
        <Show when={flows().length > 0}>
          <RoutingFlows flows={flows()} />
        </Show>
        <Show when={items().length > 0}>
          <div class="rmp-pipeline-head">
            <span class="rmp-pipeline-head-label">pipeline</span>
            <span class="rmp-pipeline-head-count tabular">
              {items().length}
            </span>
          </div>
          <div class="rmp-card-grid">
            <For each={items()}>{(item) => <IssueCard item={item} />}</For>
          </div>
        </Show>
      </Show>
    </Show>
  );
}

// First-load placeholder — skeleton, not spinner. Two cards so the grid shape
// is legible before data arrives.
function PipelineSkeleton(): JSX.Element {
  return (
    <div class="rmp-card-grid">
      <div class="rmp-card-skeleton" />
      <div class="rmp-card-skeleton" />
    </div>
  );
}

// Teaching empty state: names what the surface does and what it's waiting on,
// plus the watched repos so an idle console still explains itself.
function PipelineEmpty(): JSX.Element {
  const allowlist = (): string[] =>
    statusResource()?.runtime.repo_allowlist ?? [];
  return (
    <div class="rmp-pipeline-empty">
      <div class="rmp-pipeline-empty-title">
        No active work — robomp is idle, waiting for forge webhook events
      </div>
      <Show when={allowlist().length > 0}>
        <div class="rmp-pipeline-empty-sub">
          watching {allowlist().join(", ")}
        </div>
      </Show>
    </div>
  );
}
