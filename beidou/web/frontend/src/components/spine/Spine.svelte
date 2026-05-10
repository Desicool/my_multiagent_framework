<script lang="ts">
  import SpineTaskList from './SpineTaskList.svelte';
  import WorkspaceNav from './WorkspaceNav.svelte';

  let { taskId }: { taskId: string } = $props();
</script>

<aside class="h-full bg-surface-2 border-r border-slate-800 flex flex-col py-3 gap-5 min-h-0">
  <!-- TASKS: takes remaining vertical space, scrolls internally so WORKSPACE always stays visible. -->
  <div class="flex-1 min-h-0 overflow-y-auto">
    <SpineTaskList />
  </div>

  <!-- WORKSPACE nav pinned at bottom; never scrolls out of view. -->
  <div class="shrink-0 border-t border-slate-800 pt-3">
    <WorkspaceNav {taskId} />
  </div>
</aside>
