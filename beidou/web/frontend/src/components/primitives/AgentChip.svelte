<script lang="ts">
  import type { AgentState } from '../../lib/types';
  import { agentMonogramBucket, agentMonogramText } from '../../lib/primitives';
  import { shortId } from '../../lib/format';

  let {
    agentId,
    agent,
    /** size: 'sm' (caller chip 18px) or 'md' (flow-strip 26px). */
    size = 'sm',
    /** if true, render the role text after the id ("· role"). */
    showRole = true,
  }: {
    agentId: string;
    agent?: AgentState;
    size?: 'sm' | 'md';
    showRole?: boolean;
  } = $props();

  const bucketBg: Record<string, string> = {
    h1: 'bg-violet-500/20 text-violet-300',
    h2: 'bg-emerald-500/20 text-emerald-300',
    h3: 'bg-sky-500/20 text-sky-300',
    h4: 'bg-rose-500/20 text-rose-300',
    h5: 'bg-amber-500/20 text-amber-300',
    h6: 'bg-cyan-500/20 text-cyan-300',
  };

  let bucket = $derived(agentMonogramBucket(agentId));
  let mono = $derived(agentMonogramText(agentId));
  let monoCls = $derived(bucketBg[bucket]);
  let dim = $derived('text-slate-400');
  let strong = $derived('text-slate-100');
  let role = $derived(agent?.name ?? agent?.role ?? null);
</script>

<span class="inline-flex items-center gap-1.5 min-w-0">
  {#if size === 'md'}
    <span class={`shrink-0 inline-flex items-center justify-center rounded font-mono font-semibold w-[26px] h-[26px] text-[11px] ${monoCls}`}>{mono}</span>
    <span class={`font-mono text-[13.5px] ${strong} truncate`}>{shortId(agentId, 14)}</span>
  {:else}
    <span class={`shrink-0 inline-flex items-center justify-center rounded font-mono font-semibold w-[18px] h-[18px] text-[9.5px] ${monoCls}`}>{mono}</span>
    <span class={`font-mono text-[11px] ${dim} truncate`}>{shortId(agentId, 12)}</span>
    {#if showRole && role}
      <span class="text-[11.5px] text-slate-300 truncate">· {role}</span>
    {/if}
  {/if}
</span>
