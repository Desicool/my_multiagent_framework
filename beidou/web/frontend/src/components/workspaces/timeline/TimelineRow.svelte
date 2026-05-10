<script lang="ts">
  import type { SectionEvent } from '../../../lib/timeline';
  import type { AgentState } from '../../../lib/types';
  import { events } from '../../../stores/events.svelte';
  import { ui, pinAgent } from '../../../stores/ui.svelte';
  import { hhmmss, elapsed } from '../../../lib/time';
  import { shortId } from '../../../lib/format';
  import { agentMonogramBucket, agentMonogramText } from '../../../lib/primitives';
  import { categoryOf, glyphOf, labelOf } from '../../../lib/timeline';

  let {
    item,
    /** Optional anchor ts used to render a "+Δs" delta in the time column. */
    sinceTs,
  }: { item: SectionEvent; sinceTs?: number } = $props();

  let ev = $derived(item.ev);
  let cat = $derived(categoryOf(ev));
  let agent: AgentState | undefined = $derived(ev.agent_id ? events.agentsById[ev.agent_id] : undefined);

  let catGlyphCls = $derived.by(() => {
    switch (cat) {
      case 'primitive':  return 'border-review text-review';
      case 'lifecycle':  return 'border-accent text-accent';
      case 'plan':       return 'border-info text-info';
      case 'question':   return 'border-violet-400 text-violet-400';
      case 'error':      return 'border-error text-error';
      case 'deprecated': return 'border-muted text-muted';
      default:           return 'border-slate-700 text-slate-400';
    }
  });

  let typeClass = $derived.by(() => {
    switch (cat) {
      case 'primitive':  return 'text-review';
      case 'lifecycle':  return 'text-accent';
      case 'plan':       return 'text-info';
      case 'question':   return 'text-violet-300';
      case 'error':      return 'text-error';
      case 'deprecated': return 'text-muted line-through decoration-1';
      default:           return 'text-slate-200';
    }
  });

  let rowBg = $derived.by(() => {
    if (item.closesFail) return 'bg-error/5';
    if (item.closes)     return 'bg-info/5';
    return '';
  });

  const monoBg: Record<string, string> = {
    h1: 'bg-violet-500/20 text-violet-300',
    h2: 'bg-emerald-500/20 text-emerald-300',
    h3: 'bg-sky-500/20 text-sky-300',
    h4: 'bg-rose-500/20 text-rose-300',
    h5: 'bg-amber-500/20 text-amber-300',
    h6: 'bg-cyan-500/20 text-cyan-300',
  };
  let monoCls = $derived(ev.agent_id ? monoBg[agentMonogramBucket(ev.agent_id)] : 'bg-slate-700/40 text-slate-400');
  let mono = $derived(ev.agent_id ? agentMonogramText(ev.agent_id) : '⌬');

  let delta = $derived.by(() => {
    if (sinceTs === undefined) return '';
    const dt = ev.ts - sinceTs;
    if (dt <= 0) return '+0s';
    return `+${elapsed(dt * 1000)}`;
  });

  // Special-case summaries
  let summary = $derived.by(() => {
    if (ev.summary) return ev.summary;
    if (ev.kind === 'task_spawned' && ev.spawned_agent_id) {
      return `task ${ev.plan_task_id ?? '?'} → spawned ${shortId(ev.spawned_agent_id, 12)}`;
    }
    if (ev.kind === 'terminate_posted' && ev.spawned_agent_id) {
      return `target ${shortId(ev.spawned_agent_id, 12)}`;
    }
    if (ev.kind === 'primitive_call') {
      const inp = ev.input ?? {};
      const to = (inp.to as string) ?? (inp.agent_id as string);
      if (to) return `→ ${shortId(to, 12)}`;
    }
    return '';
  });

  function pin(): void {
    if (ev.agent_id) pinAgent(ev.agent_id);
  }
</script>

<div class={`grid grid-cols-[90px_22px_1fr] gap-x-3 items-start py-2 px-1 rounded ${rowBg} hover:bg-slate-200/[0.025]`}>
  <!-- Time col -->
  <div class="font-mono tabular-nums text-[11px] text-slate-400 text-right pt-0.5">
    {hhmmss(ev.ts)}
    {#if delta}<span class="block text-muted text-[10px] mt-px">{delta}</span>{/if}
  </div>

  <!-- Glyph col -->
  <div class={`relative z-[1] mt-0.5 w-[22px] h-[22px] rounded-full bg-surface border-2 ${catGlyphCls} flex items-center justify-center text-[10px] font-mono font-semibold`}>
    {glyphOf(ev)}
  </div>

  <!-- Content col -->
  <div class="min-w-0">
    <div class="flex items-baseline gap-2.5 flex-wrap">
      <span class={`font-mono text-[12px] font-semibold ${typeClass}`}>{labelOf(ev)}</span>
      {#if item.closes}
        <span class={`text-[10px] uppercase tracking-wider px-1.5 py-px rounded font-semibold ${item.closesFail ? 'bg-error/10 text-error border border-error/30' : 'bg-review/10 text-review border border-review/30'}`}>
          closes section{item.closesFail ? ' · failed' : ''}
        </span>
      {/if}
      {#if cat === 'deprecated'}
        <span class="text-[10px] uppercase tracking-wider px-1.5 py-px rounded font-semibold bg-muted/10 text-muted border border-muted/30">deprecated</span>
      {/if}
      {#if ev.agent_id}
        <button
          type="button"
          onclick={pin}
          class="inline-flex items-center gap-1 font-mono text-[11px] text-slate-300 hover:text-slate-100 cursor-pointer"
          title="Pin in /agents"
        >
          <span class={`shrink-0 inline-flex items-center justify-center rounded font-mono font-semibold w-[18px] h-[18px] text-[9.5px] ${monoCls}`}>{mono}</span>
          <span>{shortId(ev.agent_id, 12)}</span>
          {#if agent?.role}<span class="text-slate-400">· {agent.role}</span>{/if}
        </button>
      {/if}
    </div>
    {#if summary}
      <div class="text-[12px] text-slate-300 mt-0.5 leading-relaxed break-words">{summary}</div>
    {/if}
  </div>
</div>
