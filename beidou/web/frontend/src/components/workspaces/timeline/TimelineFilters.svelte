<script lang="ts">
  import type { TimelineCategory } from '../../../lib/timeline';

  type Selection = {
    category: TimelineCategory | 'all';
    agentId: string | 'all';
    showAll: boolean;
  };

  let {
    selection = $bindable(),
    counts,
    agentOptions,
  }: {
    selection: Selection;
    counts: Record<TimelineCategory | 'all', number>;
    agentOptions: Array<{ id: string; label: string }>;
  } = $props();

  type Pill = { id: TimelineCategory | 'all'; label: string; swatch?: string };
  const pills: Pill[] = [
    { id: 'all',        label: 'All' },
    { id: 'primitive',  label: 'Primitive calls', swatch: 'bg-review' },
    { id: 'lifecycle',  label: 'Lifecycle',       swatch: 'bg-accent' },
    { id: 'plan',       label: 'Plan',            swatch: 'bg-info' },
    { id: 'question',   label: 'Questions',       swatch: 'bg-violet-400' },
    { id: 'error',      label: 'Errors',          swatch: 'bg-error' },
  ];
</script>

<div class="flex gap-1.5 flex-wrap items-center">
  {#each pills as p (p.id)}
    <button
      type="button"
      onclick={() => (selection.category = p.id)}
      class={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-[11.5px] font-medium border transition-colors cursor-pointer
        ${selection.category === p.id
          ? 'bg-slate-200/[0.04] border-slate-600 text-slate-100'
          : 'bg-transparent border-surface-border text-slate-400 hover:border-slate-700 hover:text-slate-200'}`}
    >
      {#if p.swatch}<span class={`w-1.5 h-1.5 rounded-full ${p.swatch}`}></span>{/if}
      {p.label}
      <span class="font-mono text-[10.5px] text-muted font-normal">{counts[p.id] ?? 0}</span>
    </button>
  {/each}

  <div class="flex-1"></div>

  <label class="inline-flex items-center gap-2 text-[11.5px] text-slate-400 cursor-pointer select-none">
    <span class={`relative inline-block w-7 h-4 rounded-full transition-colors ${selection.showAll ? 'bg-review/35' : 'bg-surface-border'}`}>
      <span class={`absolute top-0.5 w-3 h-3 rounded-full transition-all ${selection.showAll ? 'left-3.5 bg-review' : 'left-0.5 bg-slate-400'}`}></span>
    </span>
    <input type="checkbox" bind:checked={selection.showAll} class="sr-only" />
    Show all events
    <span class="text-[10.5px] text-muted">(turn.usage, tool spans, status)</span>
  </label>

  <select
    bind:value={selection.agentId}
    class="bg-surface border border-surface-border text-slate-300 px-2.5 py-1 rounded text-[11.5px] font-sans cursor-pointer"
  >
    <option value="all">All agents</option>
    {#each agentOptions as a (a.id)}
      <option value={a.id}>{a.label}</option>
    {/each}
  </select>
</div>
