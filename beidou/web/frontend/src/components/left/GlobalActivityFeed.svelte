<script lang="ts">
  import { events } from '../../stores/events.svelte';
  import { hhmmss } from '../../lib/time';
  import { shortId } from '../../lib/format';

  // Semantic dot colors using the new token set
  const dotColor: Record<string, string> = {
    turn:               'bg-info',
    tool_start:         'bg-pending',
    tool_error:         'bg-error',
    team_created:       'bg-accent',
    agent_started:      'bg-success',
    agent_completed:    'bg-muted',
    agent_error:        'bg-error',
    contract_violation: 'bg-error',
    run_cost:           'bg-pending/70',
    config_warning:     'bg-pending/70',
  };

  // Render newest-first, last 80
  let visible = $derived.by(() =>
    events.globalActivity.slice(-80).reverse()
  );
</script>

<div class="mt-3 px-3">
  <h3 class="text-[10px] uppercase tracking-wider text-muted mb-2 px-1">Activity</h3>
  <div class="overflow-y-auto max-h-[calc(100vh-18rem)] pr-1">
    {#each visible as item}
      <div class="flex items-start gap-2 py-1 border-b border-surface-border/60">
        <!-- Event type dot -->
        <span class={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${dotColor[item.kind] ?? 'bg-muted'}`}></span>
        <div class="min-w-0 flex-1">
          <!-- Meta row: time + agent chip -->
          <div class="flex items-center gap-1 mb-0.5">
            <span class="text-xs text-muted font-mono">{hhmmss(item.ts)}</span>
            {#if item.agent_id}
              {#if events.agentsById[item.agent_id]?.name || events.agentsById[item.agent_id]?.role}
                <span class="inline-flex items-center rounded bg-surface-raised px-1.5 py-px text-xs text-slate-300 border border-surface-border/80">
                  {events.agentsById[item.agent_id]?.name ?? events.agentsById[item.agent_id]?.role}
                </span>
              {:else}
                <span class="text-xs text-muted font-mono">[{shortId(item.agent_id, 6)}]</span>
              {/if}
            {/if}
          </div>
          <!-- Activity label -->
          <div class="text-xs text-slate-300 line-clamp-2 break-words leading-snug">{item.label}</div>
        </div>
      </div>
    {:else}
      <p class="text-xs text-muted italic px-1">No activity yet…</p>
    {/each}
  </div>
</div>
