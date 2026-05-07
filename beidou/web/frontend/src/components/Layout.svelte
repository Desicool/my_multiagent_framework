<script lang="ts">
  import type { Snippet } from 'svelte';
  import Spine from './spine/Spine.svelte';

  let { taskId, workspace }: { taskId: string; workspace: Snippet } = $props();
</script>

<div class="grid h-[calc(100vh-44px)] grid-cols-[240px_minmax(0,1fr)] bg-slate-950">
  <Spine {taskId} />
  <section class="overflow-hidden min-w-0">{@render workspace()}</section>
</div>
