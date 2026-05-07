type SendStatus = 'idle' | 'sending' | 'sent' | 'error';

const ui = $state({
  pinnedAgentId: null as string | null,
  expandedTeams: new Set<string>(),
  agentDrafts: {} as Record<string, string>,
  agentSendStatus: {} as Record<string, SendStatus>,
  agentSendError: {} as Record<string, string | null>,
  /** When true, the keyboard-shortcut palette overlay (`?`) is showing. */
  shortcutOverlayOpen: false,
});

export function toggleShortcutOverlay(): void {
  ui.shortcutOverlayOpen = !ui.shortcutOverlayOpen;
}

export function closeShortcutOverlay(): void {
  ui.shortcutOverlayOpen = false;
}

export function pinAgent(agent_id: string | null): void {
  ui.pinnedAgentId = agent_id;
}

export function toggleTeam(team_id: string): void {
  if (ui.expandedTeams.has(team_id)) ui.expandedTeams.delete(team_id);
  else ui.expandedTeams.add(team_id);
  // Re-create the Set so Svelte rune reactivity picks up the change.
  ui.expandedTeams = new Set(ui.expandedTeams);
}

export function setExpanded(team_id: string, open: boolean): void {
  if (open) ui.expandedTeams.add(team_id);
  else ui.expandedTeams.delete(team_id);
  ui.expandedTeams = new Set(ui.expandedTeams);
}

export function setDraft(agent_id: string, text: string): void {
  ui.agentDrafts[agent_id] = text;
}

export function setSendStatus(agent_id: string, status: SendStatus, error: string | null = null): void {
  ui.agentSendStatus[agent_id] = status;
  ui.agentSendError[agent_id] = error;
}

export { ui };
