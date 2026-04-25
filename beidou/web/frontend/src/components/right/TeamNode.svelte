<script lang="ts">
  import { events } from '../../stores/events.svelte';
  import { ui, toggleTeam } from '../../stores/ui.svelte';
  import { shortId } from '../../lib/format';
  import AgentRow from './AgentRow.svelte';
  import TeamNode from './TeamNode.svelte';
  import { onMount } from 'svelte';
  import type { TeamState } from '../../lib/types';

  let { team, depth }: { team: TeamState; depth: number } = $props();

  // Default expanded for the first depth level
  onMount(() => {
    if (depth <= 0) {
      // setExpanded would mutate; just call toggleTeam if not already in set.
      if (!ui.expandedTeams.has(team.team_id)) {
        toggleTeam(team.team_id);
      }
    }
  });

  let expanded = $derived(ui.expandedTeams.has(team.team_id));

  // Children sorted by created_at.
  let memberAgents = $derived.by(() =>
    Object.values(events.agentsById)
      .filter((a) => a.team_id === team.team_id)
      .sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0))
  );
  let childTeams = $derived.by(() =>
    Object.values(events.teamsById)
      .filter((t) => t.parent_team_id === team.team_id)
      .sort((a, b) => (a.created_at ?? 0) - (b.created_at ?? 0))
  );

  function onClick(): void { toggleTeam(team.team_id); }

  let indent = $derived(`padding-left: ${depth * 0.75}rem`);
</script>

<div class="text-sm">
  <button
    type="button"
    class="w-full flex items-center gap-1 px-1 py-1 hover:bg-slate-900 rounded text-left"
    style={indent}
    onclick={onClick}
  >
    <span class="text-slate-500 w-3 inline-block text-xs">{expanded ? '▾' : '▸'}</span>
    <span class="text-violet-300 truncate">{team.name ?? shortId(team.team_id, 8)}</span>
    <span class="ml-auto text-[10px] text-slate-600 font-mono">{memberAgents.length}</span>
  </button>

  {#if expanded}
    {#each memberAgents as a (a.agent_id)}
      <AgentRow agent={a} depth={depth + 1} />
    {/each}
    {#each childTeams as ct (ct.team_id)}
      <TeamNode team={ct} depth={depth + 1} />
    {/each}
  {/if}
</div>
