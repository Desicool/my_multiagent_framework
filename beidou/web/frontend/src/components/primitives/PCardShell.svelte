<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { ToolStreamItem } from '../../lib/types';
  import { hhmmss } from '../../lib/time';
  import {
    type PrimitiveCategory,
    categoryAccentClasses,
  } from '../../lib/primitives';

  let {
    /** Category drives the top-stripe and category-label color. */
    category,
    /** Pre-computed primitive name shown in mono in the head (e.g. "send_message"). */
    primitiveName,
    /** Category label (e.g. "Coordination"). Resolved by caller from the primitive meta. */
    categoryLabel,
    deprecated = false,
    item,
    /** Optional badge snippets rendered after the primitive name in the head. */
    badges,
    /** Flow-strip slot rendered between head and body. Leave undefined for body-only cards. */
    flow,
    /** Body grid (caller is responsible for k/v rows; PCardShell provides container styles). */
    body,
  }: {
    category: PrimitiveCategory;
    primitiveName: string;
    categoryLabel: string;
    deprecated?: boolean;
    item: ToolStreamItem;
    badges?: Snippet;
    flow?: Snippet;
    body: Snippet;
  } = $props();

  let pending = $derived(item.duration_ms === null);
  let isError = $derived(item.is_error === true);

  let accent = $derived(categoryAccentClasses(category));
  let stripeClass = $derived(deprecated ? 'bg-muted' : accent.stripeClass);
  let categoryClass = $derived(deprecated ? 'text-muted' : accent.textClass);

  let nameClass = $derived(deprecated
    ? 'text-slate-400 line-through decoration-slate-600 decoration-1'
    : 'text-slate-100');
</script>

<div class={`relative bg-surface-raised border border-surface-border rounded-md overflow-hidden my-1.5 ${pending ? 'opacity-90' : ''}`}>
  <!-- Top accent stripe -->
  <div class={`absolute top-0 left-0 right-0 h-0.5 ${stripeClass}`}></div>

  <!-- Head -->
  <div class="flex items-center gap-2.5 px-3.5 pt-2.5 pb-1.5 flex-wrap">
    <span class={`text-[10px] tracking-widest uppercase font-semibold ${categoryClass}`}>{categoryLabel}</span>
    <span class={`font-mono text-[13.5px] font-semibold ${nameClass}`}>{primitiveName}</span>
    {#if badges}{@render badges()}{/if}
    <div class="ml-auto flex items-center gap-3 font-mono text-[11.5px] text-slate-400 tabular-nums">
      <span>{hhmmss(item.ts)}</span>
      {#if pending}
        <span class="inline-flex items-center gap-1.5 text-pending">
          <svg class="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none">
            <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" stroke-dasharray="40 40" />
          </svg>
          pending
        </span>
      {:else if isError}
        <span class="text-error">✗ {item.duration_ms}ms</span>
      {:else}
        <span>{item.duration_ms}ms</span>
      {/if}
    </div>
  </div>

  {#if flow}{@render flow()}{/if}

  <!-- Body grid -->
  <div class="px-3.5 pt-2 pb-3 border-t border-slate-800/60 grid grid-cols-[100px_1fr] gap-x-3.5 gap-y-1.5 text-xs">
    {@render body()}
  </div>

  {#if isError && item.error_reason}
    <div class="mx-3.5 mb-3 px-3 py-2 rounded bg-error/10 border border-error/20">
      <pre class="text-xs text-error/90 font-mono whitespace-pre-wrap max-h-64 overflow-auto">{item.error_reason}</pre>
    </div>
  {/if}
</div>
