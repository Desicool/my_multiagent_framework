<script lang="ts">
  import { questions } from '../../stores/questions.svelte';

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

{#if questions.list.length > 0}
  <button
    type="button"
    onclick={jumpToBanner}
    class="w-full mt-3 px-3 py-2 rounded border-l-4 border-amber-400 bg-amber-500/10 text-amber-100 text-left text-sm hover:bg-amber-500/20"
  >
    <span class="font-semibold">⚠ {questions.list.length} question{questions.list.length === 1 ? '' : 's'} waiting</span>
    <span class="block text-xs text-amber-200/80 mt-0.5">Click to answer</span>
  </button>
{/if}
