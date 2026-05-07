<script lang="ts">
  import type { TimelineCategory } from '../../lib/timeline';
  import { categoryOf, countByCategory, deriveTimeline, isCollapsed, isMilestone } from '../../lib/timeline';
  import { events } from '../../stores/events.svelte';
  import { shortId } from '../../lib/format';
  import TimelineFilters from './timeline/TimelineFilters.svelte';
  import SectionBody from './timeline/SectionBody.svelte';
  import CollapsedSubplans from './timeline/CollapsedSubplans.svelte';

  let selection = $state<{
    category: TimelineCategory | 'all';
    agentId: string | 'all';
    showAll: boolean;
  }>({ category: 'all', agentId: 'all', showAll: false });

  let counts = $derived(countByCategory(events.timelineEvents));
  let agentOptions = $derived.by(() => {
    const seen = new Map<string, { id: string; label: string }>();
    for (const ev of events.timelineEvents) {
      if (!ev.agent_id) continue;
      if (seen.has(ev.agent_id)) continue;
      const a = events.agentsById[ev.agent_id];
      const label = a?.name ?? a?.role ?? shortId(ev.agent_id, 6);
      seen.set(ev.agent_id, { id: ev.agent_id, label: `${label} · ${shortId(ev.agent_id, 6)}` });
    }
    return [...seen.values()].sort((x, y) => x.label.localeCompare(y.label));
  });

  let tree = $derived(deriveTimeline(events.timelineEvents, events.agentsById, { showAll: selection.showAll }));

  // Apply category + agent + errors-only filtering at the row level (post-derive).
  let filteredTree = $derived.by(() => {
    if (selection.category === 'all' && selection.agentId === 'all') return tree;
    function filterEvents(evts: ReturnType<typeof deriveTimeline>[number]): typeof evts {
      if (isCollapsed(evts)) return evts;
      const filtered = evts.events.filter((e) => {
        if (selection.category !== 'all' && categoryOf(e.ev) !== selection.category) return false;
        if (selection.agentId !== 'all' && e.ev.agent_id !== selection.agentId) return false;
        return true;
      });
      const filteredChildren = evts.children.map(filterEvents);
      return { ...evts, events: filtered, children: filteredChildren };
    }
    return tree.map(filterEvents);
  });

  let totalEvents = $derived(events.timelineEvents.length);
  let totalMilestones = $derived(counts.all);
  let plansCount = $derived.by(() => {
    let n = 0;
    for (const e of events.timelineEvents) if (e.kind === 'plan_declared') n++;
    return n;
  });
  let planTaskSections = $derived.by(() => {
    let n = 0;
    function walk(s: ReturnType<typeof deriveTimeline>[number]): void {
      if (isCollapsed(s)) { n += s.tasks.length; return; }
      if (s.kind === 'task' || s.kind === 'subplan' || s.kind === 'subplan-failed' || s.kind === 'pending') n++;
      for (const c of s.children) walk(c);
    }
    for (const s of tree) walk(s);
    return n;
  });
</script>

<div class="h-full flex flex-col bg-slate-950 overflow-hidden">
  <!-- Sticky header -->
  <div class="sticky top-0 bg-surface border-b border-surface-border px-6 pt-3.5 pb-3 z-10">
    <div class="flex items-baseline gap-3 mb-3">
      <h1 class="text-[15px] font-semibold text-slate-100 tracking-tight">Timeline</h1>
      <span class="font-mono text-xs text-muted tabular-nums">
        {totalMilestones} milestone{totalMilestones === 1 ? '' : 's'} · {plansCount} plan{plansCount === 1 ? '' : 's'} · {planTaskSections} plan task{planTaskSections === 1 ? '' : 's'}
        {#if !selection.showAll && totalEvents > totalMilestones}
          · {totalEvents - totalMilestones} firehose events filtered
        {/if}
      </span>
    </div>

    <TimelineFilters bind:selection {counts} {agentOptions} />
  </div>

  <!-- Body -->
  <div class="flex-1 overflow-y-auto">
    {#if events.timelineEvents.length === 0}
      <div class="text-slate-600 text-center mt-12">
        <div class="text-sm italic">No events yet — waiting for the agent run to begin.</div>
      </div>
    {:else}
      <div class="px-6 pt-3 pb-2">
        <div class="px-3 py-2 bg-review/[0.04] border border-review/20 rounded text-[11px] text-slate-400 leading-relaxed flex gap-2.5 items-start">
          <span class="font-mono text-[9.5px] text-review bg-review/10 border border-review/30 px-1.5 py-px rounded uppercase tracking-wider font-semibold shrink-0 mt-px">Partitioning</span>
          <span>Sections mirror the <code class="font-mono text-slate-200 bg-surface border border-surface-border px-1 rounded">declare_plan</code> tree. Each plan task = one section, bounded by its <code class="font-mono text-slate-200 bg-surface border border-surface-border px-1 rounded">spawn_agent</code> and matching <code class="font-mono text-slate-200 bg-surface border border-surface-border px-1 rounded">task_done</code>/<code class="font-mono text-slate-200 bg-surface border border-surface-border px-1 rounded">task_failed</code>. Nested <code class="font-mono text-slate-200 bg-surface border border-surface-border px-1 rounded">declare_plan</code> creates indented sub-sections. Default expansion: 3 most-active sub-tasks per parent.</span>
        </div>
      </div>

      <div class="pb-8">
        {#each filteredTree as node, i (i)}
          {#if isCollapsed(node)}
            <CollapsedSubplans node={node} />
          {:else}
            <SectionBody section={node} depth={0} />
          {/if}
        {/each}
      </div>
    {/if}
  </div>
</div>
