<script lang="ts">
  import * as dagre from '@dagrejs/dagre';
  import type { PlanTaskSpec } from '../../lib/timeline';

  let {
    tasks,
    /** Optional: when present, marks closed tasks (done / failed). */
    statusByTaskId,
  }: {
    tasks: PlanTaskSpec[];
    statusByTaskId?: Record<string, 'done' | 'failed' | 'pending' | 'spawned'>;
  } = $props();

  // Lay out the graph with dagre. Re-runs when tasks change.
  let layout = $derived.by(() => {
    if (tasks.length === 0) return null;

    const g = new dagre.graphlib.Graph();
    g.setGraph({
      rankdir: 'TB',     // top-to-bottom
      ranksep: 32,
      nodesep: 18,
      marginx: 8,
      marginy: 8,
    });
    g.setDefaultEdgeLabel(() => ({}));

    const NODE_W = 120;
    const NODE_H = 34;

    for (const t of tasks) {
      g.setNode(t.task_id, { width: NODE_W, height: NODE_H, label: t.task_id, role: t.role ?? '' });
    }
    for (const t of tasks) {
      for (const dep of t.depends_on ?? []) {
        // Skip edges to unknown deps so dagre doesn't synthesise dangling nodes.
        if (!g.hasNode(dep)) continue;
        g.setEdge(dep, t.task_id);
      }
    }

    dagre.layout(g);

    const graphMeta = g.graph() as { width: number; height: number };
    const nodes = g.nodes().map((id) => {
      const n = g.node(id) as { x: number; y: number; width: number; height: number; label: string; role: string };
      return { id, x: n.x, y: n.y, w: n.width, h: n.height, label: n.label, role: n.role };
    });
    const edges = g.edges().map((e) => {
      const ed = g.edge(e) as { points: { x: number; y: number }[] };
      return { v: e.v, w: e.w, points: ed.points };
    });

    return {
      width: graphMeta.width,
      height: graphMeta.height,
      nodes,
      edges,
    };
  });

  /** Cubic-bezier path through dagre's polyline points (smooths out the polyline). */
  function pathFromPoints(pts: { x: number; y: number }[]): string {
    if (pts.length === 0) return '';
    if (pts.length === 1) return `M${pts[0].x},${pts[0].y}`;
    if (pts.length === 2) return `M${pts[0].x},${pts[0].y} L${pts[1].x},${pts[1].y}`;
    let d = `M${pts[0].x},${pts[0].y}`;
    for (let i = 1; i < pts.length - 1; i++) {
      const mid = { x: (pts[i].x + pts[i + 1].x) / 2, y: (pts[i].y + pts[i + 1].y) / 2 };
      d += ` Q${pts[i].x},${pts[i].y} ${mid.x},${mid.y}`;
    }
    const last = pts[pts.length - 1];
    d += ` T${last.x},${last.y}`;
    return d;
  }

  function statusFill(id: string): { fill: string; stroke: string; text: string } {
    const s = statusByTaskId?.[id];
    if (s === 'done')    return { fill: 'rgba(16,185,129,0.10)', stroke: '#10b981', text: '#a7f3d0' };
    if (s === 'failed')  return { fill: 'rgba(244,63,94,0.10)',  stroke: '#f43f5e', text: '#fda4af' };
    if (s === 'pending') return { fill: 'rgba(100,116,139,0.10)', stroke: '#475569', text: '#94a3b8' };
    return { fill: 'rgba(14,165,233,0.10)', stroke: '#0ea5e9', text: '#bae6fd' };
  }
</script>

{#if layout}
  <div class="overflow-x-auto py-1">
    <svg
      width={Math.max(layout.width, 100)}
      height={Math.max(layout.height, 40)}
      viewBox={`0 0 ${Math.max(layout.width, 100)} ${Math.max(layout.height, 40)}`}
      class="font-mono"
      style="display:block;"
    >
      <defs>
        <marker id="dag-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill="#475569"></path>
        </marker>
      </defs>

      <!-- Edges -->
      {#each layout.edges as e (e.v + '->' + e.w)}
        <path
          d={pathFromPoints(e.points)}
          fill="none"
          stroke="#334155"
          stroke-width="1.25"
          marker-end="url(#dag-arrow)"
        />
      {/each}

      <!-- Nodes -->
      {#each layout.nodes as n (n.id)}
        {@const s = statusFill(n.id)}
        <g transform={`translate(${n.x - n.w / 2}, ${n.y - n.h / 2})`}>
          <rect
            width={n.w}
            height={n.h}
            rx="4"
            ry="4"
            fill={s.fill}
            stroke={s.stroke}
            stroke-width="1"
          />
          <text
            x={n.w / 2}
            y={n.role ? 14 : 20}
            text-anchor="middle"
            fill={s.text}
            font-size="11"
            font-weight="600"
            font-family="ui-monospace, 'JetBrains Mono', Consolas, monospace"
          >{n.label}</text>
          {#if n.role}
            <text
              x={n.w / 2}
              y={26}
              text-anchor="middle"
              fill="#64748b"
              font-size="9.5"
              font-family="Inter, system-ui, sans-serif"
            >· {n.role.length > 18 ? n.role.slice(0, 17) + '…' : n.role}</text>
          {/if}
        </g>
      {/each}
    </svg>
  </div>
{:else}
  <div class="text-slate-500 italic text-[11.5px]">(no tasks)</div>
{/if}
