<script lang="ts">
  import type { PrimitiveCategory } from '../../../lib/primitives';

  type Row = { cat: PrimitiveCategory; name: string; rule: string; swatch: string };

  const rows: Row[] = [
    { cat: 'coordination', name: 'Coordination', rule: 'A2A msg / discovery — no topology change', swatch: 'bg-success' },
    { cat: 'lifecycle',    name: 'Lifecycle',    rule: 'creates / destroys agent or team',         swatch: 'bg-accent' },
    { cat: 'plan',         name: 'Plan',         rule: 'DAG add / remove / query — pure data',     swatch: 'bg-info' },
    { cat: 'review',       name: 'Review',       rule: 'work-ready signal or review queue (does not terminate)', swatch: 'bg-review' },
    { cat: 'human',        name: 'Human',        rule: 'leaves agent system → user via leader chain', swatch: 'bg-accent/70' },
  ];
</script>

<div class="mt-2.5 px-3 py-2 bg-surface-raised/50 border border-surface-border rounded flex gap-4 flex-wrap text-[11px] text-slate-400">
  {#each rows as r (r.cat)}
    <div class="flex items-baseline gap-1.5">
      <span class={`w-2 h-2 rounded-full self-center ${r.swatch}`}></span>
      <span class="font-semibold text-slate-300">{r.name}</span>
      <span class="text-muted">{r.rule}</span>
    </div>
  {/each}
</div>
