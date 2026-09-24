/* SimpleAgent 工作台前端。
   原则：零依赖、零构建，原生 DOM + fetch + EventSource。
   客户端不承载业务逻辑：状态在 Python 侧，这里只负责「渲染事件 + 发命令」。 */

const $ = (id) => document.getElementById(id);
const streamEl = () => $("pane-chat");

/* SSE 帧类型。服务端用 `event: <type>` 发送，EventSource 必须按名字监听，
   onmessage 只收默认的 message 事件，收不到带 event 名的帧。 */
const FRAME_TYPES = [
  "text_delta", "reasoning_delta", "message_done", "tool_call_start", "tool_result",
  "approval_request", "verification", "status", "usage", "error", "max_steps", "turn_end",
  "unknown",
];

const LS_KEY = "sa.workbench.current";   // 记住上次打开的会话，刷新后自动回到原位

const state = {
  spaces: [],
  profiles: [],
  executors: [],
  defaultProfile: "",
  spaceId: null,
  sessionId: null,
  messages: [],        // 当前会话的历史，导出 Markdown 用
  es: null,
  lastSeq: 0,
  running: false,
  startedAt: null,     // 本轮开始时间，用于显示已用时
  noReplyTimer: null,  // 兜底：发出去之后一直没有任何帧就提醒
  acc: "",           // 当前助手消息的累积文本
  accEl: null,       // 累积文本渲染到的元素
  streamEl: null,    // 流式时的光标占位容器
  toolCards: new Map(),   // call_id -> { body, toggle }
  showAll: new Set(),     // 展开了「查看全部」的空间 id
  filter: "",
  tab: "chat",            // 当前右栏 tab
  editingSpace: null,     // 向导处于「空间设置」模式时是那个空间，新建时为 null
  commandSpaceId: null,   // 指挥台调度者住的系统空间（/api/meta 给），左栏不显示
  commandSpace: null,     // 它的详情：点进调度会话时，头部、日志要用
  skills: {},             // 空间 id -> 能用 /技能名 调的技能（/ 菜单和 /help 用）
};

/* ────────────────────────────── 请求封装 ────────────────────────────── */
/* 浏览器对同一个 host 最多开 6 条 HTTP/1.1 连接，连接占满时新请求只会排队：
   不报错、不超时，界面上就是「点了没反应」。给每个请求设个上限，至少变成看得见的错误。 */
const REQ_TIMEOUT_MS = 20000;

async function req(method, path, data, timeoutMs = REQ_TIMEOUT_MS) {
  try {
    const r = await fetch(path, {
      method,
      headers: data === undefined ? {} : { "Content-Type": "application/json" },
      body: data === undefined ? undefined : JSON.stringify(data),
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!r.ok) {
      let msg = `${r.status}`;
      try { msg = (await r.json()).error || msg; } catch { /* 非 JSON 响应就用状态码 */ }
      throw new Error(msg);
    }
    return r.status === 204 ? null : await r.json();
  } catch (e) {
    // 超时可能发生在排队、等响应头或读 body 的任一阶段，统一换成看得懂的提示。
    // 读 body 时超时 Chromium 报的是 AbortError 而不是 TimeoutError；这里没有别处会 abort，两个都算超时
    if (e.name === "TimeoutError" || e.name === "AbortError") {
      throw new Error("请求超时：sa serve 没响应，或浏览器到它的连接被占满（工作台标签页开太多时会这样）");
    }
    throw e;
  }
}
const api = {
  get: (p) => req("GET", p),
  post: (p, d, t) => req("POST", p, d, t),
  patch: (p, d) => req("PATCH", p, d),
  del: (p) => req("DELETE", p),
};

/* ────────────────────────────── 小工具 ────────────────────────────── */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* 极简 Markdown：先转义再处理，避免注入。只支持代码块、行内代码、段落。
   控制面板的消息弹层在用：那里是失败原因、stderr 这类文本，以 # 开头的行不该变成标题。
   对话里模型的回复走 markdown.js 的 renderMarkdown。 */
function renderText(raw) {
  let s = escapeHtml(raw);
  s = s.replace(/```[\w]*\n([\s\S]*?)```/g, (_, code) => `<pre class="code">${code.replace(/\n$/, "")}</pre>`);
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  return s;
}

function relTime(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const min = Math.floor((Date.now() - t) / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  if (min < 1440) return `${Math.floor(min / 60)} 小时前`;
  if (min < 1440 * 7) return `${Math.floor(min / 1440)} 天前`;
  return new Date(t).toLocaleDateString("zh-CN");
}

function shortPath(p, n = 28) {
  if (!p) return "";
  return p.length > n ? "…" + p.slice(-n) : p;
}

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 2200);
}

const BADGE = { "claude-code": ["CC", "b-cc"], opencode: ["OC", "b-oc"], simpleagent: ["SA", "b-sa"] };
const VMARK = { passed: ["✓", "v-passed"], failed: ["✗", "v-failed"], stale: ["⚠", "v-stale"], running: ["◐", "v-running"], unknown: ["○", "v-unknown"] };
const STATUS_TEXT = { running: "运行中", verifying: "验证中", done: "完成", cancelled: "已取消", error: "出错", idle: "空闲" };

/* 卡片徽标：执行者决定「谁跑」，形态决定「在哪儿跑」。
   内置执行者 + 通用形态就显示「通用」，其余按执行者缩写。 */
function badgeFor(sp) {
  const exec = sp.executor || "simpleagent";
  if (exec === "simpleagent") return sp.kind === "generic" ? ["通用", ""] : ["SA", "b-sa"];
  return BADGE[exec] || ["SA", "b-sa"];
}

/* 会话的执行者在创建时锁定（meta.agent）。空间中途切过执行者的话，老会话的历史在原执行者那边，
   接不过来：只能看、导出，不能再发消息（后端同样会拒，返回 409）。 */
function lockedBy(sp, m) {
  if (!sp || !m) return null;
  const exec = sp.executor || "simpleagent";
  const agent = m.agent || "simpleagent";
  return agent === exec ? null : agent;
}

function statusDot(s) {
  return s === "running" ? "●" : s === "error" ? "✗" : s === "done" ? "✓" : s === "cancelled" ? "⊘" : "○";
}

function fmtDur(sec) {
  const m = Math.floor(sec / 60);
  return m ? `${m} 分 ${sec % 60} 秒` : `${sec} 秒`;
}

/* ────────────────────────────── 左栏 ────────────────────────────── */
function renderSpaces() {
  const box = $("space-list");
  const filter = state.filter.trim().toLowerCase();
  box.innerHTML = "";
  $("space-count").textContent = `(${state.spaces.length})`;

  if (!state.spaces.length) {
    box.innerHTML = `<div class="empty-sub" style="padding:8px 6px">还没有空间，点上方「+ 新建空间」开始。</div>`;
    return;
  }

  for (const sp of state.spaces) {
    const [label, cls] = badgeFor(sp);
    const dir = sp.cwd || null;
    const danger =
      (sp.executor || "simpleagent") !== "simpleagent" && sp.permission === "full"
        ? `<span class="warn-chip" title="这个空间的外部 agent 不经确认就会改文件、跑命令">全放行</span>`
        : "";
    const vs = sp.sessions || [];
    const vsum = vs.length
      ? `<span class="vsum" title="最近 ${vs.length} 个会话里通过验证的数量">✓ ${
          vs.filter((m) => (m.verification || {}).status === "passed").length
        }/${vs.length}</span>`
      : "";

    const card = document.createElement("div");
    card.className = "space is-open";
    card.innerHTML = `
      <div class="space-head">
        <span class="space-name">${escapeHtml(sp.name)}</span>
        <span class="badge ${cls}">${label}</span>
        ${danger}
        ${vsum}
        <span class="space-actions">
          <button class="icon-btn" title="在这个空间新建会话" data-act="new-session">＋</button>
          <button class="icon-btn" title="空间设置（名称、执行者、模型、权限）" data-act="settings">⚙</button>
          <button class="icon-btn" title="关闭（只是不显示，不删数据）" data-act="close">×</button>
        </span>
      </div>
      ${dir ? `<div class="space-dir" title="${escapeHtml(dir)}">${escapeHtml(shortPath(dir))}</div>` : ""}
      <div class="sessions"></div>`;

    const list = card.querySelector(".sessions");
    const sessions = (sp.sessions || []).filter(
      (m) => !filter || (m.title || "").toLowerCase().includes(filter));

    if (!sessions.length) {
      list.innerHTML = `<div class="space-dir" style="padding:2px 6px">没有匹配的会话</div>`;
    }
    for (const m of sessions) {
      const [mk, mcls] = VMARK[(m.verification || {}).status] || VMARK.unknown;
      const row = document.createElement("div");
      row.className = "session" + (m.id === state.sessionId ? " is-active" : "");
      const locked = lockedBy(sp, m);
      const [lk, lkcls] = BADGE[locked] || [];
      const lockBadge = locked
        ? `<span class="badge ${lkcls || ""} locked" title="由 ${escapeHtml(locked)} 跑的会话，空间已切换执行者，只读">${lk || escapeHtml(locked)}</span>`
        : "";
      row.innerHTML = `
        <span class="st ${m.status}">${statusDot(m.status)}</span>
        <span class="title" title="${escapeHtml(m.title || "")}">${m.pinned ? "★ " : ""}${escapeHtml(m.title || "新会话")}</span>
        ${m.parent_session_id ? `<span class="badge b-cmd" title="由指挥台派发">派</span>` : ""}
        ${lockBadge}
        <span class="vmark ${mcls}">${mk}</span>
        <span class="time">${relTime(m.updated_at || m.created_at)}</span>
        <span class="row-actions">
          <button class="icon-btn" data-act="pin" title="${m.pinned ? "取消置顶" : "置顶"}">${m.pinned ? "★" : "☆"}</button>
          <button class="icon-btn" data-act="rename" title="重命名">✎</button>
        </span>`;
      row.onclick = () => selectSession(sp.id, m.id);
      row.querySelector('[data-act="pin"]').onclick = (ev) => {
        ev.stopPropagation();
        api.patch(`/api/sessions/${m.id}`, { pinned: !m.pinned })
          .then(loadSpaces)
          .catch((e) => toast(e.message));
      };
      row.querySelector('[data-act="rename"]').onclick = (ev) => {
        ev.stopPropagation();
        startRename(row, m);
      };
      list.appendChild(row);
    }

    if (!state.showAll.has(sp.id)) {
      const more = document.createElement("div");
      more.className = "more";
      more.textContent = "查看全部";
      more.onclick = async () => {
        state.showAll.add(sp.id);
        try {
          const all = await api.get(`/api/spaces/${sp.id}/sessions?limit=50`);
          sp.sessions = all;
          sp._total = all.length;
          renderSpaces();
        } catch (e) { toast(`加载失败：${e.message}`); }
      };
      list.appendChild(more);
    }

    card.querySelector('[data-act="new-session"]').onclick = (ev) => {
      ev.stopPropagation();
      newSession(sp.id);
    };
    card.querySelector('[data-act="settings"]').onclick = (ev) => {
      ev.stopPropagation();
      openModal(sp);
    };
    card.querySelector('[data-act="close"]').onclick = async (ev) => {
      ev.stopPropagation();
      await api.patch(`/api/spaces/${sp.id}`, { opened: false }).catch((e) => toast(e.message));
      await loadSpaces();
    };
    box.appendChild(card);
  }
}

