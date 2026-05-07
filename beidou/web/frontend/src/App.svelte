<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import TopBar from './components/TopBar.svelte';
  import Layout from './components/Layout.svelte';
  import AgentsWorkspace from './components/workspaces/AgentsWorkspace.svelte';
  import PlaceholderWorkspace from './components/workspaces/PlaceholderWorkspace.svelte';
  import TasksList from './components/home/TasksList.svelte';
  import { route, startRouter, navigate } from './lib/router.svelte';
  import { startPolling as startTasks, stopPolling as stopTasks } from './stores/tasks.svelte';
  import { startPolling as startQ, stopPolling as stopQ, questions } from './stores/questions.svelte';
  import { openTask, closeTask } from './lib/streamService';
  import { notifications } from './lib/notifications';
  import { ui, pinAgent } from './stores/ui.svelte';
  import { events } from './stores/events.svelte';

  let stopRouter: (() => void) | null = null;
  let pinAgentListener: ((ev: Event) => void) | null = null;
  let prevQids = new Set<string>();

  onMount(() => {
    notifications.init();
    stopRouter = startRouter();
    startTasks();
    startQ();
    pinAgentListener = (ev: Event) => {
      const detail = (ev as CustomEvent<{ agentId: string }>).detail;
      if (detail?.agentId) pinAgent(detail.agentId);
    };
    window.addEventListener('beidou:pin-agent', pinAgentListener);
  });

  onDestroy(() => {
    notifications.destroy();
    if (stopRouter) stopRouter();
    stopTasks();
    stopQ();
    closeTask();
    if (pinAgentListener) window.removeEventListener('beidou:pin-agent', pinAgentListener);
  });

  // Legacy /tasks/{id} → /tasks/{id}/agents
  $effect(() => {
    const r = route.current;
    if (r.name === 'task') navigate(`/tasks/${r.taskId}/agents`);
  });

  // Active task drives the WS subscription regardless of which workspace is selected.
  let activeTaskId = $derived.by(() => {
    const r = route.current;
    if (r.name === 'task_workspace') return r.taskId;
    if (r.name === 'task') return r.taskId;
    return null;
  });

  $effect(() => {
    if (activeTaskId) openTask(activeTaskId);
    else closeTask();
  });

  // Auto-pin root agent when entering a task.
  $effect(() => {
    if (activeTaskId && !ui.pinnedAgentId && events.rootAgentId) {
      pinAgent(events.rootAgentId);
    }
  });

  $effect(() => {
    const list = questions.list;
    notifications.setQuestionCount(list.length);
    const currentQids = new Set(list.map((q) => q.qid));
    for (const q of list) {
      if (!prevQids.has(q.qid)) {
        const asker = events.agentsById[q.asker_agent_id];
        const askerLabel = asker?.name ?? asker?.role ?? q.asker_agent_id.slice(0, 6);
        notifications.notifyNewQuestion(q, askerLabel);
      }
    }
    prevQids = currentQids;
  });
</script>

<svelte:head><title>Beidou</title></svelte:head>

<div class="min-h-screen bg-slate-950 text-slate-100">
  <TopBar />

  {#if route.current.name === 'home'}
    <main class="overflow-y-auto h-[calc(100vh-44px)]"><TasksList /></main>

  {:else if route.current.name === 'task_workspace'}
    {@const r = route.current}
    <Layout taskId={r.taskId}>
      {#snippet workspace()}
        {#if r.workspace === 'agents'}
          <AgentsWorkspace />
        {:else if r.workspace === 'timeline'}
          <PlaceholderWorkspace name="timeline" glyph="≡" pr="PR 3" hint="milestone event timeline" />
        {:else if r.workspace === 'tools'}
          <PlaceholderWorkspace name="tools" glyph="▦" pr="PR 2" hint="primitive card explorer" />
        {:else}
          <PlaceholderWorkspace name={r.workspace} glyph="○" pr="P1" hint="deferred post-MVP" />
        {/if}
      {/snippet}
    </Layout>

  {:else if route.current.name === 'task'}
    <main class="p-8 text-slate-500 text-sm">Redirecting to /agents…</main>

  {:else if route.current.name === 'agent'}
    <main class="p-8 text-slate-400 max-w-3xl mx-auto">
      <p class="mb-2">Agent-only deep links are no longer top-level routes.</p>
      <p class="text-sm text-slate-500">
        Open the task that hosts this agent and use the team tree to pin it.
      </p>
    </main>

  {:else}
    <main class="p-8 text-slate-400">
      Unknown route: {route.current.name === 'unknown' ? route.current.raw : ''}
    </main>
  {/if}
</div>
