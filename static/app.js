"use strict";

const state = {
  user: null,
  jobs: [],
  currentJob: null,
  builds: [],
  selectedPaths: [],
  pathQuery: "",
  suggestions: [],
  suggestionsOpen: false,
  suggestionCursor: null,
  searchTimer: null,
  searchVersion: 0,
  suppressPathFocus: false,
  pollTimer: null,
  buildFilter: "",
  highlightedBuildId: null,
  highlightedBuildPending: false,
  highlightedBuildTimer: null,
  pathRequiredTimer: null,
  logRefreshVersion: 0,
  pendingCancelBuildId: null,
};

const icons = {
  check: '<svg viewBox="0 0 24 24"><path d="m7 12 3.2 3.2L17 8.5"/></svg>',
  failed: '<svg viewBox="0 0 24 24"><path d="m8 8 8 8M16 8l-8 8"/></svg>',
  running: '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 4v6h-6"/></svg>',
  queued: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="1"/></svg>',
  chevron: '<svg viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 6h6l2 2h10v11H3z"/></svg>',
};

const statusMap = {
  queued: { label: "等待中", icon: icons.queued },
  running: { label: "构建中", icon: icons.running },
  cancelling: { label: "终止中", icon: icons.running },
  cancelled: { label: "已终止", icon: icons.stop },
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
  window.setTimeout(() => document.querySelector("#account")?.focus(), 30);
}

function showApp() {
  document.querySelector("#login-view").classList.add("hidden");
  document.querySelector("#app-view").classList.remove("hidden");
  document.querySelector("#user-name").textContent = state.user.name;
  document.querySelector("#user-name").title = `登录账号：${state.user.account}`;
  document.querySelector("#user-avatar").textContent = state.user.name.slice(0, 1).toUpperCase();
  route();
}

function navigate(path) {
  if (location.pathname !== path) history.pushState({}, "", path);
  route();
}

function updateWordmarkNavigation(interactive) {
  const wordmark = document.querySelector("#wordmark");
  if (!wordmark) return;
  wordmark.classList.toggle("is-link", interactive);
  if (interactive) {
    wordmark.dataset.nav = "/";
    wordmark.setAttribute("role", "link");
    wordmark.setAttribute("tabindex", "0");
    wordmark.setAttribute("aria-label", "返回项目列表");
    wordmark.setAttribute("title", "返回项目列表");
  } else {
    delete wordmark.dataset.nav;
    wordmark.removeAttribute("role");
    wordmark.removeAttribute("tabindex");
    wordmark.removeAttribute("aria-label");
    wordmark.removeAttribute("title");
  }
}

