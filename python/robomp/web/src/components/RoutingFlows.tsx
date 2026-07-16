import { For, type JSX, Show } from "solid-js";

import { fmtAge, shortText } from "../format";
import { routingTargetStatus } from "../routing-flows";
import type { RoutingFlow } from "../types";
import { Pill } from "./Pill";

export interface RoutingFlowsProps {
  flows: RoutingFlow[];
}

export function RoutingFlows(props: RoutingFlowsProps): JSX.Element {
  return (
    <section class="rmp-routing" aria-labelledby="routing-heading">
      <div class="rmp-pipeline-head">
        <h2 class="rmp-pipeline-head-label" id="routing-heading">
          routing
        </h2>
        <span class="rmp-pipeline-head-count tabular">
          {props.flows.length}
        </span>
      </div>
      <div class="rmp-routing-list">
        <For each={props.flows}>
          {(flow, index) => {
            const source =
              flow.source_repo && flow.source_number != null
                ? `${flow.source_repo}#${flow.source_number}`
                : flow.source_key;
            return (
              <article
                class="rmp-route"
                aria-labelledby={`routing-source-${index()}`}
              >
                <header class="rmp-route-head">
                  <div class="rmp-route-source">
                    <span class="rmp-route-kicker">source</span>
                    <h3 id={`routing-source-${index()}`}>
                      <IssueAnchor url={flow.source_url} label={source} />
                    </h3>
                  </div>
                  <div class="rmp-route-badges">
                    <Pill class="rmp-route-action">
                      {flow.action.replaceAll("_", " ")}
                    </Pill>
                    <Pill
                      class={
                        flow.explicit
                          ? "rmp-route-explicit"
                          : "rmp-route-automatic"
                      }
                    >
                      {flow.explicit ? "explicit" : "automatic"}
                    </Pill>
                  </div>
                  <time
                    class="rmp-route-created"
                    dateTime={flow.created_at}
                    title={flow.created_at}
                  >
                    {fmtAge(flow.created_at)}
                  </time>
                </header>
                <ol
                  class="rmp-route-targets"
                  aria-label={`Targets for ${source}`}
                >
                  <For each={flow.targets}>
                    {(target) => (
                      <li class="rmp-route-target" data-state={target.state}>
                        <div class="rmp-route-target-main">
                          <span class="rmp-route-target-key">
                            {target.key ?? `project ${target.project_id}`}
                          </span>
                          <span class="rmp-route-project tabular">
                            project {target.project_id}
                          </span>
                          <Show when={target.url}>
                            <IssueAnchor
                              url={target.url}
                              label={
                                target.repo && target.number != null
                                  ? `${target.repo}#${target.number}`
                                  : (target.canonical_key ??
                                    target.key ??
                                    "open issue")
                              }
                            />
                          </Show>
                        </div>
                        <Pill
                          state={target.state}
                          dot={target.state === "running"}
                          class="rmp-route-state"
                        >
                          {routingTargetStatus(target)}
                        </Pill>
                        <div class="rmp-route-target-meta">
                          <span>
                            mode <code>{target.mode}</code>
                          </span>
                          <Show
                            when={
                              target.task_kind &&
                              target.task_kind !== target.mode
                            }
                          >
                            <span>{target.task_kind}</span>
                          </Show>
                          <span class="tabular">
                            {target.attempts === 1
                              ? "1 attempt"
                              : `${target.attempts} attempts`}
                          </span>
                          <Show when={target.started_at}>
                            <time
                              dateTime={target.started_at ?? undefined}
                              title={target.started_at ?? ""}
                            >
                              started {fmtAge(target.started_at)}
                            </time>
                          </Show>
                          <Show when={target.finished_at}>
                            <time
                              dateTime={target.finished_at ?? undefined}
                              title={target.finished_at ?? ""}
                            >
                              finished {fmtAge(target.finished_at)}
                            </time>
                          </Show>
                        </div>
                        <Show when={target.last_error}>
                          <div
                            class="rmp-route-error"
                            title={target.last_error ?? ""}
                          >
                            <strong>error</strong>{" "}
                            {shortText(target.last_error, 240)}
                          </div>
                        </Show>
                      </li>
                    )}
                  </For>
                </ol>
              </article>
            );
          }}
        </For>
      </div>
    </section>
  );
}

function IssueAnchor(props: {
  url: string | null;
  label: string;
}): JSX.Element {
  return (
    <Show when={props.url} fallback={<span>{props.label}</span>}>
      <a
        class="rmp-route-link"
        href={props.url ?? undefined}
        target="_blank"
        rel="noopener noreferrer"
      >
        {props.label}
      </a>
    </Show>
  );
}
