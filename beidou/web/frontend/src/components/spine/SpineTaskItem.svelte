<script lang="ts">
  import { route, navigate } from '../../lib/router.svelte';
  import { tasks } from '../../stores/tasks.svelte';
  import { shortId } from '../../lib/format';

  let { taskId }: { taskId: string } = $props();

  let t = $derived(tasks.list.find((x) => x.task_id === taskId));
  let agentsAlive = $derived(t?.live_agent_count ?? 0);

  let isActive = $derived.by(() => {
    const r = route.current;
    if (r.name === 'task' && r.taskId === taskId) return true;
    if (r.name === 'task_workspace' && r.taskId === taskId) return true;
    return false;
  });

  function go(): void {
    const r = route.current;
    const ws = r.name === 'task_workspace' ? r.workspace : 'agents';
    navigate(`/tasks/${taskId}/${ws}`);
  }
</script>

<button
  type="button"
  onclick={go}
  class="w-full text-left flex items-center gap-2 px-3 py-1.5 text-xs transition-colors cursor-pointer
    {isActive
      ? 'bg-cyan-950/40 text-slate-100'
      : 'text-slate-400 hover:text-slate-100 hover:bg-slate-800/40'}"
  title={taskId}
>
  <span class="shrink-0 {isActive ? 'text-review' : 'text-slate-600'}">○</span>
  <span class="font-mono tabnum truncate">{shortId(taskId, 10)}</span>
  <span class="ml-auto font-mono tabnum text-[10px] text-slate-600 shrink-0">
    {agentsAlive > 0 ? `${agentsAlive} ag` : '—'}
  </span>
</button>