/* 行内重命名：标题换成输入框，回车保存 / Esc 取消。
   不用 prompt()：那是浏览器原生弹窗，出现在这个界面里很跳。 */
function startRename(row, meta) {
  const titleEl = row.querySelector(".title");
  if (row.querySelector("input")) return;
  const input = document.createElement("input");
  input.className = "rename";
  input.value = meta.title || "";
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const next = input.value.trim();
    if (save && next && next !== meta.title) {
      try {
        await api.patch(`/api/sessions/${meta.id}`, { title: next });
        meta.title = next;
      } catch (e) { toast(e.message); }
    }
    renderSpaces();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

/* 左栏的空间 + 指挥台的系统空间：后者左栏不显示，但点进调度会话时头部、日志、导出都要用它 */
function findSpace(id) {
  return state.spaces.find((s) => s.id === id)
    || (state.commandSpace && state.commandSpace.id === id ? state.commandSpace : null);
}

/* ────────────────────────────── 右栏头部 ────────────────────────────── */
function renderHeader() {
  const sp = findSpace(state.spaceId);
  const m = sp && (sp.sessions || []).find((x) => x.id === state.sessionId);
  $("ws-crumb").textContent = m ? `${sp.name} / ${m.title || "新会话"}` : (sp ? sp.name : "未选择会话");

  const badge = $("ws-badge");
  if (sp) {
    const [label, cls] = badgeFor(sp);
    badge.className = `badge ${cls}`;
    badge.textContent = label;
    badge.title =
      (sp.executor || "simpleagent") !== "simpleagent" && sp.permission === "full"
        ? "这个空间的外部 agent 不经确认就会改文件、跑命令"
        : "";
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }

  const dir = (sp && sp.cwd) || "";
  $("ws-dir").textContent = shortPath(dir, 46);
  $("ws-dir").title = dir || "";

  const v = (m && m.verification) || {};
  const vc = $("ws-verify");
  if (m && v.status && v.status !== "unknown") {
    const text = { passed: "已验证 ✓", failed: "未通过 ✗", stale: "已失效 ⚠", running: "验证中 ◐" }[v.status];
    vc.textContent = text || v.status;
    vc.className = `chip c-${v.status}`;
    vc.classList.remove("hidden");
  } else {
    vc.classList.add("hidden");
  }

  const u = (m && m.usage) || {};
  const total = (u.prompt_tokens || 0) + (u.completion_tokens || 0);
  const uc = $("ws-usage");
  if (total) {
    uc.textContent = `${total.toLocaleString()} tokens`;
    uc.className = "chip";
    uc.classList.remove("hidden");
  } else {
    uc.classList.add("hidden");
  }

  const locked = lockedBy(sp, m);
  $("btn-new-session").disabled = !sp;
  $("btn-rerun").disabled = !m || state.running || !!locked;
  $("btn-export").disabled = !m;
  $("btn-verify").disabled = !m;
  $("btn-stop").disabled = !state.running;
  $("input").disabled = !m || state.running || !!locked;
  $("btn-send").disabled = !m || state.running || !!locked;
  $("input").placeholder = locked
    ? `这个会话由 ${locked} 跑，空间已切到 ${sp.executor}：新建会话继续，或在 ⚙ 里把空间切回去`
    : "输入消息…（Enter 发送 · Shift+Enter 换行）";
  const ps = $("profile");
  const external = !!sp && (sp.executor || "simpleagent") !== "simpleagent";
  ps.disabled = !sp || external;
  if (external) {
    // 外部 agent 的模型由它自己的配置决定，这里没有可切的东西，别放假选项骗人
    ps.innerHTML = `<option value="">本机默认（不注入配置）</option>`;
    ps.title = "这个空间绑的是外部 agent，模型由它自己的配置决定";
  } else {
    ps.innerHTML = state.profiles.map((p) => `<option value="${p}">${p}</option>`).join("");
    if (sp) ps.value = sp.profile;
    ps.title = "内置执行者的模型 profile";
  }
}

/* ────────────────────────────── 消息流 ────────────────────────────── */
function clearStream() {
  const box = streamEl();
  box.innerHTML = "";
  state.toolCards.clear();
  state.acc = "";
  state.accEl = null;
  state.streamEl = null;
}

function addUserBubble(text) {
  const box = streamEl();
  $("empty-state")?.remove();
  const el = document.createElement("div");
  el.className = "msg user";
  el.innerHTML = `<div class="avatar">你</div><div class="body"><div class="who">用户</div><div class="text"></div></div>`;
  el.querySelector(".text").textContent = text;
  box.appendChild(el);
  scrollDown();
}

function ensureAssistantBubble() {
  if (state.accEl) return state.accEl;
  const box = streamEl();
  $("empty-state")?.remove();
  const el = document.createElement("div");
  el.className = "msg assistant";
  el.innerHTML = `<div class="avatar">AI</div><div class="body"><div class="who">助手</div><div class="text md"></div></div>`;
  box.appendChild(el);
  state.accEl = el.querySelector(".text");
  state.streamEl = el;
  return state.accEl;
}

/* 流式光标放进最后一个块的末尾（段落、列表项、代码块里），不要另起一行。
   链接、换行、分隔线这类元素里面放不了，停在它们外面 */
const CURSOR_STOP = new Set(["A", "BR", "HR", "INPUT"]);

function placeCursor(el) {
  let host = el;
  while (host.lastChild && host.lastChild.nodeType === Node.ELEMENT_NODE
    && !CURSOR_STOP.has(host.lastChild.tagName)) host = host.lastChild;
  host.insertAdjacentHTML("beforeend", '<span class="cursor">&nbsp;</span>');
}

/* 收尾时按完整文本再渲染一遍去掉光标：外部 CLI 执行者不一定先发 message_done 再发工具调用 */
function finishAssistantBubble() {
  if (state.accEl) state.accEl.innerHTML = renderMarkdown(state.acc);
  state.acc = "";
  state.accEl = null;
  state.streamEl = null;
}

function addToolCard(name, argsText, callId) {
  const box = streamEl();
  $("empty-state")?.remove();
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `
    <div class="card-head"><span class="name">${escapeHtml(name)}</span>
      <span class="args">${escapeHtml(shortPath(argsText, 60))}</span>
      <span class="tail">展开</span></div>
    <div class="card-body hidden"></div>`;
  const body = card.querySelector(".card-body");
  const tail = card.querySelector(".tail");
  card.querySelector(".card-head").onclick = () => {
    body.classList.toggle("hidden");
    tail.textContent = body.classList.contains("hidden") ? "展开" : "收起";
  };
  box.appendChild(card);
  if (callId) state.toolCards.set(callId, { card, body });
  scrollDown();
  return { card, body };
}

function addErrorCard(text) {
  const box = streamEl();
  $("empty-state")?.remove();
  const el = document.createElement("div");
  el.className = "card is-error";
  el.textContent = text;
  box.appendChild(el);
  scrollDown();
}

/* 调度者的跨空间计划（propose_plan 的审批参数）按步骤渲染；解析不了就原样显示 */
function planHtml(argsText) {
  let plan = null;
  try { plan = JSON.parse(argsText || "{}"); } catch { /* 原样显示 */ }
  if (!plan || !Array.isArray(plan.steps)) return `<div class="card-body">${escapeHtml(argsText || "")}</div>`;
  const steps = plan.steps.map((st) => `<li><b>${escapeHtml(st.space)}</b>：${escapeHtml(st.task)}${
    (st.after || []).length ? `<span class="after">（等第 ${escapeHtml(st.after.join("、"))} 步做完）</span>` : ""}</li>`).join("");
  return `<div class="plan">${plan.summary ? `<div class="plan-sum">${escapeHtml(plan.summary)}</div>` : ""}<ol>${steps}</ol></div>`;
}

function addApprovalCard(approvalId, toolName, argsText, reason) {
  const box = streamEl();
  const el = document.createElement("div");
  el.className = "card is-approval";
  el.dataset.approval = approvalId;
  // 计划每次都要人看：不给「始终允许」
  const isPlan = toolName === "propose_plan";
  el.innerHTML = `
    <div><b>${isPlan ? "确认执行计划" : "需要批准"}</b> · <code>${escapeHtml(toolName)}</code></div>
    ${reason ? `<div class="card-body">${escapeHtml(reason)}</div>` : ""}
    ${isPlan ? planHtml(argsText) : `<div class="card-body">${escapeHtml(argsText)}</div>`}
    <div style="margin-top:8px;display:flex;gap:6px">
      <button class="btn btn-primary" data-act="allow">${isPlan ? "确认执行" : "允许"}</button>
      ${isPlan ? "" : `<button class="btn" data-act="always">本次会话始终允许</button>`}
      <button class="btn" data-act="deny">拒绝</button>
    </div>`;
  el.querySelectorAll("button").forEach((b) => {
    b.onclick = async () => {
      try {
        await api.post(`/api/approvals/${approvalId}`, { action: b.dataset.act });
        el.querySelectorAll("button").forEach((x) => (x.disabled = true));
        el.querySelector("div").textContent = `已处理：${b.textContent}`;
      } catch (e) { toast(`提交失败：${e.message}`); }
    };
  });
  box.appendChild(el);
  scrollDown();
}

/* 只在用户本来就贴着底部时才自动滚动：否则翻看上面内容时会被流式输出一直拽回去 */
function scrollDown(force) {
  const box = streamEl();
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
  if (force || nearBottom) box.scrollTop = box.scrollHeight;
}

/* 历史消息 → 气泡。assistant 带 tool_calls 时画成工具卡，tool 消息回填到对应卡片。 */
function renderHistory(messages) {
  clearStream();
  const box = streamEl();
  if (!messages.length) {
    box.innerHTML = `<div class="empty"><div class="empty-title">新会话</div>
      <div class="empty-sub">在下面输入第一句话。</div></div>`;
    return;
  }
  for (const m of messages) {
    if (m.role === "user") {
      addUserBubble(typeof m.content === "string" ? m.content : JSON.stringify(m.content));
    } else if (m.role === "assistant") {
      if (m.content) {
        const el = document.createElement("div");
        el.className = "msg assistant";
        el.innerHTML = `<div class="avatar">AI</div><div class="body"><div class="who">助手</div>
          <div class="text md">${renderMarkdown(m.content)}</div></div>`;
        box.appendChild(el);
      }
      for (const tc of m.tool_calls || []) {
        const args = tc.function ? tc.function.arguments : "";
        addToolCard(tc.function ? tc.function.name : "tool", args, tc.id);
      }
    } else if (m.role === "tool") {
      const hit = state.toolCards.get(m.tool_call_id);
      const text = typeof m.content === "string" ? m.content : JSON.stringify(m.content);
      if (hit) {
        hit.body.textContent = text;
        hit.body.classList.remove("hidden");
      } else {
        addToolCard(m.name || "tool", "", m.tool_call_id).body.textContent = text;
      }
    }
  }
  scrollDown();
}

/* ────────────────────────────── SSE ────────────────────────────── */
function closeStream() {
  if (state.es) { state.es.close(); state.es = null; }
}

/* resume=true 是续传：保留 lastSeq，让服务端只重放它之后的帧。
   EventSource 没法自己设 Last-Event-ID 头，所以用 query 传。 */
function subscribe(sessionId, { resume = false, from = null } = {}) {
  closeStream();
  // from：历史已经画到这一帧了（GET /api/sessions/{id} 给的 seq），只要这之后的帧。
  // 不记的话，页面在后台时打开会话、切回前台续传会带 last_event_id=0，
  // 服务端把整段历史的帧重放一遍，叠在已经画好的历史上（审批卡还会带着能点的按钮）
  if (!resume) state.lastSeq = from ?? 0;
  // 后台标签页不占连接，等切回前台再连（见 boot 里的 visibilitychange）
  if (document.hidden) return;
  const query = resume || from !== null ? `?last_event_id=${state.lastSeq}` : "";
  const es = new EventSource(`/api/sessions/${sessionId}/events${query}`);
  state.es = es;
  for (const t of FRAME_TYPES) {
    es.addEventListener(t, (ev) => {
      try { onFrame(t, JSON.parse(ev.data)); } catch { /* 坏帧忽略 */ }
    });
  }
}

async function onFrame(type, frame) {
  // 收到任何一帧就说明服务端活着，撤掉「无响应」提醒
  if (state.noReplyTimer) { clearTimeout(state.noReplyTimer); state.noReplyTimer = null; }
  if (frame.seq && frame.seq <= state.lastSeq) return;  // 断线重放去重
  if (frame.seq) state.lastSeq = frame.seq;
  const p = frame.payload || {};

  if (type === "text_delta") {
    state.acc += p.text || "";
    const el = ensureAssistantBubble();
    el.innerHTML = renderMarkdown(state.acc);
    placeCursor(el);
    scrollDown();
  } else if (type === "reasoning_delta") {
    let d = state.streamEl && state.streamEl.querySelector("details.reasoning");
    if (!d) {
      ensureAssistantBubble();
      d = document.createElement("details");
      d.className = "reasoning";
      d.innerHTML = `<summary>思考过程</summary><div></div>`;
      state.streamEl.querySelector(".body").appendChild(d);
    }
    d.querySelector("div").textContent += p.text || "";
  } else if (type === "tool_call_start") {
    finishAssistantBubble();
    const args = typeof p.arguments === "string" ? p.arguments : JSON.stringify(p.arguments);
    addToolCard(p.name, args, p.call_id);
  } else if (type === "tool_result") {
    const hit = state.toolCards.get(p.call_id);
    if (hit) {
      hit.body.textContent = p.content || "";
      hit.body.classList.remove("hidden");
      hit.card.classList.toggle("is-error", !!p.is_error);
    } else {
      addToolCard(p.name || "tool", "", p.call_id).body.textContent = p.content || "";
    }
    scrollDown();
  } else if (type === "message_done") {
    finishAssistantBubble();
    if (p.usage) updateUsage(p.usage);
    await refreshSessions();
  } else if (type === "status") {
    state.running = p.status === "running" || p.status === "verifying";
    if (state.running) {
      if (!state.startedAt) state.startedAt = Date.now();
      $("status-hint").textContent = STATUS_TEXT[p.status] || p.status;
    } else {
      const sec = state.startedAt ? Math.floor((Date.now() - state.startedAt) / 1000) : null;
      state.startedAt = null;
      $("status-hint").textContent = sec !== null && p.status === "done"
        ? `${STATUS_TEXT[p.status]}，用时 ${fmtDur(sec)}`
        : (STATUS_TEXT[p.status] || p.status);
    }
    renderHeader();
    if (p.status !== "running") await refreshSessions();
  } else if (type === "error") {
    state.running = false;
    state.startedAt = null;
    addErrorCard(p.message || "出错了");
    renderHeader();
  } else if (type === "max_steps") {
    addErrorCard(`达到最大步数（${p.max_steps}）已停止。`);
  } else if (type === "approval_request") {
    addApprovalCard(p.approval_id, p.tool_name, p.arguments || "", p.reason);
  } else if (type === "verification") {
    await refreshSessions();
  } else if (type === "usage") {
    updateUsage(p);
  }
}

function updateUsage(u) {
  const total = (u.prompt_tokens || 0) + (u.completion_tokens || 0);
  if (!total) return;
  const el = $("ws-usage");
  el.textContent = `${total.toLocaleString()} tokens`;
  el.className = "chip";
  el.classList.remove("hidden");
}

/* ────────────────────────────── 交互 ────────────────────────────── */
async function loadSpaces() {
  state.spaces = await api.get("/api/spaces");
  await loadCommandSpace();
  renderSpaces();
  renderHeader();
}

async function loadCommandSpace() {
  if (!state.commandSpaceId) return;
  try {
    state.commandSpace = await api.get(`/api/spaces/${state.commandSpaceId}`);
  } catch { /* 拿不到只影响调度会话的头部显示 */ }
}

async function refreshSessions() {
  // 只刷左侧列表，不重画消息流（否则会打断正在看的上下文）
  try {
    const spaces = await api.get("/api/spaces");
    const merged = new Map(spaces.map((s) => [s.id, s]));
    state.spaces = state.spaces.map((s) => {
      const fresh = merged.get(s.id);
      if (!fresh) return s;
      return state.showAll.has(s.id) ? { ...s, ...fresh, sessions: s.sessions } : fresh;
    });
    await loadCommandSpace();
    renderSpaces();
    renderHeader();
  } catch { /* 刷新失败不影响当前会话 */ }
}

/* 补出「没收到那一帧」的审批卡：SSE 首次连接不重放，所以晚连上来的客户端
   （切到某个会话、或者控制面板下发的任务）必须主动拉一次待审批列表。 */
async function renderPendingApprovals(sessionId) {
  if (!sessionId) return;
  let pending = [];
  try {
    pending = (await api.get("/api/approvals")).pending || [];
  } catch { return; }
  for (const p of pending) {
    if (p.session_id !== sessionId) continue;
    if (document.querySelector(`[data-approval="${p.approval_id}"]`)) continue;
    addApprovalCard(p.approval_id, p.tool_name, p.arguments || "", p.reason);
  }
}

async function selectSession(spaceId, sessionId) {
  if (panelState.open) closePanel();
  const sp = findSpace(spaceId);
  const meta = sp && (sp.sessions || []).find((x) => x.id === sessionId);
  state.spaceId = spaceId;
  state.sessionId = sessionId;
  // 切回来时如果它其实还在跑（比如刷新了页面），按落盘的状态恢复「运行中」
  state.running = !!meta && meta.status === "running";
  state.startedAt = state.running ? Date.now() : null;
  $("status-hint").textContent = state.running ? "运行中…" : "";
  renderSpaces();
  renderHeader();
  let seq = null;
  try {
    const data = await api.get(`/api/sessions/${sessionId}`);
    state.messages = data.messages || [];
    renderHistory(state.messages);
    scrollDown(true);
    if (typeof data.seq === "number") seq = data.seq;
  } catch (e) {
    addErrorCard(`加载会话失败：${e.message}`);
  }
  subscribe(sessionId, { from: seq });
  await renderPendingApprovals(sessionId);
  // 停在非对话 tab 时切换会话，面板内容要跟着换
  if (state.tab && state.tab !== "chat") await switchTab(state.tab);
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({ spaceId, sessionId }));
  } catch { /* 隐私模式下写不了，忽略 */ }
}

