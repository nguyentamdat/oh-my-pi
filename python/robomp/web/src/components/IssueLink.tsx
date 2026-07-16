import type { JSX } from "solid-js";

import { issueUrl, prUrl } from "../format";

export interface IssueLinkProps {
  repo: string;
  number: number | string;
  href?: string | null;
}

export function IssueLink(props: IssueLinkProps): JSX.Element {
  const href =
    props.href === undefined ? issueUrl(props.repo, props.number) : props.href;
  const label = (
    <>
      {props.repo}
      <span class="text-ink-400">#</span>
      {props.number}
    </>
  );
  if (href === null) {
    return <span class="font-mono text-[12px] text-ink-100">{label}</span>;
  }
  return (
    <a
      class="font-mono text-[12px] text-ink-100 hover:text-accent-2"
      href={href}
      target="_blank"
      rel="noopener"
    >
      {label}
    </a>
  );
}

export interface PrLinkProps {
  repo: string;
  number: number | string | null | undefined;
  href?: string | null;
}

export function PrLink(props: PrLinkProps): JSX.Element {
  if (props.number == null || props.number === "") {
    return <span class="text-ink-400">—</span>;
  }
  const href =
    props.href === undefined ? prUrl(props.repo, props.number) : props.href;
  if (href === null) {
    return (
      <span class="font-mono text-[12px] text-accent-2">#{props.number}</span>
    );
  }
  return (
    <a
      class="font-mono text-[12px] text-accent-2 hover:underline"
      href={href}
      target="_blank"
      rel="noopener"
    >
      #{props.number}
    </a>
  );
}
