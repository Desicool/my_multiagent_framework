<script lang="ts">
  import { onDestroy } from 'svelte';
  import type { ToolStreamItem } from '../../../lib/types';
  import { hhmmss } from '../../../lib/time';

  let { item }: { item: ToolStreamItem } = $props();

  let pending = $derived(item.duration_ms === null);
  let isError = $derived(item.is_error === true);
  let now = $state(Date.now());
  let timer: number | null = null;

  $effect(() => {
    // Tick every 250ms while pending
    if (pending) {
      if (timer === null) timer = window.setInterval(() => { now = Date.now(); }, 250);
    } else if (timer !== null) {
      clearInterval(timer); timer = null;
    }
  });
  onDestroy(() => { if (timer !== null) clearInterval(timer); });

  let elapsedMs = $derived(now - item.ts * 1000);
  let elapsedLabel = $derived(elapsedMs < 1000 ? `${Math.max(0, Math.round(elapsedMs))}ms` : `${(elapsedMs / 1000).toFixed(1)}s`);

  let inputJson = $derived(item.input ? JSON.stringify(item.input, null, 2) : '');
  let inputPreview = $derived(item.input ? JSON.stringify(item.input).slice(0, 80) : '');

  function toggle(): void {
    if (pending) return; // can't collapse pending
    item.expanded = !item.expanded;
  }

  let borderClass = $derived(
    pending ? 'border-l-4 border-amber-400' :
    isError ? 'border-l-4 border-rose-500' :
    'border-l-4 border-emerald-500'
  );
  let bgClass = $derived(
    pending ? 'bg-amber-500/5' : isError ? 'bg-rose-500/5' : 'bg-emerald-500/5'
  );
</script>

<div class={`my-2 rounded ${borderClass} ${bgClass}`}>
  <button
    type="button"
    class="w-full flex items-center gap-2 px-3 py-2 text-left text-sm cursor-pointer disabled:cursor-default"
    onclick={toggle}
    disabled={pending}
  >
    <span class="text-[10px] text-slate-500 font-mono">{hhmmss(item.ts)}</span>
    {#if pending}
      <svg class="w-3 h-3 animate-spin text-amber-400" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" stroke-dasharray="40 40" />
      </svg>
      <span class="text-amber-200 font-mono">running: {item.name}</span>
      <span class="text-amber-300/80 font-mono">{elapsedLabel}</span>
      <span class="text-slate-500 font-mono truncate ml-auto">{inputPreview}</span>
    {:else if isError}
      <span class="text-rose-300">✗ {item.name}</span>
      <span class="text-rose-400/80 font-mono">{item.duration_ms}ms</span>
      <span class={`ml-auto text-slate-500 transition-transform ${item.expanded ? 'rotate-90' : ''}`}>▸</span>
    {:else}
      <span class="text-emerald-300">✓ {item.name}</span>
      <span class="text-emerald-400/80 font-mono">{item.duration_ms}ms</span>
      <span class={`ml-auto text-slate-500 transition-transform ${item.expanded ? 'rotate-90' : ''}`}>▸</span>
    {/if}
  </button>
  {#if pending || item.expanded}
    {#if inputJson}
      <pre class="mx-3 mb-2 p-2 max-h-96 overflow-auto bg-slate-950 border border-slate-800 rounded text-xs text-slate-300 font-mono whitespace-pre-wrap">{inputJson}</pre>
    {:else}
      <p class="mx-3 mb-2 text-xs text-slate-600 italic">(no input)</p>
    {/if}
    {#if !pending && isError}
      <p class="mx-3 mb-2 text-[11px] text-rose-300/80 italic">Tool errored. The result body is not in the event stream — see JSONL or pinned-agent terminal.</p>
    {/if}
  {/if}
</div>
