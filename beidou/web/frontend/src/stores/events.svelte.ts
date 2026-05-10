// WIRING: Phase 4 App.svelte / a service module should call:
//   import { applyEvent } from './events.svelte';
//   import { triggerRepoll } from './questions.svelte';
//   // In ws.ts onEvent callback:
//   applyEvent(ev);  // routes ALL events through the reducer
//   if (ev.type === 'question_asked' || ev.type === 'question_answered') {
//     triggerRepoll();  // immediate re-poll for rich question data
//   }

import type { BeidouEvent } from '../lib/types';
import { applyEvent as reducerApply } from '../reducer/reduce';
import { createInitialState, type ReducerState } from '../reducer/state';

const events = $state<ReducerState>(createInitialState());

export function getEvents(): ReducerState {
  return events;
}

function normalizeEvent(ev: BeidouEvent): void {
  const raw = ev as Record<string, unknown>;

  if (!raw.type && raw.event) raw.type = raw.event;

  if (!raw.caller_id && raw.agent_id) raw.caller_id = raw.agent_id;

  if ((raw.type ?? raw.event) === 'turn.usage') {
    if (raw.in_tok === undefined && raw.input_tokens !== undefined) raw.in_tok = raw.input_tokens;
    if (raw.out_tok === undefined && raw.output_tokens !== undefined) raw.out_tok = raw.output_tokens;
    if (raw.cache_read === undefined && raw.cache_read_input_tokens !== undefined) raw.cache_read = raw.cache_read_input_tokens;
    if (raw.cache_create === undefined && raw.cache_creation_input_tokens !== undefined) raw.cache_create = raw.cache_creation_input_tokens;
  }

  if ((raw.type ?? raw.event) === 'agent_error') {
    if (!raw.error && raw.exception) raw.error = String(raw.exception);
    if (!raw.error && raw.msg) raw.error = String(raw.msg);
  }

  if ((raw.type ?? raw.event) === 'config_warning') {
    if (!raw.message && raw.warning) raw.message = raw.warning;
  }

  if ((raw.type ?? raw.event) === 'agent_started') {
    if (!raw.model && raw.model_requested) raw.model = raw.model_requested;
  }

  if ((raw.type ?? raw.event) === 'contract_violation') {
    if (!raw.reason && raw.stop_reason) raw.reason = raw.stop_reason;
  }

  if ((raw.type ?? raw.event) === 'team_created') {
    if (!raw.team_id && raw.new_team_id) raw.team_id = raw.new_team_id;
    if (!raw.name && raw.team_name) raw.name = raw.team_name;
  }
}

export function applyEvent(ev: BeidouEvent): void {
  normalizeEvent(ev);
  reducerApply(events, ev);
  if ((ev as { ts?: number }).ts && (ev as { ts: number }).ts > events.cursor) {
    events.cursor = (ev as { ts: number }).ts;
  }
}

export function bumpCursor(c: number): void {
  if (c > events.cursor) events.cursor = c;
}

export function resetEvents(): void {
  // Replace fields in place so consumers' references stay valid.
  Object.assign(events, createInitialState());
  events.agentsById = {};
  events.teamsById = {};
  events.globalActivity = [];
  events.timelineEvents = [];
}

export { events };
