<script lang="ts">
  import { onDestroy } from 'svelte';
  import type { ToolStreamItem } from '../../../lib/types';
  import { hhmmss } from '../../../lib/time';
  import { displayToolName } from '../../../lib/format';

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

  // Semantic token classes per state
  let borderClass = $derived(
    pending ? 'border-l-2 border-pending' :
    isError ? 'border-l-2 border-error' :
    'border-l-2 border-success'
  );
  let bgClass = $derived(
    pending ? 'bg-pending/5' : isError ? 'bg-error/5' : 'bg-success/5'
  );

  let nameClass = $derived(
    pending ? 'text-pending/90' :
    isError ? 'text-error/80' :
    'text-success/90'
  );
  let durationClass = $derived(
    pending ? 'text-pending/60' :
    isError ? 'text-error/60' :
    'text-success/60'
  );
</script>

<div class={`my-1.5 rounded ${borderClass} ${bgClass}`}>
  <button
    type="button"
    class="w-full flex items-center gap-2 px-3 py-2 text-left text-sm cursor-pointer disabled:cursor-default"
    onclick={toggle}
    disabled={pending}
  >
    <span class="text-xs text-muted font-mono shrink-0">{hhmmss(item.ts)}</span>

    {#if pending}
      <!-- Spinner icon -->
      <svg class="w-3 h-3 animate-spin text-pending shrink-0" viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" stroke-dasharray="40 40" />
      </svg>
      <span class={`font-mono text-sm ${nameClass}`}>{displayToolName(item.name)}</span>
      <span class={`font-mono text-xs ${durationClass}`}>{elapsedLabel}</span>
      <span class="text-muted font-mono text-xs truncate ml-auto">{inputPreview}</span>
    {:else if isError}
      <span class={`text-xs font-semibold shrink-0 ${nameClass}`}>✗</span>
      <span class={`font-mono text-sm ${nameClass}`}>{displayToolName(item.name)}</span>
      <span class={`font-mono text-xs ${durationClass}`}>{item.duration_ms}ms</span>
      <span class={`ml-auto text-muted transition-transform ${item.expanded ? 'rotate-90' : ''}`}>▸</span>
    {:else}
      <span class={`text-xs font-semibold shrink-0 ${nameClass}`}>✓</span>
      <span class={`font-mono text-sm ${nameClass}`}>{displayToolName(item.name)}</span>
      <span class={`font-mono text-xs ${durationClass}`}>{item.duration_ms}ms</span>
      <span class={`ml-auto text-muted transition-transform ${item.expanded ? 'rotate-90' : ''}`}>▸</span>
    {/if}
  </button>

  {#if pending || item.expanded}
    {#if inputJson}
      <pre class="mx-3 mb-2 p-2 max-h-96 overflow-auto bg-surface border border-surface-border rounded text-xs text-slate-300 font-mono whitespace-pre-wrap">{inputJson}</pre>
    {:else}
      <p class="mx-3 mb-2 text-xs text-muted italic">(no input)</p>
    {/if}
    {#if !pending && isError}
      <div class="mx-3 mb-2 px-3 py-2 rounded bg-error/10 border border-error/20">
        <p class="text-xs text-error/80 italic">Tool errored. The result body is not in the event stream — see JSONL or pinned-agent terminal.</p>
      </div>
    {/if}
  {/if}
</div>
