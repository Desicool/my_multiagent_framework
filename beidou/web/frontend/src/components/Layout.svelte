<script lang="ts">
  import type { Snippet } from 'svelte';
  let { left, middle, right }: { left: Snippet; middle: Snippet; right: Snippet } = $props();
</script>

<div class="grid h-[calc(100vh-3.25rem)] grid-cols-[280px_minmax(0,1fr)_320px] gap-px bg-slate-800">
  <aside class="bg-slate-950 overflow-y-auto">{@render left()}</aside>
  <main class="bg-slate-950 overflow-hidden flex flex-col">{@render middle()}</main>
  <aside class="bg-slate-950 overflow-y-auto">{@render right()}</aside>
</div>
