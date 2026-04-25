// Beidou web UI — Alpine.js single-page app.
// Architecture: central reducer, hash router, WS + poll fallback.
//
// Event field note: actual JSONL events use:
//   "event" (not "type") for the event type name
//   "llm_response" with tokens_in/tokens_out/cost_usd/duration_ms
//   "tool_called" with "tool" (the name), "duration_ms"
//   "team_created" with "new_team_id", "leader_agent_id"
// WS envelope: {type:"event", event:{...real event...}, cursor}
//              {type:"resumed", cursor}

// ---- Dedup sets (kept outside Alpine proxy to avoid reactivity overhead) ----
// turn.usage dedup: `${agent_id}|${message_id}|${ts}` — but for llm_response we
//   use `${agent_id}|${ts}` since there is no message_id in the current emitter.
// tool pair dedup:  `${tool_use_id}` if present, else `${agent_id}|${ts}|${tool}`
const _seenTurn = new Set();
const _seenTool = new Set();

// ---- Pure utility helpers ----
const ESC_MAP = { "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" };
const esc     = (s) => String(s ?? "").replace(/[&<>"']/g, c => ESC_MAP[c]);
const short   = (id) => (id || "").slice(-8);
const short6  = (id) => (id || "").slice(-6);
const fmtMoney = (n) => n ? "$" + Number(n).toFixed(4) : "$0.0000";
const fmtNum  = (n) => { n = Number(n || 0); return n >= 1000 ? (n/1000).toFixed(1)+"k" : String(n|0); };

function ago(ts, now) {
  if (!ts) return "—";
  const s = Math.max(0, (now || Date.now()/1000) - ts);
  if (s < 60)    return `${s|0}s ago`;
  if (s < 3600)  return `${(s/60)|0}m ago`;
  if (s < 86400) return `${(s/3600)|0}h ago`;
  return `${(s/86400)|0}d ago`;
}

function elapsedStr(a, b) {
  if (!a) return "—";
  const s = Math.max(0, (b || Date.now()/1000) - a);
  if (s < 60)   return `${s.toFixed(1)}s`;
  if (s < 3600) return `${(s/60).toFixed(1)}m`;
  return `${(s/3600).toFixed(1)}h`;
}

function hhmmss(ts) {
  return ts ? new Date(ts*1000).toTimeString().slice(0,8) : "--:--:--";
}

function agentStatusFromState(ag) {
  if (!ag) return "idle";
  if (ag.ended_at) return "done";
  const state = ag._statusState;
  if (state === "done")    return "done";
  if (state === "blocked") return "blocked";
  if (state === "idle")    return "idle";
  return "working";
}

function dotCls(status) {
  if (status === "running" || status === "working") return "dot running pulse";
  if (status === "done")    return "dot done";
  if (status === "failed")  return "dot failed";
  if (status === "blocked") return "dot blocked";
  if (status === "idle")    return "dot idle";
  return "dot idle";
}

// ---- Central reducer ----
// Returns a NEW state object (shallow copy of state with updated fields).
// Dedup sets are module-level and mutated in place (intentional).

function initialState() {
  return {
    task:           null,
    teams:          {},      // team_id -> team obj
    agents:         {},      // agent_id -> agent obj
    stats:          { total_cost_usd: 0, total_tokens: 0 },
    pendingTools:   {},      // tool_use_id -> {name, started_at, agent_id}
    recentMessages: [],      // last 50 send_message events
    leaderStream:   [],      // ordered events for root agent left pane
    questions:      [],      // merged from pending-questions poll
    cursor:         0,
    debugLog:       [],      // unknown events, capped at 20
  };
}

function reduce(state, ev) {
  const type = ev.event;
  const aid  = ev.agent_id;

  // Helper: shallow-clone state
  const s = Object.assign({}, state);

  // ---- Dedup for llm_response (treat as turn.usage equivalent) ----
  if (type === "llm_response" || type === "turn.usage") {
    const msgId = ev.message_id || `${aid}|${ev.ts}`;
    const dedupKey = `${aid}|${msgId}`;
    if (_seenTurn.has(dedupKey)) return state;
    _seenTurn.add(dedupKey);
  }

  // ---- Dedup for tool pairs ----
  if (type === "tool_called" || type === "tool_result") {
    const toolKey = ev.tool_use_id || `${aid}|${ev.ts}|${ev.tool||ev.name}`;
    if (type === "tool_result") {
      if (!_seenTool.has(toolKey)) return state; // result before call — ignore
    } else {
      if (_seenTool.has(toolKey)) return state;  // duplicate call
      _seenTool.add(toolKey);
    }
  }

  // ---- Update cursor ----
  if (ev.ts && ev.ts > s.cursor) s.cursor = ev.ts;

  // Clone agents/teams maps shallowly so we can mutate agent/team safely
  s.agents = Object.assign({}, s.agents);
  s.teams  = Object.assign({}, s.teams);

  // ---- Handler per event type ----
  switch (type) {

    case "agent_started": {
      const existing = s.agents[aid];
      if (!existing) {
        s.agents[aid] = {
          agent_id:      aid,
          team_id:       ev.team_id || null,
          role:          ev.role || "agent",
          model:         ev.model || null,
          skill:         ev.skill || null,
          system_prompt: ev.system_prompt || null,
          tools:         ev.tools || [],
          skills:        ev.skills || [],
          started_at:    ev.ts,
          ended_at:      null,
          _statusState:  "working",
          _statusDetail: null,
          _lastEventType: "agent_started",
          _lastEventTs:   ev.ts,
          tokens_in:     0,
          tokens_out:    0,
          llm_calls:     0,
          tool_calls:    0,
          cost_usd:      0,
          _turns:        [],   // per-turn records for detail table
          _toolList:     [],   // tool calls list for detail pane
          _stream:       [],   // live chronological stream: text + tool events
        };
      } else {
        const ag = Object.assign({}, existing);
        ag.started_at = ag.started_at || ev.ts;
        if (ev.system_prompt) ag.system_prompt = ev.system_prompt;
        if (ev.tools && ev.tools.length) ag.tools = ev.tools;
        if (ev.skills && ev.skills.length) ag.skills = ev.skills;
        ag._lastEventType = "agent_started";
        ag._lastEventTs   = ev.ts;
        s.agents[aid]     = ag;
      }
      break;
    }

    case "agent_completed": {
      const ag = Object.assign({}, s.agents[aid] || { agent_id: aid });
      ag.ended_at      = ev.ts;
      ag._statusState  = "done";
      ag._lastEventType = "agent_completed";
      ag._lastEventTs   = ev.ts;
      s.agents[aid]    = ag;
      break;
    }

    case "llm_response":
    case "turn.usage": {
      // llm_response: tokens_in, tokens_out, cost_usd, duration_ms, stop_reason
      // turn.usage:   input_tokens, output_tokens, cache_*, stop_reason, model, message_id
      const ag = Object.assign({}, s.agents[aid] || { agent_id: aid, tokens_in:0, tokens_out:0, llm_calls:0, cost_usd:0, _turns:[], _toolList:[], _stream:[] });
      const inTok  = ev.tokens_in  || ev.input_tokens  || 0;
      const outTok = ev.tokens_out || ev.output_tokens || 0;
      ag.tokens_in  = (ag.tokens_in  || 0) + inTok;
      ag.tokens_out = (ag.tokens_out || 0) + outTok;
      ag.llm_calls  = (ag.llm_calls  || 0) + 1;
      // Keep running stats.total_tokens updated from individual turn events
      s.stats = Object.assign({}, s.stats, {
        total_tokens: (s.stats.total_tokens||0) + inTok + outTok,
      });
      ag._lastEventType = type;
      ag._lastEventTs   = ev.ts;
      // Per-turn record for detail table
      ag._turns = [...(ag._turns||[]), {
        ts:            ev.ts,
        model:         ev.model || ag.model || "—",
        in_tok:        inTok,
        out_tok:       outTok,
        cache_read:    ev.cache_read_input_tokens    || 0,
        cache_create:  ev.cache_creation_input_tokens|| 0,
        stop_reason:   ev.stop_reason || "—",
        duration_ms:   ev.duration_ms || null,
      }];
      s.agents[aid] = ag;

      // Left pane: leader stream item
      s.leaderStream = appendLeaderStreamItem(s, aid, {
        type: "turn",
        ts:   ev.ts,
        in_tok:  inTok,
        out_tok: outTok,
        stop_reason: ev.stop_reason,
        model: ev.model,
      });
      break;
    }

    case "assistant_text": {
      const ag = Object.assign({}, s.agents[aid] || { agent_id: aid, _stream: [] });
      ag._stream = [...(ag._stream||[]), {
        type:        "text",
        ts:          ev.ts,
        message_id:  ev.message_id,
        text:        ev.text || "",
        stop_reason: ev.stop_reason,
      }];
      if (ag._stream.length > 200) ag._stream = ag._stream.slice(-200);
      ag._lastEventType = "assistant_text";
      ag._lastEventTs   = ev.ts;
      s.agents[aid] = ag;
      break;
    }

    case "tool_called": {
      const ag = Object.assign({}, s.agents[aid] || { agent_id: aid, tool_calls:0, _turns:[], _toolList:[], _stream:[] });
      ag.tool_calls    = (ag.tool_calls||0) + 1;
      ag._lastEventType = "tool_called";
      ag._lastEventTs   = ev.ts;
      const toolName   = ev.tool || ev.name || "?";
      // Track pending tool uses
      const toolUseId  = ev.tool_use_id || `${aid}|${ev.ts}`;
      s.pendingTools   = Object.assign({}, s.pendingTools, {
        [toolUseId]: { name: toolName, started_at: ev.ts, agent_id: aid }
      });
      // Add to agent tool list
      ag._toolList = [...(ag._toolList||[]), {
        tool_use_id: toolUseId,
        name: toolName,
        started_at: ev.ts,
        duration_ms: ev.duration_ms || null,
        is_error: false,
        pending: ev.duration_ms == null,
      }];
      // Per-agent live stream entry
      ag._stream = [...(ag._stream||[]), {
        type:        "tool_start",
        ts:          ev.ts,
        name:        toolName,
        tool_use_id: toolUseId,
        duration_ms: null,
        is_error:    null,
      }];
      if (ag._stream.length > 200) ag._stream = ag._stream.slice(-200);
      s.agents[aid]    = ag;

      // Left pane: inline tool item
      s.leaderStream = appendLeaderStreamItem(s, aid, {
        type: "tool_start",
        ts:   ev.ts,
        name: toolName,
        tool_use_id: toolUseId,
      });
      break;
    }

    case "tool_result": {
      const toolUseId = ev.tool_use_id;
      const pending   = s.pendingTools[toolUseId];
      if (pending) {
        const pt = Object.assign({}, s.pendingTools);
        delete pt[toolUseId];
        s.pendingTools = pt;
        // Update agent tool list entry
        const ag = Object.assign({}, s.agents[pending.agent_id] || {});
        if (ag._toolList) {
          ag._toolList = ag._toolList.map(t =>
            t.tool_use_id === toolUseId
              ? Object.assign({}, t, { duration_ms: ev.duration_ms, is_error: !!ev.is_error, pending: false })
              : t
          );
        }
        // Patch per-agent stream entry
        if (ag._stream) {
          ag._stream = ag._stream.map(item =>
            (item.type === "tool_start" && item.tool_use_id === toolUseId)
              ? Object.assign({}, item, { duration_ms: ev.duration_ms, is_error: !!ev.is_error })
              : item
          );
        }
        ag._lastEventType = "tool_result";
        ag._lastEventTs   = ev.ts;
        s.agents[pending.agent_id] = ag;
        // Left pane: update tool item
        s.leaderStream = updateLeaderStreamTool(s.leaderStream, toolUseId, {
          duration_ms: ev.duration_ms,
          is_error:    !!ev.is_error,
        });
      }
      break;
    }

    case "run.cost": {
      const ag = Object.assign({}, s.agents[aid] || { agent_id: aid });
      ag.cost_usd = (ag.cost_usd||0) + (ev.total_cost_usd||0);
      s.agents[aid] = ag;
      s.stats = Object.assign({}, s.stats, {
        total_cost_usd: (s.stats.total_cost_usd||0) + (ev.total_cost_usd||0),
      });
      break;
    }

    case "status": {
      const ag = Object.assign({}, s.agents[aid] || { agent_id: aid });
      ag._statusState  = ev.state || "working";
      ag._statusDetail = ev.detail || null;
      ag._lastEventType = "status";
      ag._lastEventTs   = ev.ts;
      s.agents[aid]    = ag;
      break;
    }

    case "team_created": {
      const teamId = ev.new_team_id || ev.team_id;
      if (teamId && !s.teams[teamId]) {
        s.teams[teamId] = {
          team_id:        teamId,
          task_id:        ev.task_id || null,
          // ev.parent_team_id if explicit; else the creating agent's team_id is the parent;
          // null means root (agent with team_id === null creates first team)
          parent_team_id: ev.parent_team_id !== undefined ? ev.parent_team_id : (ev.team_id || null),
          name:           ev.team_name || ev.name || "team",
          leader_agent_id: ev.leader_agent_id || null,
          workspace_path: ev.workspace_path || null,
          created_at:     ev.ts,
        };
      }
      break;
    }

    case "send_message": {
      const msg = {
        ts:        ev.ts,
        from:      ev.sender_agent_id || ev.caller_id || ev.agent_id || "?",
        to:        ev.recipient_agent_id || ev.to || "?",
        content:   ev.content || ev.message || "",
        _new:      true,
      };
      const msgs = [msg, ...(s.recentMessages||[])].slice(0, 50);
      s.recentMessages = msgs;
      break;
    }

    case "question_asked": {
      // We use the poll endpoint as the source of truth for questions;
      // but we also update the questions list here if data is available.
      // The SignalPane Questions tab re-polls on each question_asked event.
      break;
    }

    case "question_answered": {
      const qid = ev.qid;
      s.questions = (s.questions||[]).filter(q => q.qid !== qid);
      break;
    }

    case "contract_violation": {
      const ag = Object.assign({}, s.agents[aid] || { agent_id: aid });
      ag._lastEventType = "contract_violation";
      ag._lastEventTs   = ev.ts;
      s.agents[aid]     = ag;
      break;
    }

    case "agent_error": {
      const ag = Object.assign({}, s.agents[aid] || { agent_id: aid });
      ag._lastEventType = "agent_error";
      ag._lastEventTs   = ev.ts;
      s.agents[aid]     = ag;
      break;
    }

    case "config_warning": {
      const ag = Object.assign({}, s.agents[aid] || { agent_id: aid });
      ag._lastEventType = "config_warning";
      ag._lastEventTs   = ev.ts;
      s.agents[aid]     = ag;
      break;
    }

    default: {
      // Unknown events go to debug log (capped at 20)
      s.debugLog = [...(s.debugLog||[]).slice(-19), { ts: ev.ts, type, raw: ev }];
      break;
    }
  }

  return s;
}

// ---- Leader stream helpers ----
function appendLeaderStreamItem(state, agentId, item) {
  const rootAgentId = findRootAgentId(state);
  if (agentId !== rootAgentId) return state.leaderStream || [];
  const stream = [...(state.leaderStream||[]), item];
  // Keep last 200 items
  if (stream.length > 200) return stream.slice(-200);
  return stream;
}

function updateLeaderStreamTool(stream, toolUseId, patch) {
  return (stream||[]).map(item =>
    (item.type === "tool_start" && item.tool_use_id === toolUseId)
      ? Object.assign({}, item, patch)
      : item
  );
}

function findRootAgentId(state) {
  // Root team = team with parent_team_id === null
  const teams = Object.values(state.teams||{});
  const rootTeam = teams.find(t => t.parent_team_id === null);
  if (rootTeam && rootTeam.leader_agent_id) return rootTeam.leader_agent_id;
  // Fallback: agent with team_id === null
  const rootAgent = Object.values(state.agents||{}).find(a => a.team_id === null);
  return rootAgent ? rootAgent.agent_id : null;
}

function buildTeamTree(state) {
  const teams  = Object.values(state.teams||{});
  const agents = Object.values(state.agents||{});
  const agentsByTeam = {};
  for (const a of agents) {
    const k = a.team_id || "__null__";
    (agentsByTeam[k] = agentsByTeam[k]||[]).push(a.agent_id);
  }
  const byParent = {};
  for (const t of teams) {
    const p = t.parent_team_id || "__top__";
    (byParent[p] = byParent[p]||[]).push(t);
  }
  const walk = (p) => (byParent[p]||[]).map(t => ({
    ...t,
    agents: agentsByTeam[t.team_id]||[],
    children: walk(t.team_id),
  }));
  // Orphan agents (team_id null) handled as children of root team if present,
  // else returned at top of tree.
  return walk("__top__");
}

// ---- Alpine component ----
if (typeof document !== "undefined") document.addEventListener("alpine:init", () => {
  window.Alpine.data("app", () => ({
    // Router
    routeName:   "home",
    routeParams: {},
    breadcrumb:  [],

    // Liveness
    liveOK:   false,
    liveMode: false,
    now:      Date.now()/1000,

    // Home view
    tasks:       [],
    homePollId:  null,

    // Task view — mirrors the reducer state
    _state: null,      // current task reducer state

    // Task view UI
    pinnedAgentId:  null,
    rightTab:       "inbox", // "inbox" | "questions" | "agent"
    expandedTeams:  {},      // team_id -> bool
    leaderAutoScroll: true,
    drawerLeft:     false,
    drawerRight:    false,

    // Questions
    pendingQuestions: [],
    questionPollId:   null,
    answerTexts:      {},  // qid -> string

    // Agent composer
    agentDraftTexts:  {},  // agentId -> draft string
    agentSendStatus:  {},  // agentId -> status string

    // Infra
    ws:         null,
    wsAttempts: 0,
    wsPollTimer: null,

    // ---- lifecycle ----
    init() {
      window.addEventListener("hashchange", () => this.onRoute());
      setInterval(() => { this.now = Date.now()/1000; }, 1000);
      this.onRoute();
    },

    onRoute() {
      this.teardown();
      const hash = location.hash || "#/";
      if (hash === "#/" || hash === "#" || hash === "") {
        this.routeName   = "home";
        this.routeParams = {};
        this.breadcrumb  = [];
        this.startHome();
      } else if (hash.startsWith("#/tasks/")) {
        const id = hash.slice("#/tasks/".length).split("?")[0];
        this.routeName   = "task";
        this.routeParams = { id };
        this.breadcrumb  = [{ label: id.slice(-12), href: `#/tasks/${id}` }];
        this.startTask(id);
      } else if (hash.startsWith("#/agents/")) {
        const id = hash.slice("#/agents/".length).split("?")[0];
        this.routeName   = "agent";
        this.routeParams = { id };
        this.breadcrumb  = [{ label: id.slice(-12), href: `#/agents/${id}` }];
        this.startAgentView(id);
      } else {
        this.routeName = "unknown";
        this.breadcrumb = [];
      }
    },

    teardown() {
      if (this.homePollId)  { clearInterval(this.homePollId);  this.homePollId = null; }
      if (this.wsPollTimer) { clearInterval(this.wsPollTimer); this.wsPollTimer = null; }
      if (this.questionPollId) { clearInterval(this.questionPollId); this.questionPollId = null; }
      if (this.ws) { try { this.ws.close(); } catch {} this.ws = null; }
      this.wsAttempts = 0;
      this.liveMode   = false;
      this.liveOK     = false;
      _seenTurn.clear();
      _seenTool.clear();
    },

    // ---- HOME ----
    async startHome() {
      await this.loadTasks();
      this.homePollId = setInterval(() => this.loadTasks(), 3000);
    },

    async loadTasks() {
      try {
        const r = await fetch("/api/tasks");
        if (!r.ok) throw new Error("http " + r.status);
        const j = await r.json();
        this.tasks   = j.tasks || [];
        this.liveOK  = true;
      } catch {
        this.liveOK = false;
      }
    },

    // ---- TASK VIEW ----
    async startTask(id) {
      this._state        = initialState();
      this.pinnedAgentId = null;
      this.rightTab      = "inbox";
      this.expandedTeams = {};
      this.leaderAutoScroll = true;
      this.pendingQuestions = [];

      await this.loadTaskSnapshot(id);
      // Auto-pin root agent so the Agent tab is live immediately
      const rootId = this.rootAgentId();
      if (rootId) {
        this.pinnedAgentId = rootId;
        this.rightTab = "agent";
      }
      this.openWS(id);
      await this.pollQuestions();
      this.questionPollId = setInterval(() => this.pollQuestions(), 5000);
    },

    async loadTaskSnapshot(id) {
      try {
        const r = await fetch(`/api/tasks/${encodeURIComponent(id)}`);
        if (!r.ok) throw new Error("http " + r.status);
        const j = await r.json();
        // Hydrate state from snapshot
        let s = initialState();
        s.task   = j.task   || null;
        s.stats  = j.stats  || { total_cost_usd: 0, total_tokens: 0 };
        s.cursor = j.cursor || 0;

        // Teams
        for (const t of (j.teams||[])) {
          s.teams[t.team_id] = t;
        }

        // Agents
        for (const a of (j.agents||[])) {
          const status = a.ended_at ? "done" : "working";
          s.agents[a.agent_id] = {
            ...a,
            _statusState:  a.status === "done" ? "done" : "working",
            _statusDetail: null,
            _lastEventType: null,
            _lastEventTs:   null,
            tokens_in:  a.tokens_in  || 0,
            tokens_out: a.tokens_out || 0,
            llm_calls:  a.llm_calls  || 0,
            tool_calls: a.tool_calls || 0,
            cost_usd:   a.total_cost_usd || 0,
            _turns:    [],
            _toolList: [],
            _stream:   [],
          };
        }

        // Expand all teams by default
        for (const t of (j.teams||[])) {
          this.expandedTeams[t.team_id] = true;
        }

        this._state = s;
        this.liveOK = true;
      } catch {
        this.liveOK = false;
      }
    },

    applyEvent(ev) {
      const next = reduce(this._state, ev);
      this._state = next;
      // Refresh pending questions on question_asked
      if (ev.event === "question_asked") this.pollQuestions();
      if (ev.event === "question_answered") {
        const qid = ev.qid;
        this.pendingQuestions = this.pendingQuestions.filter(q => q.qid !== qid);
      }
      // Auto-expand new teams
      if (ev.event === "team_created") {
        const tid = ev.new_team_id || ev.team_id;
        if (tid) this.expandedTeams[tid] = true;
      }
      // Auto-scroll leader pane
      if (this.leaderAutoScroll) {
        this.$nextTick && this.$nextTick(() => {
          const el = document.getElementById("leader-scroll");
          if (el) el.scrollTop = el.scrollHeight;
        });
      }
      // Auto-scroll pinned agent stream pane
      if (ev.agent_id && ev.agent_id === this.pinnedAgentId) {
        this.$nextTick && this.$nextTick(() => {
          const el = document.getElementById("agent-stream-" + ev.agent_id);
          if (el) el.scrollTop = el.scrollHeight;
        });
      }
    },

    openWS(id) {
      const proto   = location.protocol === "https:" ? "wss:" : "ws:";
      const cursor  = this._state ? this._state.cursor : 0;
      const url     = `${proto}//${location.host}/ws/tasks/${encodeURIComponent(id)}?since=${cursor}`;
      let ws;
      try {
        ws = new WebSocket(url);
      } catch {
        this.liveOK = false;
        this.scheduleWSReconnect(id);
        return;
      }
      this.ws = ws;

      ws.onopen = () => {
        this.liveOK    = true;
        this.wsAttempts = 0;
      };

      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "event" && msg.event) {
            this.applyEvent(msg.event);
            if (msg.cursor && msg.cursor > (this._state||{}).cursor) {
              this._state = Object.assign({}, this._state, { cursor: msg.cursor });
            }
          } else if (msg.type === "resumed") {
            this.liveMode = true;
          }
        } catch {}
      };

      ws.onerror = () => { this.liveOK = false; };

      ws.onclose = () => {
        this.liveOK = false;
        this.ws     = null;
        if (this.routeName !== "task" || this.routeParams.id !== id) return;
        this.wsAttempts++;
        this.scheduleWSReconnect(id);
      };
    },

    scheduleWSReconnect(id) {
      const backoffs = [500, 1000, 2000, 5000, 10000];
      const delay    = backoffs[Math.min(this.wsAttempts - 1, backoffs.length - 1)] || 500;
      if (this.wsAttempts >= 3) {
        // Fall back to polling
        if (!this.wsPollTimer) {
          this.wsPollTimer = setInterval(() => this.pollEventsFallback(id), 2000);
        }
        return;
      }
      setTimeout(() => {
        if (this.routeName === "task" && this.routeParams.id === id) {
          this.openWS(id);
        }
      }, delay);
    },

    async pollEventsFallback(id) {
      try {
        const cursor = (this._state||{}).cursor || 0;
        const r = await fetch(`/api/tasks/${encodeURIComponent(id)}/events?since=${cursor}&limit=200`);
        if (!r.ok) return;
        const j = await r.json();
        for (const ev of j.events||[]) this.applyEvent(ev);
        if (j.cursor && j.cursor > cursor) {
          this._state = Object.assign({}, this._state, { cursor: j.cursor });
        }
        this.liveOK = true;
        // Try to re-open WS
        if (!this.ws && this.wsAttempts >= 3) {
          this.wsAttempts = 0;
          clearInterval(this.wsPollTimer);
          this.wsPollTimer = null;
          this.openWS(id);
        }
      } catch {
        this.liveOK = false;
      }
    },

    async pollQuestions() {
      try {
        const r = await fetch("/api/questions/pending");
        if (!r.ok) return;
        const j = await r.json();
        this.pendingQuestions = j.questions || [];
      } catch {}
    },

    async submitAnswer(qid) {
      const answer = (this.answerTexts[qid]||"").trim();
      if (!answer) return;
      try {
        const r = await fetch(`/api/questions/${encodeURIComponent(qid)}/answer`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ answer }),
        });
        if (r.ok) {
          this.pendingQuestions = this.pendingQuestions.filter(q => q.qid !== qid);
          delete this.answerTexts[qid];
        }
      } catch {}
    },

    async sendToAgent(agentId) {
      const content = (this.agentDraftTexts[agentId] || "").trim();
      if (!content) return;
      this.agentSendStatus = Object.assign({}, this.agentSendStatus, { [agentId]: "sending…" });
      try {
        const r = await fetch(`/api/agents/${encodeURIComponent(agentId)}/send`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content }),
        });
        if (r.ok) {
          this.agentDraftTexts = Object.assign({}, this.agentDraftTexts, { [agentId]: "" });
          this.agentSendStatus = Object.assign({}, this.agentSendStatus, { [agentId]: "sent" });
          setTimeout(() => {
            this.agentSendStatus = Object.assign({}, this.agentSendStatus, { [agentId]: "" });
          }, 1500);
        } else {
          const j = await r.json().catch(() => ({}));
          this.agentSendStatus = Object.assign({}, this.agentSendStatus, {
            [agentId]: "error: " + (j.detail || r.status),
          });
        }
      } catch {
        this.agentSendStatus = Object.assign({}, this.agentSendStatus, { [agentId]: "error" });
      }
    },

    // ---- AGENT STANDALONE VIEW ----
    agentViewData: null,

    async startAgentView(id) {
      this.agentViewData = null;
      try {
        const r = await fetch(`/api/agents/${encodeURIComponent(id)}`);
        if (!r.ok) throw new Error("http " + r.status);
        const j = await r.json();
        this.agentViewData = j.agent;
        this.liveOK = true;
      } catch {
        this.liveOK = false;
      }
    },

    // ---- Computed helpers ----
    stateTask()   { return (this._state||{}).task; },
    stateTeams()  { return Object.values((this._state||{}).teams||{}); },
    stateAgents() { return Object.values((this._state||{}).agents||{}); },
    stateStats()  { return (this._state||{}).stats||{}; },

    rootTeam() {
      return this.stateTeams().find(t => t.parent_team_id === null) || null;
    },

    rootAgentId() {
      const rt = this.rootTeam();
      if (rt && rt.leader_agent_id) return rt.leader_agent_id;
      const nullTeam = this.stateAgents().find(a => a.team_id === null);
      return nullTeam ? nullTeam.agent_id : null;
    },

    rootAgent() {
      const rid = this.rootAgentId();
      return rid ? (this._state||{}).agents[rid] : null;
    },

    teamTree() {
      return this._state ? buildTeamTree(this._state) : [];
    },

    pinnedAgent() {
      if (!this.pinnedAgentId || !this._state) return null;
      return this._state.agents[this.pinnedAgentId] || null;
    },

    pinAgent(agentId) {
      this.pinnedAgentId = agentId;
      this.rightTab = "agent";
    },

    agentStatus(ag) {
      return agentStatusFromState(ag);
    },

    agentLastActivity(ag) {
      if (!ag) return "—";
      const t = ag._lastEventType;
      const tool = ag._toolList && ag._toolList.length
        ? ag._toolList[ag._toolList.length-1].name : null;
      if (!t) return "waiting…";
      if (t === "tool_called" && tool) return `→ ${tool}`;
      if (t === "tool_result" && tool) return `✓ ${tool}`;
      if (t === "llm_response" || t === "turn.usage") return "thinking";
      if (t === "agent_completed") return "done";
      if (t === "status" && ag._statusDetail) return ag._statusDetail.slice(0, 40);
      return t;
    },

    agentTokens(ag) {
      if (!ag) return 0;
      return (ag.tokens_in||0) + (ag.tokens_out||0);
    },

    agentCost(ag) {
      if (!ag) return 0;
      // cost_usd set from run.cost; fallback to summed llm_response cost
      return ag.cost_usd || 0;
    },

    taskElapsed() {
      const t = this.stateTask();
      if (!t) return "—";
      return elapsedStr(t.started_at, t.ended_at || this.now);
    },

    totalCost() {
      const s = this.stateStats();
      const t = this.stateTask();
      const c = s.total_cost_usd || t && t.total_cost_usd || 0;
      // Also sum from agents (run.cost accumulation)
      const agentSum = this.stateAgents().reduce((acc, a) => acc + (a.cost_usd||0), 0);
      return Math.max(c, agentSum);
    },

    // ---- Render helpers (called from x-html) ----

    renderTeamTree() {
      const tree = this.teamTree();
      if (!tree || !tree.length) return '<div class="mono text-xs fg-2 p-3">No teams yet…</div>';
      const self = this;
      const renderNode = (node, depth) => {
        const tid  = node.team_id;
        const open = self.expandedTeams[tid] !== false;
        const pad  = (depth * 14) + 6;
        const memberCount = (node.agents||[]).length;
        const safeToggle = `(function(){var el=document.querySelector('[x-data]');if(el&&window.Alpine){var d=window.Alpine.$data(el);d.expandedTeams['${esc(tid)}']=!d.expandedTeams['${esc(tid)}'];}})()`;
        const agentRows = open ? (node.agents||[]).map(aid => {
          const a = self._state && self._state.agents[aid];
          if (!a) return "";
          const st   = self.agentStatus(a);
          const dc   = dotCls(st);
          const toks = fmtNum(self.agentTokens(a));
          const cost = a.cost_usd ? fmtMoney(a.cost_usd) : null;
          const act  = esc(self.agentLastActivity(a));
          const pinned = self.pinnedAgentId === aid ? "background:var(--bg-2);" : "";
          const safePin = `(function(){var el=document.querySelector('[x-data]');if(el&&window.Alpine){var d=window.Alpine.$data(el);d.pinAgent('${esc(aid)}');}})()`;
          return `<div class="row" style="padding-left:${pad+14}px;height:auto;min-height:34px;flex-wrap:wrap;gap:6px;cursor:pointer;${pinned}" onclick="${esc(safePin)}">
            <span class="${esc(dc)}"></span>
            <span class="mono text-xs fg-1" style="min-width:50px;">${esc((a.role||"agent").slice(0,14))}</span>
            <span class="mono fg-2" style="font-size:10px;">${esc(aid.slice(-8))}</span>
            <span class="flex-1"></span>
            <span class="chip mono num" style="font-size:10px;">${esc(toks)} tok</span>
            ${cost ? `<span class="chip chip-emerald mono num" style="font-size:10px;">${esc(cost)}</span>` : ""}
            <span class="mono fg-2 truncate-1" style="font-size:10px;max-width:90px;">${act}</span>
          </div>`;
        }).join("") : "";
        const childRows = open ? (node.children||[]).map(c => renderNode(c, depth+1)).join("") : "";
        return `<div>
          <div class="row" style="padding-left:${pad}px;cursor:pointer;" onclick="${esc(safeToggle)}">
            <span class="fg-2 mono" style="width:10px;font-size:11px;">${open ? "▾" : "▸"}</span>
            <span style="color:var(--amber);">◆</span>
            <span class="mono text-xs fg-0">${esc(node.name||"team")}</span>
            <span class="chip mono fg-2" style="font-size:10px;">${memberCount} members</span>
          </div>
          ${agentRows}${childRows}
        </div>`;
      };
      return tree.map(n => renderNode(n, 0)).join("");
    },

    renderPinnedAgent() {
      const a = this.pinnedAgent();
      if (!a) return '<div class="mono text-xs fg-2 p-3">Click an agent in the tree to pin it here.</div>';
      const status  = this.agentStatus(a);
      const dCls    = dotCls(status);
      const turns   = (a._turns||[]).slice(-20);
      const tools   = (a._toolList||[]).slice(-20);
      const stream  = (a._stream||[]).slice(-200);
      const skillsHtml = (a.skills||[]).map(s => `<span class="chip chip-violet">${esc(s)}</span>`).join(" ");
      const promptHtml = a.system_prompt
        ? `<details class="mb-2"><summary class="mono text-xs fg-2 cursor-pointer p-2" style="list-style:none;">System prompt ▸</summary><pre class="mono p-2 fg-1 whitespace-pre-wrap" style="font-size:10px;margin:0;max-height:130px;overflow-y:auto;">${esc(a.system_prompt)}</pre></details>`
        : "";
      const turnRows = turns.map(t =>
        `<tr><td>${esc(hhmmss(t.ts))}</td><td class="num">${esc(fmtNum(t.in_tok))}</td><td class="num">${esc(fmtNum(t.out_tok))}</td><td class="num">${esc(fmtNum(t.cache_read))}</td><td class="num">${esc(fmtNum(t.cache_create))}</td><td>${esc(t.stop_reason)}</td></tr>`
      ).join("");
      const toolRows = tools.map(t =>
        `<div class="flex items-center gap-2 px-2 py-1 mono divider" style="font-size:11px;">
          <span style="color:var(--sky)">→</span>
          <span class="fg-0 flex-1">${esc(t.name)}</span>
          ${t.duration_ms != null ? `<span class="fg-2 num">${esc(String(t.duration_ms))}ms</span>` : ""}
          ${t.pending ? `<span class="dot running pulse" style="width:6px;height:6px;"></span>` :
            t.is_error ? `<span style="color:var(--rose)">✗</span>` : `<span style="color:var(--emerald)">✓</span>`}
        </div>`
      ).join("");
      const streamRows = stream.map(item => {
        if (item.type === "text") {
          return `<div class="mb-2">
            <span class="mono fg-2" style="font-size:10px;">${esc(hhmmss(item.ts))}</span>
            <pre class="mono fg-1" style="font-size:11px;white-space:pre-wrap;margin:2px 0 0 0;">${esc(item.text)}</pre>
          </div>`;
        } else if (item.type === "tool_start") {
          const pending = item.duration_ms == null;
          const indicator = pending
            ? `<span class="dot running pulse" style="width:6px;height:6px;display:inline-block;"></span>`
            : item.is_error
              ? `<span style="color:var(--rose)">✗</span>`
              : `<span style="color:var(--emerald)">✓</span>`;
          const dur = !pending && item.duration_ms != null
            ? `<span class="fg-2 num" style="font-size:10px;">${esc(String(item.duration_ms))}ms</span>`
            : "";
          return `<div class="flex items-center gap-2 py-0.5 mono" style="font-size:11px;">
            <span class="fg-2" style="font-size:10px;">${esc(hhmmss(item.ts))}</span>
            <span style="color:var(--sky);">→</span>
            <span class="fg-0 flex-1">${esc(item.name)}</span>
            ${dur}
            ${indicator}
          </div>`;
        }
        return "";
      }).join("");
      return `<div class="p-3">
        <div class="flex items-center gap-2 mb-3">
          <span class="${esc(dCls)}"></span>
          <span class="mono text-xs fg-0">${esc(a.role||"agent")}</span>
          <span class="mono fg-2" style="font-size:10px;">${esc(a.agent_id.slice(-8))}</span>
        </div>
        <div class="grid gap-1 mono text-xs mb-3" style="grid-template-columns:80px 1fr;">
          <span class="fg-2">Model</span><span class="fg-1">${esc(a.model||"—")}</span>
          <span class="fg-2">Status</span><span class="fg-1">${esc(status)}</span>
          <span class="fg-2">Tokens in</span><span class="fg-1 num">${esc(fmtNum(a.tokens_in))}</span>
          <span class="fg-2">Tokens out</span><span class="fg-1 num">${esc(fmtNum(a.tokens_out))}</span>
          <span class="fg-2">Cost</span><span class="chip-emerald num">${esc(fmtMoney(a.cost_usd))}</span>
        </div>
        ${skillsHtml ? `<div class="flex flex-wrap gap-1 mb-3">${skillsHtml}</div>` : ""}
        ${promptHtml}
        ${stream.length > 0 ? `<div class="mono text-xs fg-2 mb-1">Live stream (last 200)</div>
        <div class="panel" id="agent-stream-${esc(a.agent_id)}" style="max-height:300px;overflow-y:auto;font-size:11px;padding:8px;">
          ${streamRows}
        </div>` : ""}
        ${turns.length > 0 ? `<div class="mono text-xs fg-2 mb-1 mt-3">Turns (last 20)</div><div style="overflow-x:auto;"><table class="detail-table"><thead><tr><th>Time</th><th>In</th><th>Out</th><th>CacheR</th><th>CacheW</th><th>Stop</th></tr></thead><tbody>${turnRows}</tbody></table></div>` : ""}
        ${tools.length > 0 ? `<div class="mono text-xs fg-2 mt-3 mb-1">Tool calls (last 20)</div><div class="panel">${toolRows}</div>` : ""}
      </div>`;
    },

    renderAgentStandalone() {
      const a = this.agentViewData;
      if (!a) return "";
      const status  = a.ended_at ? "done" : "running";
      const dCls    = dotCls(status);
      const toolsHtml  = (a.tools||[]).map(t => `<span class="chip chip-sky">${esc(t)}</span>`).join(" ");
      const skillsHtml = (a.skills||[]).map(s => `<span class="chip chip-violet">${esc(s)}</span>`).join(" ");
      return `<div class="panel p-4" style="max-width:600px;">
        <div class="flex items-center gap-2 mb-3">
          <span class="${esc(dCls)}"></span>
          <span class="mono text-sm fg-0">${esc(a.role||"agent")}</span>
          <span class="mono text-xs fg-2">${esc(a.agent_id)}</span>
        </div>
        <div class="grid gap-1 mono text-xs mb-3" style="grid-template-columns:90px 1fr;">
          <span class="fg-2">Model</span><span class="fg-1">${esc(a.model||"—")}</span>
          <span class="fg-2">Skill</span><span class="fg-1">${esc(a.skill||"—")}</span>
          <span class="fg-2">Task</span>
          <span>${a.task_id ? `<a class="fg-0 underline" href="#/tasks/${esc(a.task_id)}">${esc(a.task_id)}</a>` : "—"}</span>
          <span class="fg-2">Status</span><span class="fg-1">${esc(status)}</span>
        </div>
        ${toolsHtml ? `<div class="mb-2"><div class="mono text-xs fg-2 mb-1">Tools</div><div class="flex flex-wrap gap-1">${toolsHtml}</div></div>` : ""}
        ${skillsHtml ? `<div class="mb-2"><div class="mono text-xs fg-2 mb-1">Skills</div><div class="flex flex-wrap gap-1">${skillsHtml}</div></div>` : ""}
        ${a.system_prompt ? `<details class="mt-2"><summary class="mono text-xs fg-2 cursor-pointer" style="list-style:none;">System prompt ▸</summary><pre class="mono p-2 fg-1 whitespace-pre-wrap panel mt-1" style="font-size:10px;max-height:200px;overflow-y:auto;">${esc(a.system_prompt)}</pre></details>` : ""}
      </div>`;
    },
  }));
});

// ---- Inline module-level export for testing (Node-compatible) ----
if (typeof module !== "undefined") {
  module.exports = { reduce, initialState, findRootAgentId, buildTeamTree };
}
