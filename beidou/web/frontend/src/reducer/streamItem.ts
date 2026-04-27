import type {
  StreamItem,
  TextStreamItem,
  ToolStreamItem,
  MessageInStreamItem,
  MessageOutStreamItem,
  TurnDividerStreamItem,
} from '../lib/types';

export type {
  StreamItem,
  TextStreamItem,
  ToolStreamItem,
  MessageInStreamItem,
  MessageOutStreamItem,
  TurnDividerStreamItem,
};

export function makeText(ev: { ts: number; text: string; message_id?: string; stop_reason?: string }): TextStreamItem {
  return { kind: 'text', ts: ev.ts, text: ev.text, message_id: ev.message_id, stop_reason: ev.stop_reason };
}

export function makeToolPending(ev: { ts: number; tool_use_id: string; name: string; input?: Record<string, unknown> }): ToolStreamItem {
  return { kind: 'tool', ts: ev.ts, tool_use_id: ev.tool_use_id, name: ev.name, input: ev.input, duration_ms: null, is_error: null, expanded: false };
}

export function makeMessageIn(ev: { ts: number; from: string; from_is_user: boolean; from_is_system?: boolean; is_initial?: boolean; content: string; message_id: string }): MessageInStreamItem {
  return { kind: 'message_in', ts: ev.ts, from: ev.from, from_is_user: ev.from_is_user, from_is_system: ev.from_is_system, is_initial: ev.is_initial, content: ev.content, message_id: ev.message_id };
}

export function makeMessageOut(ev: { ts: number; to: string; content: string; message_id: string }): MessageOutStreamItem {
  return { kind: 'message_out', ts: ev.ts, to: ev.to, content: ev.content, message_id: ev.message_id };
}

export function makeTurnDivider(ev: { ts: number; in_tok?: number; out_tok?: number; stop_reason?: string; model?: string }): TurnDividerStreamItem {
  return { kind: 'turn_divider', ts: ev.ts, in_tok: ev.in_tok ?? 0, out_tok: ev.out_tok ?? 0, stop_reason: ev.stop_reason, model: ev.model };
}
