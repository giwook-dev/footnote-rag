// footnote-rag 웹 화면. 빌드 단계·외부 라이브러리 없이 기존 HTTP API만 호출한다.
// 보안 규칙: API에서 받은 문자열은 textContent / createElement로만 화면에 넣는다.
// 권한 판단은 서버가 한다. 역할에 따른 탭 숨김은 편의 기능일 뿐이다.
"use strict";

(() => {
  const TOKEN_KEY = "footnote-rag.token";
  const DART_URL_PREFIX = "https://dart.fss.or.kr/";
  const POLL_MS = 3000;
  const FOOTNOTE_MARK = /\[(\d+)\]/;
  const REVIEWER_ROLES = ["reviewer", "admin"];
  const BUSY_STATUSES = ["queued", "processing"];
  const REPROCESSABLE_STATUSES = ["ready", "failed"];
  const AUDIT_REPORT_DETAILS = ["F001", "F002"];
  const ANNUAL_REPORT_HINT = "상장사는 감사보고서를 사업보고서(A001)에 포함해 공시합니다.";
  const MODEL_REFERENCE = "model_reference";
  const MODEL_REFERENCE_NOTICE = "로컬 모델이 지정한 근거 — 인용문은 근사 매칭";

  const state = {
    user: null,
    tab: "documents",
    docs: [],
    pollTimer: null,
    history: [],
    active: null,
    fnSeq: 0,
    dartReports: [],
    comparison: null,
    diffNote: null,
    diffSummaries: {},
    diffView: null, // 변동 분석 화면의 필터·정렬·탭 상태(freshDiffView)
  };

  const $ = (id) => document.getElementById(id);

  // ---------- DOM helpers ----------

  function el(tag, opts = {}, children = []) {
    const node = document.createElement(tag);
    if (opts.className) node.className = opts.className;
    if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
    if (opts.attrs) {
      for (const [key, value] of Object.entries(opts.attrs)) node.setAttribute(key, String(value));
    }
    for (const child of children) {
      if (child !== null && child !== undefined && child !== false) node.append(child);
    }
    return node;
  }

  const BADGE_CLASS = {
    ready: "b-ok",
    published: "b-ok",
    approved: "b-ok",
    succeeded: "b-ok",
    queued: "b-wait",
    processing: "b-wait",
    running: "b-wait",
    pending_review: "b-wait",
    failed: "b-fail",
    rejected: "b-fail",
  };

  function statusBadge(status) {
    const label = status || "unknown";
    return el("span", { className: `badge ${BADGE_CLASS[label] || "b-muted"}`, text: label });
  }

  function fmtInt(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString("ko-KR") : "—";
  }

  function fmtDate(yyyymmdd) {
    const s = String(yyyymmdd || "");
    return /^\d{8}$/.test(s) ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s;
  }

  function fmtDateTime(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? String(iso || "") : d.toLocaleString("ko-KR");
  }

  function dateInputValue(d) {
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function localMidnight(value) {
    const [y, m, d] = value.split("-").map(Number);
    return new Date(y, m - 1, d);
  }

  function setBusy(button, spinner, busy) {
    if (button) button.disabled = busy;
    if (spinner) spinner.hidden = !busy;
  }

  // ---------- alerts ----------

  function showAlert(message, kind = "error") {
    const box = $("alerts");
    const item = el("div", {
      className: `alert ${kind}`,
      attrs: { role: kind === "error" ? "alert" : "status" },
    });
    const close = el("button", {
      className: "small",
      text: "닫기",
      attrs: { type: "button", "aria-label": "알림 닫기" },
    });
    close.addEventListener("click", () => item.remove());
    item.append(el("span", { className: "msg", text: message }), close);
    box.append(item);
    while (box.children.length > 4) box.firstElementChild.remove();
    setTimeout(() => item.remove(), kind === "error" ? 12000 : 6000);
  }

  // ---------- token ----------

  function getToken() {
    try {
      return sessionStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  }

  function setToken(token) {
    try {
      sessionStorage.setItem(TOKEN_KEY, token);
    } catch {
      showAlert("이 브라우저에서는 sessionStorage를 쓸 수 없어 로그인 상태를 저장하지 못합니다.");
    }
  }

  function clearToken() {
    try {
      sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      /* nothing stored */
    }
  }

  // ---------- API ----------

  class ApiError extends Error {
    constructor(status, detail) {
      super(detail);
      this.status = status;
      this.detail = detail;
    }
  }

  async function readBody(resp) {
    let text = "";
    try {
      text = await resp.text();
    } catch {
      return null;
    }
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }

  function formatDetail(data, status) {
    const detail = data && typeof data === "object" && "detail" in data ? data.detail : data;
    if (detail === null || detail === undefined || detail === "") return `HTTP ${status}`;
    if (typeof detail === "string") return detail.slice(0, 500);
    if (Array.isArray(detail)) {
      return detail
        .map((e) => {
          const loc = Array.isArray(e && e.loc)
            ? e.loc.filter((p) => p !== "body" && p !== "query").join(".")
            : "";
          const msg = e && e.msg ? e.msg : JSON.stringify(e);
          return loc ? `${loc}: ${msg}` : msg;
        })
        .join("; ")
        .slice(0, 500);
    }
    return JSON.stringify(detail).slice(0, 500);
  }

  // allow: 호출자가 직접 처리할 상태 코드(예: 409). auth=false면 토큰을 붙이지 않는다(로그인).
  async function api(path, { method = "GET", json, form, allow = [], auth = true } = {}) {
    const headers = { Accept: "application/json" };
    let body;
    if (json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(json);
    } else if (form !== undefined) {
      headers["Content-Type"] = "application/x-www-form-urlencoded";
      body = new URLSearchParams(form).toString();
    }
    const token = auth ? getToken() : null;
    if (token) headers.Authorization = `Bearer ${token}`;

    let resp;
    try {
      resp = await fetch(path, { method, headers, body, cache: "no-store" });
    } catch {
      showAlert("네트워크 오류: 서버에 연결할 수 없습니다. 잠시 후 다시 시도하세요.");
      throw new ApiError(0, "network error");
    }
    const data = await readBody(resp);
    if (resp.ok || allow.includes(resp.status)) return { status: resp.status, data };

    const detail = formatDetail(data, resp.status);
    if (resp.status === 401 && auth) {
      onUnauthorized();
      throw new ApiError(401, detail);
    }
    showAlert(`요청 실패 (${resp.status}): ${detail}`);
    throw new ApiError(resp.status, detail);
  }

  // 이벤트 핸들러용: ApiError는 이미 알림으로 보였으므로 삼키고, 그 밖의 오류만 알린다.
  function guard(fn) {
    return async (...args) => {
      try {
        await fn(...args);
      } catch (err) {
        if (err instanceof ApiError) return;
        console.error(err);
        showAlert(`화면 오류: ${err && err.message ? err.message : String(err)}`);
      }
    };
  }

  // ---------- session ----------

  function resetUi() {
    stopPolling();
    state.user = null;
    state.docs = [];
    state.history = [];
    state.active = null;
    state.dartReports = [];
    state.comparison = null;
    state.diffNote = null;
    state.diffSummaries = {};
    state.diffView = null;
    for (const id of [
      "answer-area", "history", "reviews-list", "usage-stats", "question-docs", "llm-status",
      "diff-current", "diff-prior", "diff-recent", "diff-stats", "diff-notes", "diff-pair-warning",
      "diff-warnings", "diff-detail-head", "diff-pane-numbers", "diff-pane-lines", "diff-pane-summary",
    ]) {
      $(id).replaceChildren();
    }
    $("diff-result").hidden = true;
    $("diff-prior-empty").hidden = true;
    $("diff-pair-warning").hidden = true;
    $("diff-note-search").value = "";
    $("diff-btn").textContent = "비교";
    $("diff-btn").classList.remove("warn");
    $("llm-status-detail").hidden = true;
    for (const id of ["dart-results", "docs-table", "audit-table"]) {
      $(id).querySelector("tbody").replaceChildren();
    }
    $("dart-results").hidden = true;
    for (const id of ["dart-not-found", "dart-empty", "dart-hint", "mode-badge", "review-count"]) {
      $(id).hidden = true;
    }
    $("history-empty").hidden = false;
    $("usage-from").value = "";
    $("usage-to").value = "";
  }

  function showLogin() {
    $("app-view").hidden = true;
    $("login-view").hidden = false;
    $("login-password").value = "";
    $("login-email").focus();
  }

  function onUnauthorized() {
    const wasLoggedIn = state.user !== null;
    clearToken();
    resetUi();
    showLogin();
    showAlert(
      wasLoggedIn
        ? "로그인이 만료되었거나 유효하지 않습니다. 다시 로그인하세요."
        : "다시 로그인하세요.",
      "info",
    );
  }

  async function login(ev) {
    ev.preventDefault();
    const button = $("login-submit");
    button.disabled = true;
    try {
      const { status, data } = await api("/auth/token", {
        method: "POST",
        form: { username: $("login-email").value.trim(), password: $("login-password").value },
        auth: false,
        allow: [401],
      });
      if (status === 401) {
        showAlert("이메일 또는 비밀번호가 올바르지 않습니다.");
        return;
      }
      setToken(data.access_token);
      $("login-password").value = "";
      await enterApp();
    } finally {
      button.disabled = false;
    }
  }

  function logout() {
    clearToken();
    resetUi();
    showLogin();
    showAlert("로그아웃했습니다.", "info");
  }

  async function enterApp() {
    const { data: me } = await api("/users/me");
    state.user = me;
    $("user-email").textContent = me.email;
    $("user-role").textContent = me.role;
    $("tab-reviews").hidden = !REVIEWER_ROLES.includes(me.role);
    $("tab-admin").hidden = me.role !== "admin";
    $("login-view").hidden = true;
    $("app-view").hidden = false;
    selectTab("documents");
    if (canReview()) guard(loadReviewCount)();
  }

  const canReview = () => state.user !== null && REVIEWER_ROLES.includes(state.user.role);

  // ---------- tabs ----------

  const LOADERS = {
    documents: () => loadDocuments(),
    questions: () => loadQuestionDocs(),
    diff: () => loadDiffTab(),
    reviews: () => loadReviews(),
    admin: () => loadAdmin(),
  };

  function visibleTabs() {
    return [...document.querySelectorAll(".tabs [role=tab]")].filter((b) => !b.hidden);
  }

  function selectTab(name, { focus = false } = {}) {
    const target = $(`tab-${name}`);
    const tab = target && !target.hidden ? name : "documents";
    state.tab = tab;
    for (const button of document.querySelectorAll(".tabs [role=tab]")) {
      const on = button.dataset.tab === tab;
      button.setAttribute("aria-selected", String(on));
      button.tabIndex = on ? 0 : -1;
      if (on && focus) button.focus();
    }
    for (const panel of document.querySelectorAll(".panel")) {
      panel.hidden = panel.id !== `panel-${tab}`;
    }
    if (tab !== "documents") stopPolling();
    guard(LOADERS[tab])();
  }

  function onTabKeydown(ev) {
    const tabs = visibleTabs();
    const index = tabs.indexOf(ev.target);
    if (index < 0) return;
    let next = null;
    if (ev.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
    else if (ev.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
    else if (ev.key === "Home") next = tabs[0];
    else if (ev.key === "End") next = tabs[tabs.length - 1];
    if (next) {
      ev.preventDefault();
      selectTab(next.dataset.tab, { focus: true });
    }
  }

  // ---------- documents tab ----------

  function stopPolling() {
    if (state.pollTimer !== null) clearTimeout(state.pollTimer);
    state.pollTimer = null;
    $("docs-polling").hidden = true;
  }

  // queued/processing 문서가 있고 문서 탭을 보고 있을 때만 3초 뒤 다시 불러온다.
  function schedulePoll() {
    stopPolling();
    const busy = state.docs.some((d) => BUSY_STATUSES.includes(d.status));
    if (busy && state.tab === "documents" && state.user !== null) {
      $("docs-polling").hidden = false;
      state.pollTimer = setTimeout(guard(loadDocuments), POLL_MS);
    }
  }

  async function fetchDocuments() {
    const { data } = await api("/documents");
    state.docs = Array.isArray(data) ? data : [];
    return state.docs;
  }

  async function loadDocuments() {
    await fetchDocuments();
    renderDocuments();
    renderDartResults();
    schedulePoll();
  }

  const isAdmin = () => state.user !== null && state.user.role === "admin";

  // admin에게만, ready/failed 문서에만 보인다. 권한 판단은 서버(403)가 한다.
  function reprocessButton(d) {
    if (!isAdmin() || !REPROCESSABLE_STATUSES.includes(d.status)) return null;
    const button = el("button", {
      className: "small",
      text: "다시 처리",
      attrs: { type: "button", "aria-label": `${d.title || "문서"} 다시 처리` },
    });
    button.addEventListener("click", guard(() => reprocessDocument(d, button)));
    return button;
  }

  async function reprocessDocument(d, button) {
    const ok = window.confirm(
      `"${d.title || "문서"}"를 다시 처리할까요? 기존 청크를 지우고 원문을 다시 파싱·임베딩합니다.`,
    );
    if (!ok) return;
    button.disabled = true;
    try {
      await api(`/documents/${encodeURIComponent(d.id)}/reprocess`, { method: "POST" });
      showAlert("다시 처리를 시작했습니다. 워커가 처리하면 상태가 ready로 바뀝니다.", "success");
      await loadDocuments(); // 상태가 queued가 되어 자동 새로고침이 다시 시작된다
    } finally {
      button.disabled = false;
    }
  }

  const STAGE_LABELS = { download: "다운로드", parse: "파싱", embed: "임베딩", store: "저장" };

  function fmtClock(iso) {
    const t = new Date(iso);
    if (Number.isNaN(t.getTime())) return "";
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
  }

  // 상태 칸 아래 줄: "임베딩 120/700" 또는 "재시도 대기(한도 초과) — 14:03:05". textContent로만 넣는다.
  function progressNote(d) {
    if (d.status === "queued" && d.next_retry_at) {
      const reason = String(d.error || "").includes("한도 초과") ? "(한도 초과)" : "";
      const text = `재시도 대기${reason} — ${fmtClock(d.next_retry_at)}`;
      return el("span", { className: "doc-progress retry", text });
    }
    const p = d.progress;
    if (d.status !== "processing" || !p || typeof p !== "object") return null;
    const label = STAGE_LABELS[p.stage] || String(p.stage || "");
    const counted = p.stage === "embed" || p.stage === "store";
    const text = counted ? `${label} ${fmtInt(p.done)}/${fmtInt(p.total)}` : `${label} 중`;
    return el("span", { className: "doc-progress", text });
  }

  function renderDocuments() {
    const tbody = $("docs-table").querySelector("tbody");
    tbody.replaceChildren(
      ...state.docs.map((d) => {
        const error = d.status === "failed" ? d.error || "오류 내용이 기록되지 않았습니다." : d.error;
        return el("tr", {}, [
          el("td", { text: d.title || "(제목 없음)" }),
          el("td", { text: (d.meta && d.meta.corp_name) || "—" }),
          el("td", {}, [statusBadge(d.status), progressNote(d)]),
          el("td", { text: fmtInt(d.section_count) }),
          el("td", { className: error ? "error" : "", text: error || "" }),
          el("td", {}, [reprocessButton(d)]),
        ]);
      }),
    );
    $("docs-table").hidden = state.docs.length === 0;
    $("docs-empty").hidden = state.docs.length > 0;
  }

  function setMode(mode) {
    const badge = $("mode-badge");
    if (mode === "fake") {
      badge.textContent = "Fake 모드(합성 데이터)";
      badge.className = "badge b-wait";
      badge.hidden = false;
    } else if (mode === "real") {
      badge.textContent = "OpenDART 실데이터";
      badge.className = "badge b-acc";
      badge.hidden = false;
    }
  }

  function parseCodes(value) {
    const codes = [];
    for (const part of value.split(",")) {
      const code = part.trim();
      if (code && !codes.includes(code)) codes.push(code);
    }
    return codes;
  }

  function syncPresets() {
    const codes = parseCodes($("dart-stock-codes").value);
    for (const button of document.querySelectorAll("[data-stock-code]")) {
      button.setAttribute("aria-pressed", String(codes.includes(button.dataset.stockCode)));
    }
  }

  function togglePreset(code) {
    const input = $("dart-stock-codes");
    const codes = parseCodes(input.value);
    const next = codes.includes(code) ? codes.filter((c) => c !== code) : [...codes, code];
    input.value = next.join(",");
    syncPresets();
  }

  async function searchDart(ev) {
    ev.preventDefault();
    const codes = parseCodes($("dart-stock-codes").value);
    if (codes.length === 0) {
      showAlert("종목코드를 하나 이상 입력하세요.");
      return;
    }
    const detail = $("dart-detail").value;
    const params = new URLSearchParams({
      stock_codes: codes.join(","),
      date_from: $("dart-date-from").value.trim(),
      date_to: $("dart-date-to").value.trim(),
      detail,
    });
    setBusy($("dart-search-btn"), $("dart-search-spinner"), true);
    try {
      const { data } = await api(`/dart/reports?${params}`);
      setMode(data.mode);
      state.dartReports = Array.isArray(data.reports) ? data.reports : [];
      const notFound = Array.isArray(data.not_found) ? data.not_found : [];
      const box = $("dart-not-found");
      if (notFound.length > 0) {
        let message = `corpCode 목록에서 찾지 못한 종목코드: ${notFound.join(", ")}`;
        if (data.mode === "fake") {
          message += " — Fake 모드에는 합성 종목코드 990001~990003만 있습니다.";
        }
        box.textContent = message;
        box.hidden = false;
      } else {
        box.hidden = true;
      }
      const empty = state.dartReports.length === 0;
      $("dart-empty").hidden = !empty;
      const hint = $("dart-hint");
      if (empty && AUDIT_REPORT_DETAILS.includes(detail)) {
        hint.textContent = ANNUAL_REPORT_HINT;
        hint.hidden = false;
      } else {
        hint.textContent = "";
        hint.hidden = true;
      }
      renderDartResults();
    } finally {
      setBusy($("dart-search-btn"), $("dart-search-spinner"), false);
    }
  }

  function renderDartResults() {
    const table = $("dart-results");
    const byRcept = new Map(state.docs.map((d) => [d.source_ref, d]));
    table.querySelector("tbody").replaceChildren(
      ...state.dartReports.map((r) => {
        const button = el("button", {
          className: "small primary",
          text: "가져오기",
          attrs: { type: "button", "aria-label": `${r.corp_name} ${r.rcept_no} 가져오기` },
        });
        button.addEventListener("click", guard(() => importDart(r.rcept_no, button)));
        const existing = byRcept.get(r.rcept_no);
        const action = el("td", {}, [
          el("div", { className: "row" }, [button, existing ? statusBadge(existing.status) : null]),
        ]);
        return el("tr", {}, [
          el("td", { text: r.corp_name }),
          el("td", { text: r.report_nm }),
          el("td", { className: "mono", text: r.rcept_no }),
          el("td", { text: fmtDate(r.rcept_dt) }),
          action,
        ]);
      }),
    );
    table.hidden = state.dartReports.length === 0;
  }

  async function importDart(rceptNo, button) {
    if (!/^\d{14}$/.test(rceptNo)) {
      showAlert("접수번호는 14자리 숫자여야 합니다.");
      return;
    }
    if (button) button.disabled = true;
    try {
      const { status, data } = await api("/documents/dart", {
        method: "POST",
        json: { rcept_no: rceptNo },
      });
      if (status === 202) {
        showAlert(
          `새 문서 가져오기를 시작했습니다(${rceptNo}). 워커가 처리하면 상태가 ready로 바뀝니다.`,
          "success",
        );
      } else {
        showAlert(
          `이미 가져온 문서입니다(${rceptNo}). 새로 만들지 않고 기존 문서를 씁니다. 현재 상태: ${data.status}`,
          "info",
        );
      }
      await loadDocuments();
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function submitRcept(ev) {
    ev.preventDefault();
    await importDart($("rcept-no").value.trim(), $("rcept-btn"));
  }

  // ---------- questions tab ----------

  async function loadQuestionDocs() {
    const checked = new Set(
      [...document.querySelectorAll("#question-docs input:checked")].map((i) => i.value),
    );
    await fetchDocuments();
    const ready = state.docs.filter((d) => d.status === "ready");
    $("question-docs").replaceChildren(
      ...ready.map((d) => {
        const box = el("input", { attrs: { type: "checkbox", value: d.id } });
        box.checked = checked.has(d.id);
        const name = d.meta && d.meta.corp_name ? `${d.meta.corp_name} · ${d.title}` : d.title;
        return el("label", {}, [box, el("span", { text: name || "(제목 없음)" })]);
      }),
    );
    $("question-docs-empty").hidden = ready.length > 0;
  }

  async function submitQuestion(ev) {
    ev.preventDefault();
    const question = $("question-text").value.trim();
    if (question.length < 2) {
      showAlert("질문을 2자 이상 입력하세요.");
      return;
    }
    const ids = [...document.querySelectorAll("#question-docs input:checked")].map((i) => i.value);
    const payload = { question };
    if (ids.length > 0) payload.document_ids = ids;
    setBusy($("question-btn"), $("question-spinner"), true);
    try {
      const { data } = await api("/questions", { method: "POST", json: payload });
      const entry = { question, answer: data };
      state.history.unshift(entry);
      showEntry(entry);
    } finally {
      setBusy($("question-btn"), $("question-spinner"), false);
    }
  }

  function showEntry(entry) {
    state.active = entry;
    $("answer-area").replaceChildren(buildAnswerCard(entry));
    renderHistory();
  }

  function renderHistory() {
    $("history").replaceChildren(
      ...state.history.map((entry) => {
        const button = el(
          "button",
          { className: entry === state.active ? "on" : "", attrs: { type: "button" } },
          [el("span", { className: "q", text: entry.question }), statusBadge(entry.answer.status)],
        );
        if (entry === state.active) button.setAttribute("aria-current", "true");
        button.addEventListener("click", () => showEntry(entry));
        return el("li", {}, [button]);
      }),
    );
    $("history-empty").hidden = state.history.length > 0;
  }

  function buildAnswerCard({ question, answer }) {
    const card = el("article", { className: "card", attrs: { "aria-label": "답변" } });
    const statusRow = el("div", { className: "row" }, [
      statusBadge(answer.status),
      answer.review_reason ? el("span", { className: "note", text: `사유: ${answer.review_reason}` }) : null,
    ]);
    card.append(
      el("div", { className: "answer-head" }, [el("h2", { text: "답변" }), statusRow]),
      el("p", { className: "answer-question", text: `Q. ${question}` }),
    );
    appendAnswerBody(card, answer.text, answer.footnotes, answer.status);
    const meta = usageMeta(answer);
    if (meta) card.append(meta);
    return card;
  }

  function usageMeta(answer) {
    const parts = [];
    const u = answer.usage;
    if (u) {
      parts.push(`입력 ${fmtInt(u.input_tokens)} · 출력 ${fmtInt(u.output_tokens)} 토큰`);
      parts.push(u.cost_usd === null || u.cost_usd === undefined ? "비용 미산정" : `$${u.cost_usd}`);
    }
    if (answer.latency_ms !== null && answer.latency_ms !== undefined) {
      parts.push(`${fmtInt(answer.latency_ms)} ms`);
    }
    if (u && u.model) parts.push(u.model);
    if (parts.length === 0) return null;
    return el(
      "div",
      { className: "meta", attrs: { "aria-label": "사용량" } },
      parts.map((p) => el("span", { text: p })),
    );
  }

  function dartLink(url, label = "DART 원문 보기 ↗") {
    if (typeof url === "string" && url.startsWith(DART_URL_PREFIX)) {
      return el("a", {
        text: label,
        attrs: { href: url, target: "_blank", rel: "noopener noreferrer" },
      });
    }
    return el("span", { className: "note", text: "DART 원문 링크 없음" });
  }

  function buildFootnoteItem(f, id) {
    const info = el("div", {}, [
      el("div", {}, [
        el("b", { text: f.corp_name || "회사명 없음" }),
        ` · ${f.document_title || "제목 없음"}`,
      ]),
      f.section_path ? el("div", { className: "path", text: f.section_path }) : null,
      f.cited_text ? el("blockquote", { text: f.cited_text }) : null,
      dartLink(f.dart_url),
    ]);
    return el("li", { attrs: { id, tabindex: "-1" } }, [
      el("span", { className: "n", text: `[${f.n}]` }),
      info,
    ]);
  }

  function highlightFootnote(container, n, item) {
    for (const node of container.querySelectorAll(".footnotes li.on, button.fn.on")) {
      node.classList.remove("on");
    }
    item.classList.add("on");
    for (const button of container.querySelectorAll(`button.fn[data-n="${n}"]`)) {
      button.classList.add("on");
    }
    const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    item.scrollIntoView({ block: "nearest", behavior: smooth ? "smooth" : "auto" });
  }

  // 본문을 /\[(\d+)\]/ 로 나눠 텍스트 노드와 각주 버튼을 만든다(HTML 문자열을 만들지 않는다).
  function appendAnswerBody(container, text, footnotes, status) {
    const notes = Array.isArray(footnotes) ? footnotes : [];
    const prefix = `fn${++state.fnSeq}`;
    const items = new Map();
    const list = el("ol", { className: "footnotes", attrs: { "aria-label": "각주" } });
    for (const f of notes) {
      const n = Number(f.n);
      if (!Number.isInteger(n) || items.has(n)) continue;
      const item = buildFootnoteItem(f, `${prefix}-${n}`);
      items.set(n, item);
      list.append(item);
    }

    const body = el("div", { className: "answer-text" });
    if (text) {
      String(text)
        .split(FOOTNOTE_MARK)
        .forEach((part, index) => {
          if (index % 2 === 0) {
            if (part) body.append(part);
            return;
          }
          const n = Number(part);
          const item = items.get(n);
          if (!item) {
            body.append(`[${part}]`);
            return;
          }
          const button = el("button", {
            className: "fn",
            text: `[${n}]`,
            attrs: {
              type: "button",
              "data-n": n,
              "aria-controls": item.id,
              "aria-label": `각주 ${n} 보기`,
            },
          });
          const activate = () => highlightFootnote(container, n, item);
          button.addEventListener("click", activate);
          button.addEventListener("focus", activate);
          body.append(el("sup", { className: "fn-sup" }, [button]));
        });
    } else {
      body.append(el("span", { className: "note", text: "답변 본문이 없습니다." }));
    }
    container.append(body);

    if (items.size > 0) {
      // 로컬 모델은 Citations API가 없어 근거 번호만 고르고, 인용문은 서버가 근사 매칭으로 고른다.
      if (notes.some((f) => f.citation_method === MODEL_REFERENCE)) {
        container.append(el("p", { className: "note fn-method", text: MODEL_REFERENCE_NOTICE }));
      }
      container.append(list);
    } else if (status === "pending_review" && state.user && state.user.role === "member") {
      container.append(
        el("p", {
          className: "notice",
          text: "검토 대기 중인 답변입니다. 검토자가 승인하면 본문과 각주가 공개됩니다.",
        }),
      );
    } else {
      container.append(
        el("p", {
          className: "notice",
          text: "이 답변에는 근거 각주가 없습니다. 문서에서 근거를 찾지 못했거나 모델이 답하지 않았을 수 있습니다.",
        }),
      );
    }
  }

  // ---------- note change analysis tab (변동 분석) ----------
  // 비교(주석 짝짓기·줄 비교·숫자 변동)는 서버 코드가 하고, 요약은 버튼을 눌렀을 때만 로컬 모델이 한다.
  // 보고서 종류 판단·필터·정렬은 아래 순수 함수가 하고(입력 → 출력 예시를 주석으로 남긴다), 렌더링 함수는
  // 그 결과만 화면에 넣는다. 필터·정렬·탭 상태는 state.diffView에 모으고 새 비교를 열면 초기화한다.

  const NOTE_STATUS_LABELS = { changed: "변경", added: "신규", removed: "삭제", unchanged: "변동 없음" };
  const NOTE_STATUS_CLASS = { changed: "b-acc", added: "b-ok", removed: "b-fail", unchanged: "b-muted" };
  const NUMBER_ROWS_PAGE = 50;
  const SUMMARY_NOTICE = "모델 요약 — 변동 판단은 담당자가 확정";
  const SUMMARY_ERROR_STATUSES = [501, 502, 503, 504];
  const REPORT_KIND_NAMES = { annual: "사업", half: "반기", quarter: "분기" };
  const PAIR_WARNING_TEXT = "같은 종류의 1년 전 보고서가 아니어서 숫자 변동이 크게 부풀 수 있습니다.";
  const AMBIGUOUS_ROWS_HELP = "같은 표·행 이름이 한 보고서에 여러 번 나와 짝을 확정할 수 없어 비교에서 뺀 행";
  const DETAIL_TABS = ["numbers", "lines", "summary"];
  const NUMBER_HEADERS = ["행", "전년", "당년", "증감", "증감률", "플래그"];
  const MISMATCH_HEADERS = ["행", "전년 보고서의 당기", "당년 보고서의 전기", "차이", "차이율", "플래그"];
  const NUMBER_COLUMNS = ["c-lbl", "c-num", "c-num", "c-num", "c-pct", "c-flag"];
  const NUMBER_FILTERS = [
    ["all", "전체"],
    ["large", "큰 변동"],
    ["rows", "행 추가·삭제"],
    ["count", "값 개수 다름"],
  ];
  const NUMBER_SORTS = [
    ["abs_delta", "|증감| 큰 순"],
    ["abs_pct", "|증감률| 큰 순"],
    ["source", "원문 순"],
  ];
  const FLAG_BADGES = {
    large_change: ["b-fail", "큰 변동"],
    prior_mismatch: ["b-wait", "전기 불일치"],
    row_added: ["b-ok", "행 추가"],
    row_removed: ["b-fail", "행 삭제"],
    value_count: ["b-muted", "값 개수 다름"],
  };

  const isComparable = (d) => d.status === "ready" && d.source === "dart" && d.meta;

  // ----- 순수 함수: 숫자·보고서 이름 -----

  // 숫자 문자열을 화면용으로. 소수점 이하는 그대로 두고 정수부에만 천 단위 쉼표를 넣는다(Number 변환 없음).
  //   fmtDecimal("1570778")                 → "1,570,778"
  //   fmtDecimal("-1054455")                → "−1,054,455"
  //   fmtDecimal("12345.678901234567")      → "12,345.678901234567"
  //   fmtDecimal("735492", { sign: true })  → "+735,492"
  //   fmtDecimal("-67.1", { sign: true })   → "−67.1"
  //   fmtDecimal("0", { sign: true })       → "0"
  //   fmtDecimal(null)                      → "—"
  //   fmtDecimal("abc")                     → "abc"
  function fmtDecimal(value, { sign = false } = {}) {
    if (value === null || value === undefined || value === "") return "—";
    const text = String(value).trim();
    const m = /^([+-]?)(\d+)(\.\d+)?$/.exec(text);
    if (!m) return text;
    const int = m[2].replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    const zero = !/[1-9]/.test(m[2] + (m[3] || ""));
    const prefix = zero ? "" : m[1] === "-" ? "−" : sign ? "+" : "";
    return `${prefix}${int}${m[3] || ""}`;
  }

  // 숫자 문자열의 부호: 1 | -1 | 0.  signOf("-3.5") → -1, signOf("0.0") → 0, signOf(null) → 0
  function signOf(value) {
    const text = String(value === null || value === undefined ? "" : value).trim();
    if (!/[1-9]/.test(text)) return 0;
    return text.startsWith("-") ? -1 : 1;
  }

  const CORRECTION_MARK_RE = /^(?:\s*\[[^\]]*\])+\s*/;

  // 보고서 종류. 정정 표시([기재정정] 등)는 종류 판단에서 무시한다.
  //   reportKind("사업보고서 (2025.12)")          → { kind: "annual", name: "사업" }
  //   reportKind("[기재정정]반기보고서 (2025.06)") → { kind: "half", name: "반기" }
  //   reportKind("분기보고서 (2026.03)")          → { kind: "quarter", name: "분기" }
  //   reportKind("감사보고서 (2025.12)")          → { kind: "other", name: "감사보고서" }
  //   reportKind(null)                            → { kind: "other", name: "보고서" }
  function reportKind(reportNm) {
    const base = String(reportNm || "").replace(CORRECTION_MARK_RE, "");
    if (base.includes("사업보고서")) return { kind: "annual", name: REPORT_KIND_NAMES.annual };
    if (base.includes("반기보고서")) return { kind: "half", name: REPORT_KIND_NAMES.half };
    if (base.includes("분기보고서")) return { kind: "quarter", name: REPORT_KIND_NAMES.quarter };
    const name = base.replace(/\s*\(\d{4}\.\d{1,2}\)\s*$/, "").trim();
    return { kind: "other", name: name || "보고서" };
  }

  // 보고서 기간(YYYY.MM).
  //   reportPeriod("사업보고서 (2025.12)")          → "2025.12"
  //   reportPeriod("[기재정정]사업보고서 (2024.12)") → "2024.12"
  //   reportPeriod("사업보고서")                    → null
  function reportPeriod(reportNm) {
    const m = /\((\d{4})\.(\d{2})\)/.exec(String(reportNm || ""));
    return m ? `${m[1]}.${m[2]}` : null;
  }

  // periodMonths("2025.12") → 24312 (연×12 + 월), periodMonths(null) → null
  function periodMonths(period) {
    const m = /^(\d{4})\.(\d{2})$/.exec(String(period || ""));
    return m ? Number(m[1]) * 12 + Number(m[2]) : null;
  }

  // 선택 상자용 이름(정정 표시는 남긴다).
  //   reportTitle("사업보고서 (2025.12)")          → "사업보고서 2025.12"
  //   reportTitle("[기재정정]사업보고서 (2024.12)") → "[기재정정]사업보고서 2024.12"
  function reportTitle(reportNm) {
    const text = String(reportNm || "").trim();
    return text.replace(/\s*\((\d{4}\.\d{2})\)\s*$/, " $1").trim() || "보고서";
  }

  // 짧은 이름(최근 비교·상세 머리).
  //   reportShort("사업보고서 (2025.12)")          → "사업 2025.12"
  //   reportShort("[기재정정]분기보고서 (2026.03)") → "[기재정정]분기 2026.03"
  function reportShort(reportNm) {
    const text = String(reportNm || "");
    const mark = (CORRECTION_MARK_RE.exec(text) || [""])[0].trim();
    const period = reportPeriod(text);
    return `${mark}${reportKind(text).name}${period ? ` ${period}` : ""}`;
  }

  // sameReportKind("사업보고서 (2024.12)", "[기재정정]사업보고서 (2025.12)") → true
  // sameReportKind("사업보고서 (2025.12)", "분기보고서 (2026.03)")           → false
  function sameReportKind(a, b) {
    const x = reportKind(a);
    const y = reportKind(b);
    return x.kind === y.kind && (x.kind !== "other" || x.name === y.name);
  }

  // 두 보고서가 "같은 종류의 1년 전 보고서"가 아니면 그 이유, 맞으면 null. 기간을 모르면 기간은 따지지 않는다.
  //   pairWarning("사업보고서 (2024.12)", "사업보고서 (2025.12)") → null
  //   pairWarning("사업보고서 (2025.12)", "분기보고서 (2026.03)")
  //     → "분기보고서 2026.03 ↔ 사업보고서 2025.12: 같은 종류의 1년 전 보고서가 아니어서 숫자 변동이
  //        크게 부풀 수 있습니다. (보고서 종류가 다름, 기간 차이 3개월)"
  //   pairWarning("사업보고서 (2023.12)", "사업보고서 (2025.12)") → "… (기간 차이 24개월)"
  //   pairWarning(null, null) → null (보고서명이 없는 이전 비교)
  function pairWarning(priorNm, currentNm) {
    const reasons = [];
    if (!sameReportKind(priorNm, currentNm)) reasons.push("보고서 종류가 다름");
    const pm = periodMonths(reportPeriod(priorNm));
    const cm = periodMonths(reportPeriod(currentNm));
    if (pm !== null && cm !== null && cm - pm !== 12) reasons.push(`기간 차이 ${cm - pm}개월`);
    if (reasons.length === 0) return null;
    return `${reportTitle(currentNm)} ↔ ${reportTitle(priorNm)}: ${PAIR_WARNING_TEXT} (${reasons.join(", ")})`;
  }

  // ----- 순수 함수: 보고서 짝 고르기 -----

  const docReportNm = (d) => (d && d.meta && d.meta.report_nm) || (d && d.title) || "";
  const docMonths = (d) => periodMonths(reportPeriod(docReportNm(d)));
  const byNewest = (a, b) => String(b.meta.rcept_dt || "").localeCompare(String(a.meta.rcept_dt || ""));

  // 같은 corp_code이고 접수일(rcept_dt)이 더 이른 문서를 같은 종류 / 다른 종류로 나눈다(각각 최근 것부터).
  //   당년 사업 2025.12, 같은 회사의 [분기 2025.09, 사업 2024.12, 사업 2023.12]
  //     → { same: [사업 2024.12, 사업 2023.12], other: [분기 2025.09] }
  function priorCandidates(current, docs) {
    const meta = (current && current.meta) || {};
    if (!meta.corp_code || !meta.rcept_dt) return { same: [], other: [] };
    const earlier = docs
      .filter(
        (d) =>
          d.id !== current.id &&
          d.meta.corp_code === meta.corp_code &&
          String(d.meta.rcept_dt || "") < String(meta.rcept_dt),
      )
      .sort(byNewest);
    const kind = docReportNm(current);
    return {
      same: earlier.filter((d) => sameReportKind(docReportNm(d), kind)),
      other: earlier.filter((d) => !sameReportKind(docReportNm(d), kind)),
    };
  }

  // 전년 자동 선택: ① 같은 종류이고 기간이 정확히 12개월 전 → ② 같은 종류 중 가장 최근 → ③ 다른 종류 중 가장 최근.
  //   당년 사업 2025.12, { same: [사업 2023.12(정정 접수가 늦음), 사업 2024.12], other: [분기 2025.09] } → 사업 2024.12
  function pickPrior(current, candidates) {
    const months = docMonths(current);
    const yearAgo = months === null ? null : candidates.same.find((d) => docMonths(d) === months - 12);
    return yearAgo || candidates.same[0] || candidates.other[0] || null;
  }

  // 당년 기본값(docs는 최근 것부터): 같은 종류 전년 후보가 있는 가장 최근 사업보고서 → 같은 종류 후보가 있는
  // 가장 최근 문서 → 전년 후보가 있는 가장 최근 문서 → 가장 최근 문서.
  //   [분기 2026.03, 사업 2025.12, 사업 2024.12] → 사업 2025.12 (분기 2026.03에는 같은 종류 후보가 없다)
  function pickCurrent(docs) {
    const withSame = docs.filter((d) => priorCandidates(d, docs).same.length > 0);
    const withAny = docs.find((d) => {
      const c = priorCandidates(d, docs);
      return c.same.length + c.other.length > 0;
    });
    return (
      withSame.find((d) => reportKind(docReportNm(d)).kind === "annual") ||
      withSame[0] ||
      withAny ||
      docs[0] ||
      null
    );
  }

  // 그 회사에서 경고 없이(같은 종류·12개월 전) 비교할 수 있는 가장 최근 짝. 사업보고서 짝을 먼저 찾는다.
  //   현대자동차 [분기 2026.03, 사업 2025.12, 사업 2024.12] → { current: 사업 2025.12, prior: 사업 2024.12 }
  function betterPair(corpCode, docs) {
    if (!corpCode) return null;
    const own = docs.filter((d) => d.meta.corp_code === corpCode);
    const annualFirst = [
      ...own.filter((d) => reportKind(docReportNm(d)).kind === "annual"),
      ...own.filter((d) => reportKind(docReportNm(d)).kind !== "annual"),
    ];
    for (const current of annualFirst) {
      const prior = pickPrior(current, priorCandidates(current, docs));
      if (prior && pairWarning(docReportNm(prior), docReportNm(current)) === null) return { current, prior };
    }
    return null;
  }

  // ----- 순수 함수: 주석 목록 -----

  const compact = (s) => String(s || "").replace(/\s+/g, "").toLowerCase();
  const noteCount = (n, key) => Number((n.counts && n.counts[key]) || 0);
  const textLines = (n) =>
    Number((n.line_stats && n.line_stats.text_added) || 0) + Number((n.line_stats && n.line_stats.text_removed) || 0);

  // noteNumber({ title: "35. 특수관계자 등과의 거래" }) → 35, noteNumber({ title: "기타" }) → Infinity
  function noteNumber(n) {
    const m = /^\s*(\d+)/.exec(String(n.title || ""));
    return m ? Number(m[1]) : Infinity;
  }

  // 목록용 제목: 범위는 화면 위에 이미 있으므로 끝의 "(연결)"/"(별도)"를 뺀다.
  //   listTitle("35. 특수관계자 등과의 거래 (연결)") → "35. 특수관계자 등과의 거래"
  function listTitle(title) {
    return String(title || "").replace(/\s*\((?:연결|별도)\)\s*$/, "") || String(title || "");
  }

  const NOTE_TESTS = {
    changed: (n) => n.status === "changed",
    added: (n) => n.status === "added",
    removed: (n) => n.status === "removed",
    prior_mismatch: (n) => noteCount(n, "prior_mismatch") > 0,
    large_change: (n) => noteCount(n, "large_change") > 0,
    watchlist: (n) => Boolean(n.watchlist),
    large: (n) => noteCount(n, "large_change") > 0,
    mismatch: (n) => noteCount(n, "prior_mismatch") > 0,
    text: (n) => textLines(n) > 0,
    added_removed: (n) => n.status === "added" || n.status === "removed",
  };

  // 주석 목록 필터(모든 조건 AND).
  //   filterNotes(notes, { changedOnly: true })              → status가 unchanged가 아닌 주석
  //   filterNotes(notes, { status: "prior_mismatch" })       → 전기 수치 불일치 행이 있는 주석
  //   filterNotes(notes, { chips: ["watchlist", "text"] })   → 관심 주석이면서 문장 추가+삭제가 1줄 이상
  //   filterNotes(notes, { search: "특수 관계" })            → 제목(공백 무시)에 "특수관계"가 있는 주석
  function filterNotes(notes, { changedOnly = false, status = null, chips = [], search = "" } = {}) {
    const query = compact(search);
    return notes.filter(
      (n) =>
        (!changedOnly || n.status !== "unchanged") &&
        (!status || !NOTE_TESTS[status] || NOTE_TESTS[status](n)) &&
        chips.every((c) => !NOTE_TESTS[c] || NOTE_TESTS[c](n)) &&
        (!query || compact(n.title).includes(query)),
    );
  }

  // 주석 목록 정렬(같으면 서버 관심도 순, 그다음 서버 순서).
  //   sortNotes(notes, "score")  → 서버 score 높은 순
  //   sortNotes(notes, "large")  → 큰 변동 수 많은 순
  //   sortNotes(notes, "text")   → 문장 변경(추가+삭제) 줄 많은 순
  //   sortNotes(notes, "number") → 제목 앞 주석 번호 순("3. 현금" < "12. 차입금"), 번호 없는 주석은 뒤
  function sortNotes(notes, sort = "score") {
    const keys = {
      score: (n) => -n.score,
      large: (n) => -noteCount(n, "large_change"),
      text: (n) => -textLines(n),
      number: noteNumber,
    };
    const key = keys[sort] || keys.score;
    const diff = (a, b) => {
      const x = key(a);
      const y = key(b);
      return x === y ? 0 : x < y ? -1 : 1;
    };
    return [...notes].sort((a, b) => diff(a, b) || b.score - a.score || a.index - b.index);
  }

  // ----- 순수 함수: 숫자 변동 표 -----

  // refLine("C12") → 12, refLine(null) → Infinity
  const refLine = (ref) => {
    const m = /^[PC](\d+)$/.exec(String(ref || ""));
    return m ? Number(m[1]) : Infinity;
  };

  // 숫자 변동(number_changes) → 표 행. 바뀐 칸(cells)이 있으면 칸마다 한 행이고, 없으면(행 추가·삭제, 값 개수
  // 다름) 변동 하나가 한 행이며 값 목록은 그대로 둔다(화면은 "n개 값"으로 접어 보여 준다).
  //   { kind: "yoy", label: "사용액", occurrence: 1, prior_values: ["835286"], current_values: ["1570778"],
  //     cells: [{ position: 1, delta: "735492", change_pct: "88.1", flags: ["large_change"], … }] }
  //     → [{ label: "사용액", occurrence: 1, position: null, prior: ["835286"], current: ["1570778"],
  //          delta: "735492", pct: "88.1", flags: ["large_change"], … }]
  function numberRows(changes) {
    const rows = [];
    for (const c of changes || []) {
      const priorValues = c.prior_values || [];
      const currentValues = c.current_values || [];
      const many = currentValues.length > 1 || priorValues.length > 1;
      const base = {
        kind: c.kind,
        table: c.table || "",
        label: c.label || "—",
        occurrence: Number(c.occurrence || 0),
        unit: c.unit || null,
        refs: [c.prior_ref && `전년 ${c.prior_ref}`, c.current_ref && `당년 ${c.current_ref}`]
          .filter(Boolean)
          .join(" · "),
        // 원문 순서: 당년 보고서의 줄 번호, 당년에 없는 행(행 삭제)은 전년 줄 번호
        line: c.current_ref ? refLine(c.current_ref) : refLine(c.prior_ref),
      };
      const kindFlags = c.kind === "prior_mismatch" ? ["prior_mismatch"] : [];
      if (c.cells && c.cells.length > 0) {
        for (const cell of c.cells) {
          rows.push({
            ...base,
            order: rows.length,
            position: many ? cell.position : null,
            prior: [cell.prior],
            current: [cell.current],
            delta: cell.delta,
            pct: cell.change_pct,
            flags: [...kindFlags, ...(cell.flags || []).filter((f) => !kindFlags.includes(f))],
            countMismatch: false,
          });
        }
      } else {
        const rowKind = c.kind === "row_added" || c.kind === "row_removed";
        rows.push({
          ...base,
          order: rows.length,
          position: null,
          prior: priorValues,
          current: currentValues,
          delta: null,
          pct: null,
          flags: rowKind ? [c.kind] : [...kindFlags, "value_count"],
          countMismatch: !rowKind,
        });
      }
    }
    return rows;
  }

  const NUMBER_ROW_TESTS = {
    all: () => true,
    large: (r) => r.flags.includes("large_change"),
    rows: (r) => r.kind === "row_added" || r.kind === "row_removed",
    count: (r) => r.countMismatch,
  };

  // 숫자 변동 행 필터.
  //   filterNumberRows(rows, { filter: "large" })      → 큰 변동 행
  //   filterNumberRows(rows, { filter: "rows" })       → 행 추가·삭제
  //   filterNumberRows(rows, { filter: "count" })      → 전년·당년 값 개수가 달라 칸 비교를 못 한 행
  //   filterNumberRows(rows, { search: "현대 카드" })   → 표 이름이나 행 이름(공백 무시)에 "현대카드"가 있는 행
  function filterNumberRows(rows, { filter = "all", search = "" } = {}) {
    const test = NUMBER_ROW_TESTS[filter] || NUMBER_ROW_TESTS.all;
    const query = compact(search);
    return rows.filter((r) => test(r) && (!query || compact(`${r.table} ${r.label}`).includes(query)));
  }

  // 숫자 변동 행 정렬(같으면 원문 순). 정렬용 크기만 Number로 비교하고 화면 값은 문자열 그대로 쓴다.
  //   sortNumberRows(rows, "abs_delta") → |증감| 큰 순(증감이 없는 행은 뒤)
  //   sortNumberRows(rows, "abs_pct")   → |증감률| 큰 순(증감률이 없는 행은 뒤)
  //   sortNumberRows(rows, "source")    → 원문 줄 순서
  function sortNumberRows(rows, sort = "abs_delta") {
    const field = sort === "abs_pct" ? "pct" : sort === "source" ? null : "delta";
    const size = (r) => {
      const value = r[field];
      if (value === null || value === undefined) return -1;
      const n = Math.abs(Number(value));
      return Number.isFinite(n) ? n : -1;
    };
    const source = (a, b) => (a.line === b.line ? 0 : a.line < b.line ? -1 : 1) || a.order - b.order;
    return [...rows].sort((a, b) => (field ? size(b) - size(a) : 0) || source(a, b));
  }

  // 같은 표 이름끼리 묶는다. 묶음 순서는 rows에서 처음 나온 순서이므로, 큰 순으로 정렬한 rows를 넣으면
  // 묶음 안 최댓값이 큰 묶음부터 나오고 묶음 안 행은 정렬 순서를 유지한다.
  //   groupNumberRows([{ table: "A", unit: "백만원" }, { table: "B", unit: null }, { table: "A", unit: "백만원" }])
  //     → [{ key: "A", table: "A", units: ["백만원"], rows: [A, A] }, { key: "B", table: "B", units: [], rows: [B] }]
  function groupNumberRows(rows) {
    const groups = new Map();
    for (const r of rows) {
      let g = groups.get(r.table);
      if (!g) {
        g = { key: r.table, table: r.table, units: [], rows: [] };
        groups.set(r.table, g);
      }
      if (r.unit && !g.units.includes(r.unit)) g.units.push(r.unit);
      g.rows.push(r);
    }
    return [...groups.values()];
  }

  // 상세 기본 탭: 전기 불일치나 큰 변동이 있으면 숫자, 아니면 문장 변경이 있으면 문장, 아니면 숫자.
  //   { counts: { prior_mismatch: 0, large_change: 2 }, … }                → "numbers"
  //   { counts: { …: 0 }, line_stats: { text_added: 3, text_removed: 0 } }  → "lines"
  function defaultDetailTab(n) {
    if (noteCount(n, "prior_mismatch") > 0 || noteCount(n, "large_change") > 0) return "numbers";
    return textLines(n) > 0 ? "lines" : "numbers";
  }

  // ----- 보고서 선택 -----

  // "현대자동차 · 사업보고서 2025.12 · 접수 2026-03-18" (접수번호는 option의 title로만)
  function reportLabel(d) {
    const meta = d.meta || {};
    const parts = [meta.corp_name, reportTitle(meta.report_nm || d.title)];
    if (meta.rcept_dt) parts.push(`접수 ${fmtDate(meta.rcept_dt)}`);
    return parts.filter(Boolean).join(" · ");
  }

  function reportOption(d, selectedId) {
    const rcept = (d.meta && d.meta.rcept_no) || d.source_ref;
    const attrs = { value: d.id };
    if (rcept) attrs.title = `접수번호 ${rcept}`;
    const option = el("option", { text: reportLabel(d), attrs });
    if (d.id === selectedId) option.selected = true;
    return option;
  }

  function fillGroupedSelect(select, groups, selectedId) {
    select.replaceChildren(
      ...groups
        .filter((g) => g.docs.length > 0)
        .map((g) => el("optgroup", { attrs: { label: g.label } }, g.docs.map((d) => reportOption(d, selectedId)))),
    );
  }

  function comparableDocs() {
    return state.docs.filter(isComparable).sort(byNewest);
  }

  function selectedDoc(id, docs) {
    return docs.find((d) => d.id === $(id).value) || null;
  }

  function updatePriorOptions() {
    const docs = comparableDocs();
    const current = selectedDoc("diff-current", docs);
    const candidates = priorCandidates(current, docs);
    const pick = pickPrior(current, candidates);
    fillGroupedSelect(
      $("diff-prior"),
      [
        { label: "같은 종류", docs: candidates.same },
        { label: "다른 종류 (권장하지 않음)", docs: candidates.other },
      ],
      pick ? pick.id : null,
    );
    const empty = candidates.same.length + candidates.other.length === 0;
    $("diff-prior-empty").hidden = !current || !empty;
    $("diff-btn").disabled = empty;
    updatePairWarning();
  }

  function selectPair(pair) {
    $("diff-current").value = pair.current.id;
    updatePriorOptions();
    $("diff-prior").value = pair.prior.id;
    updatePairWarning();
  }

  // 실행 전 경고: 고른 두 보고서가 같은 종류의 1년 전 보고서가 아니면 이유를 보이고 버튼 문구를 바꾼다(막지 않는다).
  function updatePairWarning() {
    const docs = comparableDocs();
    const current = selectedDoc("diff-current", docs);
    const prior = selectedDoc("diff-prior", docs);
    const warning = current && prior ? pairWarning(docReportNm(prior), docReportNm(current)) : null;
    const box = $("diff-pair-warning");
    const button = $("diff-btn");
    button.textContent = warning === null ? "비교" : "그래도 비교";
    button.classList.toggle("warn", warning !== null);
    box.replaceChildren();
    box.hidden = warning === null;
    if (warning === null) return;
    box.append(el("span", { className: "warn-icon", text: "⚠", attrs: { "aria-hidden": "true" } }), ` ${warning}`);
    const better = betterPair(current.meta.corp_code, docs);
    if (better) {
      const fix = el("button", {
        className: "small",
        text: `${reportShort(docReportNm(better.current))} ↔ ${reportShort(docReportNm(better.prior))}로 바꾸기`,
        attrs: { type: "button" },
      });
      fix.addEventListener("click", () => selectPair(better));
      box.append(" ", fix);
    }
  }

  async function loadDiffTab() {
    await fetchDocuments();
    const docs = comparableDocs();
    const previous = $("diff-current").value;
    const previousPrior = $("diff-prior").value;
    const keep = docs.find((d) => d.id === previous) || null;
    const pick = keep || pickCurrent(docs);
    // 당년 보고서는 회사별로 묶는다(최근 문서가 있는 회사부터).
    const groups = new Map();
    for (const d of docs) {
      const key = d.meta.corp_code || d.meta.corp_name || "";
      if (!groups.has(key)) groups.set(key, { label: d.meta.corp_name || d.meta.corp_code || "회사", docs: [] });
      groups.get(key).docs.push(d);
    }
    fillGroupedSelect($("diff-current"), [...groups.values()], pick ? pick.id : null);
    $("diff-docs-empty").hidden = docs.length > 0;
    updatePriorOptions();
    if (keep && [...$("diff-prior").options].some((o) => o.value === previousPrior)) {
      $("diff-prior").value = previousPrior;
      updatePairWarning();
    }
    await loadRecentComparisons();
  }

  // "현대자동차 · 사업 2024.12 → 사업 2025.12 · 연결" (같은 종류의 1년 전 보고서가 아닌 비교는 앞에 ⚠)
  function recentLabel(c) {
    const scope = c.scope === "separate" ? "별도" : "연결";
    const warn = pairWarning(c.prior_report_nm, c.current_report_nm) ? "⚠ " : "";
    const prior = c.prior_report_nm ? reportShort(c.prior_report_nm) : c.prior_rcept_no;
    const current = c.current_report_nm ? reportShort(c.current_report_nm) : c.current_rcept_no;
    return `${warn}${c.corp_name || "회사"} · ${prior} → ${current} · ${scope}`;
  }

  async function loadRecentComparisons() {
    const { data } = await api("/comparisons");
    const items = Array.isArray(data) ? data : [];
    $("diff-recent").replaceChildren(
      ...items.slice(0, 8).map((c) => {
        const warning = pairWarning(c.prior_report_nm, c.current_report_nm);
        const title = [`${c.prior_rcept_no} → ${c.current_rcept_no}`, fmtDateTime(c.created_at), warning];
        const button = el("button", {
          className: "small",
          text: recentLabel(c),
          attrs: { type: "button", title: title.filter(Boolean).join(" · ") },
        });
        button.addEventListener("click", guard(() => openComparison(c.id)));
        return el("li", {}, [button]);
      }),
    );
    $("diff-recent-empty").hidden = items.length > 0;
  }

  function selectedScope() {
    const checked = document.querySelector('input[name="diff-scope"]:checked');
    return checked ? checked.value : "consolidated";
  }

  async function runComparison(ev) {
    if (ev) ev.preventDefault();
    const prior = $("diff-prior").value;
    const current = $("diff-current").value;
    if (!prior || !current) {
      showAlert("전년 보고서를 먼저 가져오세요.");
      return;
    }
    setBusy($("diff-btn"), $("diff-spinner"), true);
    try {
      const { data } = await api("/comparisons", {
        method: "POST",
        json: { prior_document_id: prior, current_document_id: current, scope: selectedScope() },
      });
      showComparison(data);
      await loadRecentComparisons();
    } finally {
      setBusy($("diff-btn"), $("diff-spinner"), false);
    }
  }

  async function openComparison(id) {
    const { data } = await api(`/comparisons/${encodeURIComponent(id)}`);
    showComparison(data);
  }

  // ----- 비교 결과 -----

  function freshDiffView() {
    return {
      status: null, // 요약 카드 필터: changed | added | removed | prior_mismatch | large_change
      search: "", // 주석 제목 검색
      chips: [], // 주석 칩(AND): watchlist | large | mismatch | text | added_removed
      sort: "score", // score | large | text | number
      tab: null, // 사용자가 고른 상세 탭(null이면 주석마다 기본 탭)
      numFilter: "all", // all | large | rows | count
      numSort: "abs_delta", // abs_delta | abs_pct | source
      numSearch: "",
      numLimit: NUMBER_ROWS_PAGE,
      collapsed: [], // 접은 표 묶음 ("num:표 이름" / "mis:표 이름")
    };
  }

  function resetNoteView() {
    state.diffView.numLimit = NUMBER_ROWS_PAGE;
    state.diffView.collapsed = [];
  }

  function showComparison(data) {
    state.comparison = data;
    state.diffSummaries = data.summaries || {};
    state.diffView = freshDiffView();
    $("diff-note-search").value = "";
    $("diff-note-sort").value = "score";
    const notes = data.result.notes || [];
    const first = visibleNotes()[0] || notes[0];
    state.diffNote = first ? first.index : null;
    renderDiffStats();
    renderNoteList();
    renderNoteDetail();
    $("diff-result").hidden = false;
  }

  function allNotes() {
    return (state.comparison && state.comparison.result.notes) || [];
  }

  function visibleNotes() {
    const view = state.diffView || freshDiffView();
    const filtered = filterNotes(allNotes(), {
      changedOnly: $("diff-changed-only").checked,
      status: view.status,
      chips: view.chips,
      search: view.search,
    });
    return sortNotes(filtered, view.sort);
  }

  function renderDiffStats() {
    const r = state.comparison.result;
    const s = r.summary;
    const counts = s.status_counts || {};
    const threshold = Math.round(Number(r.settings && r.settings.change_threshold) * 100);
    const cards = [
      ["", "당년 주석", s.current_notes, "필터 해제(전체 주석)"],
      ["changed", "변경", counts.changed],
      ["added", "신규", counts.added],
      ["removed", "삭제", counts.removed],
      ["prior_mismatch", "전기 수치 불일치", s.prior_mismatch, "전기 수치 불일치 행이 있는 주석만"],
      [
        "large_change",
        `큰 변동(±${Number.isFinite(threshold) ? threshold : 30}%↑)`,
        s.large_change,
        "큰 변동 행이 있는 주석만",
      ],
    ];
    const nodes = cards.map(([key, label, value, help]) => {
      const button = el(
        "button",
        {
          className: "stat-card",
          attrs: { type: "button", "aria-pressed": "false", "data-stat": key, title: help || `${label} 주석만` },
        },
        [el("span", { text: label }), el("b", { text: fmtInt(value) })],
      );
      button.addEventListener("click", () => {
        const view = state.diffView;
        view.status = key === "" || view.status === key ? null : key;
        syncNoteFilters();
        refreshNotes();
      });
      return button;
    });
    nodes.push(
      el("div", { className: "stat-card static", attrs: { title: AMBIGUOUS_ROWS_HELP } }, [
        el("span", { text: "비교 보류 행(중복) ⓘ" }),
        el("b", { text: fmtInt(s.ambiguous_rows) }),
        el("span", { className: "sr-only", text: AMBIGUOUS_ROWS_HELP }),
      ]),
    );
    $("diff-stats").replaceChildren(...nodes);
    syncNoteFilters();
    renderDiffWarnings();
  }

  // 서버 경고 + 보고서 짝 경고. 보고서 종류 경고이면 "같은 종류의 1년 전 보고서로 다시 비교" 버튼을 붙인다.
  function renderDiffWarnings() {
    const r = state.comparison.result;
    const box = $("diff-warnings");
    const serverWarnings = r.warnings || [];
    const pair = pairWarning(r.prior && r.prior.report_nm, r.current && r.current.report_nm);
    const messages = pair ? [...serverWarnings, pair] : [...serverWarnings];
    box.replaceChildren();
    box.hidden = messages.length === 0;
    if (messages.length === 0) return;
    box.append(
      el("span", { className: "warn-icon", text: "⚠", attrs: { "aria-hidden": "true" } }),
      el("span", { className: "sr-only", text: "경고: " }),
      el("span", { className: "msg", text: messages.join(" ") }),
    );
    const kindWarning = pair !== null || serverWarnings.some((w) => w.includes("보고서명에"));
    const better = kindWarning ? betterPair(r.current && r.current.corp_code, comparableDocs()) : null;
    const same =
      better &&
      better.current.id === (r.current && r.current.document_id) &&
      better.prior.id === (r.prior && r.prior.document_id);
    if (!better || same) return;
    const button = el("button", {
      className: "small",
      text: "같은 종류의 1년 전 보고서로 다시 비교",
      attrs: {
        type: "button",
        title: `${reportShort(docReportNm(better.prior))} → ${reportShort(docReportNm(better.current))}`,
      },
    });
    button.addEventListener(
      "click",
      guard(async () => {
        selectPair(better);
        const scope = document.querySelector(`input[name="diff-scope"][value="${state.comparison.scope}"]`);
        if (scope) scope.checked = true;
        await runComparison();
      }),
    );
    box.append(button);
  }

  // 카드·칩의 aria-pressed를 state.diffView와 맞춘다.
  function syncNoteFilters() {
    const view = state.diffView || freshDiffView();
    for (const card of $("diff-stats").querySelectorAll("button[data-stat]")) {
      const key = card.dataset.stat;
      card.setAttribute("aria-pressed", String(key === "" ? view.status === null : view.status === key));
    }
    for (const chip of document.querySelectorAll("[data-note-chip]")) {
      chip.setAttribute("aria-pressed", String(view.chips.includes(chip.dataset.noteChip)));
    }
  }

  // 필터가 바뀌면 목록을 다시 그리고, 고른 주석이 목록에서 빠지면 첫 주석을 보여 준다.
  function refreshNotes() {
    if (!state.comparison) return;
    const notes = visibleNotes();
    if (notes.length > 0 && !notes.some((n) => n.index === state.diffNote)) {
      state.diffNote = notes[0].index;
      resetNoteView();
      renderNoteDetail();
    }
    renderNoteList();
  }

  function noteBadges(n) {
    const badges = [
      el("span", { className: `badge ${NOTE_STATUS_CLASS[n.status] || "b-muted"}`, text: NOTE_STATUS_LABELS[n.status] || n.status }),
    ];
    if (n.watchlist) badges.push(el("span", { className: "badge b-wait", text: "관심 주석" }));
    if (noteCount(n, "large_change") > 0) {
      badges.push(el("span", { className: "badge b-fail", text: `큰 변동 ${noteCount(n, "large_change")}` }));
    }
    if (noteCount(n, "prior_mismatch") > 0) {
      badges.push(el("span", { className: "badge b-wait", text: `전기 불일치 ${noteCount(n, "prior_mismatch")}` }));
    }
    return badges;
  }

  function noteListNote(n) {
    const ls = n.line_stats;
    if (n.status === "added") return "전년 보고서에 없는 주석";
    if (n.status === "removed") return "당년 보고서에 없는 주석";
    const parts = [];
    if (ls.text_added || ls.text_removed) parts.push(`문장 +${ls.text_added} / −${ls.text_removed}`);
    if (ls.numbers_only) parts.push(`숫자만 변경 ${ls.numbers_only}줄`);
    return parts.join(" · ") || "변동 없음";
  }

  function renderNoteList({ focusIndex = null } = {}) {
    const notes = visibleNotes();
    $("diff-notes").replaceChildren(
      ...notes.map((n) => {
        const on = n.index === state.diffNote;
        const button = el(
          "button",
          { className: `note-item${on ? " sel" : ""}`, attrs: { type: "button", "data-note-index": n.index, title: n.title } },
          [
            el("span", { className: "t", text: listTitle(n.title) }),
            el("span", { className: "row" }, noteBadges(n)),
            el("span", { className: "note", text: noteListNote(n) }),
          ],
        );
        if (on) button.setAttribute("aria-current", "true");
        button.addEventListener("click", () => {
          if (state.diffNote !== n.index) resetNoteView();
          state.diffNote = n.index;
          renderNoteList({ focusIndex: n.index });
          renderNoteDetail();
        });
        return el("li", {}, [button]);
      }),
    );
    $("diff-notes-count").textContent = `${fmtInt(allNotes().length)}개 중 ${fmtInt(notes.length)}개 표시`;
    $("diff-notes-empty").hidden = notes.length > 0;
    if (focusIndex !== null) {
      const target = $("diff-notes").querySelector(`button[data-note-index="${focusIndex}"]`);
      if (target) target.focus();
    }
  }

  function currentNote() {
    return allNotes().find((n) => n.index === state.diffNote) || null;
  }

  // ----- 주석 상세 -----

  function flagBadge(flag) {
    const [cls, label] = FLAG_BADGES[flag] || ["b-muted", flag];
    return el("span", { className: `badge ${cls}`, text: label });
  }

  function valuesCell(values, unit) {
    const suffix = unit === "%" ? "%" : "";
    if (!values || values.length === 0) return el("td", { className: "n", text: "—" });
    if (values.length === 1) return el("td", { className: "n", text: `${fmtDecimal(values[0])}${suffix}` });
    // 값이 여러 개면 한 줄로 잇지 않고 개수만 보인 뒤 펼친다(값마다 한 줄).
    const list = el(
      "ol",
      { className: "vals-list" },
      values.map((v, i) => el("li", { text: `${fmtDecimal(v)}${suffix}`, attrs: { title: `${i + 1}열` } })),
    );
    return el("td", { className: "n many" }, [
      el("details", { className: "vals" }, [el("summary", { text: `${fmtInt(values.length)}개 값` }), list]),
    ]);
  }

  function signedCell(value, suffix = "") {
    if (value === null || value === undefined) return el("td", { className: "n", text: "—" });
    const dir = signOf(value);
    return el("td", {
      className: `n${dir > 0 ? " up" : dir < 0 ? " down" : ""}`,
      text: `${fmtDecimal(value, { sign: true })}${suffix}`,
    });
  }

  function numberRowNode(r, mismatch) {
    const label = el("td", { className: "lbl", attrs: r.refs ? { title: `원문 ${r.refs}` } : {} }, [
      r.label,
      r.occurrence > 0 ? el("span", { className: "occ", text: ` (${r.occurrence + 1}번째)` }) : null,
      r.position ? el("span", { className: "occ", text: ` (${r.position}열)` }) : null,
    ]);
    return el("tr", { className: `r${mismatch ? " mis" : ""}` }, [
      label,
      valuesCell(r.prior, r.unit),
      valuesCell(r.current, r.unit),
      signedCell(r.delta),
      signedCell(r.pct, "%"),
      el("td", { className: "flags" }, r.flags.map(flagBadge)),
    ]);
  }

  function groupMeta(g, count) {
    const parts = [];
    if (g.units.length > 0) parts.push(`단위 ${g.units.join(", ")}`);
    parts.push(`${fmtInt(count)}행`);
    return ` · ${parts.join(" · ")}`;
  }

  // 표 이름 묶음: 전체 폭 머리줄을 누르면 그 묶음의 행을 접는다(상태는 state.diffView.collapsed).
  function groupBody(g, prefix, count, mismatch) {
    const key = `${prefix}:${g.key}`;
    const view = state.diffView;
    const collapsed = view.collapsed.includes(key);
    const arrow = el("span", { text: collapsed ? "▸ " : "▾ ", attrs: { "aria-hidden": "true" } });
    const toggle = el(
      "button",
      { className: "group-toggle", attrs: { type: "button", "aria-expanded": String(!collapsed) } },
      [arrow, g.table || "(표 이름 없음)", el("span", { className: "muted", text: groupMeta(g, count) })],
    );
    const rows = g.rows.map((r) => numberRowNode(r, mismatch));
    for (const tr of rows) tr.hidden = collapsed;
    toggle.addEventListener("click", () => {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(open));
      arrow.textContent = open ? "▾ " : "▸ ";
      for (const tr of rows) tr.hidden = !open;
      view.collapsed = open ? view.collapsed.filter((k) => k !== key) : [...view.collapsed, key];
    });
    return el("tbody", {}, [
      el("tr", { className: "group" }, [
        el("th", { attrs: { colspan: NUMBER_COLUMNS.length, scope: "colgroup" } }, [toggle]),
      ]),
      ...rows,
    ]);
  }

  function numberTable(groups, { headers, mismatch = false, totals = null }) {
    const prefix = mismatch ? "mis" : "num";
    const table = el("table", { className: `num-table${mismatch ? " mismatch" : ""}` }, [
      el("colgroup", {}, NUMBER_COLUMNS.map((c) => el("col", { className: c }))),
      el("thead", {}, [
        el(
          "tr",
          {},
          headers.map((h, i) => el("th", { className: i >= 1 && i <= 4 ? "n" : "", text: h, attrs: { scope: "col" } })),
        ),
      ]),
      ...groups.map((g) => groupBody(g, prefix, (totals && totals.get(g.key)) || g.rows.length, mismatch)),
    ]);
    return el("div", { className: "table-wrap" }, [table]);
  }

  function renderNumberResults(rows, results) {
    const view = state.diffView;
    const filtered = sortNumberRows(
      filterNumberRows(rows, { filter: view.numFilter, search: view.numSearch }),
      view.numSort,
    );
    if (filtered.length === 0) {
      results.replaceChildren(el("p", { className: "note", text: "조건에 맞는 행이 없습니다." }));
      return;
    }
    const shown = filtered.slice(0, view.numLimit);
    const totals = new Map(groupNumberRows(filtered).map((g) => [g.key, g.rows.length]));
    const more = el("div", { className: "more" });
    if (shown.length < filtered.length) {
      const button = el("button", {
        className: "small",
        text: `더 보기 (${fmtInt(Math.min(NUMBER_ROWS_PAGE, filtered.length - shown.length))}행)`,
        attrs: { type: "button" },
      });
      button.addEventListener("click", () => {
        view.numLimit += NUMBER_ROWS_PAGE;
        renderNumberResults(rows, results);
        const next = results.querySelector(".more button");
        if (next) next.focus();
      });
      more.append(button, " ");
    }
    more.append(el("span", { className: "note", text: `${fmtInt(filtered.length)}행 중 ${fmtInt(shown.length)}행 표시` }));
    results.replaceChildren(numberTable(groupNumberRows(shown), { headers: NUMBER_HEADERS, totals }), more);
  }

  function numberToolbar(rows, results) {
    const view = state.diffView;
    const chips = NUMBER_FILTERS.map(([key, label]) =>
      el("button", {
        className: "chip",
        text: `${label} ${fmtInt(filterNumberRows(rows, { filter: key }).length)}`,
        attrs: { type: "button", "aria-pressed": String(view.numFilter === key), "data-num-filter": key },
      }),
    );
    for (const chip of chips) {
      chip.addEventListener("click", () => {
        view.numFilter = chip.dataset.numFilter;
        view.numLimit = NUMBER_ROWS_PAGE;
        for (const other of chips) other.setAttribute("aria-pressed", String(other === chip));
        renderNumberResults(rows, results);
      });
    }
    const sort = el(
      "select",
      { attrs: { "aria-label": "숫자 변동 정렬" } },
      NUMBER_SORTS.map(([key, label]) => {
        const option = el("option", { text: `정렬: ${label}`, attrs: { value: key } });
        if (key === view.numSort) option.selected = true;
        return option;
      }),
    );
    sort.addEventListener("change", () => {
      view.numSort = sort.value;
      view.numLimit = NUMBER_ROWS_PAGE;
      renderNumberResults(rows, results);
    });
    const search = el("input", {
      attrs: { type: "search", placeholder: "표·행 검색", "aria-label": "표·행 검색", autocomplete: "off" },
    });
    search.value = view.numSearch;
    search.addEventListener("input", () => {
      view.numSearch = search.value;
      view.numLimit = NUMBER_ROWS_PAGE;
      renderNumberResults(rows, results);
    });
    return el("div", { className: "num-toolbar" }, [
      el("div", { className: "chips", attrs: { role: "group", "aria-label": "숫자 변동 필터" } }, chips),
      sort,
      search,
    ]);
  }

  function numberPane(n, rows) {
    const mismatches = rows.filter((r) => r.kind === "prior_mismatch");
    const others = rows.filter((r) => r.kind !== "prior_mismatch");
    const out = [];
    if (mismatches.length > 0) {
      out.push(
        el("h4", { text: `전기 수치 불일치 — 당년 보고서의 전기 값 vs 전년 보고서의 당기 값 (${fmtInt(mismatches.length)})` }),
        numberTable(groupNumberRows(mismatches), { headers: MISMATCH_HEADERS, mismatch: true }),
        el("p", {
          className: "note",
          text: "전기 수치 불일치 = 당년 보고서의 ‘전기’ 값이 전년 보고서의 ‘당기’ 값과 다름. 재작성·재분류 후보이며 판단은 담당자가 합니다.",
        }),
      );
    }
    out.push(el("h4", { text: "전년 대비 숫자 변동 (코드 계산)" }));
    if (others.length === 0) {
      out.push(el("p", { className: "note", text: "전년 대비 숫자 변동이 없습니다." }));
    } else {
      const results = el("div", { className: "num-results" });
      out.push(numberToolbar(others, results), results);
      renderNumberResults(others, results);
    }
    if (n.ambiguous_rows) {
      out.push(el("p", { className: "note", text: `비교 보류 행 ${fmtInt(n.ambiguous_rows)}: ${AMBIGUOUS_ROWS_HELP}` }));
    }
    out.push(
      el("p", {
        className: "note",
        text: "숫자는 코드가 원문 표에서 계산한 값입니다. 단위는 원문 ‘(단위 : …)’ 표기를 따르며, 표기가 없거나 이전에 저장한 비교이면 표시하지 않습니다.",
      }),
    );
    return out;
  }

  // " | "로 나뉜 표 줄은 셀 구분자를 흐리게 보여 준다.
  function lineText(text) {
    const out = [];
    String(text).split(" | ").forEach((part, i) => {
      if (i > 0) out.push(el("span", { className: "cellsep", text: " | " }));
      out.push(part);
    });
    return out;
  }

  function lineNode(e) {
    const sign = e.op === "delete" ? "−" : "+";
    return el("div", { className: e.op === "delete" ? "del" : "add", attrs: { id: `diff-line-${e.ref}`, tabindex: "-1" } }, [
      el("span", { className: "ln", text: e.ref }),
      `${sign} `,
      ...lineText(e.text),
    ]);
  }

  function lineDiffNode(n) {
    const text = n.lines.filter((e) => !e.numbers_only);
    const numbersOnly = n.lines.filter((e) => e.numbers_only);
    const box = el("div", { className: "difflines", attrs: { "aria-label": "줄 비교" } });
    if (n.line_stats.equal > 0) {
      box.append(el("div", { className: "fold", text: `… 동일한 ${fmtInt(n.line_stats.equal)}줄 생략 …` }));
    }
    for (const e of text) box.append(lineNode(e));
    if (numbersOnly.length > 0) {
      const details = el("details", { className: "fold-numbers" }, [
        el("summary", { text: `▸ 숫자만 바뀐 줄 ${fmtInt(n.line_stats.numbers_only)}쌍 (펼치기)` }),
        ...numbersOnly.map(lineNode),
      ]);
      box.append(details);
    }
    if (n.lines.length === 0) box.append(el("div", { className: "fold", text: "바뀐 줄이 없습니다." }));
    if (n.truncated) {
      box.append(el("div", { className: "fold", text: "줄이 많아 앞부분만 저장했습니다(NOTE_DIFF_MAX_LINES)." }));
    }
    return box;
  }

  // 요약의 근거 줄 링크: 문장 변경 탭으로 바꾼 뒤 그 줄로 이동한다.
  function scrollToLine(ref) {
    const node = $(`diff-line-${ref}`);
    if (!node) {
      showAlert(`근거 줄 ${ref}은 화면에 표시된 줄 목록에 없습니다.`, "info");
      return;
    }
    selectDetailTab("lines");
    const details = node.closest("details");
    if (details) details.open = true;
    for (const other of document.querySelectorAll(".difflines .hl")) other.classList.remove("hl");
    node.classList.add("hl");
    const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    node.scrollIntoView({ block: "center", behavior: smooth ? "smooth" : "auto" });
    node.focus({ preventScroll: true });
  }

  function summaryNode(n) {
    const box = el("div", { className: "sum", attrs: { "aria-live": "polite" } });
    const summary = state.diffSummaries[String(n.index)];
    const button = el("button", {
      className: "small primary",
      text: summary ? "요약 다시 생성 (로컬 LLM)" : "요약 생성 (로컬 LLM)",
      attrs: { type: "button" },
    });
    const spinner = el("span", { className: "spinner", attrs: { "aria-label": "요약 생성 중" } });
    spinner.hidden = true;
    const head = el("div", { className: "row" }, [button, spinner]);
    box.append(head);
    if (n.status === "unchanged") {
      button.disabled = true;
      box.append(el("p", { className: "note", text: "변동이 없는 주석은 요약하지 않습니다." }));
    }
    const errorBox = el("p", { className: "notice", attrs: { role: "status" } });
    errorBox.hidden = true;
    box.append(errorBox);
    if (summary) {
      const meta = [summary.model];
      if (summary.latency_ms !== null && summary.latency_ms !== undefined) {
        meta.push(`${(summary.latency_ms / 1000).toFixed(1)}초`);
      }
      if (summary.cost_usd !== null && summary.cost_usd !== undefined) meta.push(`비용 $${summary.cost_usd}`);
      head.append(el("span", { className: "note", text: meta.join(" · ") }));
      if (summary.status === "ok") {
        head.append(el("span", { className: "badge b-ok", text: "● 숫자 원문 확인됨" }));
      } else {
        const numbers = summary.ungrounded_numbers.length > 0 ? summary.ungrounded_numbers.join(", ") : "없음";
        head.append(
          el("span", {
            className: "badge b-wait",
            text: `확인 필요: 원문에 없는 숫자 ${numbers}`,
          }),
        );
      }
      const list = el("ul", {}, summary.bullets.map((b) => {
        const refs = b.refs.map((ref) => {
          const link = el("button", { className: "ref", text: `[${ref}]`, attrs: { type: "button", "aria-label": `근거 줄 ${ref}로 이동(문장 변경 탭)` } });
          link.addEventListener("click", () => scrollToLine(ref));
          return link;
        });
        const warn = [];
        if (!b.grounded) {
          const why = b.ungrounded_numbers.length > 0 ? `원문에 없는 숫자 ${b.ungrounded_numbers.join(", ")}` : "유효한 근거 줄 없음";
          warn.push(el("span", { className: "badge b-wait", text: `확인 필요: ${why}` }));
        }
        return el("li", {}, [`${b.text} `, ...refs, ...warn]);
      }));
      box.append(list);
      if (summary.bullets.length === 0) box.append(el("p", { className: "note", text: "모델이 요약 항목을 만들지 않았습니다." }));
    }
    box.append(el("p", { className: "note sum-notice", text: SUMMARY_NOTICE }));
    button.addEventListener("click", guard(() => requestSummary(n, button, spinner, errorBox)));
    return box;
  }

  async function requestSummary(n, button, spinner, errorBox) {
    const id = state.comparison.id;
    setBusy(button, spinner, true);
    errorBox.hidden = true;
    try {
      const { status, data } = await api(
        `/comparisons/${encodeURIComponent(id)}/notes/${n.index}/summary`,
        { method: "POST", allow: SUMMARY_ERROR_STATUSES },
      );
      if (SUMMARY_ERROR_STATUSES.includes(status)) {
        // 501(Claude 미지원)·503(Ollama 연결 실패)·504(시간 초과) 메시지를 그대로 보여 준다.
        errorBox.textContent = `요약 실패 (${status}): ${formatDetail(data, status)}`;
        errorBox.hidden = false;
        return;
      }
      state.diffSummaries[String(n.index)] = data;
      if (state.comparison.id === id && state.diffNote === n.index) renderNoteDetail();
    } finally {
      setBusy(button, spinner, false);
    }
  }

  function detailHead(n) {
    const r = state.comparison.result;
    const side = (label, info) => [
      `${label} ${info && info.report_nm ? reportShort(info.report_nm) : "보고서"} · `,
      dartLink(info && info.dart_url, "DART 원문 ↗"),
    ];
    const matched =
      n.matched_by === "key" ? "짝짓기: key 일치" : n.matched_by === "similar_title" ? "짝짓기: 유사 제목" : "짝 없음";
    const status = `상태: ${NOTE_STATUS_LABELS[n.status] || n.status}`;
    const ambiguous = n.ambiguous_rows ? ` · 비교 보류 행 ${fmtInt(n.ambiguous_rows)}` : "";
    return [
      el("h3", { className: "diff-title" }, [n.title, " ", ...noteBadges(n)]),
      el("div", { className: "diff-sides" }, [
        ...side("당년", r.current),
        el("span", { className: "sep", text: " | ", attrs: { "aria-hidden": "true" } }),
        ...side("전년", r.prior),
      ]),
      el("details", { className: "diff-info" }, [
        el("summary", { text: "경로·짝짓기 정보" }),
        el("div", { className: "path", text: `당년: ${n.current_section_path || "(해당 주석 없음)"}` }),
        el("div", { className: "path", text: `전년: ${n.prior_section_path || "(해당 주석 없음)"}` }),
        el("p", { className: "note", text: `${matched} · ${status}${ambiguous}` }),
      ]),
    ];
  }

  // 상세 내부 탭 표시. selectDetailTab은 사용자가 고른 탭으로 기억해 같은 비교의 다른 주석에서도 유지한다.
  function showDetailTab(name, { focus = false } = {}) {
    const tab = DETAIL_TABS.includes(name) ? name : "numbers";
    for (const button of document.querySelectorAll("[data-diff-tab]")) {
      const on = button.dataset.diffTab === tab;
      button.setAttribute("aria-selected", String(on));
      button.tabIndex = on ? 0 : -1;
      if (on && focus) button.focus();
    }
    for (const key of DETAIL_TABS) $(`diff-pane-${key}`).hidden = key !== tab;
  }

  function selectDetailTab(name, { focus = false } = {}) {
    if (state.diffView) state.diffView.tab = name;
    showDetailTab(name, { focus });
  }

  function onDetailTabKeydown(ev) {
    const tabs = [...document.querySelectorAll("[data-diff-tab]")];
    const index = tabs.indexOf(ev.currentTarget);
    let next = null;
    if (ev.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
    else if (ev.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
    else if (ev.key === "Home") next = tabs[0];
    else if (ev.key === "End") next = tabs[tabs.length - 1];
    if (next) {
      ev.preventDefault();
      selectDetailTab(next.dataset.diffTab, { focus: true });
    }
  }

  function renderNoteDetail() {
    const n = state.comparison ? currentNote() : null;
    $("diff-detail-empty").hidden = Boolean(n);
    $("diff-subtabs").hidden = !n;
    if (!n) {
      $("diff-detail-head").replaceChildren();
      for (const key of DETAIL_TABS) {
        $(`diff-pane-${key}`).replaceChildren();
        $(`diff-pane-${key}`).hidden = true;
      }
      return;
    }
    const rows = numberRows(n.number_changes);
    const ls = n.line_stats;
    $("diff-detail-head").replaceChildren(...detailHead(n));
    $("diff-tab-numbers").textContent = `숫자 변동 (${fmtInt(rows.length)})`;
    $("diff-tab-lines").textContent = `문장 변경 (${fmtInt(textLines(n))})`;
    $("diff-tab-lines").title = `추가 ${fmtInt(ls.text_added)}줄 · 삭제 ${fmtInt(ls.text_removed)}줄 · 숫자만 바뀐 줄 ${fmtInt(ls.numbers_only)}쌍`;
    $("diff-pane-numbers").replaceChildren(...numberPane(n, rows));
    $("diff-pane-lines").replaceChildren(lineDiffNode(n));
    $("diff-pane-summary").replaceChildren(summaryNode(n));
    showDetailTab(state.diffView.tab || defaultDetailTab(n));
  }

  // ---------- reviews tab ----------

  function setReviewCount(count) {
    const badge = $("review-count");
    badge.textContent = count >= 200 ? "200+" : String(count);
    badge.hidden = count === 0;
  }

  async function loadReviewCount() {
    const { data } = await api("/reviews?status=pending_review&limit=200");
    setReviewCount(Array.isArray(data) ? data.length : 0);
  }

  async function loadReviews() {
    const { data } = await api("/reviews?status=pending_review&limit=200");
    const items = Array.isArray(data) ? data : [];
    setReviewCount(items.length);
    $("reviews-list").replaceChildren(...items.map(buildReviewCard));
    $("reviews-empty").hidden = items.length > 0;
  }

  function buildReviewCard(item) {
    const card = el("article", { className: "card", attrs: { "aria-label": "검토 대기 답변" } });
    card.append(
      el("div", { className: "answer-head" }, [
        el("h2", { text: `Q. ${item.question}` }),
        el("div", { className: "row" }, [
          statusBadge(item.status),
          el("span", { className: "note", text: `사유: ${item.review_reason || "—"}` }),
        ]),
      ]),
      el("p", { className: "note", text: `생성: ${fmtDateTime(item.created_at)}` }),
    );
    appendAnswerBody(card, item.text, item.footnotes, item.status);

    const commentId = `comment-${item.answer_id}`;
    const comment = el("textarea", {
      attrs: { id: commentId, maxlength: "2000", placeholder: "코멘트 (선택)" },
    });
    const approve = el("button", { className: "primary", text: "승인", attrs: { type: "button" } });
    const reject = el("button", { className: "danger", text: "반려", attrs: { type: "button" } });
    const decide = (decision) =>
      guard(() => decideReview(item.answer_id, decision, comment.value.trim(), [approve, reject]));
    approve.addEventListener("click", decide("approve"));
    reject.addEventListener("click", decide("reject"));
    card.append(
      el("div", { className: "review-actions" }, [
        el("label", { text: "코멘트(선택)", attrs: { for: commentId } }),
        comment,
        el("div", { className: "row" }, [approve, reject]),
      ]),
    );
    return card;
  }

  async function decideReview(answerId, decision, comment, buttons) {
    for (const b of buttons) b.disabled = true;
    try {
      const { status, data } = await api(`/reviews/${encodeURIComponent(answerId)}`, {
        method: "POST",
        json: { decision, comment: comment || null },
        allow: [409],
      });
      if (status === 409) {
        showAlert(`이미 다른 검토자가 처리함 (${formatDetail(data, status)})`, "info");
      } else {
        showAlert(decision === "approve" ? "승인했습니다. 답변이 공개됩니다." : "반려했습니다.", "success");
      }
    } finally {
      for (const b of buttons) b.disabled = false;
    }
    await loadReviews();
  }

  // ---------- admin tab ----------

  async function loadAdmin() {
    if (!$("usage-from").value || !$("usage-to").value) {
      const today = new Date();
      const weekAgo = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 7);
      $("usage-from").value = dateInputValue(weekAgo);
      $("usage-to").value = dateInputValue(today);
    }
    await Promise.all([loadLlmStatus(), loadUsage(), loadAudit()]);
  }

  const PROVIDER_LABELS = { fake: "Fake", real: "Claude API", local: "로컬(Ollama)" };

  function yesNo(value, unknown) {
    if (value === true) return "예";
    if (value === false) return "아니요";
    return unknown;
  }

  async function loadLlmStatus() {
    const { data } = await api("/llm/status");
    const stats = [
      ["제공자", PROVIDER_LABELS[data.provider] || String(data.provider || "—")],
      ["모델", String(data.model || "—")],
      ["연결", yesNo(data.connected, "확인 안 함")],
      ["모델 있음", yesNo(data.model_available, "—")],
    ];
    if (data.fallback) {
      // Local cascade (LOCAL_LLM_FALLBACK_MODEL): one line for the fallback model.
      const fb = data.fallback;
      stats.push([
        "상향 모델",
        `${String(fb.model || "—")} · 연결 ${yesNo(fb.connected, "확인 안 함")} · 모델 ${yesNo(fb.model_available, "—")}`,
      ]);
    }
    $("llm-status").replaceChildren(
      ...stats.map(([label, value]) =>
        el("div", {}, [el("dt", { text: label }), el("dd", { text: value })]),
      ),
    );
    const detail = $("llm-status-detail");
    const parts = [];
    if (data.base_url) parts.push(`주소: ${data.base_url}`);
    if (data.detail) parts.push(String(data.detail));
    detail.textContent = parts.join(" · ");
    detail.hidden = parts.length === 0;
  }

  async function loadUsage() {
    const from = $("usage-from").value;
    const to = $("usage-to").value;
    if (!from || !to) {
      showAlert("사용량 기간을 선택하세요.");
      return;
    }
    const start = localMidnight(from);
    const end = localMidnight(to);
    end.setDate(end.getDate() + 1); // 종료일 포함
    if (start >= end) {
      showAlert("시작일이 종료일보다 늦습니다.");
      return;
    }
    const params = new URLSearchParams({ from: start.toISOString(), to: end.toISOString() });
    const { data } = await api(`/usage?${params}`);
    const ms = (v) => (v === null || v === undefined ? "—" : `${fmtInt(Math.round(v))} ms`);
    const totalTokens =
      Number(data.input_tokens || 0) +
      Number(data.output_tokens || 0) +
      Number(data.cache_read_input_tokens || 0) +
      Number(data.cache_creation_input_tokens || 0);
    const stats = [
      ["호출 수", fmtInt(data.calls)],
      ["토큰 합계", fmtInt(totalTokens)],
      ["입력 / 출력 토큰", `${fmtInt(data.input_tokens)} / ${fmtInt(data.output_tokens)}`],
      ["비용 합계 (USD)", `$${data.cost_usd}`],
      ["지연시간 p50", ms(data.latency_ms_p50)],
      ["지연시간 p95", ms(data.latency_ms_p95)],
    ];
    if (Number(data.unpriced_calls) > 0) stats.push(["가격 미등록 호출", fmtInt(data.unpriced_calls)]);
    $("usage-stats").replaceChildren(
      ...stats.map(([label, value]) =>
        el("div", {}, [el("dt", { text: label }), el("dd", { text: value })]),
      ),
    );
  }

  async function loadAudit() {
    const params = new URLSearchParams({ limit: "50" });
    const action = $("audit-action").value.trim();
    if (action) params.set("action", action);
    const { data } = await api(`/audit-logs?${params}`);
    const items = data && Array.isArray(data.items) ? data.items : [];
    $("audit-table").querySelector("tbody").replaceChildren(
      ...items.map((r) => {
        const target = r.target_type
          ? `${r.target_type}${r.target_id ? `:${r.target_id}` : ""}`
          : r.target_id || "—";
        const actor = r.actor_user_id
          ? el("td", {
              className: "mono",
              text: String(r.actor_user_id).slice(0, 8),
              attrs: { title: r.actor_user_id },
            })
          : el("td", { text: "—" });
        return el("tr", {}, [
          el("td", { text: fmtDateTime(r.created_at) }),
          el("td", { className: "mono", text: r.action }),
          el("td", { className: "mono", text: target }),
          actor,
          el("td", { className: "mono", text: r.request_id || "—" }),
        ]);
      }),
    );
    $("audit-empty").hidden = items.length > 0;
  }

  async function createUser(ev) {
    ev.preventDefault();
    const button = $("user-btn");
    button.disabled = true;
    try {
      const { data } = await api("/users", {
        method: "POST",
        json: {
          email: $("user-email-input").value.trim(),
          password: $("user-password-input").value,
          role: $("user-role-input").value,
        },
      });
      showAlert(`사용자 ${data.email} (${data.role})를 추가했습니다.`, "success");
      $("user-form").reset();
      await loadAudit();
    } finally {
      button.disabled = false;
    }
  }

  // ---------- wiring ----------

  function bindEvents() {
    $("login-form").addEventListener("submit", guard(login));
    $("logout-btn").addEventListener("click", logout);

    for (const button of document.querySelectorAll(".tabs [role=tab]")) {
      button.addEventListener("click", () => selectTab(button.dataset.tab));
      button.addEventListener("keydown", onTabKeydown);
    }

    $("dart-search-form").addEventListener("submit", guard(searchDart));
    $("dart-stock-codes").addEventListener("input", syncPresets);
    for (const button of document.querySelectorAll("[data-stock-code]")) {
      button.addEventListener("click", () => togglePreset(button.dataset.stockCode));
    }
    $("rcept-form").addEventListener("submit", guard(submitRcept));
    $("docs-refresh").addEventListener("click", guard(loadDocuments));

    $("question-form").addEventListener("submit", guard(submitQuestion));
    for (const button of document.querySelectorAll("[data-example]")) {
      button.addEventListener("click", () => {
        $("question-text").value = button.dataset.example;
        $("question-text").focus();
      });
    }

    $("diff-form").addEventListener("submit", guard(runComparison));
    $("diff-current").addEventListener("change", updatePriorOptions);
    $("diff-prior").addEventListener("change", updatePairWarning);
    $("diff-changed-only").addEventListener("change", refreshNotes);
    $("diff-note-search").addEventListener("input", () => {
      if (!state.diffView) return;
      state.diffView.search = $("diff-note-search").value;
      refreshNotes();
    });
    $("diff-note-sort").addEventListener("change", () => {
      if (!state.diffView) return;
      state.diffView.sort = $("diff-note-sort").value;
      refreshNotes();
    });
    for (const chip of document.querySelectorAll("[data-note-chip]")) {
      chip.addEventListener("click", () => {
        if (!state.diffView) return;
        const key = chip.dataset.noteChip;
        const chips = state.diffView.chips;
        state.diffView.chips = chips.includes(key) ? chips.filter((c) => c !== key) : [...chips, key];
        syncNoteFilters();
        refreshNotes();
      });
    }
    for (const tab of document.querySelectorAll("[data-diff-tab]")) {
      tab.addEventListener("click", () => selectDetailTab(tab.dataset.diffTab));
      tab.addEventListener("keydown", onDetailTabKeydown);
    }

    $("reviews-refresh").addEventListener("click", guard(loadReviews));

    $("llm-status-refresh").addEventListener("click", guard(loadLlmStatus));
    $("usage-form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      guard(loadUsage)();
    });
    $("audit-form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      guard(loadAudit)();
    });
    $("user-form").addEventListener("submit", guard(createUser));
  }

  async function boot() {
    bindEvents();
    syncPresets();
    if (!getToken()) {
      showLogin();
      return;
    }
    try {
      await enterApp();
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      if (err.status !== 401) showLogin();
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    guard(boot)();
  });
})();
