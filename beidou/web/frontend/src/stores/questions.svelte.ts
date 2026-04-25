import type { PendingQuestion } from '../lib/types';
import { getPendingQuestions } from '../lib/api';

const questions = $state({
  list: [] as PendingQuestion[],
  lastFetched: 0,
  error: null as string | null,
});

let pollTimer: number | null = null;
let inflight = false;

async function fetchOnce(): Promise<void> {
  if (inflight) return;
  inflight = true;
  try {
    questions.list = await getPendingQuestions();
    questions.error = null;
    questions.lastFetched = Date.now();
  } catch (e) {
    questions.error = (e as Error).message;
  } finally {
    inflight = false;
  }
}

export function startPolling(): void {
  if (pollTimer !== null) return;
  fetchOnce();
  pollTimer = window.setInterval(fetchOnce, 5000);
}

export function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

/** Called by ws.ts on question_asked / question_answered events to refresh now. */
export function triggerRepoll(): void {
  fetchOnce();
}

export { questions };