async function route() {
  closeDrawer();
  clearInterval(state.pollTimer);
  const match = location.pathname.match(/^\/jobs\/([^/]+)$/);
  if (match) {
    updateWordmarkNavigation(true);
    await renderJob(decodeURIComponent(match[1]));
  } else {
    if (location.pathname !== "/") history.replaceState({}, "", "/");
    updateWordmarkNavigation(false);
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
    state.selectedPaths = [];
    state.pathQuery = "";
    state.suggestions = [];
    state.searchVersion = 0;
    state.suggestionsOpen = false;
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
    <section class="form-section"><div class="section-heading"><div><h3>资源更新路径 <span class="required-mark" aria-hidden="true">*</span></h3><p>输入关键词检索目录，支持模糊匹配和多路径</p></div></div><div id="path-rows"></div><div id="selected-paths"></div></section>
    <section class="form-section"><label><span class="field-label">构建说明</span><textarea id="note" maxlength="500" placeholder="简要说明本次更新内容，方便团队成员追溯（可选）"></textarea></label></section>
    <div class="form-footer"><span class="form-footer-note"><svg viewBox="0 0 24 24"><path d="M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Z"/><path d="M12 16v-4M12 8h.01"/></svg>将更新资源并触发 Jenkins OTA</span><button id="build-submit" class="button primary build-submit" type="submit">开始构建 <span>→</span></button></div>
  </form>`;
}

function renderPathRows() {
  const root = document.querySelector("#path-rows");
  if (!root) return;
  const show = state.suggestionsOpen;
  const activeOption = show && state.suggestionCursor !== null
    ? `path-suggestion-${state.suggestionCursor}`
    : "";
  const list = state.suggestions.length
    ? state.suggestions.map((path, optionIndex) => {
      const active = state.suggestionCursor === optionIndex;
      return `<button id="path-suggestion-${optionIndex}" class="suggestion-item${active ? " is-active" : ""}" type="button" role="option" aria-selected="${active}" data-select-path data-suggestion-index="${optionIndex}" data-path="${escapeHTML(path)}">${icons.folder}<span>${escapeHTML(path)}</span></button>`;
    }).join("")
    : '<div class="suggestion-empty">未找到匹配目录</div>';
  root.innerHTML = `<div class="path-input-wrap"><span class="input-wrap"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg><input class="path-input" role="combobox" aria-autocomplete="list" aria-expanded="${show}" aria-controls="path-suggestions" ${activeOption ? `aria-activedescendant="${activeOption}"` : ""} value="${escapeHTML(state.pathQuery)}" placeholder="搜索资源目录，如 ResourcesCommon" autocomplete="off" /></span>${show ? `<div id="path-suggestions" class="path-suggestions" role="listbox">${list}</div>` : ""}</div>`;

  const selectedRoot = document.querySelector("#selected-paths");
  selectedRoot.innerHTML = state.selectedPaths.length
    ? `<div class="selected-paths"><span class="selected-label">已选择 ${state.selectedPaths.length} 个路径</span><div class="path-chips">${state.selectedPaths.map((path, index) => `<span class="path-chip" title="${escapeHTML(path)}">${icons.folder}<span>${escapeHTML(path)}</span><button type="button" data-remove-selected-path="${index}" title="删除路径" aria-label="删除资源路径 ${escapeHTML(path)}">${icons.failed}</button></span>`).join("")}</div></div>`
    : '<div class="selected-paths is-empty"><span class="selected-empty">尚未选择资源路径</span></div>';
}

function searchPath(query) {
  clearTimeout(state.searchTimer);
  state.suggestionsOpen = true;
  state.suggestionCursor = null;
  const requestVersion = state.searchVersion + 1;
  state.searchVersion = requestVersion;
  state.searchTimer = setTimeout(async () => {
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(state.currentJob.id)}/paths?q=${encodeURIComponent(query)}`);
      if (state.searchVersion !== requestVersion || !state.suggestionsOpen) return;
      state.suggestions = result.paths.filter(path => !state.selectedPaths.includes(path));
      state.suggestionCursor = null;
      const restoreFocus = document.activeElement?.matches(".path-input");
      if (restoreFocus) state.suppressPathFocus = true;
      renderPathRows();
      const input = document.querySelector(".path-input");
      if (input && restoreFocus) {
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      }
      state.suppressPathFocus = false;
    } catch (error) { toast(error.message, "error"); }
  }, 180);
}

function closePathSuggestions() {
  if (!state.suggestionsOpen) return;
  clearTimeout(state.searchTimer);
  state.searchVersion += 1;
  state.pathQuery = "";
  state.suggestions = [];
  state.suggestionsOpen = false;
  state.suggestionCursor = null;
  renderPathRows();
}

