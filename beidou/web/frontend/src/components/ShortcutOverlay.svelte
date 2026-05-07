<script lang="ts">
  import { ui, closeShortcutOverlay } from '../stores/ui.svelte';

  function onBackdrop(e: MouseEvent): void {
    if (e.target === e.currentTarget) closeShortcutOverlay();
  }

  type Row = { keys: string; desc: string };
  const rows: { group: string; items: Row[] }[] = [
    {
      group: 'Workspaces',
      items: [
        { keys: '1', desc: 'Open /agents (3-pane view)' },
        { keys: '2', desc: 'Open /timeline (plan-task tree)' },
        { keys: '3', desc: 'Open /tools (primitive call explorer)' },
        { keys: '0 / Esc', desc: 'Return home (task list) / close this overlay' },
      ],
    },
    {
      group: 'Help & export',
      items: [
        { keys: '?', desc: 'Toggle this shortcut overlay' },
        { keys: 'e', desc: 'Export current task as JSON' },
      ],
    },
    {
      group: 'Reading',
      items: [
        { keys: 'click event-row', desc: 'Pin the actor agent in /agents stream' },
        { keys: 'click filter pill', desc: 'Filter rows by category' },
      ],
    },
  ];
</script>

{#if ui.shortcutOverlayOpen}
  <div
    role="dialog"
    aria-modal="true"
    aria-label="Keyboard shortcuts"
    tabindex="-1"
    class="fixed inset-0 z-50 bg-slate-950/70 backdrop-blur-sm flex items-center justify-center p-6"
    onclick={onBackdrop}
    onkeydown={(e) => { if (e.key === 'Escape') closeShortcutOverlay(); }}
  >
    <div class="bg-surface-raised border border-surface-border rounded-md shadow-xl max-w-lg w-full p-5">
      <div class="flex items-baseline justify-between mb-4">
        <h2 class="text-sm font-semibold text-slate-100 tracking-tight">Keyboard shortcuts</h2>
        <button
          type="button"
          onclick={closeShortcutOverlay}
          class="text-[11px] text-muted hover:text-slate-200 cursor-pointer font-mono"
          aria-label="Close"
        >
          esc
        </button>
      </div>
      <div class="flex flex-col gap-4">
        {#each rows as g (g.group)}
          <div>
            <div class="text-[10px] uppercase tracking-widest text-muted font-semibold mb-1.5">{g.group}</div>
            <div class="flex flex-col gap-1">
              {#each g.items as r (r.keys)}
                <div class="grid grid-cols-[120px_1fr] gap-3 items-center text-[12px]">
                  <span class="font-mono text-slate-100 bg-surface border border-surface-border px-2 py-0.5 rounded text-[11px] text-center">{r.keys}</span>
                  <span class="text-slate-300">{r.desc}</span>
                </div>
              {/each}
            </div>
          </div>
        {/each}
      </div>
    </div>
  </div>
{/if}
