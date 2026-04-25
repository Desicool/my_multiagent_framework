<script lang="ts">
  import TaskOverview from './TaskOverview.svelte';
  import QuestionCountChip from './QuestionCountChip.svelte';
  import GlobalActivityFeed from './GlobalActivityFeed.svelte';
</script>

<div class="h-full overflow-y-auto pb-6">
  <TaskOverview />
  <div class="px-3"><QuestionCountChip /></div>
  <GlobalActivityFeed />
</div>
