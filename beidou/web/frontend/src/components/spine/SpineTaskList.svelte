<script lang="ts">
  import { tasks } from '../../stores/tasks.svelte';
  import SpineTaskItem from './SpineTaskItem.svelte';
</script>

<div>
  <div class="px-4 pb-1.5">
    <span class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Tasks</span>
  </div>
  <div class="flex flex-col">
    {#each tasks.list as t (t.task_id)}
      <SpineTaskItem taskId={t.task_id} />
    {:else}
      <p class="px-4 py-2 text-xs text-slate-600 italic">No active tasks</p>
    {/each}
  </div>
</div>
