<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import TopBar from './components/TopBar.svelte';
  import Layout from './components/Layout.svelte';
  import TaskOverviewPanel from './components/left/TaskOverviewPanel.svelte';
  import PinnedAgentPanel from './components/middle/PinnedAgentPanel.svelte';
  import TeamTreePanel from './components/right/TeamTreePanel.svelte';
  import TasksList from './components/home/TasksList.svelte';
  import { route, startRouter } from './lib/router.svelte';
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

  $effect(() => {
    const r = route.current;
    if (r.name === 'task') {
      openTask(r.taskId);
    } else {
      closeTask();
    }
  });

  $effect(() => {
    const r = route.current;
    if (r.name === 'task' && !ui.pinnedAgentId && events.rootAgentId) {
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
    <main class="overflow-y-auto h-[calc(100vh-3.25rem)]"><TasksList /></main>
  {:else if route.current.name === 'task' || route.current.name === 'agent'}
    <Layout>
      {#snippet left()}<TaskOverviewPanel />{/snippet}
      {#snippet middle()}<PinnedAgentPanel />{/snippet}
      {#snippet right()}<TeamTreePanel />{/snippet}
    </Layout>
  {:else}
    <main class="p-8 text-slate-400">Unknown route: {route.current.name === 'unknown' ? route.current.raw : ''}</main>
  {/if}
</div>
