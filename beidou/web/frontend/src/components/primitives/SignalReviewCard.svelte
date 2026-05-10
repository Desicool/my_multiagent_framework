<script lang="ts">
  import type { AgentState, ToolStreamItem } from '../../lib/types';
  import PCardShell from './PCardShell.svelte';
  import AgentChip from './AgentChip.svelte';

  let { item, callerAgent }: { item: ToolStreamItem; callerAgent?: AgentState } = $props();

  let envelope = $derived((item.input?.envelope as string) ?? (item.input?.message as string) ?? '');
  let state = $derived((item.input?.state as string) ?? '');
  let iteration = $derived(item.input?.iteration as number | undefined);
  let callerId = $derived(callerAgent?.agent_id ?? '');

  // Pick tag based on state — convention: state 'review_pending' or 'iteration_ready'
  let tag = $derived.by(() => {
    if (state === 'iteration_ready' || /\[ITERATION READY\]/.test(envelope)) return '[ITERATION READY]';
    return '[REVIEW REQUIRED]';
  });
</script>

<PCardShell
  category="review"
  categoryLabel="Review"
  primitiveName="signal_review"
  {item}
>
  {#snippet badges()}
    <span class="text-[10.5px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-review/10 text-review border border-review/30 font-semibold">
      stays alive
    </span>
  {/snippet}

  {#snippet body()}
    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">caller</div>
    <div>
      <AgentChip agentId={callerId} agent={callerAgent} />
      {#if iteration !== undefined}
        <span class="text-slate-400 text-[11.5px] ml-2">· iteration {iteration}</span>
      {/if}
    </div>

    {#if state}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">state</div>
      <div class="font-mono text-[11.5px] text-slate-300">{state}</div>
    {/if}

    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">envelope</div>
    <div>
      <span class="inline-block bg-review/10 text-review border border-review/30 px-1.5 py-px rounded text-[10.5px] font-semibold tracking-wider mb-1.5">
        {tag}
      </span>
      {#if envelope}
        <pre class="bg-surface border border-surface-border rounded px-3 py-2.5 font-mono text-[11.5px] leading-relaxed text-slate-200 whitespace-pre-wrap break-words">{envelope}</pre>
      {:else}
        <div class="text-slate-500 italic text-[11.5px]">(no envelope)</div>
      {/if}
    </div>
  {/snippet}
</PCardShell>
