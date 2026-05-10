<script lang="ts">
  import type { AgentState, ToolStreamItem } from '../../lib/types';
  import PCardShell from './PCardShell.svelte';
  import AgentChip from './AgentChip.svelte';
  import FlowStrip from './FlowStrip.svelte';
  import { events } from '../../stores/events.svelte';

  let { item, callerAgent }: { item: ToolStreamItem; callerAgent?: AgentState } = $props();

  let to = $derived((item.input?.to as string) ?? '');
  let content = $derived((item.input?.content as string) ?? '');
  let messageId = $derived((item.input?.message_id as string) ?? '');
  let expectsReply = $derived(item.input?.expects_reply === true);
  let toAgent = $derived(events.agentsById[to]);
  let truncated = $derived(content.length > 280);
  let preview = $derived(truncated ? content.slice(0, 280) : content);
  let callerId = $derived(callerAgent?.agent_id ?? '');
</script>

<PCardShell
  category="coordination"
  categoryLabel="Coordination"
  primitiveName="send_message"
  {item}
>
  {#snippet badges()}
    {#if expectsReply}
      <span class="text-[10.5px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-success/10 text-success border border-success/30 font-semibold">
        expects reply ↩
      </span>
    {/if}
  {/snippet}

  {#snippet flow()}
    <FlowStrip
      fromId={callerId}
      fromAgent={callerAgent}
      toId={to}
      toAgent={toAgent}
      variant="outbound"
    />
  {/snippet}

  {#snippet body()}
    {#if messageId}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">message_id</div>
      <div class="font-mono text-[11.5px] text-slate-400 tabular-nums truncate">{messageId}</div>
    {/if}

    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">content</div>
    <div class="text-slate-200 leading-relaxed text-[11.5px] whitespace-pre-wrap break-words">{preview}{#if truncated}<span class="text-slate-500">… ({content.length} chars)</span>{/if}</div>
  {/snippet}
</PCardShell>
