"use strict";

const state = {
  user: null,
  jobs: [],
  currentJob: null,
  builds: [],
  pathSlots: [""],
  pathSelections: [false],
  suggestions: {},
  activeSuggestion: null,
  suggestionCursor: null,
  searchTimers: {},
  searchVersions: {},
  suppressPathFocus: false,
  pollTimer: null,
  buildFilter: "",
  highlightedBuildId: null,
  highlightedBuildPending: false,
  highlightedBuildTimer: null,
};

const icons = {
  check: '<svg viewBox="0 0 24 24"><path d="m7 12 3.2 3.2L17 8.5"/></svg>',
  failed: '<svg viewBox="0 0 24 24"><path d="m8 8 8 8M16 8l-8 8"/></svg>',
  running: '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 4v6h-6"/></svg>',
  queued: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  chevron: '<svg viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 6h6l2 2h10v11H3z"/></svg>',
};

const statusMap = {
  queued: { label: "等待中", icon: icons.queued },
  running: { label: "构建中", icon: icons.running },
  success: { label: "构建成功", icon: icons.check },
  failed: { label: "构建失败", icon: icons.failed },
};

function escapeHTML(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("登录已过期，请重新登录");
  }
  if (!response.ok) {
    const error = new Error(payload.error || `请求失败（${response.status}）`);
    error.payload = payload;
    throw error;
  }
  return payload;
}

function toast(message, type = "success") {
  const root = document.querySelector("#toast-root");
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = message;
  root.appendChild(item);
  window.setTimeout(() => item.remove(), 3500);
}

function showLogin() {
  clearInterval(state.pollTimer);
  state.user = null;
  document.querySelector("#app-view").classList.add("hidden");
  document.querySelector("#login-view").classList.remove("hidden");
  window.setTimeout(() => document.querySelector("#username")?.focus(), 30);
}

function showApp() {
  document.querySelector("#login-view").classList.add("hidden");
  document.querySelector("#app-view").classList.remove("hidden");
  document.querySelector("#user-name").textContent = state.user.username;
  document.querySelector("#user-avatar").textContent = state.user.username.slice(0, 1).toUpperCase();
  route();
}

function navigate(path) {
  if (location.pathname !== path) history.pushState({}, "", path);
  route();
}

async function route() {
  closeDrawer();
  clearInterval(state.pollTimer);
  const match = location.pathname.match(/^\/jobs\/([^/]+)$/);
  if (match) await renderJob(decodeURIComponent(match[1]));
  else {
    if (location.pathname !== "/") history.replaceState({}, "", "/");
    await renderJobs();
  }
}

function statusHTML(status, compact = false) {
  const item = statusMap[status] || statusMap.queued;
  return `<span class="status-orb ${escapeHTML(status)}">${item.icon}</span>${compact ? "" : `<span class="status-badge ${escapeHTML(status)}">${item.label}</span>`}`;
}

