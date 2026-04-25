<script lang="ts">
  import { events } from '../../stores/events.svelte';
  import { ui } from '../../stores/ui.svelte';
  import { fmtMoney, fmtNum, shortId } from '../../lib/format';

  let agent = $derived(ui.pinnedAgentId ? events.agentsById[ui.pinnedAgentId] : null);

  const statusColors: Record<string, string> = {
    working: 'bg-emerald-500/20 text-emerald-200 border-emerald-500/40',
    blocked: 'bg-rose-500/20 text-rose-200 border-rose-500/40',
    idle: 'bg-slate-500/20 text-slate-300 border-slate-500/40',
    done: 'bg-slate-700/40 text-slate-400 border-slate-600',
    unknown: 'bg-slate-500/10 text-slate-500 border-slate-700',
  };
</script>

{#if agent}
  <header class="sticky top-0 z-20 px-4 py-3 border-b border-slate-800 bg-slate-950/95 backdrop-blur">
    <div class="flex items-center justify-between gap-3">
      <div class="min-w-0 flex items-baseline gap-3">
        <h1 class="text-lg font-semibold text-slate-100 truncate">
          {agent.role ?? 'agent'}
          <span class="text-slate-500 font-mono text-xs ml-2">{shortId(agent.agent_id, 8)}</span>
        </h1>
        <span class={`inline-flex items-center gap-1 px-2 py-0.5 text-xs rounded border ${statusColors[agent.status] ?? statusColors.unknown}`}>
          {agent.status}{#if agent.status_detail} · {agent.status_detail}{/if}
        </span>
      </div>
    </div>
    <dl class="mt-2 grid grid-cols-4 gap-3 text-xs">
      <div><dt class="text-slate-500">model</dt><dd class="text-slate-200 font-mono truncate">{agent.model ?? '—'}</dd></div>
      <div><dt class="text-slate-500">tokens</dt><dd class="text-slate-200 font-mono">{fmtNum(agent.tokens_in)}↑ {fmtNum(agent.tokens_out)}↓</dd></div>
      <div><dt class="text-slate-500">tools</dt><dd class="text-slate-200 font-mono">{agent.tool_calls}</dd></div>
      <div><dt class="text-slate-500">cost</dt><dd class="text-slate-200 font-mono">{fmtMoney(agent.cost_usd)}</dd></div>
    </dl>
  </header>
{:else}
  <header class="sticky top-0 z-20 px-4 py-3 border-b border-slate-800 bg-slate-950/95">
    <p class="text-sm text-slate-500">No agent pinned. Click an agent in the team tree.</p>
  </header>
{/if}