/* ─────────────────────── 控制面板（W5） ─────────────────────── */
const DISPATCH_PLACEHOLDER =
  "要做什么？调度者会派给合适的空间，跨空间先给你看计划（@空间名 开头 = 直接下发）";
const panelState = {
  open: false,
  summary: null,
  dispatch: [],      // 指挥台任务卡：本页下发的 + 从后台恢复的近期任务
  inbox: [],
  inboxView: "active",  // active：当前 | archived：归档
  counts: { unread: 0, active: 0, archived: 0 },
  modalItem: null,      // 弹层里正在看的那条消息（带全文）
  todos: [],
  pollTimer: null,
  mentions: { open: false, items: [], index: 0, from: 0 },
  replyTo: null,        // 追问模式：卡片上点了「追问」，下一句直接发给这个会话，不经过调度者
};

async function openPanel() {
  panelState.open = true;
  panelState.inboxView = "active";  // 每次打开都先看当前消息，归档是要找东西时才去翻的
  $("ws-header").classList.add("hidden");
  $("tabs").classList.add("hidden");
  $("composer").classList.add("hidden");
  for (const id of Object.values(PANES)) $(id).classList.add("hidden");
  $("pane-panel").classList.remove("hidden");
  $("nav-panel").classList.add("is-active-nav");
  await loadPanel();
  if (!panelState.pollTimer) panelState.pollTimer = setInterval(pollDispatch, 3000);
}

function closePanel() {
  panelState.open = false;
  if (panelState.pollTimer) { clearInterval(panelState.pollTimer); panelState.pollTimer = null; }
  state.tab = "chat";  // 从面板跳走时总是回到对话视图：那里才有完整上下文
  $("ws-header").classList.remove("hidden");
  $("tabs").classList.remove("hidden");
  $("composer").classList.toggle("hidden", state.tab !== "chat");
  $("pane-panel").classList.add("hidden");
  for (const [key, id] of Object.entries(PANES)) $(id).classList.toggle("hidden", key !== state.tab);
  $("nav-panel").classList.remove("is-active-nav");
}

async function loadPanel() {
  const [summary, todos] = await Promise.all([
    api.get("/api/panel/summary"),
    api.get("/api/todos"),
    loadInbox(),
  ]);
  panelState.summary = summary;
  panelState.todos = todos;
  renderStats();
  renderTodos();
  await restoreDispatch(summary);
  renderDispatch();
}

function renderStats() {
  const s = panelState.summary || {};
  const cells = [
    ["运行中", s.running ? s.running.length : 0],
    ["未读消息", s.unread || 0],
    ["待办", s.todos || 0],
    ["累计 tokens", (s.today_tokens || 0).toLocaleString()],
  ];
  $("panel-stats").innerHTML = cells.map(([k, v]) =>
    `<div class="panel-stat"><div class="k">${k}</div><div class="v">${escapeHtml(String(v))}</div></div>`).join("");
}

/* ── 指挥台 ── */
/* 解析 `@空间名 任务描述`：先精确匹配，再前缀，最后包含匹配。
   没有 rest 就是「只查状态」，不下发。 */
function parseTarget(text) {
  const m = text.match(/@([^\s@]+)/);
  if (!m) return null;
  const name = m[1];
  const lower = name.toLowerCase();
  const space =
    state.spaces.find((s) => s.name === name)
    || state.spaces.find((s) => s.name.toLowerCase().startsWith(lower))
    || state.spaces.find((s) => s.name.toLowerCase().includes(lower));
  return { token: m[0], name, space, rest: text.replace(m[0], "").trim() };
}

