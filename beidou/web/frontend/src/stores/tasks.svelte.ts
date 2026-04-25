import { getTasks, type TaskListItem } from '../lib/api';

const tasks = $state({
  list: [] as TaskListItem[],
  lastFetched: 0,
  error: null as string | null,
});

let pollTimer: number | null = null;

async function fetchOnce(): Promise<void> {
  try {
    tasks.list = await getTasks();
    tasks.error = null;
    tasks.lastFetched = Date.now();
  } catch (e) {
    tasks.error = (e as Error).message;
  }
}

export function startPolling(): void {
  if (pollTimer !== null) return;
  fetchOnce();
  pollTimer = window.setInterval(fetchOnce, 3000);
}

export function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

export { tasks };
