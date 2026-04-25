<script lang="ts">
  import type { TurnDividerStreamItem } from '../../../lib/types';
  let { item }: { item: TurnDividerStreamItem } = $props();
</script>

<div class="my-2 flex items-center gap-2 text-[10px] text-slate-600 uppercase tracking-widest">
  <span class="flex-1 border-t border-slate-800"></span>
  <span class="px-1">turn · in:{item.in_tok} out:{item.out_tok}{#if item.stop_reason} · {item.stop_reason}{/if}</span>
  <span class="flex-1 border-t border-slate-800"></span>
</div>
