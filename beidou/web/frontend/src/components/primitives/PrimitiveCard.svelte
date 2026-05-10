<script lang="ts">
  import type { AgentState, ToolStreamItem } from '../../lib/types';
  import { bareToolName } from '../../lib/primitives';

  import SendMessageCard from './SendMessageCard.svelte';
  import ListPeersCard from './ListPeersCard.svelte';
  import SpawnAgentCard from './SpawnAgentCard.svelte';
  import TerminateChildCard from './TerminateChildCard.svelte';
  import CreateTeamCard from './CreateTeamCard.svelte';
  import DeclarePlanCard from './DeclarePlanCard.svelte';
  import RemovePlanCard from './RemovePlanCard.svelte';
  import ListReadyCard from './ListReadyCard.svelte';
  import SignalReviewCard from './SignalReviewCard.svelte';
  import RequestTerminationCard from './RequestTerminationCard.svelte';
  import ReportStatusCard from './ReportStatusCard.svelte';
  import ListPendingReviewsCard from './ListPendingReviewsCard.svelte';
  import AskUserCard from './AskUserCard.svelte';

  let { item, callerAgent }: { item: ToolStreamItem; callerAgent?: AgentState } = $props();

  let bare = $derived(bareToolName(item.name));
</script>

{#if bare === 'send_message'}
  <SendMessageCard {item} {callerAgent} />
{:else if bare === 'list_peers'}
  <ListPeersCard {item} {callerAgent} />
{:else if bare === 'spawn_agent'}
  <SpawnAgentCard {item} {callerAgent} />
{:else if bare === 'terminate_child'}
  <TerminateChildCard {item} {callerAgent} />
{:else if bare === 'create_team'}
  <CreateTeamCard {item} {callerAgent} />
{:else if bare === 'declare_plan'}
  <DeclarePlanCard {item} {callerAgent} />
{:else if bare === 'remove_plan'}
  <RemovePlanCard {item} {callerAgent} />
{:else if bare === 'list_ready'}
  <ListReadyCard {item} {callerAgent} />
{:else if bare === 'signal_review'}
  <SignalReviewCard {item} {callerAgent} />
{:else if bare === 'request_termination'}
  <RequestTerminationCard {item} {callerAgent} />
{:else if bare === 'report_status'}
  <ReportStatusCard {item} {callerAgent} />
{:else if bare === 'list_pending_reviews'}
  <ListPendingReviewsCard {item} {callerAgent} />
{:else if bare === 'ask_user'}
  <AskUserCard {item} {callerAgent} />
{/if}
