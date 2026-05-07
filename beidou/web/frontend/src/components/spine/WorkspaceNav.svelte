<script lang="ts">
  import { route, navigate, type Workspace } from '../../lib/router.svelte';

  let { taskId }: { taskId: string } = $props();

  type Item = { id: Workspace; label: string; glyph: string; disabled?: boolean; badge?: string };

  const items: Item[] = [
    { id: 'agents',    label: 'Agents',   glyph: '▤' },
    { id: 'timeline',  label: 'Timeline', glyph: '≡' },
    { id: 'tools',     label: 'Tools',    glyph: '▦' },
    { id: 'stats',     label: 'Stats',    glyph: '◔', disabled: true, badge: 'P1' },
    { id: 'overview',  label: 'Overview', glyph: '○', disabled: true, badge: 'P1' },
    { id: 'questions', label: 'Q&A',      glyph: '○', disabled: true, badge: 'P1' },
  ];

  let current = $derived(
    route.current.name === 'task_workspace' ? route.current.workspace : 'agents'
  );

  function go(id: Workspace): void {
    navigate(`/tasks/${taskId}/${id}`);
  }
</script>

<div>
  <div class="px-4 pb-1.5">
    <span class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Workspace</span>
  </div>
  <div class="flex flex-col">
    {#each items as ws}
      <button
        type="button"
        disabled={ws.disabled}
        onclick={() => !ws.disabled && go(ws.id)}
        class="w-full text-left flex items-center gap-2 px-3 py-1.5 text-xs transition-colors border-l-2
          {ws.disabled
            ? 'text-slate-600 cursor-not-allowed opacity-50 border-transparent'
            : current === ws.id
              ? 'bg-cyan-950/40 text-slate-100 border-review'
              : 'text-slate-400 hover:text-slate-100 hover:bg-slate-800/30 border-transparent cursor-pointer'}"
        title={ws.disabled ? `${ws.label} — ${ws.badge}` : ws.label}
      >
        <span class="shrink-0 w-3 text-center {current === ws.id ? 'text-review' : 'text-slate-600'}">{ws.glyph}</span>
        <span class="flex-1">{ws.label}</span>
        {#if ws.badge}
          <span class="font-mono text-[9px] text-slate-600">·{ws.badge}</span>
        {/if}
      </button>
    {/each}
  </div>
</div>
