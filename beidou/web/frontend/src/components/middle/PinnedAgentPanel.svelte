<script lang="ts">
  import { events } from '../../stores/events.svelte';
  import { ui } from '../../stores/ui.svelte';
  import AgentHeader from './AgentHeader.svelte';
  import QuestionBanner from './QuestionBanner.svelte';
  import AgentStream from './AgentStream.svelte';
  import Composer from './Composer.svelte';

  let agent = $derived(ui.pinnedAgentId ? events.agentsById[ui.pinnedAgentId] : null);
</script>

<div class="flex flex-col h-full">
  <AgentHeader />
  <QuestionBanner />
  {#if agent}
    <AgentStream {agent} />
    <Composer {agent} />
  {:else}
    <div class="flex-1 flex items-center justify-center text-slate-600 text-sm">
      Select an agent in the team tree to begin.
    </div>
  {/if}
</div>
