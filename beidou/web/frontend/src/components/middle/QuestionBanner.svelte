<script lang="ts">
  import { tick } from 'svelte';
  import { questions, triggerRepoll } from '../../stores/questions.svelte';
  import { events } from '../../stores/events.svelte';
  import { pinAgent } from '../../stores/ui.svelte';
  import { submitAnswer } from '../../lib/api';
  import { shortId } from '../../lib/format';

  let answers = $state<Record<string, string>>({});
  let submitting = $state<Record<string, boolean>>({});
  let errors = $state<Record<string, string | null>>({});

  let primary = $derived(questions.list[0] ?? null);
  let rest = $derived(questions.list.slice(1));

  function askerLabel(askerId: string): string {
    const a = events.agentsById[askerId];
    return a?.role ? `${a.role} · ${shortId(askerId, 4)}` : shortId(askerId, 8);
  }

  async function onSubmit(qid: string): Promise<void> {
    const text = (answers[qid] ?? '').trim();
    if (!text) return;
    submitting[qid] = true;
    errors[qid] = null;
    try {
      await submitAnswer(qid, text);
      delete answers[qid];
      await triggerRepoll();
      await tick();
      const focusable = document.querySelector('[data-question-banner] textarea') as HTMLTextAreaElement | null;
      if (focusable) focusable.focus();
      else {
        const composer = document.querySelector('[data-composer-textarea]') as HTMLTextAreaElement | null;
        composer?.focus();
      }
    } catch (e) {
      errors[qid] = (e as Error).message;
    } finally {
      submitting[qid] = false;
    }
  }

  function onPickFromList(askerId: string): void {
    pinAgent(askerId);
  }

  function onBannerClick(ev: MouseEvent, askerId: string): void {
    const t = ev.target as HTMLElement;
    if (t.closest('textarea, button')) return;
    pinAgent(askerId);
  }

  function onKey(ev: KeyboardEvent, qid: string): void {
    if ((ev.metaKey || ev.ctrlKey) && ev.key === 'Enter') {
      ev.preventDefault();
      onSubmit(qid);
    }
  }
</script>

{#if primary}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <section
    role="alert"
    aria-live="assertive"
    data-question-banner
    class="sticky top-[calc(var(--agent-header-h,5.5rem))] z-10 px-4 py-3 border-l-4 border-amber-400 bg-amber-500/10 backdrop-blur cursor-pointer"
    onclick={(e) => onBannerClick(e, primary.asker_agent_id)}
  >
    <div class="flex items-center gap-2 text-xs uppercase tracking-wider text-amber-300 mb-1.5">
      ⚠ Question waiting
      <span class="text-amber-200/80 normal-case lowercase">from {askerLabel(primary.asker_agent_id)}</span>
    </div>
    <p class="text-sm text-amber-50 mb-2 whitespace-pre-wrap">{primary.prompt}</p>
    {#if primary.context_hint}
      <p class="text-xs text-amber-200/80 italic mb-2">{primary.context_hint}</p>
    {/if}
    <textarea
      class="w-full rounded bg-slate-900/80 border border-amber-500/40 p-2 text-sm text-slate-100 focus:outline-none focus:border-amber-400 resize-y"
      rows="2"
      placeholder="Your answer (Cmd/Ctrl+Enter to submit)"
      bind:value={answers[primary.qid]}
      onkeydown={(e) => onKey(e, primary.qid)}
    ></textarea>
    <div class="mt-2 flex items-center gap-2">
      <button
        type="button"
        class="px-3 py-1 rounded bg-amber-500 hover:bg-amber-400 text-amber-950 text-sm font-semibold disabled:opacity-50"
        onclick={() => onSubmit(primary.qid)}
        disabled={submitting[primary.qid] || !(answers[primary.qid] ?? '').trim()}
      >
        {submitting[primary.qid] ? 'sending…' : 'Answer'}
      </button>
      {#if errors[primary.qid]}
        <span class="text-xs text-rose-300">{errors[primary.qid]}</span>
      {/if}
    </div>

    {#if rest.length > 0}
      <ul class="mt-3 space-y-1 border-t border-amber-500/30 pt-2">
        {#each rest as q}
          <li>
            <button
              type="button"
              class="w-full text-left text-xs text-amber-100/90 hover:bg-amber-500/10 px-2 py-1 rounded flex items-center gap-2"
              onclick={(e) => { e.stopPropagation(); onPickFromList(q.asker_agent_id); }}
            >
              <span class="text-amber-300">▸</span>
              <span class="text-amber-200/90">{askerLabel(q.asker_agent_id)}</span>
              <span class="text-amber-100/80 truncate">· {q.prompt.slice(0, 80)}</span>
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
{/if}
