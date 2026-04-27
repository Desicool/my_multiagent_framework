<script lang="ts">
  import type { MessageInStreamItem, MessageOutStreamItem } from '../../../lib/types';
  import { events } from '../../../stores/events.svelte';
  import { shortId } from '../../../lib/format';
  import { hhmmss } from '../../../lib/time';
  import { renderMarkdown } from '../../../lib/markdown';

  let { item }: { item: MessageInStreamItem | MessageOutStreamItem } = $props();

  function label(): string {
    if (item.kind === 'message_in') {
      if (item.from_is_user) return 'from you';
      const a = events.agentsById[item.from];
      return `from ${a?.name ?? a?.role ?? shortId(item.from, 6)}`;
    }
    const a = events.agentsById[item.to];
    return `→ ${a?.name ?? a?.role ?? shortId(item.to, 6)}`;
  }

  let isUserBubble = $derived(item.kind === 'message_in' && item.from_is_user);
  let isOut = $derived(item.kind === 'message_out');
  let html = $derived(renderMarkdown(item.content));

  // Bubble surface classes
  let bubbleClass = $derived(
    isUserBubble ? 'bg-success/10 border border-success/30' :
    isOut        ? 'bg-info/5 border border-info/20' :
                   'bg-surface-raised/60 border border-surface-border'
  );

  // Speaker label color
  let speakerClass = $derived(
    isUserBubble ? 'text-success/70' :
    isOut        ? 'text-info/70' :
                   'text-muted'
  );
</script>

<div class={`my-2 flex ${isUserBubble ? 'justify-end' : 'justify-start'}`}>
  <div class={`max-w-[85%] px-3 py-2.5 rounded-lg ${bubbleClass}`}>
    <!-- Speaker + timestamp meta row -->
    <div class="flex items-center gap-2 mb-1.5">
      <span class="text-xs text-muted font-mono">{hhmmss(item.ts)}</span>
      <span class={`text-xs font-medium ${speakerClass}`}>{label()}</span>
    </div>
    <!-- Message body -->
    <div class="prose prose-invert prose-sm max-w-none">{@html html}</div>
  </div>
</div>
