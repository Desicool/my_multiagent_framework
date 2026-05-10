<script lang="ts">
  import type { AgentState, ToolStreamItem } from '../../lib/types';
  import type { PlanTaskSpec } from '../../lib/timeline';
  import PCardShell from './PCardShell.svelte';
  import AgentChip from './AgentChip.svelte';
  import PlanDAG from './PlanDAG.svelte';

  let { item, callerAgent }: { item: ToolStreamItem; callerAgent?: AgentState } = $props();

  let tasks = $derived.by<PlanTaskSpec[]>(() => {
    const t = item.input?.tasks ?? item.input?.plan;
    if (!Array.isArray(t)) return [];
    return (t as Array<Record<string, unknown>>).map((raw) => ({
      task_id: ((raw.id ?? raw.task_id) as string | undefined) ?? '',
      role: raw.role as string | undefined,
      skill: raw.skill as string | undefined,
      depends_on: (raw.depends_on as string[] | undefined) ?? [],
    }));
  });
  let callerId = $derived(callerAgent?.agent_id ?? '');
</script>

<PCardShell
  category="plan"
  categoryLabel="Plan"
  primitiveName="declare_plan"
  {item}
>
  {#snippet body()}
    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">caller</div>
    <div><AgentChip agentId={callerId} agent={callerAgent} /></div>

    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">DAG</div>
    <div>
      <PlanDAG {tasks} />
    </div>

    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">tasks</div>
    <div class="font-mono text-[11.5px] text-slate-400">{tasks.length} declared</div>
  {/snippet}
</PCardShell>
