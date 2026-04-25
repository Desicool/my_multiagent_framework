<script lang="ts">
  import { tasks } from '../../stores/tasks.svelte';
  import { ago } from '../../lib/time';
  import { fmtMoney, shortId } from '../../lib/format';
  import { navigate } from '../../lib/router.svelte';
</script>

<div class="p-6 max-w-4xl mx-auto text-slate-200">
  <div class="flex items-baseline justify-between mb-4">
    <h2 class="text-2xl font-semibold">Tasks</h2>
    {#if tasks.lastFetched}
      <span class="text-xs text-slate-600">updated {ago(tasks.lastFetched / 1000)}</span>
    {/if}
  </div>

  {#if tasks.error}
    <p class="text-sm text-rose-400 mb-3">error: {tasks.error}</p>
  {/if}

  {#if tasks.list.length === 0}
    <p class="text-slate-600 italic">No tasks yet. Run <code class="text-slate-400 font-mono">beidou run "&lt;your prompt&gt;"</code> in a terminal.</p>
  {:else}
    <ul class="divide-y divide-slate-800 border border-slate-800 rounded">
      {#each tasks.list as t (t.task_id)}
        <li>
          <button
            type="button"
            onclick={() => navigate(`/tasks/${t.task_id}`)}
            class="w-full text-left px-4 py-3 hover:bg-slate-900 flex items-center gap-4"
          >
            <div class="min-w-0 flex-1">
              <div class="text-sm text-slate-100 truncate">
                {t.description?.trim() || '(no description)'}
              </div>
              <div class="mt-0.5 flex items-center gap-3 text-xs text-slate-500 font-mono">
                <span>{shortId(t.task_id, 8)}</span>
                <span>·</span>
                <span class="truncate">{t.model ?? '—'}</span>
                {#if t.skill}<span>·</span><span class="truncate">{t.skill}</span>{/if}
              </div>
            </div>
            <div class="flex items-center gap-4 text-xs text-slate-400 shrink-0">
              {#if t.started_at}
                <span class="text-slate-500">{ago(t.started_at)}</span>
              {/if}
              <span class="font-mono">{fmtMoney(t.total_cost_usd ?? 0)}</span>
              {#if (t.live_agent_count ?? 0) > 0}
                <span class="inline-flex items-center gap-1 text-emerald-300">
                  <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
                  {t.live_agent_count} live
                </span>
              {/if}
            </div>
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</div>
