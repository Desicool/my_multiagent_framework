type Route =
  | { name: 'home' }
  | { name: 'task'; taskId: string }
  | { name: 'agent'; agentId: string }
  | { name: 'unknown'; raw: string };

function parseHash(hash: string): Route {
  const h = hash.replace(/^#/, '').replace(/^\//, '');
  if (h === '' || h === '/') return { name: 'home' };
  const m1 = h.match(/^tasks\/([^/]+)\/?$/);
  if (m1) return { name: 'task', taskId: decodeURIComponent(m1[1]) };
  const m2 = h.match(/^agents\/([^/]+)\/?$/);
  if (m2) return { name: 'agent', agentId: decodeURIComponent(m2[1]) };
  return { name: 'unknown', raw: h };
}

const route = $state<{ current: Route }>({ current: parseHash(typeof location !== 'undefined' ? location.hash : '') });

export function startRouter(): () => void {
  const handler = () => { route.current = parseHash(location.hash); };
  window.addEventListener('hashchange', handler);
  route.current = parseHash(location.hash);
  return () => window.removeEventListener('hashchange', handler);
}

export function navigate(hash: string): void {
  if (typeof location !== 'undefined') location.hash = hash.startsWith('#') ? hash : `#${hash}`;
}

export { route };
export type { Route };
