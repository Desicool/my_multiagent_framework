<script lang="ts">
  import type { AgentState, ToolStreamItem } from '../../lib/types';
  import PCardShell from './PCardShell.svelte';
  import AgentChip from './AgentChip.svelte';

  let { item, callerAgent }: { item: ToolStreamItem; callerAgent?: AgentState } = $props();
  let callerId = $derived(callerAgent?.agent_id ?? '');
</script>

<PCardShell
  category="plan"
  categoryLabel="Plan"
  primitiveName="list_ready"
  {item}
>
  {#snippet body()}
    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">caller</div>
    <div><AgentChip agentId={callerId} agent={callerAgent} /></div>

    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">query</div>
    <div class="font-mono text-[11.5px] text-slate-400 italic">tasks with no open dependencies</div>
  {/snippet}
</PCardShell>
