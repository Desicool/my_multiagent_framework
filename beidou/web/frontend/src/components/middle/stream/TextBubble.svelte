<script lang="ts">
  import type { TextStreamItem } from '../../../lib/types';
  import { renderMarkdown } from '../../../lib/markdown';
  import { hhmmss } from '../../../lib/time';

  let { item }: { item: TextStreamItem } = $props();
  let html = $derived(renderMarkdown(item.text));
</script>

<div class="my-2 px-3 py-2 rounded bg-slate-900/40 border-l-2 border-slate-700">
  <div class="text-[10px] text-slate-500 font-mono mb-1">{hhmmss(item.ts)}</div>
  <div class="prose prose-invert prose-sm max-w-none prose-pre:bg-slate-950 prose-pre:border prose-pre:border-slate-800">{@html html}</div>
</div>
