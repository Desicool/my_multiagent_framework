<script lang="ts">
  import type { AgentState, ToolStreamItem } from '../../lib/types';
  import PCardShell from './PCardShell.svelte';
  import AgentChip from './AgentChip.svelte';

  let { item, callerAgent }: { item: ToolStreamItem; callerAgent?: AgentState } = $props();

  let taskIds = $derived.by<string[]>(() => {
    const t = item.input?.task_ids ?? item.input?.task_id;
    if (Array.isArray(t)) return t as string[];
    if (typeof t === 'string') return [t];
    return [];
  });
  let callerId = $derived(callerAgent?.agent_id ?? '');
</script>

<PCardShell
  category="plan"
  categoryLabel="Plan"
  primitiveName="remove_plan"
  {item}
>
  {#snippet body()}
    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">caller</div>
    <div><AgentChip agentId={callerId} agent={callerAgent} /></div>

    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">tasks</div>
    <div class="font-mono text-[11.5px] text-slate-300">
      {#if taskIds.length === 0}
        <span class="text-slate-500 italic">(none)</span>
      {:else}
        {taskIds.join(', ')}
      {/if}
    </div>
  {/snippet}
</PCardShell>
