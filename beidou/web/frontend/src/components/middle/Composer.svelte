<script lang="ts">
  import type { AgentState } from '../../lib/types';
  import { ui, setDraft, setSendStatus } from '../../stores/ui.svelte';
  import { sendMessage } from '../../lib/api';
  import { shortId } from '../../lib/format';

  let { agent }: { agent: AgentState } = $props();

  let draft = $derived(ui.agentDrafts[agent.agent_id] ?? '');
  let status = $derived(ui.agentSendStatus[agent.agent_id] ?? 'idle');
  let error = $derived(ui.agentSendError[agent.agent_id] ?? null);

  async function onSend(): Promise<void> {
    const text = draft.trim();
    if (!text) return;
    setSendStatus(agent.agent_id, 'sending');
    try {
      await sendMessage(agent.agent_id, text);
      setDraft(agent.agent_id, '');
      setSendStatus(agent.agent_id, 'sent');
      setTimeout(() => {
        if (ui.agentSendStatus[agent.agent_id] === 'sent') setSendStatus(agent.agent_id, 'idle');
      }, 1500);
    } catch (e) {
      setSendStatus(agent.agent_id, 'error', (e as Error).message);
    }
  }

  function onKey(ev: KeyboardEvent): void {
    if ((ev.metaKey || ev.ctrlKey) && ev.key === 'Enter') {
      ev.preventDefault();
      onSend();
    }
  }

  function onInput(ev: Event): void {
    setDraft(agent.agent_id, (ev.target as HTMLTextAreaElement).value);
  }
</script>

<div class="border-t border-slate-800 bg-slate-950/95 px-4 py-3 sticky bottom-0">
  <div class="flex items-end gap-2">
    <textarea
      data-composer-textarea
      class="flex-1 rounded bg-slate-900 border border-slate-700 p-2 text-sm text-slate-100 focus:outline-none focus:border-emerald-500 resize-y"
      rows="2"
      placeholder={`Message ${agent.name ?? agent.role ?? shortId(agent.agent_id, 4)} (Cmd/Ctrl+Enter to send)`}
      value={draft}
      oninput={onInput}
      onkeydown={onKey}
    ></textarea>
    <button
      type="button"
      class="px-3 py-2 rounded bg-emerald-500 hover:bg-emerald-400 text-emerald-950 text-sm font-semibold disabled:opacity-50"
      onclick={onSend}
      disabled={status === 'sending' || !draft.trim()}
    >
      {status === 'sending' ? 'sending…' : 'Send'}
    </button>
  </div>
  <div class="mt-1 h-4 text-xs">
    {#if status === 'sent'}
      <span class="text-emerald-400">sent</span>
    {:else if status === 'error'}
      <span class="text-rose-400">error: {error}</span>
    {/if}
  </div>
</div>
