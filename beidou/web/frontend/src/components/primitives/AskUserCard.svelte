<script lang="ts">
  import type { AgentState, ToolStreamItem, SubQuestion } from '../../lib/types';
  import PCardShell from './PCardShell.svelte';
  import AgentChip from './AgentChip.svelte';
  import FlowStrip from './FlowStrip.svelte';
  import { events } from '../../stores/events.svelte';
  import { questions } from '../../stores/questions.svelte';

  let { item, callerAgent }: { item: ToolStreamItem; callerAgent?: AgentState } = $props();

  let qid = $derived((item.input?.qid as string) ?? '');
  let context = $derived((item.input?.context as string) ?? (item.input?.context_hint as string) ?? '');
  let callerId = $derived(callerAgent?.agent_id ?? '');

  // Sub-questions can come either from the tool input directly OR from the
  // pending question store (richer data, populated by /api).
  let pending = $derived(qid ? questions.list.find((q) => q.qid === qid) : undefined);
  let subQuestions = $derived.by<SubQuestion[]>(() => {
    if (pending?.questions) return pending.questions;
    const inp = item.input?.questions;
    if (Array.isArray(inp)) return inp as SubQuestion[];
    return [];
  });
  let chain = $derived(pending?.chain ?? []);
  let chainHops = $derived(chain.length || undefined);
</script>

<PCardShell
  category="human"
  categoryLabel="Human"
  primitiveName="ask_user"
  {item}
>
  {#snippet badges()}
    {#if subQuestions.length > 0}
      <span class="text-[10.5px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-accent/10 text-accent border border-accent/30 font-semibold">
        {subQuestions.length} sub-q
      </span>
    {/if}
  {/snippet}

  {#snippet flow()}
    <FlowStrip
      fromId={callerId}
      fromAgent={callerAgent}
      fromLabel="Asker"
      toId="user"
      toLabel="Routes to (terminal)"
      variant="user"
      userTerminal
      chainHops={chainHops}
    />
  {/snippet}

  {#snippet body()}
    {#if qid}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">qid</div>
      <div class="font-mono text-[11.5px] text-slate-400 truncate">{qid}</div>
    {/if}

    {#if chain.length > 0}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">chain</div>
      <div class="flex items-center gap-1 flex-wrap font-mono text-[11px]">
        {#each chain as hop, i (hop + i)}
          <span class={hop === 'user' ? 'text-pending bg-pending/10 border border-pending/30 px-1.5 py-px rounded' : 'text-slate-200'}>{hop}</span>
          {#if i < chain.length - 1}
            <span class="text-muted">→</span>
          {/if}
        {/each}
      </div>
    {/if}

    {#if context}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">context</div>
      <div class="text-slate-400 text-[11.5px] leading-relaxed line-clamp-3">{context}</div>
    {/if}

    {#if subQuestions.length > 0}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">questions</div>
      <div class="flex flex-col gap-1 mt-px">
        {#each subQuestions as q (q.header + q.question.slice(0, 32))}
          <div class="grid grid-cols-[80px_1fr] gap-2.5 py-1 items-baseline">
            <span class="text-center text-[10px] uppercase tracking-wider text-accent bg-accent/10 border border-accent/30 px-1.5 py-px rounded font-semibold">
              {q.header}
            </span>
            <span class="text-slate-200 text-[11.5px] leading-snug">{q.question}</span>
          </div>
        {/each}
      </div>
    {/if}
  {/snippet}
</PCardShell>
