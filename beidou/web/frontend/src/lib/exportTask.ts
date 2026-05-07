/**
 * Export the current task's reducer state as a JSON file.
 *
 * Captures everything the UI has materialised for the task: task record,
 * agent state (without internal _stream/_pendingTools — those are reducer
 * scaffolding, not data the user wants to share), team state, the flat
 * timeline-event log, and aggregate stats. The file is downloaded via a
 * Blob URL so no network round-trip / endpoint is needed.
 */

import type { ReducerState, GlobalActivityItem } from '../reducer/state';
import type { AgentState, TeamState, TimelineEvent } from './types';

type ExportShape = {
  schema: 'beidou-task-export/v1';
  exported_at: string;
  task: ReducerState['task'];
  rootAgentId: string | null;
  stats: ReducerState['stats'];
  agents: Array<Omit<AgentState, '_stream' | '_pendingTools' | '_seenTurn' | '_seenTool'>>;
  teams: TeamState[];
  globalActivity: GlobalActivityItem[];
  timelineEvents: TimelineEvent[];
};

export function buildTaskExport(state: ReducerState): ExportShape {
  const agents = Object.values(state.agentsById).map((a) => {
    // Drop the reactive scaffolding; keep human-meaningful fields.
    const { _stream, _pendingTools, _seenTurn, _seenTool, ...rest } = a;
    return rest;
  });
  return {
    schema: 'beidou-task-export/v1',
    exported_at: new Date().toISOString(),
    task: state.task,
    rootAgentId: state.rootAgentId,
    stats: state.stats,
    agents,
    teams: Object.values(state.teamsById),
    globalActivity: [...state.globalActivity],
    timelineEvents: [...state.timelineEvents],
  };
}

/** Serialise + trigger a browser download. */
export function downloadTaskJSON(state: ReducerState, filename?: string): void {
  const data = buildTaskExport(state);
  const json = JSON.stringify(data, null, 2);
  const blob = new Blob([json], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const taskId = state.task?.task_id ?? 'unknown';
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename ?? `beidou-${taskId}-${stamp}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Release the blob URL; revoke async so the download has a chance to start.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
