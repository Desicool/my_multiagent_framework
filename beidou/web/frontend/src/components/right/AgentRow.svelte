<script lang="ts">
  import type { AgentState } from '../../lib/types';
  import { ui, pinAgent } from '../../stores/ui.svelte';
  import { fmtMoney, fmtNum, shortId } from '../../lib/format';

  let { agent, depth }: { agent: AgentState; depth: number } = $props();

  let pinned = $derived(ui.pinnedAgentId === agent.agent_id);
  let dotColor = $derived.by(() => {
    switch (agent.status) {
      case 'working': return 'bg-emerald-400 animate-pulse';
      case 'blocked': return 'bg-rose-500';
      case 'idle': return 'bg-slate-500';
      case 'done': return 'bg-slate-700';
      default: return 'bg-slate-600';
    }
  });

  function onClick(): void { pinAgent(agent.agent_id); }

  let indent = $derived(`padding-left: ${depth * 0.75}rem`);
</script>

<button
  type="button"
  class={`w-full flex items-center gap-2 px-1 py-1 text-sm text-left rounded ${pinned ? 'bg-emerald-500/10 border-l-2 border-emerald-400' : 'hover:bg-slate-900'}`}
  style={indent}
  onclick={onClick}
  title={agent.agent_id}
>
  <span class="w-3 inline-block"></span>
  <span class={`w-1.5 h-1.5 rounded-full shrink-0 ${dotColor}`}></span>
  <span class="text-slate-200 truncate">{agent.role ?? 'agent'}</span>
  <span class="text-slate-500 font-mono text-[10px] truncate">{shortId(agent.agent_id, 4)}</span>
  <span class="ml-auto flex items-center gap-2 text-[10px] text-slate-500 font-mono shrink-0">
    {fmtNum(agent.tokens_in + agent.tokens_out)}
    <span class="text-slate-600">·</span>
    {fmtMoney(agent.cost_usd)}
  </span>
</button>
