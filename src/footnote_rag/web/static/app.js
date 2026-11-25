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
    for (const id of ["answer-area", "history", "reviews-list", "usage-stats", "question-docs", "llm-status"]) {
      $(id).replaceChildren();
    }
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

  function dartLink(url) {
    if (typeof url === "string" && url.startsWith(DART_URL_PREFIX)) {
      return el("a", {
        text: "DART 원문 보기 ↗",
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
