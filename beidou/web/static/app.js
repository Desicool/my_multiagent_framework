// Beidou web UI — Alpine.js single-page app.
// Route/store/WS logic; templates rendered as strings for Alpine's x-html.
import { Copy, Pause, Play, ChevronDown, ChevronRight, createIcons }
  from "https://esm.sh/lucide@0.344.0";
const LUCIDE = { Copy, Pause, Play, ChevronDown, ChevronRight };

const ESC_MAP = { "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC_MAP[c]);
const short = (id) => (id || "").slice(-6);
const fmtMoney = (n) => "$" + Number(n || 0).toFixed(4);
const fmtNum = (n) => { n = Number(n || 0); return n >= 1000 ? (n/1000).toFixed(1) + "k" : String(n | 0); };
const ago = (ts, now) => {
  if (!ts) return "—";
  const s = Math.max(0, now - ts);
  if (s < 60) return `${s | 0}s ago`;
  if (s < 3600) return `${(s / 60) | 0}m ago`;
  if (s < 86400) return `${(s / 3600) | 0}h ago`;
  return `${(s / 86400) | 0}d ago`;
};
const elapsedStr = (a, b) => {
  if (!a) return "—";
  const s = Math.max(0, (b || Date.now() / 1000) - a);
  if (s < 60) return `${s.toFixed(1)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(1)}h`;
};
const hhmmss = (ts) => ts ? new Date(ts * 1000).toTimeString().slice(0, 8) : "--:--:--";
const wall = (ts) => ts ? new Date(ts * 1000).toISOString().replace("T", " ").slice(0, 19) : "—";
const evKey = (e) => `${e.ts}|${e.agent_id}|${e.event}`;

// ---------- Alpine component ----------
document.addEventListener("alpine:init", () => {
  window.Alpine.data("app", () => ({
    // Routing
    routeName: "home",
    routeParams: {},
    breadcrumb: [],

    // Liveness
    liveOK: false,
    now: Date.now() / 1000,

    // Home store
    tasks: [],
    homePollId: null,

    // Task store
    task: null,
    teams: [],
    teamTree: [],
    agents: [],
    agentById: {},
    stats: {},
    events: [],          // descending by ts for feed
    eventKeys: new Set(),
    snapshotTs: 0,

    // UI state
    selectedTeamId: "__all__",
    eventsPaused: false,
    pausedBuffer: [],
    followTail: true,
    eventFilter: "all",
    timelineOpen: true,
    expandedTeams: {},   // team_id -> bool (true = expanded)

    // Agent detail store
    agent: null,
    agentEvents: [],
    agentEventFilter: "all",
    promptExpanded: false,

    // Infra
    ws: null,
    wsAttempts: 0,
    wsPollTimer: null,
    flashMap: {},
    drawerLeft: false,
    drawerRight: false,
    timelineInstance: null,

    // ----- lifecycle -----
    init() {
      window.addEventListener("hashchange", () => this.onRoute());
      setInterval(() => { this.now = Date.now() / 1000; }, 2000);
      this.onRoute();
    },

    onRoute() {
      this.teardown();
      const hash = location.hash || "#/";
      const m = hash.match(/^#\/tasks\/([^/?]+)/) || hash.match(/^#\/agents\/([^/?]+)/);
      if (hash === "#/" || hash === "") {
        this.routeName = "home";
        this.breadcrumb = [];
        this.startHome();
      } else if (hash.startsWith("#/tasks/") && m) {
        this.routeName = "task";
        this.routeParams = { id: m[1] };
        this.breadcrumb = [{ label: m[1], href: `#/tasks/${m[1]}` }];
        this.startTask(m[1]);
      } else if (hash.startsWith("#/agents/") && m) {
        this.routeName = "agent";
        this.routeParams = { id: m[1] };
        this.breadcrumb = [{ label: m[1], href: `#/agents/${m[1]}` }];
        this.startAgent(m[1]);
      } else {
        this.routeName = "unknown";
        this.breadcrumb = [];
      }
    },

    teardown() {
      if (this.homePollId) { clearInterval(this.homePollId); this.homePollId = null; }
      if (this.wsPollTimer) { clearInterval(this.wsPollTimer); this.wsPollTimer = null; }
      if (this.ws) { try { this.ws.close(); } catch {} this.ws = null; }
      if (this.timelineInstance) { try { this.timelineInstance.destroy(); } catch {} this.timelineInstance = null; }
      this.wsAttempts = 0;
    },

    // ----- HOME -----
    async startHome() {
      await this.loadTasks();
      this.homePollId = setInterval(() => this.loadTasks(), 3000);
    },
    async loadTasks() {
      try {
        const r = await fetch("/api/tasks");
        if (!r.ok) throw new Error("http " + r.status);
        const j = await r.json();
        this.tasks = j.tasks || [];
        this.liveOK = true;
      } catch {
        this.liveOK = false;
      }
    },

    renderHome() {
      const rows = this.tasks.map((t) => {
        const statusCls = t.status === "running" ? "running pulse"
                        : t.status === "failed"  ? "failed"
                        : "done";
        return `
          <a class="row mono text-[12px]" href="#/tasks/${esc(t.task_id)}">
            <span class="dot ${statusCls}" aria-hidden="true"></span>
            <span class="fg-0" style="width: 220px;">${esc(t.task_id)}</span>
            <span class="fg-1 truncate-60" style="flex:1;">${esc((t.description || "").slice(0, 60))}</span>
            <span class="chip">${esc(t.model || "—")}</span>
            <span class="chip num">${Number(t.live_agent_count || 0)} live</span>
            <span class="fg-2 num" style="width: 90px;">${esc(ago(t.started_at, this.now))}</span>
            <span class="fg-0 num" style="width: 90px;">${esc(fmtMoney(t.total_cost_usd))}</span>
            <span class="fg-1 num" style="width: 80px;">${esc(fmtNum(t.total_tokens))} tok</span>
          </a>`;
      }).join("");

      const empty = `
        <div class="panel p-6 text-center fg-1">
          <p class="fg-0 mb-1">No tasks yet.</p>
          <p class="mono text-[12px]">Run <span class="chip">beidou run "your task"</span> to get started.</p>
        </div>`;

      return `
        <div class="flex items-center justify-between mb-3">
          <h1 class="text-[14px] fg-0" style="font-weight: 500;">Recent tasks</h1>
          <span class="fg-2 text-[11px] mono num">${this.tasks.length} shown</span>
        </div>
        ${this.tasks.length ? `<div class="panel">${rows}</div>` : empty}
      `;
    },

    // ----- TASK -----
    async startTask(id) {
      this.resetTaskStore();
      await this.loadTaskSnapshot(id);
      this.openWS(id);
      // After HTML renders, try to draw timeline
      queueMicrotask(() => this.$nextTick(() => this.drawTimeline()));
    },
    resetTaskStore() {
      this.task = null; this.teams = []; this.teamTree = []; this.agents = [];
      this.agentById = {}; this.stats = {}; this.events = []; this.eventKeys = new Set();
      this.snapshotTs = 0; this.selectedTeamId = "__all__"; this.expandedTeams = {};
    },
    async loadTaskSnapshot(id) {
      try {
        const r = await fetch(`/api/tasks/${encodeURIComponent(id)}`);
        if (!r.ok) throw new Error("http " + r.status);
        const j = await r.json();
        this.task = j.task || null;
        this.teams = j.teams || [];
        this.teamTree = j.team_tree || [];
        this.agents = j.agents || [];
        this.agentById = Object.fromEntries(this.agents.map((a) => [a.agent_id, a]));
        this.stats = j.stats || {};
        this.snapshotTs = j.snapshot_ts || 0;
        // Prime feed with last events per agent (they are already dicts)
        const lep = j.last_event_per_agent || {};
        const primed = Object.values(lep).filter(Boolean).sort((a, b) => (b.ts || 0) - (a.ts || 0));
        this.events = [];
        this.eventKeys = new Set();
        for (const e of primed) this.pushEvent(e, { silent: true });
        // Expand all teams by default
        const walkTeams = (nodes) => {
          for (const n of nodes || []) {
            this.expandedTeams[n.team_id ?? "__root__"] = true;
            walkTeams(n.children);
          }
        };
        walkTeams(this.teamTree);
        this.liveOK = true;
      } catch {
        this.liveOK = false;
      }
    },
    openWS(id) {
      this.wsAttempts += 1;
      if (this.wsAttempts > 3) {
        this.startPollFallback(id);
        return;
      }
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${location.host}/ws/tasks/${encodeURIComponent(id)}?since=${this.snapshotTs}`;
      try {
        const ws = new WebSocket(url);
        this.ws = ws;
        ws.onopen = () => { this.liveOK = true; this.wsAttempts = 0; };
        ws.onmessage = (ev) => {
          try {
            const e = JSON.parse(ev.data);
            this.handleEvent(e);
          } catch {}
        };
        ws.onerror = () => { this.liveOK = false; };
        ws.onclose = () => {
          this.liveOK = false;
          this.ws = null;
          if (this.routeName !== "task" || this.routeParams.id !== id) return;
          const backoff = [500, 1000, 2000, 5000, 10000];
          const delay = backoff[Math.min(this.wsAttempts, backoff.length - 1)] ?? 10000;
          setTimeout(() => {
            if (this.routeName === "task" && this.routeParams.id === id) {
              this.loadTaskSnapshot(id).then(() => this.openWS(id));
            }
          }, delay);
        };
      } catch {
        this.liveOK = false;
      }
    },
    startPollFallback(id) {
      this.wsPollTimer = setInterval(async () => {
        try {
          const since = this.snapshotTs;
          const r = await fetch(`/api/tasks/${encodeURIComponent(id)}/events?since=${since}`);
          if (!r.ok) return;
          const j = await r.json();
          for (const e of j.events || []) {
            this.handleEvent(e);
            if (e.ts && e.ts > this.snapshotTs) this.snapshotTs = e.ts;
          }
          this.liveOK = true;
        } catch {
          this.liveOK = false;
        }
      }, 2000);
    },
    handleEvent(e) {
      this.pushEvent(e, { silent: false });
      const aid = e.agent_id;
      const ag = this.agentById[aid];
      if (e.event === "agent_started") {
        if (!ag) {
          const na = {
            agent_id: aid,
            team_id: e.team_id || null,
            role: e.role || "agent",
            model: e.model || null,
            status: "running",
            started_at: e.ts,
            current_activity: null,
            llm_calls: 0, tool_calls: 0,
            input_tokens: 0, output_tokens: 0,
            total_cost_usd: 0,
          };
          this.agents.push(na);
          this.agentById[aid] = na;
        } else {
          ag.status = "running";
          ag.started_at = ag.started_at || e.ts;
        }
      } else if (e.event === "agent_completed" && ag) {
        ag.status = "done";
        ag.ended_at = e.ts;
        ag.current_activity = { event: "agent_completed", ts: e.ts };
      } else if (e.event === "tool_called" && ag) {
        ag.tool_calls = (ag.tool_calls || 0) + 1;
        ag.current_activity = e;
      } else if (e.event === "llm_response" && ag) {
        ag.llm_calls = (ag.llm_calls || 0) + 1;
        ag.input_tokens  = (ag.input_tokens  || 0) + (e.input_tokens  || 0);
        ag.output_tokens = (ag.output_tokens || 0) + (e.output_tokens || 0);
        ag.total_cost_usd = (ag.total_cost_usd || 0) + (e.cost || 0);
        ag.current_activity = e;
      } else if (e.event === "team_created") {
        const team = {
          team_id: e.team_id || e.new_team_id,
          parent_team_id: e.parent_team_id || null,
          name: e.team_name || e.name || "team",
          leader_agent_id: e.leader_agent_id || null,
          created_at: e.ts,
        };
        if (!this.teams.find((t) => t.team_id === team.team_id)) {
          this.teams.push(team);
          this.rebuildTeamTree();
          this.expandedTeams[team.team_id] = true;
        }
      } else if (e.event === "task_completed" && this.task) {
        this.task.status = "done";
        this.task.ended_at = e.ts;
      }
      if (aid) {
        this.flashMap[aid] = Date.now() / 1000;
      }
    },
    pushEvent(e, { silent }) {
      const k = evKey(e);
      if (this.eventKeys.has(k)) return;
      this.eventKeys.add(k);
      if (!silent && this.eventsPaused) {
        this.pausedBuffer.push(e);
        return;
      }
      // Feed is reverse-chronological; inserting by ts keeps snapshot + live merged cleanly.
      const ts = e.ts || 0;
      let i = 0;
      while (i < this.events.length && (this.events[i].ts || 0) > ts) i++;
      this.events.splice(i, 0, e);
      if (this.events.length > 500) this.events.length = 500;
    },
    resumeEvents() {
      this.eventsPaused = false;
      for (const e of this.pausedBuffer.reverse()) this.events.unshift(e);
      this.pausedBuffer = [];
      if (this.events.length > 500) this.events.length = 500;
    },
    rebuildTeamTree() {
      const byParent = {};
      for (const t of this.teams) {
        const p = t.parent_team_id || "__top__";
        (byParent[p] = byParent[p] || []).push(t);
      }
      const agentsByTeam = {};
      for (const a of this.agents) {
        const k = a.team_id || "__null__";
        (agentsByTeam[k] = agentsByTeam[k] || []).push(a.agent_id);
      }
      const walk = (p) => (byParent[p] || []).map((t) => ({
        ...t,
        children: walk(t.team_id),
        agents: agentsByTeam[t.team_id] || [],
      }));
      this.teamTree = [{
        team_id: null,
        name: "root",
        children: walk("__top__"),
        agents: agentsByTeam["__null__"] || [],
      }];
    },

    // ----- Card activity derivation -----
    cardActivity(a) {
      const act = a.current_activity;
      if (a.status === "done") {
        const elapsed = elapsedStr(a.started_at, a.ended_at);
        return { icon: "✓", text: `done · ${elapsed}`, color: "var(--done)" };
      }
      if (!act) return { icon: "◌", text: "idle", color: "var(--fg-2)" };
      const age = this.now - (act.ts || 0);
      if (age > 30) return { icon: "◌", text: "idle", color: "var(--fg-2)" };
      if (act.event === "tool_called") {
        const d = act.duration_ms != null ? ` · ${act.duration_ms}ms` : "";
        return { icon: "⏵", text: `tool_called · ${act.tool || "?"}${d}`, color: "var(--tool)" };
      }
      if (act.event === "llm_response") {
        const d = act.duration_ms != null ? `${act.duration_ms}ms` : "—";
        const sr = act.stop_reason ? ` · ${act.stop_reason}` : "";
        return { icon: "◆", text: `llm_response · ${d}${sr}`, color: "var(--llm)" };
      }
      return { icon: "·", text: act.event || "—", color: "var(--fg-1)" };
    },

    visibleAgents() {
      if (this.selectedTeamId === "__all__") return this.agents;
      if (this.selectedTeamId === "__root__") return this.agents.filter((a) => !a.team_id);
      return this.agents.filter((a) => a.team_id === this.selectedTeamId);
    },

    visibleEvents() {
      const f = this.eventFilter;
      if (f === "all") return this.events;
      return this.events.filter((e) => e.event === f);
    },

    // ----- Rendering -----
    renderTask() {
      if (!this.task) {
        return `<div class="panel p-6 fg-1">Loading task…</div>`;
      }
      const t = this.task;
      const statusCls = t.status === "running" ? "running pulse"
                      : t.status === "failed"  ? "failed" : "done";
      const s = this.stats || {};
      const elapsed = elapsedStr(t.started_at, t.ended_at);

      const header = `
        <div class="panel p-3 mb-3">
          <div class="flex items-start justify-between gap-4">
            <div>
              <div class="flex items-center gap-2 mb-1">
                <span class="dot ${statusCls}" aria-hidden="true"></span>
                <span class="mono text-[12px] fg-0">${esc(t.task_id)}</span>
                <button class="btn" @click="copy(task && task.task_id || '')" aria-label="Copy task id">Copy task_id</button>
              </div>
              <p class="fg-0 text-[13px]" style="margin:0;">${esc(t.description || "")}</p>
            </div>
            <div class="flex items-center gap-2 text-[11px] mono fg-1">
              <span class="chip">${esc(t.model || "—")}</span>
              <span class="chip">${esc(t.template || "default")}</span>
              <span class="num">${esc(ago(t.started_at, this.now))}</span>
              <span class="fg-2">·</span>
              <span class="num fg-0">${esc(fmtMoney(s.total_cost_usd ?? t.total_cost_usd))}</span>
              <span class="fg-2">·</span>
              <span class="num">${esc(fmtNum(s.total_tokens ?? t.total_tokens))} tok</span>
              <span class="fg-2">·</span>
              <span class="num">${esc(elapsed)}</span>
            </div>
          </div>
        </div>`;

      const timeline = `
        <div class="panel mb-3">
          <button class="w-full flex items-center justify-between px-3 py-2 text-[11px] mono fg-1"
                  @click="timelineOpen = !timelineOpen; $nextTick(() => drawTimeline())"
                  :aria-expanded="timelineOpen">
            <span>Timeline</span>
            <span x-text="timelineOpen ? '▾' : '▸'"></span>
          </button>
          <div x-show="timelineOpen" id="beidou-timeline" style="height: 120px;"></div>
        </div>`;

      return `
        ${header}
        ${timeline}
        <div class="flex gap-3" style="align-items: flex-start;">
          <aside class="pane-left panel" :class="{ 'drawer-open': drawerLeft }"
                 style="width: 260px; flex: none; max-height: 70vh; overflow: auto;">
            <div class="px-3 py-2 fg-1 text-[11px] mono divider" style="border-bottom:1px solid var(--border);">
              Team tree
            </div>
            ${this.renderTeamTree()}
          </aside>

          <section class="flex-1" style="min-width: 0;">
            ${this.agents.length === 0
              ? `<div class="panel p-6 text-center fg-1">No agents yet — waiting for events…</div>`
              : `<div class="grid gap-3" style="grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));">
                   ${this.visibleAgents().map((a) => this.renderAgentCard(a)).join("")}
                 </div>`}
          </section>

          <aside class="pane-right panel" :class="{ 'drawer-open': drawerRight }"
                 style="width: 340px; flex: none; max-height: 70vh; display:flex; flex-direction:column;">
            ${this.renderEventFeed()}
          </aside>
        </div>`;
    },

    renderTeamTree() {
      const renderNode = (node, depth) => {
        const tid = node.team_id ?? "__root__";
        const open = this.expandedTeams[tid] !== false;
        const kids = (node.children || []).map((c) => renderNode(c, depth + 1)).join("");
        const agents = (node.agents || []).map((aid) => {
          const a = this.agentById[aid];
          if (!a) return "";
          const cls = a.status === "running" ? "running" : a.status === "failed" ? "failed" : "done";
          return `
            <a href="#/agents/${esc(aid)}"
               class="row mono text-[11px]" style="height: 28px; padding-left: 0;">
              <span class="dot ${cls}" aria-hidden="true"></span>
              <span class="fg-0">${esc(short(aid))}</span>
              <span class="fg-2">${esc(a.role || "")}</span>
            </a>`;
        }).join("");
        const agentCount = (node.agents || []).length;
        return `
          <div>
            <div class="row mono text-[12px]" style="padding-left: 8px; cursor: pointer;"
                 @click="toggleTeam('${esc(tid)}'); selectTeam('${esc(tid)}')">
              <span class="fg-2" style="width: 10px;">${open ? "▾" : "▸"}</span>
              <span style="color: var(--team);">◆</span>
              <span class="fg-0">${esc(node.name || "team")}</span>
              <span class="fg-2 num">(${agentCount})</span>
            </div>
            ${open ? `<div class="tree-indent">${agents}${kids}</div>` : ""}
          </div>`;
      };
      const filters = `
        <div class="flex gap-1 px-2 py-2" style="border-bottom:1px solid var(--border);">
          <button class="btn" :class="selectedTeamId === '__all__' ? 'active' : ''"
                  @click="selectTeam('__all__')">all</button>
        </div>`;
      return filters + (this.teamTree.length
        ? this.teamTree.map((n) => renderNode(n, 0)).join("")
        : `<div class="p-3 fg-2 text-[11px]">No teams yet.</div>`);
    },

    toggleTeam(tid) {
      const cur = this.expandedTeams[tid];
      this.expandedTeams[tid] = cur === undefined ? false : !cur;
    },
    selectTeam(tid) { this.selectedTeamId = tid; },

    renderAgentCard(a) {
      const cls = a.status === "running" ? "running pulse"
                : a.status === "failed"  ? "failed" : "done";
      const act = this.cardActivity(a);
      const flashing = (this.flashMap[a.agent_id] || 0) > (Date.now() / 1000 - 0.6);
      const stats = `${a.llm_calls || 0} LLM · ${a.tool_calls || 0} tools · ${fmtNum((a.input_tokens || 0) + (a.output_tokens || 0))} tok · ${fmtMoney(a.total_cost_usd)}`;
      return `
        <a href="#/agents/${esc(a.agent_id)}" class="card ${flashing ? "flash" : ""}">
          <div class="flex items-center justify-between mb-1">
            <div class="flex items-center gap-2 mono text-[12px]">
              <span class="dot ${cls}" aria-hidden="true"></span>
              <span class="fg-0">agt_${esc(short(a.agent_id))}</span>
            </div>
            <span class="chip chip-team">${esc(a.role || "agent")}</span>
          </div>
          <div class="mono fg-2 text-[11px]" style="margin-bottom: 6px;">${esc(a.model || "—")}</div>
          <div class="mono text-[11px]" style="color: ${act.color}; margin-bottom: 8px;">
            ${esc(act.icon)} ${esc(act.text)}
          </div>
          <div class="divider" style="margin: 0 -12px 8px;"></div>
          <div class="mono text-[11px] fg-1 num">${esc(stats)}</div>
        </a>`;
    },

    renderEventFeed() {
      const chips = ["agent_started","llm_response","tool_called","team_created","agent_completed"];
      const chipRow = chips.map((c) => `
        <button class="btn ${this.eventFilter === c ? "active" : ""}"
                @click="eventFilter = eventFilter === '${c}' ? 'all' : '${c}'">${c}</button>
      `).join("");

      const rows = this.visibleEvents().slice(0, 200).map((e) => {
        let detail = "";
        if (e.event === "tool_called") detail = `${e.tool || ""}${e.duration_ms != null ? " · " + e.duration_ms + "ms" : ""}`;
        else if (e.event === "llm_response") detail = `${e.duration_ms || ""}ms${e.stop_reason ? " · " + e.stop_reason : ""}`;
        else if (e.event === "team_created") detail = e.team_name || e.name || "";
        else if (e.event === "agent_started") detail = e.role || "";
        const color = e.event === "tool_called" ? "var(--tool)"
                    : e.event === "llm_response" ? "var(--llm)"
                    : e.event === "team_created" ? "var(--team)"
                    : e.event === "agent_completed" ? "var(--done)"
                    : "var(--running)";
        return `
          <div class="mono text-[11px] flex items-center gap-2 px-3" style="height: 28px; border-bottom: 1px solid var(--border);">
            <span class="fg-2 num">${esc(hhmmss(e.ts))}</span>
            <span class="fg-1">agt_${esc(short(e.agent_id))}</span>
            <span class="dot" style="background: ${color};" aria-hidden="true"></span>
            <span style="color: ${color};">${esc(e.event)}</span>
            <span class="fg-2 truncate-60" style="flex:1;">${esc(detail)}</span>
          </div>`;
      }).join("");

      return `
        <div class="flex items-center gap-1 px-2 py-2" style="border-bottom:1px solid var(--border); flex-wrap: wrap;">
          <button class="btn ${this.eventsPaused ? "active" : ""}"
                  @click="eventsPaused ? resumeEvents() : (eventsPaused = true)"
                  aria-label="Pause events">
            ${this.eventsPaused ? `▶ resume (${this.pausedBuffer.length})` : "⏸ pause"}
          </button>
          <button class="btn ${this.followTail ? "active" : ""}"
                  @click="followTail = !followTail">∨ follow</button>
          <button class="btn ${this.eventFilter === 'all' ? 'active' : ''}"
                  @click="eventFilter = 'all'">all</button>
          ${chipRow}
        </div>
        <div style="overflow:auto; flex:1;">
          ${rows || `<div class="p-3 fg-2 text-[11px]">No events yet.</div>`}
        </div>`;
    },

    drawTimeline() {
      const mount = document.getElementById("beidou-timeline");
      if (!mount || !window.vis || !this.timelineOpen) return;
      if (this.timelineInstance) { try { this.timelineInstance.destroy(); } catch {} this.timelineInstance = null; }
      const groups = new window.vis.DataSet();
      const items  = new window.vis.DataSet();
      const teamRows = [{ team_id: "__root__", name: "root" }, ...this.teams];
      teamRows.forEach((t, i) => {
        groups.add({ id: t.team_id || "__root__", content: t.name || "team", order: i });
      });
      for (const a of this.agents) {
        const gid = a.team_id || "__root__";
        const start = (a.started_at || this.now) * 1000;
        const end = (a.ended_at || this.now) * 1000;
        const color = a.status === "running" ? "#3ecf8e" : "#64748b";
        items.add({
          id: `a-${a.agent_id}`,
          group: gid,
          start, end,
          type: "range",
          content: short(a.agent_id),
          style: `background:${color};border-color:${color};color:#0b0d10;`,
          title: `agt_${short(a.agent_id)} · ${a.role || ""}`,
        });
      }
      for (const e of this.events.slice(0, 300)) {
        if (e.event !== "tool_called" && e.event !== "llm_response") continue;
        const ag = this.agentById[e.agent_id];
        const gid = (ag && ag.team_id) || "__root__";
        const color = e.event === "tool_called" ? "#38bdf8" : "#a78bfa";
        items.add({
          id: `e-${evKey(e)}`,
          group: gid,
          start: (e.ts || 0) * 1000,
          type: "point",
          style: `color:${color};`,
          title: `${e.event} · agt_${short(e.agent_id)} · ${new Date((e.ts || 0) * 1000).toISOString()}`,
        });
      }
      try {
        this.timelineInstance = new window.vis.Timeline(mount, items, groups, {
          height: "120px", stack: false, showCurrentTime: true, zoomMin: 1000,
        });
      } catch {}
    },

    // ----- AGENT -----
    async startAgent(id) {
      this.agent = null; this.agentEvents = [];
      try {
        const r = await fetch(`/api/agents/${encodeURIComponent(id)}`);
        if (!r.ok) throw new Error("http " + r.status);
        const j = await r.json();
        this.agent = j.agent;
        this.agentEvents = j.events || [];
        this.liveOK = true;
      } catch {
        this.liveOK = false;
      }
    },
    renderAgent() {
      if (!this.agent) return `<div class="panel p-6 fg-1">Loading agent…</div>`;
      const a = this.agent;
      const cls = a.status === "running" ? "running pulse" : a.status === "failed" ? "failed" : "done";
      const tools = (a.tools || []).map((t) => `<span class="chip chip-tool">${esc(t)}</span>`).join(" ");
      const skills = (a.skills || []).map((s) => `<span class="chip chip-skill">${esc(s)}</span>`).join(" ");
      const prompt = esc(a.system_prompt || "");
      // Team label links back to the parent task (spec: "Team (with link back to task view)").
      const teamLabel = a.team_id ? short(a.team_id) : "root";
      const teamLink = a.task_id
        ? `<a class="fg-1 underline" href="#/tasks/${esc(a.task_id)}">${esc(teamLabel)}</a>`
        : esc(teamLabel);

      const filtered = this.agentEventFilter === "all"
        ? this.agentEvents
        : this.agentEvents.filter((e) => e.event === this.agentEventFilter);

      const timelineRows = filtered.map((e) => {
        const icon = e.event === "tool_called" ? "⏵" : e.event === "llm_response" ? "◆" : "·";
        const color = e.event === "tool_called" ? "var(--tool)" : e.event === "llm_response" ? "var(--llm)" : "var(--fg-1)";
        const dur = e.duration_ms != null ? `<span class="chip num">${e.duration_ms}ms</span>` : "";
        const detail = e.event === "tool_called" ? (e.tool || "") : (e.stop_reason || "");
        return `
          <div class="mono text-[11px] flex items-center gap-2 px-3"
               style="height: 32px; border-bottom:1px solid var(--border);">
            <span style="color: ${color};">${esc(icon)}</span>
            <span class="fg-0" style="width: 120px;">${esc(e.event)}</span>
            <span class="fg-2 num" style="width: 90px;">${esc(hhmmss(e.ts))}</span>
            ${dur}
            <span class="fg-1 truncate-60" style="flex:1;">${esc(detail)}</span>
          </div>`;
      }).join("");

      return `
        <div class="flex gap-3" style="align-items: flex-start;">
          <aside class="panel" style="width: 360px; flex: none;">
            <div class="px-3 py-2 mono text-[11px] fg-2" style="border-bottom:1px solid var(--border);">PROPERTIES</div>
            <div class="px-3 py-2 grid gap-1 text-[12px] mono" style="grid-template-columns: 80px 1fr;">
              <span class="fg-2">Role</span><span class="fg-0">${esc(a.role || "—")}</span>
              <span class="fg-2">Model</span><span class="fg-0">${esc(a.model || "—")}</span>
              <span class="fg-2">Template</span><span class="fg-0">${esc(a.template || "default")}</span>
              <span class="fg-2">Team</span><span>${teamLink}</span>
              <span class="fg-2">Started</span><span class="fg-1">${esc(wall(a.started_at))} · ${esc(ago(a.started_at, this.now))}</span>
              <span class="fg-2">Status</span>
              <span class="flex items-center gap-2"><span class="dot ${cls}"></span><span class="fg-0">${esc(a.status)}</span></span>
            </div>
            <div class="divider" style="border-top:1px solid var(--border);"></div>
            <div class="px-3 py-2 mono text-[11px] fg-2">STATS</div>
            <div class="px-3 py-2 grid gap-1 text-[12px] mono num" style="grid-template-columns: 110px 1fr;">
              <span class="fg-2">LLM calls</span><span class="fg-0">${a.llm_calls || 0}</span>
              <span class="fg-2">Tool calls</span><span class="fg-0">${a.tool_calls || 0}</span>
              <span class="fg-2">Tokens</span><span class="fg-0">${fmtNum(a.input_tokens)} in · ${fmtNum(a.output_tokens)} out</span>
              <span class="fg-2">Cost</span><span class="fg-0">${fmtMoney(a.total_cost_usd)}</span>
            </div>
            <div class="divider" style="border-top:1px solid var(--border);"></div>
            <div class="px-3 py-2 mono text-[11px] fg-2">TOOLS</div>
            <div class="px-3 pb-2 flex flex-wrap gap-1">${tools || `<span class="fg-2 text-[11px]">none</span>`}</div>
            <div class="px-3 py-2 mono text-[11px] fg-2">SKILLS</div>
            <div class="px-3 pb-2 flex flex-wrap gap-1">${skills || `<span class="fg-2 text-[11px]">none</span>`}</div>
            <div class="divider" style="border-top:1px solid var(--border);"></div>
            <div class="px-3 py-2 flex items-center justify-between">
              <span class="mono text-[11px] fg-2">SYSTEM PROMPT</span>
              <span class="flex gap-1">
                <button class="btn" @click="copy(agent && agent.system_prompt || '')">Copy</button>
                <button class="btn" @click="promptExpanded = !promptExpanded"
                        x-text="promptExpanded ? 'Collapse' : 'Expand'"></button>
              </span>
            </div>
            <pre class="px-3 pb-3 mono text-[11px] fg-1 whitespace-pre-wrap ${this.promptExpanded ? "" : "clamp-8"}" style="margin:0;">${prompt || "—"}</pre>
          </aside>

          <section class="panel flex-1" style="min-width: 0;">
            <div class="px-3 py-2 flex items-center gap-1" style="border-bottom:1px solid var(--border);">
              <span class="mono text-[11px] fg-2" style="margin-right:8px;">TIMELINE</span>
              ${["all","tool_called","llm_response"].map((f) => `
                <button class="btn ${this.agentEventFilter === f ? "active" : ""}"
                        @click="agentEventFilter = '${f}'">${f}</button>
              `).join("")}
            </div>
            <div>${timelineRows || `<div class="p-3 fg-2 text-[11px] mono">No events yet.</div>`}</div>
          </section>
        </div>`;
    },

    // ----- util -----
    async copy(text) {
      try { await navigator.clipboard.writeText(text); } catch {}
    },
  }));
});

// Hydrate Lucide icons on any [data-lucide] after each render. Not strictly
// required — most iconography is unicode — but fulfils the ESM import contract
// and lets `<i data-lucide="copy">` appear anywhere in the future.
const hydrateIcons = () => {
  try { createIcons({ icons: LUCIDE }); } catch {}
};
const mo = new MutationObserver(() => hydrateIcons());
mo.observe(document.documentElement, { subtree: true, childList: true });
hydrateIcons();
