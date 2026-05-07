<script lang="ts">
  import type { AgentState, ToolStreamItem } from '../../lib/types';
  import PCardShell from './PCardShell.svelte';
  import AgentChip from './AgentChip.svelte';

  type PlanTask = {
    task_id?: string;
    role?: string;
    skill?: string;
    depends_on?: string[];
    description?: string;
  };

  let { item, callerAgent }: { item: ToolStreamItem; callerAgent?: AgentState } = $props();

  let tasks = $derived.by<PlanTask[]>(() => {
    const t = item.input?.tasks ?? item.input?.plan;
    if (Array.isArray(t)) return t as PlanTask[];
    return [];
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
      {#if tasks.length === 0}
        <div class="text-slate-500 italic text-[11.5px]">(no tasks)</div>
      {:else}
        <div class="flex flex-col gap-1 py-1">
          {#each tasks as task, i (task.task_id ?? i)}
            <div class="flex items-center gap-2 px-2.5 py-1.5 bg-surface border border-surface-border rounded text-[11.5px]">
              <span class="w-1.5 h-1.5 rounded-full bg-info shrink-0"></span>
              <span class="font-mono text-slate-100 font-medium">{task.task_id ?? `task_${i}`}</span>
              {#if task.role}
                <span class="text-slate-400 text-[11px]">· {task.role}</span>
              {/if}
              {#if task.depends_on && task.depends_on.length}
                <span class="ml-auto font-mono text-[10.5px] text-muted">depends_on: [{task.depends_on.join(', ')}]</span>
              {:else}
                <span class="ml-auto font-mono text-[10.5px] text-muted">no deps</span>
              {/if}
            </div>
            {#if i < tasks.length - 1}
              <div class="text-muted font-mono text-[11px] pl-3.5">↓</div>
            {/if}
          {/each}
        </div>
      {/if}
    </div>

    <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">tasks</div>
    <div class="font-mono text-[11.5px] text-slate-400">{tasks.length} declared</div>
  {/snippet}
</PCardShell>