function relativeTime(dateString) {
  if (!dateString) return "—";
  const seconds = Math.max(0, (Date.now() - new Date(dateString).getTime()) / 1000);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)} 天前`;
  return formatDate(dateString, false);
}

function formatDate(value, time = true) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    ...(time ? { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false } : {}),
  }).format(new Date(value));
}

function duration(value) {
  if (value == null) return "—";
  if (value < 60) return `${Math.round(value)} 秒`;
  return `${Math.floor(value / 60)} 分 ${Math.round(value % 60)} 秒`;
}

function jobIconHTML(job) {
  return `<img class="job-icon" src="/res/${encodeURIComponent(job.id)}_icon.png" alt="${escapeHTML(job.display_name)} 图标" />`;
}

async function renderJobs() {
  const root = document.querySelector("#page-root");
  state.currentJob = null;
  root.innerHTML = `<main class="page"><div class="breadcrumbs">构建中心 ${icons.chevron} <span>全部项目</span></div><div class="page-heading"><div><p class="eyebrow">ALL PROJECTS</p><h1>选择构建项目</h1><p>管理团队资源更新与 OTA 构建任务</p></div></div><div class="jobs-table"><div class="empty-state">正在加载项目…</div></div></main>`;
  try {
    const payload = await api("/api/jobs");
    renderJobsPayload(payload);
    startJobsPolling();
  } catch (error) {
    root.querySelector(".jobs-table").innerHTML = `<div class="empty-state">${escapeHTML(error.message)}</div>`;
  }
}

function renderJobsPayload(payload) {
  if (location.pathname !== "/") return;
  state.jobs = payload.jobs;
  const rows = payload.jobs.map(job => {
    const last = job.last_build;
    const status = last?.status || "success";
    return `<article class="job-row" data-job-id="${escapeHTML(job.id)}" tabindex="0" role="button">
      <div>${statusHTML(status, true)}</div>
      <div class="job-name">${jobIconHTML(job)}<div><b>${escapeHTML(job.display_name)}</b><small>${escapeHTML(job.description)}</small></div></div>
      <div class="build-cell"><b>${last ? `#${last.build_number}` : "尚无构建"}</b><small>${last ? relativeTime(last.created_at) : "点击进入创建第一个任务"}</small></div>
      <span class="status-badge ${escapeHTML(status)}">${last ? statusMap[status].label : "准备就绪"}</span>
      <span class="job-open">${icons.chevron}</span>
    </article>`;
  }).join("");
  document.querySelector("#page-root").innerHTML = `<main class="page">
    <div class="breadcrumbs"><span>构建中心</span>${icons.chevron}<span>全部项目</span></div>
    <div class="page-heading"><div><p class="eyebrow">ALL PROJECTS</p><h1>选择构建项目</h1><p>管理团队资源更新与 OTA 构建任务</p></div><div class="page-summary"><span>${payload.jobs.length}</span> 个可用项目</div></div>
    <section class="jobs-table"><div class="job-table-head"><span>状态</span><span>项目</span><span>最近构建</span><span>结果</span><span></span></div>${rows || '<div class="empty-state">暂未配置项目</div>'}</section>
  </main>`;
}

function startJobsPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    if (location.pathname !== "/" || document.hidden) return;
    try {
      renderJobsPayload(await api("/api/jobs"));
    } catch (_error) { /* 下一轮自动重试 */ }
  }, 2000);
}

function breadcrumbs(job) {
  return `<div class="breadcrumbs"><button data-nav="/">构建中心</button>${icons.chevron}<button data-nav="/">全部项目</button>${icons.chevron}<span>${escapeHTML(job.display_name)}</span></div>`;
}

async function renderJob(jobId) {
  const root = document.querySelector("#page-root");
  root.innerHTML = `<main class="page job-page"><div class="empty-state">正在加载构建信息…</div></main>`;
  try {
    const [jobPayload, buildsPayload] = await Promise.all([
      api(`/api/jobs/${encodeURIComponent(jobId)}`),
      api(`/api/jobs/${encodeURIComponent(jobId)}/builds`),
    ]);
    state.currentJob = jobPayload.job;
    state.builds = buildsPayload.builds;
    state.pathSlots = [""];
    state.pathSelections = [false];
    state.suggestions = {};
    state.searchVersions = {};
    state.activeSuggestion = null;
    state.suggestionCursor = null;
    state.buildFilter = "";
    root.innerHTML = `<main class="page job-page">
      ${breadcrumbs(state.currentJob)}
      <div class="job-header"><div class="job-title">${jobIconHTML(state.currentJob)}<div><h1>${escapeHTML(state.currentJob.display_name)}</h1><p>${escapeHTML(state.currentJob.description)} · 根目录 ${escapeHTML(state.currentJob.resource_root_name)}</p></div></div><span class="server-state"><i></i> 可构建</span></div>
      <div class="job-layout">
        <aside class="panel build-sidebar"><div class="panel-header"><h2>构建记录</h2><span id="build-count" class="build-count">${state.builds.length}</span></div><label class="build-filter"><span class="input-wrap"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><input id="build-filter" placeholder="筛选编号或构建者" /></span></label><div id="build-list" class="build-list"></div></aside>
        <section class="panel build-form-panel"><div class="form-heading"><p class="eyebrow">NEW BUILD</p><h2>创建资源更新任务</h2><p>确认资源范围后提交构建</p></div>${buildFormHTML()}</section>
      </div>
    </main>`;
    renderBuildList();
    renderPathRows();
    startPolling();
  } catch (error) {
    root.innerHTML = `<main class="page"><div class="empty-state">${escapeHTML(error.message)}</div></main>`;
  }
}

function buildFormHTML() {
  return `<form id="build-form" class="build-form">
    <section class="form-section"><div class="section-heading"><div><h3>资源更新路径 <span class="subtle">*</span></h3><p>输入关键词检索目录，支持同时更新多个路径</p></div><button id="add-path" class="add-path-button" type="button"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>添加路径</button></div><div id="path-rows"></div><div id="selected-paths"></div></section>
    <section class="form-section"><label><span class="field-label">构建说明</span><textarea id="note" maxlength="500" placeholder="简要说明本次更新内容，方便团队成员追溯（可选）"></textarea></label></section>
    <div class="form-footer"><span class="form-footer-note"><svg viewBox="0 0 24 24"><path d="M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Z"/><path d="M12 16v-4M12 8h.01"/></svg>将更新资源、提交代码并触发 Jenkins OTA</span><button id="build-submit" class="button primary build-submit" type="submit">开始构建 <span>→</span></button></div>
  </form>`;
}

function renderPathRows() {
  const root = document.querySelector("#path-rows");
  if (!root) return;
  root.innerHTML = state.pathSlots.map((value, index) => {
    const selected = Boolean(value && state.pathSelections[index]);
    const suggestions = state.suggestions[index] || [];
    const show = state.activeSuggestion === index && !selected;
    const activeOption = show && state.suggestionCursor !== null
      ? `path-suggestion-${index}-${state.suggestionCursor}`
      : "";
    const list = suggestions.length
      ? suggestions.map((path, optionIndex) => {
        const active = state.suggestionCursor === optionIndex;
        return `<button id="path-suggestion-${index}-${optionIndex}" class="suggestion-item${active ? " is-active" : ""}" type="button" role="option" aria-selected="${active}" data-select-path="${index}" data-suggestion-index="${optionIndex}" data-path="${escapeHTML(path)}">${icons.folder}<span>${escapeHTML(path)}</span></button>`;
      }).join("")
      : '<div class="suggestion-empty">未找到匹配目录</div>';
    return `<div class="path-row"><span class="path-index">${String(index + 1).padStart(2, "0")}</span><div class="path-input-wrap${selected ? " is-selected" : ""}"><span class="input-wrap"><svg viewBox="0 0 24 24">${selected ? '<path d="m7 12 3.2 3.2L17 8.5"/>' : '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>'}</svg><input class="path-input" role="combobox" aria-autocomplete="list" aria-expanded="${show}" aria-controls="path-suggestions-${index}" ${activeOption ? `aria-activedescendant="${activeOption}"` : ""} data-path-index="${index}" value="${escapeHTML(value)}" placeholder="搜索资源目录，如 ResourcesCommon" autocomplete="off" ${selected ? "readonly" : ""} />${selected ? `<button class="clear-path-selection" data-clear-path="${index}" type="button" title="重新选择" aria-label="重新选择资源路径">${icons.failed}</button>` : ""}</span>${show ? `<div id="path-suggestions-${index}" class="path-suggestions" role="listbox">${list}</div>` : ""}</div><button class="remove-path" data-remove-path="${index}" type="button" aria-label="删除路径" ${state.pathSlots.length === 1 ? "disabled" : ""}><svg viewBox="0 0 24 24"><path d="M5 12h14"/></svg></button></div>`;
  }).join("");
  const selected = state.pathSlots.filter((value, index) => value && state.pathSelections[index]);
  document.querySelector("#selected-paths").innerHTML = selected.length ? `<div class="selected-paths"><span class="selected-label">已选择 ${selected.length} 个路径</span><div class="path-chips">${selected.map(path => `<span class="path-chip">${icons.folder}<span>${escapeHTML(path)}</span></span>`).join("")}</div></div>` : "";
}

function searchPath(index, query) {
  clearTimeout(state.searchTimers[index]);
  if (state.pathSelections[index]) return;
  state.activeSuggestion = index;
  state.suggestionCursor = null;
  const requestVersion = (state.searchVersions[index] || 0) + 1;
  state.searchVersions[index] = requestVersion;
  state.searchTimers[index] = setTimeout(async () => {
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(state.currentJob.id)}/paths?q=${encodeURIComponent(query)}`);
      if (state.searchVersions[index] !== requestVersion || state.activeSuggestion !== index) return;
      state.suggestions[index] = result.paths.filter(path => !state.pathSlots.some((value, slot) => slot !== index && state.pathSelections[slot] && value === path));
      state.suggestionCursor = null;
      const restoreFocus = document.activeElement?.dataset.pathIndex === String(index);
      if (restoreFocus) state.suppressPathFocus = true;
      renderPathRows();
      const input = document.querySelector(`[data-path-index="${index}"]`);
      if (input && restoreFocus) {
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      }
      state.suppressPathFocus = false;
    } catch (error) { toast(error.message, "error"); }
  }, 180);
}