function updateSuggestionCursor(cursor) {
  if (!state.suggestions.length) return;
  state.suggestionCursor = cursor;
  const input = document.querySelector(".path-input");
  const options = document.querySelectorAll("[data-select-path]");
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

function selectPathSuggestion(path) {
  if (!path || state.selectedPaths.includes(path)) return;
  if (state.selectedPaths.length >= 20) {
    toast("最多可添加 20 个资源路径", "error");
    return;
  }
  clearTimeout(state.searchTimer);
  state.searchVersion += 1;
  state.selectedPaths.push(path);
  state.pathQuery = "";
  state.suggestions = [];
  state.suggestionsOpen = false;
  state.suggestionCursor = null;
  renderPathRows();
  animateNewPathChip(state.selectedPaths.length - 1);
  state.suppressPathFocus = true;
  document.querySelector(".path-input")?.focus();
  state.suppressPathFocus = false;
}

function animateNewPathChip(index) {
  const chip = document.querySelectorAll(".path-chip")[index];
  if (!chip) return;
  chip.classList.add("path-chip-new");
  const clearAnimation = () => chip.classList.remove("path-chip-new");
  chip.addEventListener("animationend", clearAnimation, { once: true });
  window.setTimeout(clearAnimation, 2000);
}

function clearPathRequiredHighlight() {
  clearTimeout(state.pathRequiredTimer);
  state.pathRequiredTimer = null;
  document.querySelector(".path-input-wrap .input-wrap")?.classList.remove("path-input-required");
}

function showPathRequiredHighlight() {
  const field = document.querySelector(".path-input-wrap .input-wrap");
  if (!field) return;
  clearTimeout(state.pathRequiredTimer);
  field.classList.remove("path-input-required");
  void field.offsetWidth;
  field.classList.add("path-input-required");
  const clearAnimation = () => {
    field.classList.remove("path-input-required");
    state.pathRequiredTimer = null;
  };
  field.addEventListener("animationend", clearAnimation, { once: true });
  state.pathRequiredTimer = window.setTimeout(clearAnimation, 2200);
}

function buildListItem(build) {
  const status = statusMap[build.status] || statusMap.queued;
  const highlighted = state.highlightedBuildPending && state.highlightedBuildId === build.id ? " build-item-new" : "";
  return `<button class="build-item${highlighted}" data-build-id="${build.id}">${statusHTML(build.status, true)}<span><span class="build-primary"><b>#${build.build_number}</b><span class="status-badge ${build.status}">${status.label}</span></span><span class="build-meta"><span>${escapeHTML(build.builder_name)}</span><span>${relativeTime(build.created_at)}</span></span></span><span class="build-arrow">${icons.chevron}</span></button>`;
}

function renderBuildList() {
  const root = document.querySelector("#build-list");
  if (!root) return;
  const needle = state.buildFilter.toLowerCase();
  const builds = state.builds.filter(build => `#${build.build_number} ${build.builder_name} ${build.builder_account}`.toLowerCase().includes(needle));
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
    build.builder_account,
    build.builder_name,
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
  clearTimeout(state.searchTimer);
  state.selectedPaths = [];
  state.pathQuery = "";
  state.suggestions = [];
  state.suggestionsOpen = false;
  state.suggestionCursor = null;
  state.searchTimer = null;
  state.searchVersion += 1;
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
      const otaVersionChanged = selected
        && drawer.dataset.otaVersion !== String(selected.parameters.jenkins_build_number ?? "");
      if (otaVersionChanged) {
        updateDrawerOtaVersion(selected);
      }
      if (selected && statusChanged && !drawerHasTextSelection(drawer)) {
        updateDrawerSummary(selected);
      }
      if (selected && (["queued", "running", "cancelling"].includes(selected.status) || statusChanged)) {
        await refreshBuildLog(selected.id);
      }
    } catch (_error) { /* 下一轮自动重试 */ }
  }, 2000);
}

