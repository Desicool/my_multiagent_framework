<script lang="ts">
  import type { CollapsedSubplans } from '../../../lib/timeline';
  import { hhmmss } from '../../../lib/time';
  import { labelOf } from '../../../lib/timeline';

  let {
    node,
    onExpand,
  }: {
    node: CollapsedSubplans;
    onExpand?: () => void;
  } = $props();

  let totalEvents = $derived(node.tasks.reduce((sum, t) => sum + t.events, 0));
  let last = $derived.by(() => {
    let bestTs = -1;
    let best: typeof node.tasks[number] | undefined;
    for (const t of node.tasks) {
      if (t.lastEvent && t.lastEvent.ts > bestTs) { bestTs = t.lastEvent.ts; best = t; }
    }
    return best;
  });
</script>

<button
  type="button"
  onclick={onExpand}
  class="ml-[60px] mr-6 mt-2 px-4 py-2 border border-dashed border-surface-border rounded bg-surface-raised/35 hover:border-slate-700 hover:bg-surface-raised/55 hover:text-slate-200 font-mono text-[11px] text-slate-400 cursor-pointer flex items-center gap-2.5 w-[calc(100%-84px)]"
>
  <span class="text-review text-[10px]">▸</span>
  <span><span class="text-slate-200 font-medium">{node.tasks.length} more sub-plan task{node.tasks.length === 1 ? '' : 's'}</span>
    {#if node.tasks.length <= 5}
      · {#each node.tasks as t, i (t.task_id)}<code class="font-mono">{t.task_id}</code>{i < node.tasks.length - 1 ? ' · ' : ''}{/each}
    {/if}
  </span>
  <span class="flex-1"></span>
  <span><span class="text-slate-200 font-medium">{totalEvents}</span> event{totalEvents === 1 ? '' : 's'}{#if last}
    · last <code class="font-mono">{labelOf(last.lastEvent!)}</code> by {last.task_id} at {hhmmss(last.lastEvent!.ts)}
  {/if}</span>
</button>
