<script lang="ts">
  import type { AgentState, ToolStreamItem } from '../../lib/types';
  import PCardShell from './PCardShell.svelte';
  import FlowStrip from './FlowStrip.svelte';
  import { events } from '../../stores/events.svelte';

  let { item, callerAgent }: { item: ToolStreamItem; callerAgent?: AgentState } = $props();

  let role = $derived((item.input?.role as string) ?? '');
  let skill = $derived((item.input?.skill as string) ?? '');
  let model = $derived((item.input?.model as string) ?? '');
  let taskId = $derived((item.input?.task_id as string) ?? '');
  let initialMessage = $derived((item.input?.initial_message as string) ?? '');
  let callerId = $derived(callerAgent?.agent_id ?? '');

  // Best-effort: find an agent spawned shortly after this call with the same role.
  // This is heuristic — we look for any agent in events whose started_at is between
  // the spawn ts and ts+5s and whose role matches.
  let spawnedAgent = $derived.by(() => {
    if (!role) return undefined;
    const candidates: AgentState[] = [];
    for (const a of Object.values(events.agentsById) as AgentState[]) {
      if (a.role === role && a.started_at && a.started_at >= item.ts && a.started_at <= item.ts + 5) {
        candidates.push(a);
      }
    }
    candidates.sort((x, y) => (x.started_at ?? 0) - (y.started_at ?? 0));
    return candidates[0];
  });
  let spawnedId = $derived(spawnedAgent?.agent_id ?? '');
</script>

<PCardShell
  category="lifecycle"
  categoryLabel="Lifecycle"
  primitiveName="spawn_agent"
  {item}
>
  {#snippet flow()}
    <FlowStrip
      fromId={callerId}
      fromAgent={callerAgent}
      fromLabel="Caller (parent)"
      toId={spawnedId || '(pending)'}
      toAgent={spawnedAgent}
      toLabel="Spawned (new agent)"
      variant="spawn"
      arrow="+⟶"
    />
  {/snippet}

  {#snippet body()}
    {#if role}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">role</div>
      <div class="font-mono text-[11.5px] text-slate-200">{role}</div>
    {/if}
    {#if skill}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">skill</div>
      <div class="font-mono text-[11.5px] text-slate-200">{skill}</div>
    {/if}
    {#if model}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">model</div>
      <div class="font-mono text-[11.5px] text-slate-400">{model}</div>
    {/if}
    {#if taskId}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">task_id</div>
      <div class="font-mono text-[11.5px] text-slate-400">{taskId}</div>
    {/if}
    {#if initialMessage}
      <div class="text-[11px] uppercase tracking-wider text-muted font-medium pt-px">initial</div>
      <div class="text-slate-300 text-[11.5px] line-clamp-2">{initialMessage.slice(0, 200)}{initialMessage.length > 200 ? '…' : ''}</div>
    {/if}
  {/snippet}
</PCardShell>