async function submitBuild(event) {
  event.preventDefault();
  const resourcePaths = [...state.selectedPaths];
  if (!resourcePaths.length) {
    const hasInput = Boolean(state.pathQuery.trim());
    toast(hasInput ? "请从模糊匹配列表中选择资源更新路径" : "玩家资源更新路径为空", "error");
    showPathRequiredHighlight();
    return;
  }
  if (state.pathQuery.trim()) {
    toast("请从模糊匹配列表中选择资源更新路径", "error");
    document.querySelector(".path-input")?.focus();
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
  state.logRefreshVersion += 1;
  drawer.dataset.buildId = String(buildId);
  drawer.innerHTML = '<div class="drawer-loading">正在加载构建详情…</div>';
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  document.querySelector("#drawer-backdrop").classList.remove("hidden");
  try {
    const payload = await api(`/api/builds/${buildId}`);
    renderDrawer(payload.build);
    await refreshBuildLog(payload.build.id);
  } catch (error) {
    drawer.innerHTML = `<div class="drawer-loading">${escapeHTML(error.message)}</div>`;
  }
}

function drawerStatusHTML(build) {
  const status = statusMap[build.status] || statusMap.queued;
  let description = `耗时 ${duration(build.duration_seconds)}`;
  if (build.status === "running") description = "打包机正在处理当前任务";
  if (build.status === "queued") description = "任务正在等待空闲构建槽";
  if (build.status === "cancelling") description = "正在停止本地脚本或 Jenkins 构建";
  if (build.status === "cancelled") description = `任务已终止 · 耗时 ${duration(build.duration_seconds)}`;
  const cancelAction = ["queued", "running"].includes(build.status)
    ? `<button class="build-cancel-button" type="button" data-cancel-build="${build.id}" aria-label="终止构建"><span aria-hidden="true">${icons.failed}</span></button>`
    : "";
  return `<div class="drawer-status-main">${statusHTML(build.status, true)}<div><h3>${status.label}</h3><p>${description}</p></div></div>${cancelAction}`;
}

function openCancelBuildDialog(buildId) {
  const build = state.builds.find(item => item.id === buildId);
  if (!build || !["queued", "running"].includes(build.status)) return;
  state.pendingCancelBuildId = buildId;
  document.querySelector("#cancel-build-number").textContent = `#${build.build_number}`;
  const dialog = document.querySelector("#cancel-build-dialog");
  if (!dialog.open) dialog.showModal();
  document.querySelector("#cancel-build-confirm")?.focus();
}

function closeCancelBuildDialog() {
  const dialog = document.querySelector("#cancel-build-dialog");
  if (dialog.open) dialog.close();
  state.pendingCancelBuildId = null;
}

async function confirmCancelBuild() {
  const buildId = state.pendingCancelBuildId;
  const build = state.builds.find(item => item.id === buildId);
  if (!build || !["queued", "running"].includes(build.status)) {
    closeCancelBuildDialog();
    return;
  }

  const confirmButton = document.querySelector("#cancel-build-confirm");
  confirmButton.disabled = true;
  confirmButton.textContent = "正在提交…";
  try {
    const payload = await api(`/api/builds/${buildId}/cancel`, {
      method: "POST",
      body: "{}",
    });
    const index = state.builds.findIndex(item => item.id === buildId);
    if (index >= 0) state.builds[index] = payload.build;
    renderBuildList();
    updateDrawerSummary(payload.build);
    closeCancelBuildDialog();
    toast(payload.build.status === "cancelled" ? "构建已终止" : "终止请求已提交");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    confirmButton.disabled = false;
    confirmButton.textContent = "确认终止";
  }
}

function renderDrawer(build) {
  const drawer = document.querySelector("#build-drawer");
  if (Number(drawer.dataset.buildId) !== build.id) return;
  drawer.dataset.buildStatus = build.status;
  drawer.dataset.otaVersion = String(build.parameters.jenkins_build_number ?? "");
  drawer.innerHTML = `<div class="drawer-top"><div><p>BUILD DETAILS</p><h2>#${build.build_number} 构建详情</h2></div><button class="icon-button drawer-close" aria-label="关闭">${icons.failed}</button></div>
    <div class="drawer-status">${drawerStatusHTML(build)}</div>
    <section class="detail-section"><h3>基础信息</h3><dl class="detail-grid"><div class="detail-row"><dt>构建项目</dt><dd>${escapeHTML(build.job_id.toUpperCase())}</dd></div><div class="detail-row"><dt>构建者</dt><dd>${escapeHTML(build.builder_name)}</dd></div><div class="detail-row"><dt>登录账号</dt><dd>${escapeHTML(build.builder_account)}</dd></div><div class="detail-row"><dt>提交时间</dt><dd>${formatDate(build.created_at)}</dd></div><div class="detail-row"><dt>开始时间</dt><dd data-drawer-field="started-at">${formatDate(build.started_at)}</dd></div><div class="detail-row"><dt>完成时间</dt><dd data-drawer-field="finished-at">${formatDate(build.finished_at)}</dd></div><div class="detail-row failure-reason${build.error_message ? "" : " hidden"}" data-drawer-failure><dt>失败原因</dt><dd>${escapeHTML(build.error_message || "")}</dd></div><div class="detail-row"><dt>OTA 版本号</dt><dd data-drawer-field="ota-version">${escapeHTML(build.parameters.jenkins_build_number ?? "—")}</dd></div></dl></section>
    <section class="detail-section"><h3>构建参数</h3><dl class="detail-grid"><div class="detail-row"><dt>资源路径</dt><dd><span class="detail-paths">${(build.parameters.resource_paths || []).map(path => `<span class="detail-path">${escapeHTML(path)}</span>`).join("")}</span></dd></div><div class="detail-row"><dt>构建说明</dt><dd>${escapeHTML(build.parameters.note || "—")}</dd></div></dl></section>
    <section class="detail-section"><h3>构建日志</h3><pre id="build-log" class="log-box">正在加载日志…</pre></section>`;
}

function updateDrawerOtaVersion(build) {
  const drawer = document.querySelector("#build-drawer");
  if (Number(drawer.dataset.buildId) !== build.id) return;
  const otaVersion = build.parameters.jenkins_build_number ?? "";
  const otaVersionNode = drawer.querySelector('[data-drawer-field="ota-version"]');
  if (otaVersionNode) otaVersionNode.textContent = otaVersion === "" ? "—" : String(otaVersion);
  drawer.dataset.otaVersion = String(otaVersion);
}

function updateDrawerSummary(build) {
  const drawer = document.querySelector("#build-drawer");
  if (Number(drawer.dataset.buildId) !== build.id) return;
  drawer.dataset.buildStatus = build.status;
  const statusRoot = drawer.querySelector(".drawer-status");
  if (statusRoot) statusRoot.innerHTML = drawerStatusHTML(build);
  const startedAt = drawer.querySelector('[data-drawer-field="started-at"]');
  const finishedAt = drawer.querySelector('[data-drawer-field="finished-at"]');
  if (startedAt) startedAt.textContent = formatDate(build.started_at);
  if (finishedAt) finishedAt.textContent = formatDate(build.finished_at);
  updateDrawerOtaVersion(build);
  const failure = drawer.querySelector("[data-drawer-failure]");
  if (failure) {
    failure.classList.toggle("hidden", !build.error_message);
    const message = failure.querySelector("dd");
    if (message) message.textContent = build.error_message || "";
  }
}

async function refreshBuildLog(buildId) {
  const drawer = document.querySelector("#build-drawer");
  if (Number(drawer.dataset.buildId) !== buildId || drawerHasTextSelection(drawer)) return;
  const requestVersion = state.logRefreshVersion + 1;
  state.logRefreshVersion = requestVersion;
  try {
    const payload = await api(`/api/builds/${buildId}/log`);
    if (state.logRefreshVersion !== requestVersion) return;
    const currentDrawer = document.querySelector("#build-drawer");
    const log = currentDrawer.querySelector("#build-log");
    if (Number(currentDrawer.dataset.buildId) !== buildId || !log || drawerHasTextSelection(currentDrawer)) return;
    const nextContent = String(payload.log ?? "");
    if (log.textContent === nextContent) return;
    const previousScrollTop = log.scrollTop;
    const wasNearBottom = log.scrollHeight - log.clientHeight - log.scrollTop <= 24;
    log.textContent = nextContent;
    if (wasNearBottom) {
      log.scrollTop = log.scrollHeight;
    } else {
      log.scrollTop = Math.min(previousScrollTop, Math.max(0, log.scrollHeight - log.clientHeight));
    }
  } catch (_error) { /* 日志轮询失败时等待下一轮自动重试 */ }
}

function closeDrawer() {
  closeCancelBuildDialog();
  const drawer = document.querySelector("#build-drawer");
  state.logRefreshVersion += 1;
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  drawer.dataset.buildId = "";
  drawer.dataset.buildStatus = "";
  drawer.dataset.otaVersion = "";
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
      body: JSON.stringify({ account: event.target.account.value, password: event.target.password.value }),
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
  const cancelButton = event.target.closest("[data-cancel-build]");
  if (cancelButton) {
    event.preventDefault();
    openCancelBuildDialog(Number(cancelButton.dataset.cancelBuild));
    return;
  }
  if (event.target.closest("[data-cancel-dialog-close]")) {
    closeCancelBuildDialog();
    return;
  }
  if (event.target.closest("#cancel-build-confirm")) {
    confirmCancelBuild();
    return;
  }
  if (!event.target.closest(".path-input-wrap")) closePathSuggestions();
  const nav = event.target.closest("[data-nav]");
  if (nav) navigate(nav.dataset.nav);
  const jobRow = event.target.closest("[data-job-id]");
  if (jobRow) navigate(`/jobs/${encodeURIComponent(jobRow.dataset.jobId)}`);
  const buildItem = event.target.closest(".build-item[data-build-id]");
  if (buildItem) openBuildDrawer(Number(buildItem.dataset.buildId));
  if (event.target.closest("#drawer-backdrop") || event.target.closest(".drawer-close")) closeDrawer();
  const suggestion = event.target.closest("[data-select-path]");
  if (suggestion) {
    selectPathSuggestion(suggestion.dataset.path);
  }
  const removeSelected = event.target.closest("[data-remove-selected-path]");
  if (removeSelected) {
    state.selectedPaths.splice(Number(removeSelected.dataset.removeSelectedPath), 1);
    renderPathRows();
    state.suppressPathFocus = true;
    document.querySelector(".path-input")?.focus();
    state.suppressPathFocus = false;
  }
});

