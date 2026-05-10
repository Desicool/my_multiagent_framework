<script lang="ts">
  import type { AgentState, ToolStreamItem } from '../../lib/types';
  import { events } from '../../stores/events.svelte';
  import { getPrimitive, type PrimitiveCategory } from '../../lib/primitives';
  import { shortId } from '../../lib/format';
  import PrimitiveCard from '../primitives/PrimitiveCard.svelte';
  import ToolFilters from './tools/ToolFilters.svelte';
  import CategoryLegend from './tools/CategoryLegend.svelte';

  type Row = {
    item: ToolStreamItem;
    callerAgent: AgentState;
    category: PrimitiveCategory;
  };

  let selection = $state<{
    category: PrimitiveCategory | 'all';
    agentId: string | 'all';
    errorsOnly: boolean;
  }>({ category: 'all', agentId: 'all', errorsOnly: false });

  // Aggregate every tool stream item across all agents that maps to a known
  // primitive. Sorted by ts ascending (timeline order).
  let allRows = $derived.by<Row[]>(() => {
    const out: Row[] = [];
    for (const a of Object.values(events.agentsById) as AgentState[]) {
      for (const it of a._stream) {
        if (it.kind !== 'tool') continue;
        const meta = getPrimitive(it.name);
        if (!meta) continue;
        out.push({ item: it, callerAgent: a, category: meta.category });
      }
    }
    out.sort((x, y) => x.item.ts - y.item.ts);
    return out;
  });

  let counts = $derived.by<Record<PrimitiveCategory | 'all', number>>(() => {
    const c: Record<PrimitiveCategory | 'all', number> = {
      all: 0, coordination: 0, lifecycle: 0, plan: 0, review: 0, human: 0,
    };
    for (const r of allRows) {
      c.all += 1;
      c[r.category] += 1;
    }
    return c;
  });

  let agentOptions = $derived.by(() => {
    const seen = new Map<string, { id: string; label: string }>();
    for (const r of allRows) {
      const a = r.callerAgent;
      if (seen.has(a.agent_id)) continue;
      const label = a.name ?? a.role ?? shortId(a.agent_id, 6);
      seen.set(a.agent_id, { id: a.agent_id, label: `${label} · ${shortId(a.agent_id, 6)}` });
    }
    return [...seen.values()].sort((x, y) => x.label.localeCompare(y.label));
  });

  let filteredRows = $derived.by<Row[]>(() => {
    return allRows.filter((r) => {
      if (selection.category !== 'all' && r.category !== selection.category) return false;
      if (selection.agentId !== 'all' && r.callerAgent.agent_id !== selection.agentId) return false;
      if (selection.errorsOnly && r.item.is_error !== true) return false;
      return true;
    });
  });
</script>

<div class="h-full flex flex-col bg-slate-950 overflow-hidden">
  <!-- Sticky header -->
  <div class="sticky top-0 bg-surface border-b border-surface-border px-6 pt-3.5 pb-3 z-10">
    <div class="flex items-baseline gap-3 mb-3">
      <h1 class="text-[15px] font-semibold text-slate-100 tracking-tight">Tools &amp; Primitives</h1>
      <span class="font-mono text-xs text-muted tabular-nums">{filteredRows.length} of {allRows.length} call{allRows.length === 1 ? '' : 's'}</span>
    </div>

    <ToolFilters bind:selection {counts} {agentOptions} />
    <CategoryLegend />
  </div>

  <!-- Body -->
  <div class="flex-1 overflow-y-auto px-6 pt-4 pb-8">
    {#if filteredRows.length === 0}
      <div class="text-slate-600 text-center mt-12">
        {#if allRows.length === 0}
          <div class="text-sm italic">No primitive calls captured yet.</div>
          <div class="text-[11.5px] mt-1.5">As agents call tools, they'll appear here.</div>
        {:else}
          <div class="text-sm italic">No calls match the current filters.</div>
        {/if}
      </div>
    {:else}
      <div class="flex flex-col gap-2.5">
        {#each filteredRows as r (r.callerAgent.agent_id + ':' + r.item.tool_use_id)}
          <PrimitiveCard item={r.item} callerAgent={r.callerAgent} />
        {/each}
      </div>
    {/if}
  </div>
</div>
