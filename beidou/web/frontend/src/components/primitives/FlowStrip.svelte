<script lang="ts">
  import type { AgentState } from '../../lib/types';
  import AgentChip from './AgentChip.svelte';
  import { agentMonogramBucket, agentMonogramText } from '../../lib/primitives';
  import { shortId } from '../../lib/format';

  let {
    fromId,
    fromAgent,
    fromLabel,
    toId,
    toAgent,
    toLabel,
    /** Visual variant — colors the arrow + (for user) the user-terminal endpoint. */
    variant = 'outbound',
    /** Override the arrow glyph (default: ⟶). Use '+⟶' for spawn, '×⟶' for terminate. */
    arrow = '⟶',
    /** When true, the destination is the human user (terminal). */
    userTerminal = false,
    chainHops,
  }: {
    fromId: string;
    fromAgent?: AgentState;
    fromLabel?: string;
    toId: string;
    toAgent?: AgentState;
    toLabel?: string;
    variant?: 'outbound' | 'spawn' | 'terminate' | 'user';
    arrow?: string;
    userTerminal?: boolean;
    chainHops?: number;
  } = $props();

  let arrowColor = $derived(
    variant === 'spawn' ? 'text-accent' :
    variant === 'terminate' ? 'text-error' :
    variant === 'user' ? 'text-pending' :
    'text-success',
  );

  // Build small mono+id row at md size with right-alignment for the to side
  let toBucket = $derived(agentMonogramBucket(toId));
  let toMono = $derived(agentMonogramText(toId));
  let toRole = $derived(toAgent?.name ?? toAgent?.role ?? null);
  const monoBg: Record<string, string> = {
    h1: 'bg-violet-500/20 text-violet-300',
    h2: 'bg-emerald-500/20 text-emerald-300',
    h3: 'bg-sky-500/20 text-sky-300',
    h4: 'bg-rose-500/20 text-rose-300',
    h5: 'bg-amber-500/20 text-amber-300',
    h6: 'bg-cyan-500/20 text-cyan-300',
  };
  let toMonoCls = $derived(userTerminal ? 'bg-amber-500/20 text-amber-300' : monoBg[toBucket]);
</script>

<div class="grid grid-cols-[1fr_auto_1fr] gap-4 items-center mx-3.5 mt-1 px-4 py-3 bg-surface border border-surface-border rounded">
  <!-- From endpoint (left, left-aligned) -->
  <div class="flex flex-col gap-0.5 min-w-0">
    <div class="text-[9.5px] tracking-widest uppercase text-muted font-semibold">
      {fromLabel ?? 'From'}
    </div>
    <AgentChip agentId={fromId} agent={fromAgent} size="md" showRole={false} />
    {#if fromAgent?.role || fromAgent?.name}
      <div class="text-[11px] text-slate-400 ml-[34px]">{fromAgent?.name ?? fromAgent?.role}</div>
    {/if}
  </div>

  <!-- Arrow -->
  <div class={`text-[22px] font-mono leading-none select-none ${arrowColor}`}>{arrow}</div>

  <!-- To endpoint (right, right-aligned) -->
  <div class="flex flex-col gap-0.5 min-w-0 items-end">
    <div class="text-[9.5px] tracking-widest uppercase text-muted font-semibold">
      {toLabel ?? 'To'}
    </div>
    {#if userTerminal}
      <span class="inline-flex items-center gap-1.5">
        <span class={`shrink-0 inline-flex items-center justify-center rounded font-mono font-semibold w-[26px] h-[26px] text-[12px] ${toMonoCls}`}>⌬</span>
        <span class="font-mono text-[13.5px] text-pending">user</span>
      </span>
      {#if chainHops}
        <div class="text-[11px] text-slate-400 mr-[34px]">via leader chain · {chainHops} {chainHops === 1 ? 'hop' : 'hops'}</div>
      {/if}
    {:else}
      <span class="inline-flex items-center gap-1.5">
        <span class={`shrink-0 inline-flex items-center justify-center rounded font-mono font-semibold w-[26px] h-[26px] text-[11px] ${toMonoCls}`}>{toMono}</span>
        <span class="font-mono text-[13.5px] text-slate-100">{shortId(toId, 14)}</span>
      </span>
      {#if toRole}
        <div class="text-[11px] text-slate-400 mr-[34px]">{toRole}</div>
      {/if}
    {/if}
  </div>
</div>
