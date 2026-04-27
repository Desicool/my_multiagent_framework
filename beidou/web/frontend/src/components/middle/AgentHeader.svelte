<script lang="ts">
  import { events } from '../../stores/events.svelte';
  import { ui } from '../../stores/ui.svelte';
  import { fmtMoney, fmtNum, shortId } from '../../lib/format';

  let agent = $derived(ui.pinnedAgentId ? events.agentsById[ui.pinnedAgentId] : null);

  // Semantic status tokens: dot color + badge class
  const dotColors: Record<string, string> = {
    working: 'bg-success animate-pulse',
    blocked: 'bg-error',
    idle:    'bg-muted',
    done:    'bg-surface-border',
    unknown: 'bg-slate-600',
  };

  const statusBadge: Record<string, string> = {
    working: 'bg-success/15 text-success border-success/30',
    blocked: 'bg-error/15 text-error/90 border-error/30',
    idle:    'bg-muted/10 text-muted border-muted/20',
    done:    'bg-surface-border/40 text-slate-400 border-surface-border',
    unknown: 'bg-slate-500/10 text-slate-500 border-slate-700',
  };
</script>

{#if agent}
  <header class="sticky top-0 z-20 px-4 pt-4 pb-3 border-b border-surface-border bg-surface/95 backdrop-blur">
    <div class="flex items-start justify-between gap-3">
      <!-- Name + role block -->
      <div class="min-w-0">
        <div class="flex items-center gap-2 mb-0.5">
          <!-- Status dot -->
          <span class={`w-2 h-2 rounded-full shrink-0 ${dotColors[agent.status] ?? dotColors.unknown}`}></span>
          <h1 class="text-base font-semibold text-slate-100 truncate leading-tight">
            {agent.name ?? agent.role ?? shortId(agent.agent_id, 4)}
          </h1>
        </div>
        <div class="flex items-center gap-2 pl-4">
          {#if agent.name && agent.role}
            <span class="text-xs text-muted">{agent.role}</span>
            <span class="text-slate-700">·</span>
          {/if}
          <span class="font-mono text-xs text-slate-600">{shortId(agent.agent_id, 8)}</span>
        </div>
      </div>
      <!-- Status badge -->
      <span class={`inline-flex items-center gap-1.5 px-2 py-0.5 text-xs rounded border shrink-0 ${statusBadge[agent.status] ?? statusBadge.unknown}`}>
        {agent.status}{#if agent.status_detail} · {agent.status_detail}{/if}
      </span>
    </div>

    <!-- Stats row -->
    <dl class="mt-3 grid grid-cols-4 gap-2 text-xs pl-4">
      <div>
        <dt class="text-muted text-[10px] uppercase tracking-wide">model</dt>
        <dd class="text-slate-300 font-mono truncate mt-0.5">{agent.model ?? '—'}</dd>
      </div>
      <div>
        <dt class="text-muted text-[10px] uppercase tracking-wide">tokens</dt>
        <dd class="text-slate-300 font-mono mt-0.5">{fmtNum(agent.tokens_in)}↑ {fmtNum(agent.tokens_out)}↓</dd>
      </div>
      <div>
        <dt class="text-muted text-[10px] uppercase tracking-wide">tools</dt>
        <dd class="text-slate-300 font-mono mt-0.5">{agent.tool_calls}</dd>
      </div>
      <div>
        <dt class="text-muted text-[10px] uppercase tracking-wide">cost</dt>
        <dd class="text-slate-300 font-mono mt-0.5">{fmtMoney(agent.cost_usd)}</dd>
      </div>
    </dl>
  </header>
{:else}
  <header class="sticky top-0 z-20 px-4 py-3 border-b border-surface-border bg-surface/95">
    <p class="text-sm text-muted">No agent pinned. Click an agent in the team tree.</p>
  </header>
{/if}
