<script lang="ts">
  import type { AgentState, ToolStreamItem } from '../../lib/types';
  import PCardShell from './PCardShell.svelte';
  import AgentChip from './AgentChip.svelte';

  let { item, callerAgent }: { item: ToolStreamItem; callerAgent?: AgentState } = $props();

  let reason = $derived((item.input?.reason as string) ?? '');
  let callerId = $derived(callerAgent?.agent_id ?? '');
</script>

<PCardShell
  category="review"
  categoryLabel="Review"
  primitiveName="request_termination"
  {item}
>
  {#snippet body()}
    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">caller</div>
    <div><AgentChip agentId={callerId} agent={callerAgent} /></div>

    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">flow</div>
    <div class="text-slate-300 text-[11.5px]">caller asks <span class="font-mono text-review">leader</span> for termination — leader decides</div>

    {#if reason}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">reason</div>
      <div class="text-slate-300 text-[11.5px]">{reason}</div>
    {/if}
  {/snippet}
</PCardShell>
