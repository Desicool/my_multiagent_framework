<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import ConnectionPill from './ConnectionPill.svelte';
  import { route, navigate } from '../lib/router.svelte';
  import { events } from '../stores/events.svelte';
  import { questions } from '../stores/questions.svelte';
  import { notifications } from '../lib/notifications';
  import { fmtMoney, shortId } from '../lib/format';

  let permission = $state<NotificationPermission>('default');
  let now = $state(Date.now());
  let timer: number | null = null;

  onMount(() => {
    permission = notifications.getPermissionStatus();
    timer = window.setInterval(() => { now = Date.now(); }, 1000);
  });
  onDestroy(() => { if (timer !== null) clearInterval(timer); });

  async function enableAlerts(): Promise<void> {
    permission = await notifications.requestPermission();
  }

  let activeTaskId = $derived.by(() => {
    const r = route.current;
    if (r.name === 'task') return r.taskId;
    if (r.name === 'task_workspace') return r.taskId;
    return null;
  });

  // Live HH:MM:SS, falls back to null before task event arrives.
  let elapsedDisplay = $derived.by(() => {
    const start = events.task?.started_at;
    if (!start) return null;
    const ms = (events.task?.ended_at ? events.task.ended_at * 1000 : now) - start * 1000;
    if (ms < 0) return null;
    const s = Math.floor(ms / 1000);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
  });

  let agentsAlive = $derived.by(() => {
    let alive = 0;
    for (const a of Object.values(events.agentsById)) if (a.status !== 'done') alive += 1;
    return alive;
  });

  let cost = $derived(events.stats?.total_cost_usd ?? events.task?.total_cost_usd ?? 0);

  let pendingForTask = $derived.by(() => {
    if (!activeTaskId) return 0;
    return questions.list.filter((q) => !q.task_id || q.task_id === activeTaskId).length;
  });

  function goAgentsForPending(): void {
    if (activeTaskId) navigate(`/tasks/${activeTaskId}/agents`);
  }
</script>

<header
  class="flex items-center gap-4 px-4 h-11 border-b border-slate-800 bg-surface-raised sticky top-0 z-30 text-[13px]"
>
  <a
    href="#/"
    class="flex items-center gap-1.5 font-semibold tracking-wide shrink-0 text-slate-100 hover:text-white"
  >
    <span class="text-review">◆</span><span>BEIDOU</span>
  </a>

  {#if activeTaskId}
    <span class="w-px h-4 bg-slate-700"></span>
    <span class="font-mono tabnum text-slate-300 truncate" title={activeTaskId}>
      {shortId(activeTaskId, 12)}
    </span>

    {#if elapsedDisplay}
      <span class="w-px h-4 bg-slate-700"></span>
      <span class="inline-flex items-baseline gap-1.5">
        <span class="text-[11px] uppercase tracking-wider text-slate-500">elapsed</span>
        <span class="font-mono tabnum text-slate-200">{elapsedDisplay}</span>
      </span>
    {/if}

    <span class="w-px h-4 bg-slate-700"></span>
    <span class="inline-flex items-baseline gap-1.5">
      <span class="font-mono tabnum text-slate-200">{agentsAlive}</span>
      <span class="text-[11px] text-slate-500">{agentsAlive === 1 ? 'agent' : 'agents'}</span>
    </span>

    <span class="w-px h-4 bg-slate-700"></span>
    <span class="inline-flex items-baseline gap-1.5">
      <span class="text-[11px] uppercase tracking-wider text-slate-500">cost</span>
      <span class="font-mono tabnum text-slate-200">{fmtMoney(cost)}</span>
    </span>
  {/if}

  <div class="ml-auto flex items-center gap-2 shrink-0">
    {#if pendingForTask > 0}
      <button
        type="button"
        onclick={goAgentsForPending}
        class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border bg-amber-500/10 text-pending border-amber-500/30 hover:bg-amber-500/20 transition-colors cursor-pointer"
        title="Pending question — click to view in /agents"
      >
        <span class="w-1.5 h-1.5 rounded-full bg-pending animate-pulse"></span>
        <span class="font-mono tabnum">{pendingForTask}</span>
        <span>{pendingForTask === 1 ? 'question' : 'questions'}</span>
      </button>
    {/if}
    {#if permission === 'default'}
      <button
        onclick={enableAlerts}
        class="text-xs px-2 py-0.5 rounded border border-slate-700 hover:border-slate-500 text-slate-300"
      >
        Enable alerts
      </button>
    {/if}
    <ConnectionPill />
  </div>
</header>