function closePathSuggestions() {
  const index = state.activeSuggestion;
  if (index === null) return;
  clearTimeout(state.searchTimers[index]);
  state.searchVersions[index] = (state.searchVersions[index] || 0) + 1;
  if (!state.pathSelections[index] && state.pathSlots[index]) {
    state.pathSlots[index] = "";
  }
  state.suggestions[index] = [];
  state.activeSuggestion = null;
  state.suggestionCursor = null;
  renderPathRows();
}

function updateSuggestionCursor(index, cursor) {
  const suggestions = state.suggestions[index] || [];
  if (!suggestions.length) return;
  state.suggestionCursor = cursor;
  const input = document.querySelector(`[data-path-index="${index}"]`);
  const options = document.querySelectorAll(`[data-select-path="${index}"]`);
  options.forEach((option, optionIndex) => {
    const active = optionIndex === cursor;
    option.classList.toggle("is-active", active);
    option.setAttribute("aria-selected", String(active));
  });
  const activeOption = options[cursor];
  if (input && activeOption) input.setAttribute("aria-activedescendant", activeOption.id);
  const list = activeOption?.closest(".path-suggestions");
  if (!list || !activeOption) return;
  if (activeOption.offsetTop < list.scrollTop) list.scrollTop = activeOption.offsetTop;
  const optionBottom = activeOption.offsetTop + activeOption.offsetHeight;
  if (optionBottom > list.scrollTop + list.clientHeight) {
    list.scrollTop = optionBottom - list.clientHeight;
  }
}

