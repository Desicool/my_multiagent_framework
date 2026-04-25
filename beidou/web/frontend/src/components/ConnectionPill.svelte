<script lang="ts">
  import { conn } from '../stores/connection.svelte';

  const labels: Record<string, string> = {
    connecting: 'connecting…',
    replaying: 'replaying',
    live: 'live',
    polling: 'polling',
    disconnected: 'disconnected',
  };

  const colorClass: Record<string, string> = {
    connecting: 'bg-amber-500/20 text-amber-200 border-amber-500/40',
    replaying: 'bg-sky-500/20 text-sky-200 border-sky-500/40',
    live: 'bg-emerald-500/20 text-emerald-200 border-emerald-500/40',
    polling: 'bg-amber-500/20 text-amber-200 border-amber-500/40',
    disconnected: 'bg-rose-500/20 text-rose-200 border-rose-500/40',
  };

  let dot = $derived.by(() =>
    conn.status === 'live' ? 'bg-emerald-400 animate-pulse' :
    conn.status === 'disconnected' ? 'bg-rose-400' :
    conn.status === 'polling' ? 'bg-amber-400 animate-pulse' :
    'bg-sky-400 animate-pulse'
  );
</script>

<span class={`inline-flex items-center gap-1.5 px-2 py-0.5 text-xs rounded border ${colorClass[conn.status] ?? colorClass.connecting}`}>
  <span class={`w-1.5 h-1.5 rounded-full ${dot}`}></span>
  {labels[conn.status] ?? conn.status}
</span>
