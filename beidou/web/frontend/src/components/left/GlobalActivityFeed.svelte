<script lang="ts">
  import { events } from '../../stores/events.svelte';
  import { hhmmss } from '../../lib/time';
  import { shortId } from '../../lib/format';

  const dotColor: Record<string, string> = {
    turn: 'bg-sky-400',
    tool_start: 'bg-amber-400',
    tool_error: 'bg-rose-400',
    team_created: 'bg-violet-400',
    agent_started: 'bg-emerald-400',
    agent_completed: 'bg-slate-400',
    agent_error: 'bg-rose-500',
    contract_violation: 'bg-rose-500',
    run_cost: 'bg-amber-300',
    config_warning: 'bg-amber-300',
  };

  // Render newest-first, last 80
  let visible = $derived.by(() =>
    events.globalActivity.slice(-80).reverse()
  );
</script>

<div class="mt-4 px-3">
  <h3 class="text-xs uppercase tracking-wider text-slate-500 mb-2">Activity</h3>
  <div class="overflow-y-auto max-h-[calc(100vh-22rem)] pr-1 space-y-1">
    {#each visible as item}
      <div class="flex items-start gap-2 text-xs leading-tight">
        <span class={`mt-1 w-1.5 h-1.5 rounded-full shrink-0 ${dotColor[item.kind] ?? 'bg-slate-500'}`}></span>
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-1 text-slate-500 font-mono">
            <span>{hhmmss(item.ts)}</span>
            {#if item.agent_id}
              {#if events.agentsById[item.agent_id]?.role}
                <span class="text-slate-400">[{events.agentsById[item.agent_id].role} · {shortId(item.agent_id, 4)}]</span>
              {:else}
                <span class="text-slate-400">[{shortId(item.agent_id, 6)}]</span>
              {/if}
            {/if}
          </div>
          <div class="text-slate-300 truncate">{item.label}</div>
        </div>
      </div>
    {:else}
      <p class="text-xs text-slate-600 italic">No activity yet…</p>
    {/each}
  </div>
</div>
