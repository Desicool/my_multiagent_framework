<script lang="ts">
  import type { Section } from '../../../lib/timeline';
  import type { AgentState } from '../../../lib/types';
  import { events } from '../../../stores/events.svelte';
  import { hhmmss, elapsed } from '../../../lib/time';
  import { shortId } from '../../../lib/format';
  import { agentMonogramBucket, agentMonogramText } from '../../../lib/primitives';

  let {
    section,
    /** Indent level (0 = root, 1 = sub-plan, 2+ deeper). */
    depth = 0,
    /** Number of body milestones in this section (for the count chip). */
    milestones,
    /** Optional label override for the kind chip ("Plan task · failed", …). */
  }: {
    section: Section;
    depth?: number;
    milestones: number;
  } = $props();

  let leader: AgentState | undefined = $derived(section.agent_id ? events.agentsById[section.agent_id] : undefined);

  const monoBg: Record<string, string> = {
    h1: 'bg-violet-500/20 text-violet-300',
    h2: 'bg-emerald-500/20 text-emerald-300',
    h3: 'bg-sky-500/20 text-sky-300',
    h4: 'bg-rose-500/20 text-rose-300',
    h5: 'bg-amber-500/20 text-amber-300',
    h6: 'bg-cyan-500/20 text-cyan-300',
  };
  let monoCls = $derived(section.agent_id ? monoBg[agentMonogramBucket(section.agent_id)] : 'bg-slate-700/40 text-slate-400');
  let mono = $derived(section.agent_id ? agentMonogramText(section.agent_id) : '⌬');
  let role = $derived(leader?.role ?? section.role);

  let kindLabel = $derived.by(() => {
    switch (section.kind) {
      case 'setup': return 'Setup';
      case 'task': return 'Plan task';
      case 'pending': return 'Plan task · pending';
      case 'subplan': return 'Sub-plan task';
      case 'subplan-failed': return 'Sub-plan task · failed';
      case 'teardown': return 'Teardown';
      default: return '';
    }
  });

  let kindClass = $derived.by(() => {
    switch (section.kind) {
      case 'setup':           return 'border-l-muted text-muted';
      case 'task':            return 'border-l-info text-info';
      case 'pending':         return 'border-l-slate-700 text-slate-400 opacity-80';
      case 'subplan':         return 'border-l-info/55 text-info/95';
      case 'subplan-failed':  return 'border-l-error/50 text-error/95';
      case 'teardown':        return 'border-l-muted text-muted';
      default:                return 'border-l-surface-border text-slate-400';
    }
  });

  let secId = $derived(section.task_id ?? (section.kind === 'setup' ? 'root prep' : section.kind === 'teardown' ? 'cascade' : ''));
  let timeRange = $derived.by(() => {
    if (section.startTs === undefined) return '';
    if (section.endTs === undefined || section.endTs === section.startTs) return hhmmss(section.startTs);
    return `${hhmmss(section.startTs)} → ${hhmmss(section.endTs)}`;
  });
  let dur = $derived.by(() => {
    if (section.startTs === undefined || section.endTs === undefined) return '';
    const ms = (section.endTs - section.startTs) * 1000;
    if (ms < 500) return '';
    return elapsed(ms);
  });
</script>

<div class={`grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 items-baseline rounded ${depth === 0 ? 'mt-4 mx-6 px-4 py-3 bg-surface-raised border border-surface-border' : 'p-2.5 bg-surface-raised/55 border border-surface-border/60'} border-l-[3px] ${kindClass.replace(/text-[a-z0-9/-]+/, '')}`}>
  <div class="flex items-baseline gap-2.5 flex-wrap">
    <span class={`text-[9.5px] uppercase tracking-widest font-semibold ${kindClass.replace(/border-l-[a-z0-9/-]+/, '')}`}>{kindLabel}</span>
    {#if secId}
      <span class={`font-mono font-semibold ${depth === 0 ? 'text-[13px]' : 'text-[12px]'} text-slate-100`}>{secId}</span>
    {/if}
    {#if section.role}
      <span class="text-[11.5px] text-slate-400">· {section.role}</span>
    {/if}
    {#if section.depends_on && section.depends_on.length > 0}
      <span class="font-mono text-[10.5px] text-muted bg-slate-200/[0.05] border border-surface-border px-1.5 py-px rounded">depends_on: [{section.depends_on.join(', ')}]</span>
    {/if}
    {#if section.depends_on !== undefined && section.depends_on.length === 0 && section.kind !== 'setup' && section.kind !== 'teardown'}
      <span class="font-mono text-[10.5px] text-muted bg-slate-200/[0.05] border border-surface-border px-1.5 py-px rounded">no deps</span>
    {/if}
  </div>
  <div class="font-mono text-[11px] text-slate-400 tabular-nums whitespace-nowrap text-right">
    {#if section.kind === 'pending'}
      <span>— · awaiting <span class="text-slate-300">{section.pending_for}</span></span>
    {:else}
      {timeRange}
      {#if dur}<span class="text-slate-200 ml-1.5">{dur}</span>{/if}
    {/if}
  </div>

  <div class="col-span-2 flex items-center gap-3 flex-wrap text-[11px] text-slate-400">
    {#if section.agent_id}
      <span class="inline-flex items-center gap-1.5 font-mono text-slate-200">
        <span class={`inline-flex items-center justify-center rounded font-mono font-semibold w-4 h-4 text-[9px] ${monoCls}`}>{mono}</span>
        {shortId(section.agent_id, 12)}
        {#if role}<span class="text-slate-400">· {role}</span>{/if}
      </span>
      <span class="text-surface-border">·</span>
    {/if}
    {#if section.kind !== 'pending'}
      <span class="font-mono tabular-nums">
        <span class="text-slate-200 font-medium">{milestones}</span> milestone{milestones === 1 ? '' : 's'}
      </span>
      {#if section.children.length > 0}
        <span class="text-surface-border">·</span>
        <span class="font-mono tabular-nums"><span class="text-slate-200 font-medium">{section.children.length}</span> nested sub-plan task{section.children.length === 1 ? '' : 's'}</span>
      {/if}
    {/if}
    {#if section.kind === 'subplan' && section.endTs}
      <span class="text-surface-border">·</span>
      <span class="text-info">closed by task_done</span>
    {/if}
    {#if section.kind === 'subplan-failed'}
      <span class="text-surface-border">·</span>
      <span class="text-error">closed by task_failed</span>
    {/if}
    {#if section.closingNote}
      <span class="text-surface-border">·</span>
      <span class="text-muted">{section.closingNote}</span>
    {/if}
  </div>
</div>
