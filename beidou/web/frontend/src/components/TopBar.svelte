<script lang="ts">
  import { onMount } from 'svelte';
  import ConnectionPill from './ConnectionPill.svelte';
  import { route } from '../lib/router.svelte';
  import { events } from '../stores/events.svelte';
  import { notifications } from '../lib/notifications';
  import { shortId } from '../lib/format';

  let permission = $state<NotificationPermission>('default');
  onMount(() => { permission = notifications.getPermissionStatus(); });

  async function enableAlerts(): Promise<void> {
    const r = await notifications.requestPermission();
    permission = r;
  }

  let crumbs = $derived.by(() => {
    const r = route.current;
    if (r.name === 'home') return [{ label: 'Tasks', href: '#/' }];
    if (r.name === 'task') {
      const desc = events.task?.description ? ` · ${events.task.description.slice(0, 60)}` : '';
      return [
        { label: 'Tasks', href: '#/' },
        { label: `${shortId(r.taskId)}${desc}`, href: `#/tasks/${r.taskId}` },
      ];
    }
    if (r.name === 'agent') {
      return [
        { label: 'Tasks', href: '#/' },
        { label: `agent ${shortId(r.agentId)}`, href: `#/agents/${r.agentId}` },
      ];
    }
    return [{ label: 'Tasks', href: '#/' }];
  });
</script>

<header class="flex items-center justify-between gap-4 px-4 py-3 border-b border-slate-800 bg-slate-950/80 backdrop-blur sticky top-0 z-30">
  <div class="flex items-center gap-3 min-w-0">
    <a href="#/" class="font-semibold tracking-wide text-emerald-300 shrink-0">北斗 Beidou</a>
    <nav class="flex items-center gap-1.5 text-sm text-slate-400 truncate">
      {#each crumbs as c, i}
        {#if i > 0}<span class="text-slate-600">/</span>{/if}
        <a href={c.href} class="hover:text-slate-200 truncate">{c.label}</a>
      {/each}
    </nav>
  </div>
  <div class="flex items-center gap-2 shrink-0">
    {#if permission === 'default'}
      <button onclick={enableAlerts} class="text-xs px-2 py-0.5 rounded border border-slate-700 hover:border-slate-500 text-slate-200">
        🔔 Enable alerts
      </button>
    {/if}
    <ConnectionPill />
  </div>
</header>