/* 指挥台的卡片不只来自本页下发：刷新页面后、或在对话视图里跑的任务，靠 summary 的
   running + recent（24 小时内的终态）补回来。完成 / 取消不进消息，指挥台就是看它们当前
   状态的地方，不能刷新一下就没了。补回来的卡片各拉一次摘要，运行中的之后交给 pollDispatch。 */
async function restoreDispatch(summary) {
  const rows = [...(summary.running || []), ...(summary.recent || [])];
  // 已有的卡片：后台的 updated_at 变了，说明它又跑过一轮（被调度者追问、在对话里接着聊）。
  // 卡片要跟上：换到最近那个调度者下面、状态和最后一句刷新。只看「在不在跑」不够：
  // 追问可能在两次轮询之间就跑完了，卡片会一直停在上一轮
  const refresh = [];
  let touched = false;
  for (const r of rows) {
    const d = panelState.dispatch.find((x) => x.sessionId === r.session_id);
    if (!d) continue;
    const seen = d.updatedAt;
    d.updatedAt = r.updated_at;
    if (!d.done || seen === undefined || seen === r.updated_at) continue;
    touched = true;
    Object.assign(d, { parentId: cardParent(r), at: Date.parse(r.updated_at) || Date.now() });
    if (r.status === "running") {
      Object.assign(d, { done: false, startedAt: null, finishedAt: null });
      d.summary = { ...(d.summary || {}), status: "running", last_text: "" };
    } else {
      d.finishedAt = r.updated_at;
      refresh.push(d);
    }
  }
  await Promise.all(refresh.map(async (d) => {
    try { d.summary = await api.get(`/api/sessions/${d.sessionId}/summary`); } catch { /* 下一轮再试 */ }
  }));
  const known = new Set(panelState.dispatch.map((d) => d.sessionId));
  const fresh = rows
    .filter((r) => !known.has(r.session_id))
    .map((r) => ({
      spaceId: r.space_id,
      spaceName: r.space_name,
      sessionId: r.session_id,
      updatedAt: r.updated_at,
      parentId: cardParent(r),  // 调度者派出（或追问过）的子任务：卡片挂到它下面
      at: Date.parse(r.updated_at) || 0,
      startedAt: null,  // 后台只记了 updated_at，不知道这一轮从哪一刻开始
      done: r.status !== "running",
      finishedAt: r.status === "running" ? null : r.updated_at,
      summary: { title: r.title, status: r.status },
    }));
  if (!fresh.length) {
    if (touched) panelState.dispatch.sort((a, b) => b.at - a.at);
    return touched;
  }
  await Promise.all(fresh.map(async (d) => {
    try {
      d.summary = await api.get(`/api/sessions/${d.sessionId}/summary`);
    } catch { /* 拉不到摘要就只显示标题和状态 */ }
  }));
  // 等摘要的这段时间里，另一轮刷新或刚下发的任务可能已经把同一个会话放进来了
  const now = new Set(panelState.dispatch.map((d) => d.sessionId));
  panelState.dispatch.push(...fresh.filter((d) => !now.has(d.sessionId)));
  panelState.dispatch.sort((a, b) => b.at - a.at);
  return true;
}

/* 卡片挂在哪张调度卡下面：最近一次让它跑的调度者，没有就看它是谁派出来的 */
function cardParent(r) {
  return r.dispatched_by || r.parent_session_id || null;
}

/* 卡片第二行：终态写明「完成 / 已取消 / 出错」再跟摘要，状态不能只靠左边那条色带 */
function dispatchLine(d, cls) {
  if (d.approval) {
    return d.approval.tool_name === "propose_plan" ? "等你确认计划" : `等待批准 ${d.approval.tool_name}`;
  }
  if (cls === "running") return "运行中…";
  const head = STATUS_TEXT[cls] || cls;
  const line = (d.summary || {}).line;
  return line ? `${head} · ${line}` : head;
}

async function renderDispatch() {
  const box = $("dispatch-list");
  if (!panelState.dispatch.length) {
    box.innerHTML = `<div class="empty-sub" style="padding:8px">24 小时内没有任务。直接说要做什么，调度者会派给合适的空间；
      也可以「@${escapeHtml((state.spaces[0] || {}).name || "空间名")} 任务」跳过调度者直接下发</div>`;
    return;
  }
  // 调度者派出的子任务排在它那张卡下面；父卡不在列表里（超过 24 小时了）就当顶层显示
  const ids = new Set(panelState.dispatch.map((d) => d.sessionId));
  const kids = new Map();
  const top = [];
  for (const d of panelState.dispatch) {
    if (d.parentId && ids.has(d.parentId)) {
      if (!kids.has(d.parentId)) kids.set(d.parentId, []);
      kids.get(d.parentId).push(d);
    } else {
      top.push(d);
    }
  }
  const ordered = [];
  for (const d of top) {
    ordered.push([d, false]);
    for (const k of (kids.get(d.sessionId) || []).sort((a, b) => a.at - b.at)) ordered.push([k, true]);
  }
  box.innerHTML = ordered.map(([d, child], i) => {
    const s = d.summary || {};
    const ap = d.approval;
    const isPlan = ap && ap.tool_name === "propose_plan";
    const cls = ap ? "error" : (s.status || "idle");
    const when = d.finishedAt ? relTime(d.finishedAt)
      : d.startedAt ? fmtDur(Math.floor((Date.now() - d.startedAt) / 1000)) : "";
    const commander = d.spaceId === state.commandSpaceId;
    // 追问：跑完了、没在等审批、没被锁（空间切过执行者）的会话才能接着说
    const canReply = d.sessionId && !ap && cls !== "running" && !s.locked;
    const replying = panelState.replyTo && panelState.replyTo.sessionId === d.sessionId;
    return `<div class="dispatch ${cls}${child ? " child" : ""}${replying ? " replying" : ""}" data-i="${i}">
      <div class="row1"><span class="dot ${cls}"></span>
        ${child ? `<span class="arrow">↳</span>` : ""}
        <span class="who">${escapeHtml(d.spaceName)}</span>
        ${commander ? `<span class="badge b-cmd" title="调度者：自动选空间派发">调度</span>` : ""}
        <span class="when">${escapeHtml(when)}</span></div>
      <div class="row2">${escapeHtml(s.title || "新会话")} · ${escapeHtml(dispatchLine(d, cls))}</div>
      ${isPlan ? planHtml(ap.arguments) : ap ? `<div class="row3">${escapeHtml(ap.arguments || "")}</div>` : ""}
      ${ap ? `<div class="dispatch-actions">
          <button class="btn btn-mini" data-act="ap-allow">${isPlan ? "确认执行" : "允许"}</button>
          ${isPlan ? "" : `<button class="btn btn-mini" data-act="ap-always">始终允许</button>`}
          <button class="btn btn-mini" data-act="ap-deny">拒绝</button>
        </div>` : ""}
      ${!ap && s.last_text ? `<div class="row3">${escapeHtml(s.last_text)}</div>` : ""}
      ${canReply ? `<div class="dispatch-actions">
          <button class="btn btn-mini" data-act="reply"
            title="${commander ? "接着和这次的调度者说：它记得自己派过什么" : "直接对这个会话说，不经过调度者"}">追问</button>
        </div>` : ""}
    </div>`;
  }).join("");
  box.querySelectorAll(".dispatch").forEach((el) => {
    const d = ordered[Number(el.dataset.i)][0];
    el.querySelectorAll("[data-act^='ap-']").forEach((btn) => {
      btn.onclick = async (ev) => {
        ev.stopPropagation();
        await api.post(`/api/approvals/${d.approval.approval_id}`,
          { action: btn.dataset.act.replace("ap-", "") });
        d.approval = null;
        renderDispatch();
        toast("已处理");
      };
    });
    const reply = el.querySelector("[data-act='reply']");
    if (reply) {
      reply.onclick = (ev) => {
        ev.stopPropagation();
        setReplyTo(d);
      };
    }
    el.onclick = async () => {
      closePanel();
      await selectSession(d.spaceId, d.sessionId);
    };
  });
}

/* ── 追问模式：卡片上点「追问」，指挥台输入框的下一句直接发给那个会话 ── */
function setReplyTo(d) {
  const commander = d.spaceId === state.commandSpaceId;
  panelState.replyTo = {
    sessionId: d.sessionId,
    spaceId: d.spaceId,
    label: `${commander ? "调度者" : d.spaceName} · ${(d.summary || {}).title || "会话"}`,
  };
  renderReplyTo();
  renderDispatch();
  $("dispatch-input").focus();
}

function clearReplyTo() {
  if (!panelState.replyTo) return;
  panelState.replyTo = null;
  renderReplyTo();
  renderDispatch();
}

function renderReplyTo() {
  const box = $("dispatch-reply");
  const r = panelState.replyTo;
  box.classList.toggle("hidden", !r);
  $("dispatch-input").placeholder = r
    ? "接着对它说（它记得之前的上下文）；Esc 退出追问"
    : DISPATCH_PLACEHOLDER;
  if (!r) { box.innerHTML = ""; return; }
  box.innerHTML = `<span class="reply-label">追问 → ${escapeHtml(r.label)}</span>
    <button class="reply-x" title="退出追问（Esc）">✕</button>`;
  box.querySelector(".reply-x").onclick = clearReplyTo;
}

/* 追问：直接往那个会话发一句。它正在跑、被锁住时后端回 409，原因显示在输入框下面 */
async function sendReply(text) {
  const r = panelState.replyTo;
  const input = $("dispatch-input");
  input.value = "";
  $("dispatch-hint").textContent = "";
  try {
    await api.post(`/api/sessions/${r.sessionId}/input`, { text });
  } catch (e) {
    input.value = text;  // 没发出去：把原话还给输入框
    $("dispatch-hint").textContent = `追问失败：${e.message}`;
    return;
  }
  let d = panelState.dispatch.find((x) => x.sessionId === r.sessionId);
  if (!d) {
    d = { spaceId: r.spaceId, spaceName: r.label, sessionId: r.sessionId, parentId: null };
    panelState.dispatch.push(d);
  }
  Object.assign(d, { at: Date.now(), startedAt: Date.now(), done: false, finishedAt: null });
  d.summary = { ...(d.summary || {}), status: "running", last_text: "" };
  panelState.dispatch.sort((a, b) => b.at - a.at);
  panelState.replyTo = null;
  renderReplyTo();
  renderDispatch();
}