function selectPathSuggestion(index, path) {
  if (!path) return;
  clearTimeout(state.searchTimers[index]);
  state.searchVersions[index] = (state.searchVersions[index] || 0) + 1;
  state.pathSlots[index] = path;
  state.pathSelections[index] = true;
  state.activeSuggestion = null;
  state.suggestionCursor = null;
  renderPathRows();
}

function buildListItem(build) {
  const status = statusMap[build.status] || statusMap.queued;
  const highlighted = state.highlightedBuildPending && state.highlightedBuildId === build.id ? " build-item-new" : "";
  return `<button class="build-item${highlighted}" data-build-id="${build.id}">${statusHTML(build.status, true)}<span><span class="build-primary"><b>#${build.build_number}</b><span class="status-badge ${build.status}">${status.label}</span></span><span class="build-meta"><span>${escapeHTML(build.username)}</span><span>${relativeTime(build.created_at)}</span></span></span><span class="build-arrow">${icons.chevron}</span></button>`;
}

function renderBuildList() {
  const root = document.querySelector("#build-list");
  if (!root) return;
  const needle = state.buildFilter.toLowerCase();
  const builds = state.builds.filter(build => `#${build.build_number} ${build.username}`.toLowerCase().includes(needle));
  document.querySelector("#build-count").textContent = state.builds.length;
  root.innerHTML = builds.length ? `<div class="date-separator">最近构建</div>${builds.map(buildListItem).join("")}` : '<div class="empty-state">暂无匹配的构建记录</div>';
  if (state.highlightedBuildPending && root.querySelector(`[data-build-id="${state.highlightedBuildId}"]`)) {
    state.highlightedBuildPending = false;
  }
}

function buildListSignature(builds) {
  return builds.map(build => [
    build.id,
    build.build_number,
    build.status,
    build.username,
    build.created_at,
  ].join(":")).join("|");
}

