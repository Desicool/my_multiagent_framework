<script lang="ts">
  import type { Section } from '../../../lib/timeline';
  import { isCollapsed } from '../../../lib/timeline';
  import SectionHeader from './SectionHeader.svelte';
  import TimelineRow from './TimelineRow.svelte';
  import CollapsedSubplans from './CollapsedSubplans.svelte';
  import SectionBody from './SectionBody.svelte';

  let {
    section,
    /** Indent level (0 = root, 1 = sub-plan, …). */
    depth = 0,
    /** Manual expand overrides keyed by task_id. */
    expanded,
    onToggleExpand,
    /** Anchor ts for delta column (ts of root section's first event). */
    anchorTs,
  }: {
    section: Section;
    depth?: number;
    expanded?: Record<string, boolean>;
    onToggleExpand?: (taskId: string) => void;
    anchorTs?: number;
  } = $props();

  let milestoneCount = $derived(section.events.length);
  let firstTs = $derived(section.events[0]?.ev.ts ?? section.startTs);
</script>

{#if depth > 0}
  <!-- Nested L1+ shell: dashed left rail + connector tick -->
  <div class="relative ml-[60px] mr-6 mt-2 pl-[18px] border-l border-dashed border-surface-border">
    <span class="absolute top-6 -left-px w-3 h-px bg-surface-border"></span>
    <SectionHeader {section} {depth} milestones={milestoneCount} />
    {#if section.events.length > 0}
      <div class="relative pl-1 pt-1.5">
        {#each section.events as item, i (i)}
          <TimelineRow {item} sinceTs={anchorTs ?? firstTs} />
        {/each}
      </div>
    {:else if section.kind === 'pending'}
      <div class="mx-0 mt-1 px-4 py-3.5 border border-dashed border-surface-border rounded font-mono text-[11.5px] text-muted bg-surface-raised/30">
        awaiting <code class="text-slate-300">{section.pending_for}</code> · no events
      </div>
    {/if}
  </div>
{:else}
  <SectionHeader {section} {depth} milestones={milestoneCount} />
  {#if section.events.length > 0}
    <div class="relative px-6 pt-2">
      <!-- Vertical rail -->
      <span class="absolute top-3 bottom-3 left-[calc(24px+90px+11px)] w-px bg-surface-border"></span>
      {#each section.events as item, i (i)}
        <TimelineRow {item} sinceTs={anchorTs ?? firstTs} />
      {/each}
    </div>
  {:else if section.kind === 'pending'}
    <div class="mx-6 mt-1 px-4 py-3.5 border border-dashed border-surface-border rounded font-mono text-[11.5px] text-muted bg-surface-raised/30">
      <code class="text-slate-300">{section.task_id}</code> awaits <code class="text-slate-300">{section.pending_for}</code> · no events yet
    </div>
  {/if}

  <!-- Children (sub-plan tasks + collapsed groups) -->
  {#each section.children as child, ci (ci)}
    {#if isCollapsed(child)}
      <CollapsedSubplans node={child} onExpand={() => onToggleExpand?.('__collapsed__' + ci)} />
    {:else}
      <SectionBody section={child} depth={depth + 1} {anchorTs} {expanded} {onToggleExpand} />
    {/if}
  {/each}
{/if}