async function pollDispatch() {
  let changed = false;
  // 先看有没有卡在审批上的：面板上不显示的话，任务会一直挂着没人管
  let pending = [];
  try {
    pending = (await api.get("/api/approvals")).pending || [];
  } catch { /* 拿不到就当没有，下一轮再试 */ }
  for (const d of panelState.dispatch) {
    const ap = pending.find((p) => p.session_id === d.sessionId);
    if (ap && !d.approval) { d.approval = ap; changed = true; }
    if (!ap && d.approval) { d.approval = null; changed = true; }
    if (d.done) continue;
    try {
      const s = await api.get(`/api/sessions/${d.sessionId}/summary`);
      const wasStatus = (d.summary || {}).status;
      d.summary = s;
      // 追问过的会话会换一个调度者：卡片跟着挂过去
      if (d.spaceId !== state.commandSpaceId) d.parentId = cardParent(s);
      if (s.status !== "running" && s.status !== "idle") {
        d.done = true;
        d.finishedAt = new Date().toISOString();
        changed = true;  // 刚结束：顺带把新产生的系统消息拉进来
      } else if (wasStatus !== s.status) {
        changed = true;
      }
    } catch { /* 单个拉取失败不影响其它卡片 */ }
  }
  renderDispatch();
  if (changed) {
    await loadInbox();
    await loadStatsOnly();
    // 左栏的会话列表也跟着刷：调度者派出的子会话要出现在各自的空间下面
    await refreshSessions();
  } else if (panelState.dispatch.some((d) => !d.done && d.spaceId === state.commandSpaceId)) {
    // 调度者跑着的时候随时会派出新的子任务：刷一下 summary，把它们的卡片补进来
    await loadStatsOnly();
  }
}

async function loadInbox() {
  const view = panelState.inboxView;
  const [items, counts] = await Promise.all([
    api.get(`/api/inbox?view=${view}&limit=${view === "archived" ? 200 : 50}`),
    api.get("/api/inbox/count"),
  ]);
  panelState.inbox = items;
  applyCounts(counts);
  renderInbox();
}

async function loadStatsOnly() {
  panelState.summary = await api.get("/api/panel/summary");
  renderStats();
  if (await restoreDispatch(panelState.summary)) renderDispatch();
}

/* 下发一条：
   - 不以 @ 开头：交给指挥台的调度者，由它挑空间；跨空间时它会先出计划等你确认
   - `@空间名 任务`：跳过调度者，直接在那个空间新建 session 跑；只 `@空间名` 回一张状态卡 */
async function dispatch() {
  const input = $("dispatch-input");
  const text = input.value.trim();
  if (!text) return;
  if (panelState.replyTo) {
    await sendReply(text);
    return;
  }
  if (!text.startsWith("@")) {
    await dispatchToCommander(text);
    return;
  }
  const t = parseTarget(text);
  if (!t) {
    $("dispatch-hint").textContent = "@ 后面要跟空间名，例如 @临时整理 清理下载目录";
    return;
  }
  if (!t.space) {
    $("dispatch-hint").textContent = `没有叫「${t.name}」的空间`;
    return;
  }
  input.value = "";
  $("dispatch-hint").textContent = "";

  if (!t.rest) {
    // 只 @：查这个空间最近一个 session 的状态，不新建
    const metas = await api.get(`/api/spaces/${t.space.id}/sessions?limit=1`);
    const m = metas[0];
    panelState.dispatch.unshift({
      spaceId: t.space.id,
      spaceName: t.space.name,
      sessionId: m ? m.id : null,
      parentId: null,
      at: Date.now(),
      startedAt: null,
      done: !m || m.status !== "running",
      finishedAt: m ? m.updated_at : null,
      summary: m
        ? { title: m.title, status: m.status }
        : { title: "这个空间还没有会话", status: "idle" },
    });
    renderDispatch();
    return;
  }

  const meta = await api.post(`/api/spaces/${t.space.id}/sessions`, {});
  await api.post(`/api/sessions/${meta.id}/input`, { text: t.rest });
  // 上面两次请求之间，轮询可能已经把这个会话当成「运行中」恢复成卡片了
  panelState.dispatch = panelState.dispatch.filter((d) => d.sessionId !== meta.id);
  panelState.dispatch.unshift({
    spaceId: t.space.id,
    spaceName: t.space.name,
    sessionId: meta.id,
    parentId: null,
    at: Date.now(),
    startedAt: Date.now(),
    done: false,
    summary: { title: t.rest.slice(0, 40), status: "running" },
  });
  renderDispatch();
}

/* 交给调度者：在指挥台的系统空间新建一个会话，把原话发过去。选空间、拆步骤都是它的事 */
async function dispatchToCommander(text) {
  const input = $("dispatch-input");
  if (!state.commandSpaceId) {
    $("dispatch-hint").textContent = "后端没有调度者（sa serve 太旧），先用 @空间名 直接下发";
    return;
  }
  input.value = "";
  $("dispatch-hint").textContent = "";
  let meta;
  try {
    meta = await api.post(`/api/spaces/${state.commandSpaceId}/sessions`, {});
    await api.post(`/api/sessions/${meta.id}/input`, { text });
  } catch (e) {
    input.value = text;  // 没发出去：把原话还给输入框，免得重打
    $("dispatch-hint").textContent = `下发失败：${e.message}`;
    return;
  }
  panelState.dispatch = panelState.dispatch.filter((d) => d.sessionId !== meta.id);
  panelState.dispatch.unshift({
    spaceId: state.commandSpaceId,
    spaceName: (state.commandSpace || {}).name || "指挥台",
    sessionId: meta.id,
    parentId: null,
    at: Date.now(),
    startedAt: Date.now(),
    done: false,
    summary: { title: text.slice(0, 40), status: "running" },
  });
  renderDispatch();
}

/* ── 消息 ── */
const SOURCE_LABEL = { system: "系统", schedule: "定时", mail: "邮件", cli: "脚本", manual: "手动" };
const LEVEL_LABEL = { info: "信息", success: "成功", warn: "注意", error: "错误" };
const INBOX_POLL_MS = 30000;

const sourceLabel = (src) => SOURCE_LABEL[src] || src || "未知";

/* 已读的消息离归档还有多久；已归档的显示归档时间。口径在服务端，这里只是换成人话 */
function archiveHint(m) {
  if (m.archived) return m.archive_at ? `归档于 ${relTime(m.archive_at)}` : "";
  if (!m.archive_at) return "";
  const min = Math.ceil((new Date(m.archive_at).getTime() - Date.now()) / 60000);
  return min <= 1 ? "即将归档" : `${min} 分钟后归档`;
}

/* 未读数同时喂给左栏角标和消息卡头部 */
function applyCounts(c) {
  panelState.counts = c;
  const n = c.unread || 0;
  const badge = $("nav-panel-badge");
  badge.textContent = n > 99 ? "99+" : String(n);
  badge.classList.toggle("hidden", n === 0);
  $("inbox-unread").textContent = String(n);
  $("inbox-archived-n").textContent = c.archived ? ` ${c.archived}` : "";
}

async function refreshBadge() {
  applyCounts(await api.get("/api/inbox/count"));
}

/* 角标和消息列表共用一个 30 秒的钟：面板开着就连列表一起刷（到期的消息由服务端判定，
   刷一下就自然挪进归档），没开只刷角标。后台标签页跳过，切回前台时补一次。 */
function pollInbox() {
  if (document.hidden) return;
  const job = panelState.open ? Promise.all([loadInbox(), loadStatsOnly()]) : refreshBadge();
  job.catch(() => { /* 拿不到就下一轮再试 */ });
}

function renderInbox() {
  const box = $("inbox-list");
  const archivedView = panelState.inboxView === "archived";
  document.querySelectorAll("#inbox-view .seg-item").forEach((el) =>
    el.classList.toggle("is-active", el.dataset.view === panelState.inboxView));
  if (!panelState.inbox.length) {
    box.innerHTML = `<div class="empty-sub" style="padding:8px">${archivedView
      ? "归档是空的。消息点开一段时间后会自动移到这里。" : "暂时没有消息。"}</div>`;
    return;
  }
  box.innerHTML = panelState.inbox.map((m) => `
    <div class="inbox-item ${m.read ? "is-read" : "is-unread"}" data-id="${escapeHtml(m.id)}">
      <span class="inbox-bar ${escapeHtml(m.level)}"></span>
      <div class="inbox-main">
        <div class="t"><span class="inbox-src">${escapeHtml(sourceLabel(m.source))}</span>${escapeHtml(m.title)}</div>
        ${m.preview ? `<div class="b">${escapeHtml(m.preview)}</div>` : ""}
      </div>
      <div class="inbox-side">
        <span class="w">${escapeHtml(relTime(m.ts))}</span>
        <span class="inbox-hint">${escapeHtml(archiveHint(m))}</span>
        <span class="inbox-acts">
          <span class="todo-tag" data-act="todo" title="转为备忘">+备忘</span>
          ${archivedView ? "" : `<span class="todo-tag" data-act="archive" title="不等倒计时，直接归档">归档</span>`}
        </span>
      </div>
    </div>`).join("");

  box.querySelectorAll(".inbox-item").forEach((el) => {
    const item = panelState.inbox.find((m) => m.id === el.dataset.id);
    el.querySelector('[data-act="todo"]').onclick = (ev) => {
      ev.stopPropagation();
      messageToTodo(item).catch((e) => toast(e.message));
    };
    const arch = el.querySelector('[data-act="archive"]');
    if (arch) {
      arch.onclick = (ev) => {
        ev.stopPropagation();
        archiveMessage(item.id).catch((e) => toast(e.message));
      };
    }
    el.onclick = () => openMessage(item).catch((e) => toast(e.message));
  });
}

/* 点一条消息：先记已读（归档倒计时从这一刻开始），再按 action 分流——
   框架内的会话直接跳过去；外部发来的文本在弹层里看全文。 */
async function openMessage(item) {
  if (!item.read) {
    const r = await api.post(`/api/inbox/${item.id}/read`, {});
    Object.assign(item, r.item);
    renderInbox();
    refreshBadge().catch(() => {});
    loadStatsOnly().catch(() => {});
  }
  if (item.action !== "session") return openMessageModal(item.id);
  const { space_id: spaceId, session_id: sessionId } = item.ref;
  try {
    await api.get(`/api/sessions/${sessionId}/summary`);
  } catch (e) {
    if (!/not found/.test(e.message)) throw e;
    return openMessageModal(item.id, "这条消息指向的会话已经不存在了（空间可能被删了），下面是消息原文。");
  }
  closePanel();
  await selectSession(spaceId, sessionId);
}

async function openMessageModal(id, note = "") {
  const m = await api.get(`/api/inbox/${id}`);
  panelState.modalItem = m;
  $("mm-title").textContent = m.title;
  const when = m.ts ? new Date(m.ts).toLocaleString("zh-CN", { hour12: false }) : "";
  $("mm-meta").textContent = [sourceLabel(m.source), when, LEVEL_LABEL[m.level] || m.level,
    archiveHint(m) || (m.archived ? "已归档" : "")].filter(Boolean).join(" · ");
  $("mm-note").textContent = note;
  $("mm-note").classList.toggle("hidden", !note);
  $("mm-body").innerHTML = m.body ? renderText(m.body) : "";
  $("mm-body").scrollTop = 0;
  // 只放行 http(s)：ref.url 来自外部投递，javascript: 之类的链接不能变成可点的按钮
  const url = (m.ref || {}).url || "";
  const safe = /^https?:\/\//i.test(url);
  $("mm-link").classList.toggle("hidden", !safe);
  if (safe) $("mm-link").href = url;
  $("mm-archive").classList.toggle("hidden", m.archived);
  $("msg-modal").classList.remove("hidden");
}

