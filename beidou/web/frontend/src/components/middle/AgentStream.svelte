<script lang="ts">
  import { tick } from 'svelte';
  import type { AgentState, StreamItem } from '../../lib/types';
  import TextBubble from './stream/TextBubble.svelte';
  import MessageBubble from './stream/MessageBubble.svelte';
  import ToolCard from './stream/ToolCard.svelte';
  import TurnDivider from './stream/TurnDivider.svelte';

  let { agent }: { agent: AgentState } = $props();

  let scrollEl: HTMLDivElement | null = null;
  let pinnedToBottom = $state(true);

  function onScroll(): void {
    if (!scrollEl) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollEl;
    pinnedToBottom = (scrollHeight - scrollTop - clientHeight) < 60;
  }

  // Auto-scroll on new items if we were pinned.
  let lastLen = -1;
  $effect(() => {
    const len = agent._stream.length;
    if (len !== lastLen) {
      lastLen = len;
      if (pinnedToBottom) {
        tick().then(() => {
          if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
        });
      }
    }
  });
</script>

<div bind:this={scrollEl} onscroll={onScroll} class="flex-1 overflow-y-auto px-4 py-2">
  {#if agent._stream.length === 0}
    <p class="text-sm text-slate-600 italic mt-4">No activity yet…</p>
  {:else}
    {#each agent._stream as item, i (i + ':' + item.kind + ':' + item.ts)}
      {#if item.kind === 'text'}
        <TextBubble {item} />
      {:else if item.kind === 'tool'}
        <ToolCard {item} />
      {:else if item.kind === 'message_in' || item.kind === 'message_out'}
        <MessageBubble {item} />
      {:else if item.kind === 'turn_divider'}
        <TurnDivider {item} />
      {/if}
    {/each}
  {/if}
</div>
