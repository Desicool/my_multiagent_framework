<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { events } from '../../stores/events.svelte';
  import { fmtMoney, fmtNum, shortId } from '../../lib/format';
  import { elapsed as elapsedStr } from '../../lib/time';

  let now = $state(Date.now());
  let timer: number | null = null;
  onMount(() => { timer = window.setInterval(() => { now = Date.now(); }, 1000); });
  onDestroy(() => { if (timer !== null) clearInterval(timer); });

  let elapsed = $derived.by(() => {
    const start = events.task?.started_at;
    if (!start) return '—';
    const end = events.task?.ended_at ? events.task.ended_at * 1000 : now;
    return elapsedStr(end - start * 1000);
  });

  let agentsAlive = $derived.by(() => {
    let alive = 0;
    for (const a of Object.values(events.agentsById)) if (a.status !== 'done') alive += 1;
    return alive;
  });

  let teamsCount = $derived(Object.keys(events.teamsById).length);
</script>

<div class="px-4 pt-4 pb-2 border-b border-slate-800">
  <div class="flex items-baseline gap-2 mb-2">
    <span class="text-xs uppercase tracking-wider text-slate-500">Task</span>
    {#if events.task}
      <span class="font-mono text-xs text-slate-400">{shortId(events.task.task_id, 8)}</span>
    {/if}
  </div>
  {#if events.task?.description}
    <p class="text-sm text-slate-200 line-clamp-2 mb-3">{events.task.description}</p>
  {:else}
    <p class="text-sm text-slate-600 italic mb-3">no description</p>
  {/if}
  <dl class="grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs">
    <dt class="text-slate-500">model</dt>
    <dd class="text-slate-200 font-mono truncate">{events.task?.model ?? '—'}</dd>
    <dt class="text-slate-500">skill</dt>
    <dd class="text-slate-200 truncate">{events.task?.skill ?? '—'}</dd>
    <dt class="text-slate-500">elapsed</dt>
    <dd class="text-slate-200 font-mono">{elapsed}</dd>
    <dt class="text-slate-500">tokens</dt>
    <dd class="text-slate-200 font-mono">{fmtNum(events.stats.total_tokens)}</dd>
    <dt class="text-slate-500">cost</dt>
    <dd class="text-slate-200 font-mono">{fmtMoney(events.stats.total_cost_usd)}</dd>
    <dt class="text-slate-500">agents alive</dt>
    <dd class="text-slate-200 font-mono">{agentsAlive}</dd>
    <dt class="text-slate-500">teams</dt>
    <dd class="text-slate-200 font-mono">{teamsCount}</dd>
  </dl>
</div>