function closeMessageModal() {
  $("msg-modal").classList.add("hidden");
  panelState.modalItem = null;
}

async function archiveMessage(id) {
  await api.post(`/api/inbox/${id}/archive`, {});
  await Promise.all([loadInbox(), loadStatsOnly()]);
  toast("已归档");
}

/* 转备忘：会话消息存成可跳转的 session 备忘，外部文本就是一条普通备忘 */
async function messageToTodo(item) {
  const isSession = item.action === "session";
  await api.post("/api/todos", {
    text: item.title,
    kind: isSession ? "session" : "text",
    ref: isSession ? item.ref : {},
  });
  await loadPanel();
  toast("已加入备忘");
}

/* ── 备忘 ── */
function renderTodos() {
  const box = $("todo-list");
  if (!panelState.todos.length) {
    box.innerHTML = `<div class="empty-sub" style="padding:6px">还没有备忘。</div>`;
    return;
  }
  box.innerHTML = panelState.todos.map((t) => `
    <div class="todo-item ${t.done ? "is-done" : ""}" data-id="${escapeHtml(t.id)}">
      <span class="todo-check" data-act="toggle"></span>
      <span class="todo-text">${escapeHtml(t.text)}</span>
      ${t.kind === "session" ? `<span class="todo-tag" data-act="goto">跳到会话</span>` : ""}
      <span class="todo-del" data-act="del">×</span>
    </div>`).join("");

  box.querySelectorAll(".todo-item").forEach((el) => {
    const item = panelState.todos.find((t) => t.id === el.dataset.id);
    el.querySelector('[data-act="toggle"]').onclick = async () => {
      await api.patch(`/api/todos/${item.id}`, { done: !item.done });
      await loadPanel();
    };
    el.querySelector('[data-act="del"]').onclick = async () => {
      await fetch(`/api/todos/${item.id}`, { method: "DELETE" });
      await loadPanel();
    };
    const goto = el.querySelector('[data-act="goto"]');
    if (goto) {
      goto.onclick = async () => {
        const spid = (item.ref || {}).space_id;
        const sid = (item.ref || {}).session_id;
        if (spid && sid) { closePanel(); await selectSession(spid, sid); }
      };
    }
  });
}

async function addTodoInline() {
  const box = $("todo-list");
  if (box.querySelector("input")) return;
  const input = document.createElement("input");
  input.className = "rename";
  input.placeholder = "写点什么，回车保存";
  box.prepend(input);
  input.focus();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const text = input.value.trim();
    if (save && text) await api.post("/api/todos", { text });
    await loadPanel();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

/* ── @ 补全 ── */
function updateMentions() {
  const ta = $("dispatch-input");
  const host = $("dispatch-hint").parentElement;
  host.querySelectorAll(".mentions").forEach((el) => el.remove());
  const upto = ta.value.slice(0, ta.selectionStart);
  const m = upto.match(/@([^\s@]*)$/);
  if (!m) { panelState.mentions.open = false; return; }

  const q = m[1].toLowerCase();
  const hits = state.spaces.filter((s) => s.name.toLowerCase().includes(q)).slice(0, 8);
  if (!hits.length) { panelState.mentions.open = false; return; }

  panelState.mentions = { open: true, items: hits, index: 0, from: upto.length - m[0].length };
  host.style.position = "relative";
  const menu = document.createElement("div");
  menu.className = "mentions";
  menu.innerHTML = hits.map((s, i) => `
    <div class="mention ${i === 0 ? "is-active" : ""}" data-i="${i}">
      <span class="badge ${badgeFor(s)[1]}">${badgeFor(s)[0]}</span>
      ${escapeHtml(s.name)}
    </div>`).join("");
  host.appendChild(menu);
  menu.querySelectorAll(".mention").forEach((el) => {
    el.onmousedown = (ev) => { ev.preventDefault(); applyMention(Number(el.dataset.i)); };
  });
}

function applyMention(i) {
  const hit = panelState.mentions.items[i];
  if (!hit) return;
  const ta = $("dispatch-input");
  const before = ta.value.slice(0, panelState.mentions.from);
  const after = ta.value.slice(ta.selectionStart);
  ta.value = `${before}@${hit.name} ${after}`;
  panelState.mentions.open = false;
  ta.focus();
  updateMentions();
}

const PANES = { chat: "pane-chat", changes: "pane-changes", files: "pane-files", logs: "pane-logs" };

async function switchTab(name) {
  state.tab = name;
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("is-active", t.dataset.tab === name);
  });
  for (const [key, id] of Object.entries(PANES)) $(id).classList.toggle("hidden", key !== name);
  $("composer").classList.toggle("hidden", name !== "chat");
  // 按需渲染：切过去时才拉最新数据，不必每收到一帧就重算
  if (name === "changes") await renderChanges();
  if (name === "files") await renderFiles();
  if (name === "logs") await renderLogs();
}

/* 变更：把消息流里 write_file / edit_file 的调用和它的结果配对 */
function collectChanges(messages) {
  const results = new Map();
  for (const m of messages) if (m.role === "tool") results.set(m.tool_call_id, m.content || "");
  const changes = [];
  for (const m of messages) {
    for (const tc of m.tool_calls || []) {
      const name = (tc.function || {}).name;
      if (name !== "write_file" && name !== "edit_file") continue;
      let args = {};
      try { args = JSON.parse((tc.function || {}).arguments || "{}"); } catch { args = {}; }
      changes.push({ name, path: args.path || "?", result: results.get(tc.id) || "" });
    }
  }
  return changes;
}

async function latestMessages() {
  if (!state.sessionId) return [];
  const data = await api.get(`/api/sessions/${state.sessionId}`);
  state.messages = data.messages || [];
  return state.messages;
}

