<script lang="ts">
  import TaskOverviewPanel from '../left/TaskOverviewPanel.svelte';
  import PinnedAgentPanel from '../middle/PinnedAgentPanel.svelte';
  import TeamTreePanel from '../right/TeamTreePanel.svelte';
</script>

<div class="grid h-full grid-cols-[280px_minmax(0,1fr)_320px] gap-px bg-slate-800 overflow-hidden">
  <aside class="bg-slate-950 overflow-y-auto"><TaskOverviewPanel /></aside>
  <main class="bg-slate-950 overflow-hidden flex flex-col"><PinnedAgentPanel /></main>
  <aside class="bg-slate-950 overflow-y-auto"><TeamTreePanel /></aside>
</div>
