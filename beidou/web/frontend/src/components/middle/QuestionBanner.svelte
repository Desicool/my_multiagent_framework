<script lang="ts">
  import { tick } from 'svelte';
  import { questions, triggerRepoll } from '../../stores/questions.svelte';
  import { events } from '../../stores/events.svelte';
  import { pinAgent } from '../../stores/ui.svelte';
  import { submitAnswer } from '../../lib/api';
  import type { StructuredAnswer } from '../../lib/types';
  import { shortId } from '../../lib/format';

  // Per-sub-question state, keyed by index within the current primary question.
  // All reset when primary.qid changes.
  let freeText      = $state<Record<number, string>>({});
  let radioChoice   = $state<Record<number, string | null>>({});   // selected option label
  let otherSelected = $state<Record<number, boolean>>({});          // "Other" radio chosen
  let otherText     = $state<Record<number, string>>({});
  let multiChecked  = $state<Record<number, Record<string, boolean>>>({});

  let submitting    = $state(false);
  let error         = $state<string | null>(null);

  let primary    = $derived(questions.list[0] ?? null);
  let rest       = $derived(questions.list.slice(1));
  // Track only the qid string — polling replaces `primary` with a fresh object
  // reference every 5s, but the qid stays === stable, so the reset effect only
  // fires when the question genuinely changes.
  let primaryQid = $derived(primary?.qid ?? null);

  // Reset all per-sub-question state whenever the primary question changes.
  $effect(() => {
    // eslint-disable-next-line @typescript-eslint/no-unused-expressions
    primaryQid;
    freeText      = {};
    radioChoice   = {};
    otherSelected = {};
    otherText     = {};
    multiChecked  = {};
    error         = null;
  });

  // Validation: every sub-question must be fully answered.
  let canSubmit = $derived.by(() => {
    if (!primary || primary.questions.length === 0) return false;
    return primary.questions.every((sq, i) => {
      if (sq.options.length === 0) {
        // Free-text
        return (freeText[i] ?? '').trim().length > 0;
      }
      if (!sq.multiSelect) {
        // Single-select
        if (otherSelected[i]) return (otherText[i] ?? '').trim().length > 0;
        return !!radioChoice[i];
      }
      // Multi-select
      return Object.values(multiChecked[i] ?? {}).some(Boolean);
    });
  });

  function askerLabel(askerId: string): string {
    const a = events.agentsById[askerId];
    return a?.role ? `${a.role} · ${shortId(askerId, 4)}` : shortId(askerId, 8);
  }

  async function onSubmit(): Promise<void> {
    if (!primary || !canSubmit || submitting) return;
    submitting = true;
    error = null;
    try {
      const answers: StructuredAnswer[] = primary.questions.map((sq, i) => {
        if (sq.options.length === 0) {
          // Free-text
          return { selected_labels: [], text: (freeText[i] ?? '').trim() };
        }
        if (!sq.multiSelect) {
          // Single-select
          if (otherSelected[i]) {
            return { selected_labels: [], text: (otherText[i] ?? '').trim() };
          }
          return { selected_labels: [radioChoice[i]!], text: null };
        }
        // Multi-select: preserve option order
        const selected = sq.options
          .filter(opt => multiChecked[i]?.[opt.label])
          .map(opt => opt.label);
        return { selected_labels: selected, text: null };
      });
      await submitAnswer(primary.qid, { answers });
      await triggerRepoll();
      await tick();
      // Focus the first focusable element in the banner, or the composer.
      const focusable = document.querySelector('[data-question-banner] input, [data-question-banner] textarea') as HTMLElement | null;
      if (focusable) focusable.focus();
      else {
        const composer = document.querySelector('[data-composer-textarea]') as HTMLTextAreaElement | null;
        composer?.focus();
      }
    } catch (e) {
      error = (e as Error).message;
    } finally {
      submitting = false;
    }
  }

  function onPickFromList(askerId: string): void {
    pinAgent(askerId);
  }

  function onBannerClick(ev: MouseEvent, askerId: string): void {
    const t = ev.target as HTMLElement;
    if (t.closest('textarea, button, input, label, fieldset')) return;
    pinAgent(askerId);
  }

  function onBannerKey(ev: KeyboardEvent): void {
    if ((ev.metaKey || ev.ctrlKey) && ev.key === 'Enter') {
      ev.preventDefault();
      onSubmit();
    }
  }
</script>