async function renderChanges() {
  const pane = $(PANES.changes);
  if (!state.sessionId) {
    pane.innerHTML = `<div class="empty"><div class="empty-sub">先选一个会话。</div></div>`;
    return;
  }
  let changes;
  try {
    changes = collectChanges(await latestMessages());
  } catch (e) {
    pane.innerHTML = `<div class="card is-error">加载失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  const badge = $("changes-count");
  badge.textContent = changes.length ? ` ${changes.length}` : "";
  badge.classList.toggle("hidden", !changes.length);
  if (!changes.length) {
    pane.innerHTML = `<div class="empty"><div class="empty-title">没有文件变更</div>
      <div class="empty-sub">这个会话还没有写过文件。</div></div>`;
    return;
  }
  pane.innerHTML = changes.map((c, i) => `
    <div class="card">
      <div class="card-head"><span class="name">${c.name === "write_file" ? "写入" : "修改"}</span>
        <span class="args">${escapeHtml(c.path)}</span><span class="tail">#${i + 1}</span></div>
      <div class="card-body">${escapeHtml(c.result.slice(0, 4000)) || "（无输出）"}</div>
    </div>`).join("");
}

async function renderFiles() {
  const pane = $(PANES.files);
  if (!state.spaceId) {
    pane.innerHTML = `<div class="empty"><div class="empty-sub">先选一个空间。</div></div>`;
    return;
  }
  let data;
  try {
    data = await api.get(`/api/spaces/${state.spaceId}/files`);
  } catch (e) {
    pane.innerHTML = `<div class="card is-error">加载失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  const render = (nodes) => `<ul class="tree">${nodes.map((n) => `
      <li><span class="tree-node ${n.type}">${n.type === "dir" ? "▸" : "·"} ${escapeHtml(n.name)}</span>
      ${n.children && n.children.length ? render(n.children) : ""}</li>`).join("")}</ul>`;
  pane.innerHTML = `<div class="ws-dir" style="margin-bottom:6px">${escapeHtml(data.cwd)}</div>
    ${data.tree.length ? render(data.tree) : `<div class="empty-sub">目录是空的。</div>`}`;
}

async function renderLogs() {
  const pane = $(PANES.logs);
  if (!state.sessionId) {
    pane.innerHTML = `<div class="empty"><div class="empty-sub">先选一个会话。</div></div>`;
    return;
  }
  await refreshSessions();  // 拿最新的 usage / verification
  const sp = findSpace(state.spaceId);
  const m = sp && (sp.sessions || []).find((x) => x.id === state.sessionId);
  if (!m) {
    pane.innerHTML = `<div class="empty"><div class="empty-sub">找不到这个会话。</div></div>`;
    return;
  }
  const u = m.usage || {};
  const v = m.verification || {};
  const bad = state.messages.filter(
    (x) => x.role === "tool" && /error|traceback/i.test(String(x.content))).length;
  pane.innerHTML = `
    <div class="card"><div class="card-head"><span class="name">会话</span></div>
      <div class="card-body">id：${escapeHtml(m.id)}
创建：${escapeHtml(m.created_at || "-")}
更新：${escapeHtml(m.updated_at || "-")}
状态：${escapeHtml(STATUS_TEXT[m.status] || m.status)}
消息：${state.messages.length} 条（其中疑似报错的工具结果 ${bad} 条）</div></div>
    <div class="card"><div class="card-head"><span class="name">用量</span></div>
      <div class="card-body">prompt：${(u.prompt_tokens || 0).toLocaleString()}
completion：${(u.completion_tokens || 0).toLocaleString()}
缓存命中：${(u.cached_tokens || 0).toLocaleString()}
思考：${(u.reasoning_tokens || 0).toLocaleString()}</div></div>
    <div class="card"><div class="card-head"><span class="name">验证</span>
        <span class="tail">${(VMARK[v.status] || VMARK.unknown)[0]} ${escapeHtml(v.status || "unknown")}</span></div>
      <div class="card-body">命令：${escapeHtml(v.command || "（未配置）")}
来源：${escapeHtml(v.source || "-")}　退出码：${v.exit_code === null || v.exit_code === undefined ? "-" : v.exit_code}
${v.finished_at ? "完成于：" + escapeHtml(v.finished_at) + "\n" : ""}${v.output ? "输出：\n" + escapeHtml(v.output) : ""}</div></div>`;
}

/* 重跑：用最后一条用户消息再跑一次（改了 prompt 或换了模型后想重试） */
async function rerun() {
  if (!state.sessionId) return;
  try {
    const r = await api.post(`/api/sessions/${state.sessionId}/rerun`, {});
    state.running = true;
    state.startedAt = Date.now();
    $("status-hint").textContent = "重跑中…";
    renderHeader();
    toast(`已重跑：${String(r.text).slice(0, 20)}`);
  } catch (e) {
    toast(`重跑失败：${e.message}`);
  }
}

function exportMarkdown() {
  const sp = findSpace(state.spaceId);
  const m = sp && (sp.sessions || []).find((x) => x.id === state.sessionId);
  if (!m) return;
  const lines = [`# ${m.title || "会话"}`, "", `> 空间：${sp.name}　导出时间：${new Date().toLocaleString("zh-CN")}`, ""];
  for (const msg of state.messages) {
    if (msg.role === "user") {
      lines.push("", "## 用户", "", typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content));
    } else if (msg.role === "assistant") {
      if (msg.content) lines.push("", "## 助手", "", msg.content);
      for (const tc of msg.tool_calls || []) {
        const f = tc.function || {};
        lines.push("", `### 工具调用：${f.name || "tool"}`, "", "```", f.arguments || "", "```");
      }
    } else if (msg.role === "tool") {
      lines.push("", `#### 工具结果：${msg.name || "tool"}`, "", "```",
        typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content), "```");
    }
  }
  const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${(m.title || "会话").replace(/[\\/:*?"<>|]/g, "_")}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
  toast("已导出 Markdown");
}

/* 斜杠命令：/help、/verify、/model 在前端处理；/技能名 原样发给后端，由 runner 展开成技能全文 */
const WEB_COMMANDS = [
  { name: "help", args: "", desc: "显示可用的命令和技能" },
  { name: "verify", args: "", desc: "跑空间里配置的验证命令" },
  { name: "model", args: "<profile>", desc: "切换这个空间的模型" },
];

/* 这个空间能用 /技能名 调的技能。有缓存直接用；fresh=true 时重新拉（技能目录可能改过） */
async function skillsFor(spaceId, fresh = false) {
  if (!spaceId) return [];
  if (!fresh && state.skills[spaceId]) return state.skills[spaceId];
  try {
    state.skills[spaceId] = (await api.get(`/api/spaces/${spaceId}/commands`)).skills || [];
  } catch {
    // 拉不到就沿用上次的（没有就当空）：菜单里少几项，不影响别的
    state.skills[spaceId] = state.skills[spaceId] || [];
  }
  return state.skills[spaceId];
}

/* 返回 true：在前端处理掉了；false：不是前端命令，交给后端当普通消息发（目前就是 /技能名） */
async function runCommand(text) {
  const [cmd, ...rest] = text.slice(1).split(/\s+/);
  const arg = rest.join(" ").trim();
  if (cmd === "help" || cmd === "") {
    const skills = await skillsFor(state.spaceId);
    const lines = ["可用命令：",
      ...WEB_COMMANDS.map((c) => `/${c.name}${c.args ? ` ${c.args}` : ""} —— ${c.desc}`),
      `（/model 当前可选：${state.profiles.join(" / ")}）`];
    if (skills.length) {
      lines.push("", "技能（/<技能名> [补充说明]）：",
        ...skills.map((s) => `/${s.name} —— ${s.description.length > 80 ? `${s.description.slice(0, 80)}…` : s.description}`));
    }
    lines.push("", "输入 / 弹出补全菜单：↑↓ 选择，Tab 补全，Enter 选中，Esc 关闭。");
    addNoteCard(lines.join("\n"));
    return true;
  }
  if (cmd === "verify") {
    if (!state.sessionId) return true;
    toast("已提交验证命令");
    try { await api.post(`/api/sessions/${state.sessionId}/verify`, {}); }
    catch (e) { toast(`验证失败：${e.message}`); }
    return true;
  }
  if (cmd === "model") {
    if (!arg) { toast("用法：/model <profile>"); return true; }
    if (!state.profiles.includes(arg)) { toast(`没有这个 profile：${arg}`); return true; }
    const cur = findSpace(state.spaceId);
    if (cur && (cur.executor || "simpleagent") !== "simpleagent") {
      toast("这个空间绑的是外部 agent，模型由它自己的配置决定");
      return true;
    }
    try {
      await api.patch(`/api/spaces/${state.spaceId}`, { profile: arg });
      await loadSpaces();
      toast(`已切换到 ${arg}`);
    } catch (e) { toast(e.message); }
    return true;
  }
  if ((await skillsFor(state.spaceId)).some((s) => s.name === cmd)) return false;
  toast(`未知命令 /${cmd}，输入 /help 看可用的`);
  return true;
}

/* ── / 补全菜单 ── 候选怎么算在 slash.js，这里只管画和按键 */
const slash = { open: false, items: [], index: 0 };

/* input 事件：刚输入 / 时顺手重新拉一次技能，回来后按那时的输入重画 */
function updateSlash() {
  const ta = $("input");
  if (ta.value.slice(0, ta.selectionStart) === "/" && state.spaceId) {
    const spaceId = state.spaceId;
    skillsFor(spaceId, true).then(() => {
      if (state.spaceId === spaceId && document.activeElement === ta) refreshSlash();
    });
  }
  refreshSlash();
}

function refreshSlash() {
  const ta = $("input");
  const sp = findSpace(state.spaceId);
  const menu = ta.disabled ? null : slashMenu(ta.value.slice(0, ta.selectionStart), {
    commands: WEB_COMMANDS,
    skills: state.skills[state.spaceId] || [],
    profiles: state.profiles,
    current: sp ? sp.profile : "",
  });
  slash.open = !!menu;
  slash.items = menu ? menu.items : [];
  slash.index = 0;
  drawSlash();
}

function closeSlash() {
  slash.open = false;
  drawSlash();
}

function drawSlash() {
  $("composer").querySelector(".slash-menu")?.remove();
  if (!slash.open) return;
  const box = document.createElement("div");
  box.className = "mentions slash-menu";
  box.innerHTML = slash.items.map((it, i) => `
    <div class="mention slash-item ${i === slash.index ? "is-active" : ""}" data-i="${i}"
      title="${escapeHtml(it.desc)}">
      <span class="slash-label">${escapeHtml(it.label)}</span>
      ${it.hint ? `<span class="slash-hint">${escapeHtml(it.hint)}</span>` : ""}
      ${it.tag ? `<span class="badge">${escapeHtml(it.tag)}</span>` : ""}
      <span class="slash-desc">${escapeHtml(it.desc)}</span>
    </div>`).join("");
  $("composer").appendChild(box);
  box.querySelectorAll(".slash-item").forEach((el) => {
    // mousedown + preventDefault：不让输入框先失焦（失焦会关菜单，click 就落空了）
    el.onmousedown = (ev) => { ev.preventDefault(); applySlash(Number(el.dataset.i), true); };
  });
  box.querySelector(".is-active")?.scrollIntoView({ block: "nearest" });
}

/* 选中一项：光标前的文字换成它。Enter / 点击选的、又不用再补参数的（/help、某个 profile）直接发送 */
function applySlash(i, submit) {
  const it = slash.items[i];
  if (!it) return;
  const ta = $("input");
  ta.value = it.value + ta.value.slice(ta.selectionEnd);
  ta.selectionStart = ta.selectionEnd = it.value.length;
  ta.focus();
  autoGrow();
  if (submit && it.submit) { closeSlash(); send(); return; }
  refreshSlash();  // 选了 /model 之后接着列 profile
}

/* 输入框的按键：菜单开着时 ↑↓ / Tab / Enter / Esc 归菜单，其余照常 */
function onInputKeydown(e) {
  if (e.isComposing) return;  // 输入法正在选词，这时的 Enter 是确认候选字，不是发送
  if (slash.open) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = slash.items.length;
      slash.index = (slash.index + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
      drawSlash();
      return;
    }
    if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
      e.preventDefault();
      applySlash(slash.index, e.key === "Enter");
      return;
    }
    if (e.key === "Escape") { e.preventDefault(); closeSlash(); return; }
  }
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
}

function addNoteCard(text) {
  const box = streamEl();
  $("empty-state")?.remove();
  const el = document.createElement("div");
  el.className = "card";
  el.style.whiteSpace = "pre-wrap";
  el.textContent = text;
  box.appendChild(el);
  scrollDown(true);
}

function autoGrow() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = `${Math.min(t.scrollHeight, 200)}px`;
}

async function newSession(spaceId) {
  const meta = await api.post(`/api/spaces/${spaceId}/sessions`, {});
  await loadSpaces();
  await selectSession(spaceId, meta.id);
}

async function send() {
  const text = $("input").value.trim();
  if (!text || !state.sessionId) return;
  $("input").value = "";
  autoGrow();
  closeSlash();
  // /技能名 前端不处理，和普通消息一样发出去
  if (text.startsWith("/") && await runCommand(text)) return;
  addUserBubble(text);
  state.running = true;
  state.startedAt = Date.now();
  $("status-hint").textContent = "已提交，等待运行…";
  renderHeader();
  // 兜底：服务进程挂了或请求根本没跑起来时，别让用户一直干等
  clearTimeout(state.noReplyTimer);
  state.noReplyTimer = setTimeout(() => {
    if (!state.running) return;
    state.running = false;
    state.startedAt = null;
    $("status-hint").textContent = "没有收到任何响应，检查 sa serve 是否还在运行（或看服务日志）";
    renderHeader();
  }, 15000);
  try {
    await api.post(`/api/sessions/${state.sessionId}/input`, { text });
  } catch (e) {
    state.running = false;
    addErrorCard(`发送失败：${e.message}`);
    renderHeader();
  }
}

/* ────────────────────────────── 新建空间向导 ────────────────────────────── */
/* 同一个弹窗两种用法：不传 sp 是新建；传 sp 是「空间设置」，只能改名称和「谁跑」那几项，
   「在哪儿跑」（形态、目录）置灰，验证命令暂不开放编辑。 */
function openModal(sp) {
  const editing = sp && sp.id ? sp : null;
  state.editingSpace = editing;
  $("modal").classList.remove("hidden");
  $("modal-title").textContent = editing ? "空间设置" : "新建空间";
  $("f-create").textContent = editing ? "保存" : "创建";
  $("f-kind").disabled = !!editing;
  $("f-cwd").disabled = !!editing;
  $("f-verify-wrap").classList.toggle("hidden", !!editing);
  // 简介只在设置里填：刚建的空间没有会话、多半也没读到东西，自动生成没什么可写的
  $("f-desc-wrap").classList.toggle("hidden", !editing);
  $("f-desc").value = editing ? (editing.description || "") : "";
  $("modal-err").textContent = "";
  if (editing) {
    $("f-name").value = editing.name;
    $("f-kind").value = editing.kind;
    $("f-cwd").value = editing.cwd || "";
    $("f-executor").value = editing.executor || "simpleagent";
    fillExecutorDependents();
    if ($("f-executor").value === "simpleagent") {
      if (state.profiles.includes(editing.profile)) $("f-profile").value = editing.profile;
    } else {
      $("f-permission").value = editing.permission || "safe";
    }
  } else {
    $("f-name").value = "";
    $("f-cwd").value = "";
    $("f-verify").value = "";
  }
  syncModalFields();
  $("f-name").focus();
}

/* 执行者名单由 /api/meta 给，启动时填一次；之后再打开向导不重填，保留上次的选择。 */
function fillExecutorOptions() {
  $("f-executor").innerHTML = state.executors
    .map((e) => `<option value="${escapeHtml(e.name)}">${escapeHtml(e.label)}</option>`)
    .join("");
  fillExecutorDependents();
}

/* 模型、权限两个下拉的选项取决于执行者，只在执行者变了时重建。
   形态、权限自己变动时不重建，不然刚选的「全放行」会被悄悄换回默认档。 */
