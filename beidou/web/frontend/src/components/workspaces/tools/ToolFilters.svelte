<script lang="ts">
  import type { PrimitiveCategory } from '../../../lib/primitives';

  type Selection = {
    category: PrimitiveCategory | 'all';
    agentId: string | 'all';
    errorsOnly: boolean;
  };

  let {
    selection = $bindable(),
    counts,
    agentOptions,
  }: {
    selection: Selection;
    counts: Record<PrimitiveCategory | 'all', number>;
    agentOptions: Array<{ id: string; label: string }>;
  } = $props();

  type CatPill = { id: PrimitiveCategory | 'all'; label: string; swatch?: string };
  const pills: CatPill[] = [
    { id: 'all',          label: 'All' },
    { id: 'plan',         label: 'Plan',         swatch: 'bg-info' },
    { id: 'lifecycle',    label: 'Lifecycle',    swatch: 'bg-accent' },
    { id: 'review',       label: 'Review',       swatch: 'bg-review' },
    { id: 'coordination', label: 'Coordination', swatch: 'bg-success' },
    { id: 'human',        label: 'Human',        swatch: 'bg-accent/70' },
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

  <select
    bind:value={selection.agentId}
    class="bg-surface border border-surface-border text-slate-300 px-2.5 py-1 rounded text-[11.5px] font-sans cursor-pointer"
  >
    <option value="all">All agents</option>
    {#each agentOptions as a (a.id)}
      <option value={a.id}>{a.label}</option>
    {/each}
  </select>

  <label class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-[11.5px] font-medium border border-surface-border text-slate-400 cursor-pointer hover:border-slate-700 hover:text-slate-200">
    <input type="checkbox" bind:checked={selection.errorsOnly} class="w-3 h-3 accent-error" />
    Errors only
  </label>
</div>
