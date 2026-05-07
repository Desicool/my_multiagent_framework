<script lang="ts">
  import type { AgentState, ToolStreamItem } from '../../lib/types';
  import PCardShell from './PCardShell.svelte';
  import FlowStrip from './FlowStrip.svelte';
  import { events } from '../../stores/events.svelte';

  let { item, callerAgent }: { item: ToolStreamItem; callerAgent?: AgentState } = $props();

  let targetId = $derived((item.input?.agent_id as string) ?? '');
  let force = $derived(item.input?.force === true);
  let reason = $derived((item.input?.reason as string) ?? '');
  let targetAgent = $derived(events.agentsById[targetId]);
  let callerId = $derived(callerAgent?.agent_id ?? '');
</script>

<PCardShell
  category="lifecycle"
  categoryLabel="Lifecycle"
  primitiveName="terminate_child"
  {item}
>
  {#snippet badges()}
    {#if force}
      <span class="text-[10.5px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-error/10 text-error border border-error/30 font-semibold">
        force
      </span>
    {/if}
  {/snippet}

  {#snippet flow()}
    <FlowStrip
      fromId={callerId}
      fromAgent={callerAgent}
      fromLabel="Leader (caller)"
      toId={targetId}
      toAgent={targetAgent}
      toLabel="Target (terminating)"
      variant="terminate"
      arrow="×⟶"
    />
  {/snippet}

  {#snippet body()}
    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">force</div>
    <div class="font-mono text-[11.5px] text-slate-400">{force ? 'true' : 'false'}</div>
    {#if reason}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">reason</div>
      <div class="text-slate-300 text-[11.5px]">{reason}</div>
    {/if}
  {/snippet}
</PCardShell>