function fillExecutorDependents() {
  const executor = $("f-executor").value;
  const sel = $("f-profile");
  if (executor !== "simpleagent") {
    // 外部 CLI 本次只支持「本机默认」：不注入任何 env/args，用它自己的配置
    sel.innerHTML = `<option value="">本机默认（不注入配置）</option>`;
  } else {
    sel.innerHTML = state.profiles
      .map((p) => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join("");
    sel.value = state.defaultProfile || state.profiles[0] || "";
  }

  const perm = $("f-permission");
  const info = state.executors.find((e) => e.name === executor);
  const perms = (info && info.permissions) || [];
  perm.innerHTML = perms
    .map((p) => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.label)}</option>`).join("");
  if (perms.length) perm.value = info.default_permission || perms[0].name;
}

/* 两个维度各管各的：形态只管工作目录，执行者只管模型、权限那几栏怎么显示。
   这里只切显隐和文案，不动下拉的选项，所以切来切去不会把上次选的东西换掉。 */
function syncModalFields() {
  const kind = $("f-kind").value;
  const external = $("f-executor").value !== "simpleagent";
  $("f-cwd-wrap").classList.toggle("hidden", kind !== "agent");
  $("f-model-hint").classList.toggle("hidden", !external);
  $("f-perm-wrap").classList.toggle("hidden", !external);
  $("f-model-label").textContent = external ? "模型" : "模型 profile";
  $("f-perm-hint").classList.toggle("hidden", !external || $("f-permission").value !== "full");
  const ed = state.editingSpace;
  $("f-switch-hint").classList.toggle(
    "hidden", !ed || (ed.executor || "simpleagent") === $("f-executor").value);
}

/* 「谁跑」那几项的请求体，新建和空间设置共用 */
function executorFields() {
  const executor = $("f-executor").value;
  const body = { executor };
  if (executor === "simpleagent") {
    body.profile = $("f-profile").value;
  } else {
    // 本机默认 = 不传 cli_model；以后有 preset 时这里换成选中的 preset 名
    const cliModel = $("f-profile").value;
    if (cliModel) body.cli_model = cliModel;
    body.permission = $("f-permission").value;
  }
  return body;
}

async function saveSpaceSettings() {
  const sp = state.editingSpace;
  const name = $("f-name").value.trim();
  if (!name) { $("modal-err").textContent = "名称不能为空"; return; }
  const btn = $("f-create");
  btn.disabled = true;
  btn.textContent = "保存中…";
  $("modal-err").textContent = "";
  try {
    await api.patch(`/api/spaces/${sp.id}`,
      { name, description: $("f-desc").value.trim(), ...executorFields() });
  } catch (e) {
    $("modal-err").textContent = `保存失败：${e.message}`;
    return;
  } finally {
    btn.disabled = false;
    btn.textContent = "保存";
  }
  $("modal").classList.add("hidden");
  state.editingSpace = null;
  try {
    await loadSpaces();
  } catch (e) {
    toast(`已保存，但刷新列表失败：${e.message}`);
  }
}

/* 让模型写一段简介，只填进文本框不保存：简介决定指挥台把任务派给谁，要人过目再存 */
async function generateDescription() {
  const sp = state.editingSpace;
  const link = $("f-desc-gen");
  if (!sp || link.dataset.busy) return;
  link.dataset.busy = "1";
  link.textContent = "生成中…";
  $("modal-err").textContent = "";
  try {
    // 要调一次模型，后端最多等 60 秒：前端的超时放宽到比它长一点
    const res = await api.post(`/api/spaces/${sp.id}/describe`, {}, 70000);
    // 生成期间弹窗可能已经关了、或者换成了别的空间
    if (state.editingSpace !== sp) return;
    $("f-desc").value = res.description || "";
    $("f-desc").focus();
  } catch (e) {
    if (state.editingSpace === sp) $("modal-err").textContent = `自动生成失败：${e.message}`;
  } finally {
    delete link.dataset.busy;
    link.textContent = "自动生成";
  }
}

async function createSpace() {
  if (state.editingSpace) { await saveSpaceSettings(); return; }
  const name = $("f-name").value.trim();
  if (!name) { $("modal-err").textContent = "名称不能为空"; return; }
  const kind = $("f-kind").value;
  const body = { name, kind, ...executorFields() };
  if (kind === "agent") {
    body.cwd = $("f-cwd").value.trim();
    if (!body.cwd) { $("modal-err").textContent = "绑定目录的空间必须填工作目录"; return; }
  }
  const v = $("f-verify").value.trim();
  if (v) { body.verify_command = v; body.verify_trigger = "on_stop"; }

  // 请求在路上时给出反馈，也防止连点建出两个同名空间
  const btn = $("f-create");
  btn.disabled = true;
  btn.textContent = "创建中…";
  $("modal-err").textContent = "";
  let sp;
  try {
    sp = await api.post("/api/spaces", body);
  } catch (e) {
    $("modal-err").textContent = `创建失败：${e.message}`;
    return;
  } finally {
    btn.disabled = false;
    btn.textContent = "创建";
  }
  $("modal").classList.add("hidden");
  // 向导已经关了，后面再出错写进 modal-err 就没人看得见，改用 toast
  try {
    await loadSpaces();
    await newSession(sp.id);
  } catch (e) {
    toast(`空间已创建，但打开会话失败：${e.message}`);
  }
}

/* ────────────────────────────── 启动 ────────────────────────────── */
async function boot() {
  const meta = await api.get("/api/meta");
  state.profiles = meta.profiles || [];
  state.defaultProfile = meta.default_profile || state.profiles[0] || "";
  state.executors = meta.executors || [{ name: "simpleagent", label: "内置 SimpleAgent" }];
  state.commandSpaceId = meta.command_space_id || null;
  fillExecutorOptions();
  await loadSpaces();

  $("btn-new-space").onclick = () => openModal();
  $("f-cancel").onclick = () => {
    $("modal").classList.add("hidden");
    state.editingSpace = null;
  };
  $("f-create").onclick = createSpace;
  $("f-desc-gen").onclick = generateDescription;
  $("f-kind").onchange = syncModalFields;
  $("f-executor").onchange = () => {
    fillExecutorDependents();
    syncModalFields();
  };
  $("f-permission").onchange = syncModalFields;
  $("btn-send").onclick = send;
  $("btn-new-session").onclick = () => state.spaceId && newSession(state.spaceId);
  $("btn-stop").onclick = async () => {
    if (!state.sessionId) return;
    await api.post(`/api/sessions/${state.sessionId}/cancel`, {}).catch((e) => toast(e.message));
    state.running = false;
    renderHeader();
  };
  $("btn-verify").onclick = async () => {
    if (!state.sessionId) return;
    toast("已提交验证命令");
    await api.post(`/api/sessions/${state.sessionId}/verify`, {}).catch((e) => toast(e.message));
  };
  $("input").addEventListener("keydown", onInputKeydown);
  $("input").addEventListener("input", () => { autoGrow(); updateSlash(); });
  $("input").addEventListener("blur", closeSlash);
  $("search").addEventListener("input", (e) => {
    state.filter = e.target.value;
    renderSpaces();
  });
  $("profile").onchange = async () => {
    if (!state.spaceId) return;
    await api.patch(`/api/spaces/${state.spaceId}`, { profile: $("profile").value })
      .then(() => loadSpaces())
      .catch((e) => toast(e.message));
  };
  $("btn-export").onclick = exportMarkdown;
  $("btn-rerun").onclick = rerun;
  document.querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => switchTab(t.dataset.tab);
  });
  $("nav-panel").onclick = () => openPanel();
  $("btn-dispatch").onclick = dispatch;
  $("dispatch-input").addEventListener("input", updateMentions);
  $("dispatch-input").addEventListener("keydown", (e) => {
    const mn = panelState.mentions;
    if (mn.open) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        mn.index = (mn.index + (e.key === "ArrowDown" ? 1 : mn.items.length - 1)) % mn.items.length;
        updateMentions();
        mn.index = Math.min(mn.index, mn.items.length - 1);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        applyMention(mn.index);
        return;
      }
      if (e.key === "Escape") { e.preventDefault(); mn.open = false; updateMentions(); return; }
    }
    if (e.key === "Escape" && panelState.replyTo) { e.preventDefault(); clearReplyTo(); return; }
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); dispatch(); }
  });
  document.querySelectorAll("#inbox-view .seg-item").forEach((el) => {
    el.onclick = async () => {
      if (panelState.inboxView === el.dataset.view) return;
      panelState.inboxView = el.dataset.view;
      await loadInbox().catch((e) => toast(e.message));
    };
  });
  $("inbox-readall").onclick = async () => {
    await api.post("/api/inbox/all/read", {});
    await loadPanel();
  };
  $("mm-close").onclick = closeMessageModal;
  $("msg-modal").onclick = (e) => { if (e.target === $("msg-modal")) closeMessageModal(); };
  $("mm-copy").onclick = async () => {
    const m = panelState.modalItem;
    if (!m) return;
    try {
      await navigator.clipboard.writeText(m.body || m.title);
      toast("已复制");
    } catch { toast("复制失败：浏览器不允许访问剪贴板"); }
  };
  $("mm-todo").onclick = () => {
    if (panelState.modalItem) messageToTodo(panelState.modalItem).catch((e) => toast(e.message));
  };
  $("mm-archive").onclick = async () => {
    const m = panelState.modalItem;
    if (!m) return;
    await archiveMessage(m.id).catch((e) => toast(e.message));
    closeMessageModal();
  };
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("msg-modal").classList.contains("hidden")) closeMessageModal();
  });
  refreshBadge().catch(() => { /* 角标拿不到不影响启动 */ });
  setInterval(pollInbox, INBOX_POLL_MS);
  $("todo-add").onclick = addTodoInline;

  // 每个工作台标签页挂一条 SSE，开到 6 个就把浏览器给这个 host 的连接占满了，
  // 之后创建空间、发消息全卡在排队里。所以切到后台就断开，回到前台再续传，
  // 错过的帧由服务端按 lastSeq 重放，前端按 seq 去重。
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { closeStream(); return; }
    if (state.sessionId && !state.es) subscribe(state.sessionId, { resume: true });
    pollInbox();  // 在后台时跳过的那几轮补上
  });

  // 运行中每秒刷新一次「已用时」
  setInterval(() => {
    if (state.running && state.startedAt) {
      $("status-hint").textContent =
        `${STATUS_TEXT.running}… ${fmtDur(Math.floor((Date.now() - state.startedAt) / 1000))}`;
    }
  }, 1000);

  // 回到上次打开的会话（刷新页面不该丢上下文）
  try {
    const saved = JSON.parse(localStorage.getItem(LS_KEY) || "null");
    const sp = saved && findSpace(saved.spaceId);
    if (sp && (sp.sessions || []).some((m) => m.id === saved.sessionId)) {
      await selectSession(saved.spaceId, saved.sessionId);
    }
  } catch { /* 存的是脏数据就当没有 */ }
}

boot().catch((e) => {
  document.body.innerHTML = `<div style="padding:40px;font:14px/1.7 system-ui">
    <b>加载失败：${escapeHtml(e.message)}</b><br>确认 <code>sa serve</code> 正在运行，然后刷新页面。</div>`;
});
