/**
 * Primitive taxonomy for the /tools workspace and inline /agents stream cards.
 *
 * The 13 Beidou primitives are grouped into 5 categories that drive the visual
 * accent (top border, dot color, swatch). The category rules are:
 *   - coordination: A2A messaging / discovery — no topology change
 *   - lifecycle:    creates / destroys an agent or team
 *   - plan:         DAG add / remove / query — pure data
 *   - review:       work-ready signal or review queue (does NOT terminate)
 *   - human:        leaves the agent system → user via leader chain
 *
 * Tool names emitted as events arrive prefixed (e.g. `mcp__beidou__send_message`);
 * `displayToolName` (lib/format.ts) strips the prefix. All lookup helpers below
 * accept the bare primitive name.
 */

export type PrimitiveCategory = 'coordination' | 'lifecycle' | 'plan' | 'review' | 'human';

export type PrimitiveMeta = {
  name: string;
  category: PrimitiveCategory;
  /** Card label shown in the top-left of the head (e.g. "Coordination"). */
  categoryLabel: string;
  /** True for primitives that have been superseded (visually muted + strikethrough). */
  deprecated?: boolean;
};

const PRIMITIVES: PrimitiveMeta[] = [
  // Coordination
  { name: 'send_message',          category: 'coordination', categoryLabel: 'Coordination' },
  { name: 'list_peers',            category: 'coordination', categoryLabel: 'Coordination' },
  // Lifecycle
  { name: 'spawn_agent',           category: 'lifecycle',    categoryLabel: 'Lifecycle' },
  { name: 'terminate_child',       category: 'lifecycle',    categoryLabel: 'Lifecycle' },
  { name: 'create_team',           category: 'lifecycle',    categoryLabel: 'Lifecycle', deprecated: true },
  // Plan
  { name: 'declare_plan',          category: 'plan',         categoryLabel: 'Plan' },
  { name: 'remove_plan',           category: 'plan',         categoryLabel: 'Plan' },
  { name: 'list_ready',            category: 'plan',         categoryLabel: 'Plan' },
  // Review
  { name: 'signal_review',         category: 'review',       categoryLabel: 'Review' },
  { name: 'request_termination',   category: 'review',       categoryLabel: 'Review' },
  { name: 'report_status',         category: 'review',       categoryLabel: 'Review', deprecated: true },
  { name: 'list_pending_reviews',  category: 'review',       categoryLabel: 'Review' },
  // Human
  { name: 'ask_user',              category: 'human',        categoryLabel: 'Human' },
];

const BY_NAME: Record<string, PrimitiveMeta> = Object.fromEntries(PRIMITIVES.map((p) => [p.name, p]));

/** Strip the `mcp__<server>__` prefix if present. Returns the bare name. */
export function bareToolName(rawName: string): string {
  const m = rawName.match(/^mcp__[^_]+(?:_[^_]+)*__(.+)$/);
  return m ? m[1] : rawName;
}

export function getPrimitive(rawName: string): PrimitiveMeta | null {
  return BY_NAME[bareToolName(rawName)] ?? null;
}

export function isPrimitive(rawName: string): boolean {
  return bareToolName(rawName) in BY_NAME;
}

export function allPrimitives(): readonly PrimitiveMeta[] {
  return PRIMITIVES;
}

/** Stable, deterministic 6-bucket monogram color for an agent id.
 *  Buckets h1..h6 map to predefined Tailwind classes (see AgentChip.svelte). */
export function agentMonogramBucket(agentId: string): 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6' {
  if (!agentId) return 'h1';
  let h = 0;
  for (let i = 0; i < agentId.length; i++) {
    h = (h * 31 + agentId.charCodeAt(i)) | 0;
  }
  const idx = Math.abs(h) % 6;
  return (['h1', 'h2', 'h3', 'h4', 'h5', 'h6'] as const)[idx];
}

/** Two-character monogram (e.g. ag_9b0c451d → "9b"). Strips a leading
 *  `<prefix>_` segment when present. Falls back to `??` for empty input. */
export function agentMonogramText(agentId: string): string {
  if (!agentId) return '??';
  const stripped = agentId.replace(/^[a-zA-Z]+_/, '');
  return stripped.slice(0, 2) || agentId.slice(0, 2);
}

/** Category → Tailwind text/border/swatch classes (semantic tokens only). */
export function categoryAccentClasses(cat: PrimitiveCategory): {
  textClass: string;
  /** Background class for the top accent stripe. */
  stripeClass: string;
  /** Background class for the swatch / pill dot (small). */
  swatchClass: string;
} {
  switch (cat) {
    case 'coordination': return { textClass: 'text-success', stripeClass: 'bg-success',  swatchClass: 'bg-success' };
    case 'lifecycle':    return { textClass: 'text-accent',  stripeClass: 'bg-accent',   swatchClass: 'bg-accent' };
    case 'plan':         return { textClass: 'text-info',    stripeClass: 'bg-info',     swatchClass: 'bg-info' };
    case 'review':       return { textClass: 'text-review',  stripeClass: 'bg-review',   swatchClass: 'bg-review' };
    case 'human':        return { textClass: 'text-accent',  stripeClass: 'bg-accent/70',swatchClass: 'bg-accent/70' };
  }
}
