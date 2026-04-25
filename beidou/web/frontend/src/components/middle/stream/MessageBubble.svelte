<script lang="ts">
  import type { MessageInStreamItem, MessageOutStreamItem } from '../../../lib/types';
  import { events } from '../../../stores/events.svelte';
  import { shortId } from '../../../lib/format';
  import { hhmmss } from '../../../lib/time';
  import { renderMarkdown } from '../../../lib/markdown';

  let { item }: { item: MessageInStreamItem | MessageOutStreamItem } = $props();

  function label(): string {
    if (item.kind === 'message_in') {
      if (item.from_is_user) return 'from user';
      const a = events.agentsById[item.from];
      return `from ${a?.role ?? shortId(item.from, 6)}`;
    }
    const a = events.agentsById[item.to];
    return `→ ${a?.role ?? shortId(item.to, 6)}`;
  }

  let isUserBubble = $derived(item.kind === 'message_in' && item.from_is_user);
  let isOut = $derived(item.kind === 'message_out');
  let html = $derived(renderMarkdown(item.content));
</script>

<div class={`my-2 flex ${isUserBubble ? 'justify-end' : 'justify-start'}`}>
  <div class={`max-w-[85%] px-3 py-2 rounded ${
    isUserBubble ? 'bg-emerald-500/15 border border-emerald-500/40' :
    isOut ? 'bg-sky-500/10 border border-sky-500/30' :
    'bg-slate-800/40 border border-slate-700'
  }`}>
    <div class="text-[10px] text-slate-500 font-mono mb-1 flex items-center gap-2">
      <span>{hhmmss(item.ts)}</span>
      <span class={isUserBubble ? 'text-emerald-300' : isOut ? 'text-sky-300' : 'text-slate-400'}>{label()}</span>
    </div>
    <div class="prose prose-invert prose-sm max-w-none">{@html html}</div>
  </div>
</div>