{#if primary}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <section
    role="alert"
    aria-live="assertive"
    data-question-banner
    class="sticky top-[calc(var(--agent-header-h,5.5rem))] z-10 px-4 py-3 border-l-4 border-amber-400 bg-amber-500/10 backdrop-blur cursor-pointer"
    onclick={(e) => onBannerClick(e, primary.asker_agent_id)}
    onkeydown={onBannerKey}
  >
    <div class="flex items-center gap-2 text-xs uppercase tracking-wider text-amber-300 mb-1.5">
      ⚠ Question waiting
      <span class="text-amber-200/80 normal-case lowercase">from {askerLabel(primary.asker_agent_id)}</span>
    </div>

    {#if primary.context_hint}
      <p class="text-xs text-amber-200/80 italic mb-2">{primary.context_hint}</p>
    {/if}

    <!-- Sub-questions -->
    <div class="space-y-4">
      {#each primary.questions as sq, i}
        <div>
          {#if sq.header}
            <div class="text-xs uppercase tracking-wider text-amber-300/90 mb-1">{sq.header}</div>
          {/if}
          <p class="text-sm text-amber-50 mb-2 whitespace-pre-wrap">{sq.question}</p>

          {#if sq.options.length === 0}
            <!-- Free-text input -->
            <textarea
              class="w-full rounded bg-slate-900/80 border border-amber-500/40 p-2 text-sm text-slate-100 focus:outline-none focus:border-amber-400 resize-y"
              rows="2"
              placeholder="Your answer (Cmd/Ctrl+Enter to submit)"
              bind:value={freeText[i]}
            ></textarea>

          {:else if !sq.multiSelect}
            <!-- Single-select with radio buttons + implicit "Other" -->
            <fieldset class="space-y-2 border-none p-0 m-0">
              <legend class="sr-only">{sq.question}</legend>
              {#each sq.options as opt}
                <label class="flex items-start gap-2 cursor-pointer">
                  <input
                    type="radio"
                    name={`q${i}`}
                    value={opt.label}
                    class="mt-0.5 accent-amber-500 shrink-0"
                    checked={radioChoice[i] === opt.label && !otherSelected[i]}
                    onchange={() => {
                      radioChoice[i] = opt.label;
                      otherSelected[i] = false;
                    }}
                  />
                  <span>
                    <span class="text-sm text-amber-50 font-semibold">{opt.label}</span>
                    {#if opt.description}
                      <span class="block text-xs text-amber-200/70">{opt.description}</span>
                    {/if}
                  </span>
                </label>
              {/each}
              <!-- Implicit "Other" option -->
              <label class="flex items-start gap-2 cursor-pointer">
                <input
                  type="radio"
                  name={`q${i}`}
                  value="__other__"
                  class="mt-0.5 accent-amber-500 shrink-0"
                  checked={otherSelected[i] === true}
                  onchange={() => {
                    otherSelected[i] = true;
                    radioChoice[i] = null;
                  }}
                />
                <span class="text-sm text-amber-50 font-semibold">Other</span>
              </label>
              {#if otherSelected[i]}
                <textarea
                  class="w-full rounded bg-slate-900/80 border border-amber-500/40 p-2 text-sm text-slate-100 focus:outline-none focus:border-amber-400 resize-y mt-1"
                  rows="2"
                  placeholder="Describe your answer…"
                  bind:value={otherText[i]}
                ></textarea>
              {/if}
            </fieldset>

          {:else}
            <!-- Multi-select with checkboxes (no "Other") -->
            <fieldset class="space-y-2 border-none p-0 m-0">
              <legend class="sr-only">{sq.question}</legend>
              {#each sq.options as opt}
                <label class="flex items-start gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    class="mt-0.5 accent-amber-500 shrink-0"
                    checked={multiChecked[i]?.[opt.label] === true}
                    onchange={(e) => {
                      multiChecked[i] = { ...(multiChecked[i] ?? {}), [opt.label]: (e.target as HTMLInputElement).checked };
                    }}
                  />
                  <span>
                    <span class="text-sm text-amber-50 font-semibold">{opt.label}</span>
                    {#if opt.description}
                      <span class="block text-xs text-amber-200/70">{opt.description}</span>
                    {/if}
                  </span>
                </label>
              {/each}
            </fieldset>
          {/if}
        </div>
      {/each}
    </div>

    <div class="mt-3 flex items-center gap-2">
      <button
        type="button"
        class="px-3 py-1 rounded bg-amber-500 hover:bg-amber-400 text-amber-950 text-sm font-semibold disabled:opacity-50"
        onclick={onSubmit}
        disabled={submitting || !canSubmit}
      >
        {submitting ? 'sending…' : 'Answer'}
      </button>
      {#if error}
        <span class="text-xs text-rose-300">{error}</span>
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
