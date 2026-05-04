<script lang="ts">
  import { questions } from '../../stores/questions.svelte';
  import { events } from '../../stores/events.svelte';
  import { ui } from '../../stores/ui.svelte';

  // Scope the count to the pinned agent's task (matches QuestionBanner scoping).
  let scopedTaskId = $derived(
    ui.pinnedAgentId ? events.agentsById[ui.pinnedAgentId]?.task_id ?? null : null
  );
  let scopedCount = $derived(
    scopedTaskId == null
      ? 0
      : questions.list.filter((q) => q.task_id === scopedTaskId).length
  );

  function jumpToBanner(): void {
    // Banner is in the middle panel; scroll its container if findable; otherwise rely on focus.
    const banner = document.querySelector('[data-question-banner]') as HTMLElement | null;
    if (banner) {
      banner.scrollIntoView({ behavior: 'smooth', block: 'start' });
      const ta = banner.querySelector('textarea') as HTMLTextAreaElement | null;
      if (ta) ta.focus();
    }
  }
</script>

{#if scopedCount > 0}
  <button
    type="button"
    onclick={jumpToBanner}
    class="w-full mt-3 px-3 py-2 rounded border-l-4 border-amber-400 bg-amber-500/10 text-amber-100 text-left text-sm hover:bg-amber-500/20"
  >
    <span class="font-semibold">⚠ {scopedCount} question{scopedCount === 1 ? '' : 's'} waiting</span>
    <span class="block text-xs text-amber-200/80 mt-0.5">Click to answer</span>
  </button>
{/if}