function highlightNewBuild(buildId) {
  clearTimeout(state.highlightedBuildTimer);
  state.highlightedBuildId = buildId;
  state.highlightedBuildPending = true;
  renderBuildList();
  state.highlightedBuildTimer = window.setTimeout(() => {
    if (state.highlightedBuildId !== buildId) return;
    state.highlightedBuildId = null;
    state.highlightedBuildPending = false;
    document.querySelector(`.build-item[data-build-id="${buildId}"]`)?.classList.remove("build-item-new");
  }, 1900);
}

function drawerHasTextSelection(drawer) {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed) return false;
  return Boolean(
    (selection.anchorNode && drawer.contains(selection.anchorNode)) ||
    (selection.focusNode && drawer.contains(selection.focusNode))
  );
}

function resetBuildForm() {
  Object.values(state.searchTimers).forEach(timer => clearTimeout(timer));
  state.pathSlots = [""];
  state.pathSelections = [false];
  state.suggestions = {};
  state.activeSuggestion = null;
  state.suggestionCursor = null;
  state.searchTimers = {};
  state.searchVersions = {};
  document.querySelector("#build-form")?.reset();
  renderPathRows();
}

function startPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    if (!state.currentJob || document.hidden) return;
    try {
      const payload = await api(`/api/jobs/${encodeURIComponent(state.currentJob.id)}/builds`);
      const listChanged = buildListSignature(state.builds) !== buildListSignature(payload.builds);
      state.builds = payload.builds;
      if (listChanged || state.highlightedBuildId === null) renderBuildList();
      const drawer = document.querySelector("#build-drawer");
      const selectedId = Number(drawer.dataset.buildId);
      const selected = state.builds.find(build => build.id === selectedId);
      const statusChanged = selected && drawer.dataset.buildStatus !== selected.status;
      if (selected && statusChanged && !drawerHasTextSelection(drawer)) {
        renderDrawer(selected, false);
      }
    } catch (_error) { /* 下一轮自动重试 */ }
  }, 2000);
}

async function submitBuild(event) {
  event.preventDefault();
  const resourcePaths = state.pathSlots.filter((value, index) => value && state.pathSelections[index]);
  if (!resourcePaths.length) {
    const hasInput = state.pathSlots.some(value => value.trim());
    toast(hasInput ? "请从模糊匹配列表中选择资源更新路径" : "玩家资源更新路径为空", "error");
    document.querySelector(".path-input")?.focus();
    return;
  }
  const unselectedIndex = state.pathSlots.findIndex((value, index) => value.trim() && !state.pathSelections[index]);
  if (unselectedIndex >= 0) {
    toast("请从模糊匹配列表中选择资源更新路径", "error");
    document.querySelector(`[data-path-index="${unselectedIndex}"]`)?.focus();
    return;
  }
  const button = document.querySelector("#build-submit");
  button.disabled = true;
  button.textContent = "正在创建任务…";
  try {
    const payload = await api(`/api/jobs/${encodeURIComponent(state.currentJob.id)}/builds`, {
      method: "POST",
      body: JSON.stringify({
        resource_paths: resourcePaths,
        note: document.querySelector("#note").value,
      }),
    });
    state.builds.unshift(payload.build);
    highlightNewBuild(payload.build.id);
    resetBuildForm();
    toast(`构建 #${payload.build.build_number} 已创建，正在后台执行`);
  } catch (error) {
    const failedBuild = error.payload?.build;
    if (failedBuild && !state.builds.some(build => build.id === failedBuild.id)) {
      state.builds.unshift(failedBuild);
      renderBuildList();
    }
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.innerHTML = "开始构建 <span>→</span>";
  }
}

async function openBuildDrawer(buildId) {
  const drawer = document.querySelector("#build-drawer");
  drawer.dataset.buildId = String(buildId);
  drawer.innerHTML = '<div class="drawer-loading">正在加载构建详情…</div>';
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  document.querySelector("#drawer-backdrop").classList.remove("hidden");
  try {
    const payload = await api(`/api/builds/${buildId}`);
    renderDrawer(payload.build, true);
  } catch (error) {
    drawer.innerHTML = `<div class="drawer-loading">${escapeHTML(error.message)}</div>`;
  }
}

