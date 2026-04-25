<script lang="ts">
  import { events } from '../../stores/events.svelte';
  import TeamNode from './TeamNode.svelte';
  import AgentRow from './AgentRow.svelte';

  // Top-level teams (parent_team_id == null) sorted by created_at.
  let topTeams = $derived.by(() => {
    return Object.values(events.teamsById)
      .filter((t) => !t.parent_team_id)
      .sort((a, b) => (a.created_at ?? 0) - (b.created_at ?? 0));
  });

  // Agents with no team — typically the root agent.
  let rootAgents = $derived.by(() => {
    return Object.values(events.agentsById)
      .filter((a) => !a.team_id)
      .sort((a, b) => (a.started_at ?? 0) - (b.started_at ?? 0));
  });
</script>

<div class="px-3 py-3">
  <h2 class="text-xs uppercase tracking-wider text-slate-500 mb-3 px-1">Team tree</h2>

  {#if rootAgents.length === 0 && topTeams.length === 0}
    <p class="text-xs text-slate-600 italic px-1">No agents yet…</p>
  {:else}
    {#each rootAgents as a (a.agent_id)}
      <AgentRow agent={a} depth={0} />
    {/each}
    {#each topTeams as t (t.team_id)}
      <TeamNode team={t} depth={0} />
    {/each}
  {/if}
</div>
