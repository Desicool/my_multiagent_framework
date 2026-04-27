<script lang="ts">
  import type { AgentState } from '../../lib/types';
  import { ui, pinAgent } from '../../stores/ui.svelte';
  import { fmtMoney, fmtNum, shortId } from '../../lib/format';

  let { agent, depth }: { agent: AgentState; depth: number } = $props();

  let pinned = $derived(ui.pinnedAgentId === agent.agent_id);

  // Status dot — consistent with AgentHeader semantic tokens
  let dotClass = $derived.by(() => {
    switch (agent.status) {
      case 'working': return 'bg-success animate-pulse';
      case 'blocked': return 'bg-error';
      case 'idle':    return 'bg-muted';
      case 'done':    return 'bg-surface-border';
      default:        return 'bg-slate-600';
    }
  });

  function onClick(): void { pinAgent(agent.agent_id); }

  let indent = $derived(`padding-left: ${depth * 0.75 + 0.5}rem`);
</script>

<button
  type="button"
  class={`w-full flex items-center gap-2 pr-2 py-1.5 text-sm text-left rounded transition-colors ${
    pinned
      ? 'bg-success/10 border-l-2 border-success/60 text-slate-100'
      : 'hover:bg-slate-800/60 border-l-2 border-transparent text-slate-300'
  }`}
  style={indent}
  onclick={onClick}
  title={agent.agent_id}
>
  <!-- Status dot -->
  <span class={`w-1.5 h-1.5 rounded-full shrink-0 ${dotClass}`}></span>

  <!-- Agent name -->
  <span class="truncate flex-1 min-w-0">
    {agent.name ?? agent.role ?? shortId(agent.agent_id, 4)}
  </span>

  {#if !agent.name}
    <span class="text-slate-600 font-mono text-xs shrink-0">{shortId(agent.agent_id, 4)}</span>
  {/if}

  <!-- Stats: token count + cost -->
  <span class="ml-auto flex items-center gap-1.5 text-xs text-muted font-mono shrink-0">
    {fmtNum(agent.tokens_in + agent.tokens_out)}
    <span class="text-slate-700">·</span>
    {fmtMoney(agent.cost_usd)}
  </span>
</button>