async function renderDrawer(build, loadLog) {
  const drawer = document.querySelector("#build-drawer");
  if (Number(drawer.dataset.buildId) !== build.id) return;
  drawer.dataset.buildStatus = build.status;
  const status = statusMap[build.status] || statusMap.queued;
  drawer.innerHTML = `<div class="drawer-top"><div><p>BUILD DETAILS</p><h2>#${build.build_number} 构建详情</h2></div><button class="icon-button drawer-close" aria-label="关闭">${icons.failed}</button></div>
    <div class="drawer-status">${statusHTML(build.status, true)}<div><h3>${status.label}</h3><p>${build.status === "running" ? "打包机正在处理当前任务" : build.status === "queued" ? "任务正在等待空闲构建槽" : `耗时 ${duration(build.duration_seconds)}`}</p></div></div>
    <section class="detail-section"><h3>基础信息</h3><dl class="detail-grid"><div class="detail-row"><dt>构建项目</dt><dd>${escapeHTML(build.job_id.toUpperCase())}</dd></div><div class="detail-row"><dt>构建者</dt><dd>${escapeHTML(build.username)}</dd></div><div class="detail-row"><dt>提交时间</dt><dd>${formatDate(build.created_at)}</dd></div><div class="detail-row"><dt>开始时间</dt><dd>${formatDate(build.started_at)}</dd></div><div class="detail-row"><dt>完成时间</dt><dd>${formatDate(build.finished_at)}</dd></div>${build.error_message ? `<div class="detail-row failure-reason"><dt>失败原因</dt><dd>${escapeHTML(build.error_message)}</dd></div>` : ""}<div class="detail-row"><dt>OTA 版本号</dt><dd>${escapeHTML(build.parameters.jenkins_build_number ?? "—")}</dd></div></dl></section>
    <section class="detail-section"><h3>构建参数</h3><dl class="detail-grid"><div class="detail-row"><dt>资源路径</dt><dd><span class="detail-paths">${(build.parameters.resource_paths || []).map(path => `<span class="detail-path">${escapeHTML(path)}</span>`).join("")}</span></dd></div><div class="detail-row"><dt>构建说明</dt><dd>${escapeHTML(build.parameters.note || "—")}</dd></div></dl></section>
    <section class="detail-section"><h3>构建日志</h3><pre id="build-log" class="log-box">${loadLog ? "正在加载日志…" : "日志随状态自动更新…"}</pre></section>`;
  if (loadLog || ["success", "failed"].includes(build.status)) {
    try {
      const payload = await api(`/api/builds/${build.id}/log`);
      const log = document.querySelector("#build-log");
      if (log) log.textContent = payload.log;
    } catch (_error) { /* 详情信息仍可使用 */ }
  }
}

function closeDrawer() {
  const drawer = document.querySelector("#build-drawer");
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  drawer.dataset.buildId = "";
  drawer.dataset.buildStatus = "";
  document.querySelector("#drawer-backdrop").classList.add("hidden");
}

document.addEventListener("submit", event => {
  if (event.target.id === "login-form") {
    event.preventDefault();
    const button = document.querySelector("#login-submit");
    const errorNode = document.querySelector("#login-error");
    errorNode.textContent = "";
    button.disabled = true;
    button.textContent = "正在登录…";
    api("/api/login", {
      method: "POST",
      body: JSON.stringify({ username: event.target.username.value, password: event.target.password.value }),
    }).then(payload => {
      state.user = payload.user;
      event.target.password.value = "";
      showApp();
    }).catch(error => { errorNode.textContent = error.message; }).finally(() => {
      button.disabled = false;
      button.innerHTML = "登录 <span>→</span>";
    });
  }
  if (event.target.id === "build-form") submitBuild(event);
});

