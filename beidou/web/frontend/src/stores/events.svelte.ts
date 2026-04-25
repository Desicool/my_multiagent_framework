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

export function applyEvent(ev: BeidouEvent): void {
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
}

export { events };
