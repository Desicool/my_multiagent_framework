<script lang="ts">
  import type { AgentState, ToolStreamItem } from '../../lib/types';
  import PCardShell from './PCardShell.svelte';
  import AgentChip from './AgentChip.svelte';

  let { item, callerAgent }: { item: ToolStreamItem; callerAgent?: AgentState } = $props();

  let envelope = $derived((item.input?.envelope as string) ?? (item.input?.message as string) ?? '');
  let state = $derived((item.input?.state as string) ?? '');
  let callerId = $derived(callerAgent?.agent_id ?? '');
</script>

<PCardShell
  category="review"
  categoryLabel="Review"
  primitiveName="report_status"
  deprecated
  {item}
>
  {#snippet badges()}
    <span class="text-[10.5px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-muted/10 text-muted border border-muted/30 font-semibold">
      deprecated · use signal_review
    </span>
  {/snippet}

  {#snippet body()}
    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">caller</div>
    <div><AgentChip agentId={callerId} agent={callerAgent} /></div>

    {#if state}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">state</div>
      <div class="font-mono text-[11.5px] text-slate-400">"{state}" → delegated to signal_review</div>
    {/if}

    {#if envelope}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">envelope</div>
      <div>
        <span class="inline-block bg-muted/10 text-muted border border-muted/30 px-1.5 py-px rounded text-[10.5px] font-semibold tracking-wider mb-1.5">
          [REVIEW REQUIRED]
        </span>
        <pre class="bg-surface border border-surface-border rounded px-3 py-2.5 font-mono text-[11.5px] leading-relaxed text-slate-300 whitespace-pre-wrap break-words">{envelope}</pre>
      </div>
    {/if}
  {/snippet}
</PCardShell>