document.addEventListener("click", event => {
  if (!event.target.closest(".path-input-wrap")) closePathSuggestions();
  const nav = event.target.closest("[data-nav]");
  if (nav) navigate(nav.dataset.nav);
  const jobRow = event.target.closest("[data-job-id]");
  if (jobRow) navigate(`/jobs/${encodeURIComponent(jobRow.dataset.jobId)}`);
  const buildItem = event.target.closest(".build-item[data-build-id]");
  if (buildItem) openBuildDrawer(Number(buildItem.dataset.buildId));
  if (event.target.closest("#drawer-backdrop") || event.target.closest(".drawer-close")) closeDrawer();
  if (event.target.closest("#add-path")) {
    if (state.pathSlots.length >= 20) return toast("最多可添加 20 个资源路径", "error");
    state.pathSlots.push(""); state.pathSelections.push(false); renderPathRows();
    document.querySelector(`[data-path-index="${state.pathSlots.length - 1}"]`)?.focus();
  }
  const remove = event.target.closest("[data-remove-path]");
  if (remove && state.pathSlots.length > 1) {
    state.pathSlots.splice(Number(remove.dataset.removePath), 1);
    state.pathSelections.splice(Number(remove.dataset.removePath), 1);
    state.suggestions = {}; state.searchVersions = {}; state.activeSuggestion = null; state.suggestionCursor = null; renderPathRows();
  }
  const suggestion = event.target.closest("[data-select-path]");
  if (suggestion) {
    const index = Number(suggestion.dataset.selectPath);
    selectPathSuggestion(index, suggestion.dataset.path);
  }
  const clearSelection = event.target.closest("[data-clear-path]");
  if (clearSelection) {
    const index = Number(clearSelection.dataset.clearPath);
    clearTimeout(state.searchTimers[index]);
    state.searchVersions[index] = (state.searchVersions[index] || 0) + 1;
    state.pathSlots[index] = "";
    state.pathSelections[index] = false;
    state.suggestions[index] = [];
    state.activeSuggestion = null;
    state.suggestionCursor = null;
    renderPathRows();
    document.querySelector(`[data-path-index="${index}"]`)?.focus();
  }
});

document.addEventListener("input", event => {
  if (event.target.matches(".path-input")) {
    const index = Number(event.target.dataset.pathIndex);
    state.pathSlots[index] = event.target.value;
    state.pathSelections[index] = false;
    searchPath(index, event.target.value);
  }
  if (event.target.id === "build-filter") {
    state.buildFilter = event.target.value;
    renderBuildList();
  }
});

document.addEventListener("focusin", event => {
  if (!state.suppressPathFocus && event.target.matches(".path-input")) searchPath(Number(event.target.dataset.pathIndex), event.target.value);
});

document.addEventListener("focusout", event => {
  if (state.suppressPathFocus || !event.target.matches(".path-input")) return;
  const wrapper = event.target.closest(".path-input-wrap");
  if (!wrapper?.contains(event.relatedTarget)) closePathSuggestions();
});

document.addEventListener("keydown", event => {
  const pathInput = event.target.closest?.(".path-input");
  if (pathInput && !event.isComposing) {
    const index = Number(pathInput.dataset.pathIndex);
    const suggestions = state.suggestions[index] || [];
    if ((event.key === "ArrowDown" || event.key === "ArrowUp") && state.activeSuggestion === index) {
      event.preventDefault();
      if (suggestions.length) {
        const direction = event.key === "ArrowDown" ? 1 : -1;
        const current = state.suggestionCursor;
        const cursor = current === null
          ? (direction > 0 ? 0 : suggestions.length - 1)
          : (current + direction + suggestions.length) % suggestions.length;
        updateSuggestionCursor(index, cursor);
      }
      return;
    }
    if (event.key === "Enter" && state.activeSuggestion === index && state.suggestionCursor !== null) {
      event.preventDefault();
      selectPathSuggestion(index, suggestions[state.suggestionCursor]);
      return;
    }
  }
  if (event.key === "Escape" && state.activeSuggestion !== null) {
    event.preventDefault();
    closePathSuggestions();
    return;
  }
  if (event.key === "Escape") closeDrawer();
  const jobRow = event.target.closest?.("[data-job-id]");
  if (jobRow && (event.key === "Enter" || event.key === " ")) navigate(`/jobs/${encodeURIComponent(jobRow.dataset.jobId)}`);
});

document.querySelector("#logout-button").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST", body: "{}" }); } catch (_error) { /* 本地状态仍然退出 */ }
  history.replaceState({}, "", "/");
  showLogin();
});

window.addEventListener("popstate", route);

(async function bootstrap() {
  try {
    const payload = await api("/api/me");
    state.user = payload.user;
    showApp();
  } catch (_error) {
    showLogin();
  }
})();