document.addEventListener("input", event => {
  if (event.target.matches(".path-input")) {
    clearPathRequiredHighlight();
    state.pathQuery = event.target.value;
    searchPath(event.target.value);
  }
  if (event.target.id === "build-filter") {
    state.buildFilter = event.target.value;
    renderBuildList();
  }
});

document.addEventListener("focusin", event => {
  if (!state.suppressPathFocus && event.target.matches(".path-input")) searchPath(event.target.value);
});

document.addEventListener("focusout", event => {
  if (state.suppressPathFocus || !event.target.matches(".path-input")) return;
  const wrapper = event.target.closest(".path-input-wrap");
  if (wrapper?.contains(event.relatedTarget)) return;
  window.setTimeout(() => {
    if (!document.hasFocus()) return;
    const currentWrapper = document.querySelector(".path-input-wrap");
    if (!currentWrapper?.contains(document.activeElement)) closePathSuggestions();
  }, 0);
});

function restorePathSuggestionsAfterPageFocus() {
  if (!state.currentJob || !state.pathQuery.trim() || state.suggestionsOpen) return;
  searchPath(state.pathQuery);
}

window.addEventListener("focus", restorePathSuggestionsAfterPageFocus);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) restorePathSuggestionsAfterPageFocus();
});

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && document.querySelector("#cancel-build-dialog")?.open) {
    event.preventDefault();
    closeCancelBuildDialog();
    return;
  }
  const wordmark = event.target.closest?.(".wordmark.is-link[data-nav]");
  if (wordmark && event.key === "Enter") {
    event.preventDefault();
    navigate(wordmark.dataset.nav);
    return;
  }
  const pathInput = event.target.closest?.(".path-input");
  if (pathInput && !event.isComposing) {
    const suggestions = state.suggestions;
    if ((event.key === "ArrowDown" || event.key === "ArrowUp") && state.suggestionsOpen) {
      event.preventDefault();
      if (suggestions.length) {
        const direction = event.key === "ArrowDown" ? 1 : -1;
        const current = state.suggestionCursor;
        const cursor = current === null
          ? (direction > 0 ? 0 : suggestions.length - 1)
          : (current + direction + suggestions.length) % suggestions.length;
        updateSuggestionCursor(cursor);
      }
      return;
    }
    if (event.key === "Enter" && state.suggestionsOpen && state.suggestionCursor !== null) {
      event.preventDefault();
      selectPathSuggestion(suggestions[state.suggestionCursor]);
      return;
    }
  }
  if (event.key === "Escape" && state.suggestionsOpen) {
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

document.querySelector("#cancel-build-dialog").addEventListener("close", () => {
  state.pendingCancelBuildId = null;
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
