<script lang="ts">
  import type { AgentState, ToolStreamItem } from '../../lib/types';
  import PCardShell from './PCardShell.svelte';
  import AgentChip from './AgentChip.svelte';

  let { item, callerAgent }: { item: ToolStreamItem; callerAgent?: AgentState } = $props();

  let teamName = $derived((item.input?.team_name as string) ?? (item.input?.name as string) ?? '');
  let members = $derived(((item.input?.members as Array<Record<string, unknown>>) ?? []));
  let callerId = $derived(callerAgent?.agent_id ?? '');
</script>

<PCardShell
  category="lifecycle"
  categoryLabel="Lifecycle"
  primitiveName="create_team"
  deprecated
  {item}
>
  {#snippet badges()}
    <span class="text-[10.5px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-muted/10 text-muted border border-muted/30 font-semibold">
      deprecated · use spawn_agent
    </span>
  {/snippet}

  {#snippet body()}
    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">caller</div>
    <div><AgentChip agentId={callerId} agent={callerAgent} /></div>
    {#if teamName}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">team</div>
      <div class="font-mono text-[11.5px] text-slate-300">{teamName}</div>
    {/if}
    {#if members.length}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">members</div>
      <div class="font-mono text-[11.5px] text-slate-400">{members.length} member{members.length === 1 ? '' : 's'}</div>
    {/if}
  {/snippet}
</PCardShell>
