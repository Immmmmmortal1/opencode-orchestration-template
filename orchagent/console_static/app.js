"use strict";

const state = {
  snapshotHash: "",
  pipelineDefinition: null,
  promptRegistry: null,
  promptFiles: [],
  catalog: { providers: [] },
  activeView: "workflow",
  selectedRole: null,
  selectedPromptTarget: null,
  promptDrafts: new Map(),
  savePromptDraft: null,
  pendingSave: null,
  saving: false,
};

const viewMeta = {
  workflow: ["Workflow Graph", "流水线阶段、门禁与边"],
  roles: ["Roles", "v2 角色声明与宿主模型映射"],
  prompts: ["Prompts", "目标绑定、来源与 Markdown 正文"],
};

const roleIdPattern = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
let toastTimer = null;

function $(selector) {
  return document.querySelector(selector);
}

function create(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function formatValue(value, fallback = "—") {
  return value === undefined || value === null || value === "" ? fallback : String(value);
}

class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
    this.code = payload && typeof payload.error === "string" ? payload.error : "http_error";
  }
}

function errorMessage(payload, fallback) {
  if (!payload || typeof payload !== "object") return fallback;
  if (typeof payload.message === "string" && payload.message) return payload.message;
  if (typeof payload.error === "string" && payload.error) {
    const check = Array.isArray(payload.checks) ? payload.checks[0] : null;
    const location = check && check.location ? `（${check.location}）` : "";
    return `${payload.error}${location}`;
  }
  const check = Array.isArray(payload.checks) ? payload.checks[0] : null;
  if (check) return formatValue(check.message || check.code, fallback);
  return fallback;
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new ApiError(`无法连接本地 API：${error.message}`, 0, null);
  }
  let payload;
  try {
    payload = await response.json();
  } catch (_) {
    throw new ApiError("API 返回了无法解析的响应", response.status, null);
  }
  if (!response.ok || (payload && payload.status === "error")) {
    throw new ApiError(errorMessage(payload, `HTTP ${response.status}`), response.status, payload);
  }
  return payload;
}

function post(path, payload) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function setConnection(kind, message) {
  const status = $("#app-status");
  status.className = `sidebar-foot ${kind}`;
  $("#app-status-text").textContent = `${location.host} · ${message}`;
}

function showToast(title, detail = "") {
  const toast = $("#toast");
  toast.textContent = "";
  toast.append(create("div", "t", title));
  if (detail) toast.append(create("div", "d", detail));
  toast.classList.add("show");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("show"), 3600);
}

function showError(container, title, detail = "") {
  container.textContent = "";
  const banner = create("div", "error-banner");
  const copy = create("div");
  copy.append(create("div", "t", title));
  if (detail) copy.append(create("div", "d", detail));
  banner.append(copy);
  container.append(banner);
}

function setEmpty(node, visible, text) {
  node.hidden = !visible;
  if (text) node.textContent = text;
}

function badge(text, kind = "") {
  return create("span", `badge${kind ? ` ${kind}` : ""}`, text);
}

function appendCell(row, value, className = "") {
  const cell = create("td", className, formatValue(value));
  row.append(cell);
  return cell;
}

function setBusy(busy) {
  state.saving = busy;
  document.querySelectorAll("button").forEach((button) => {
    if (busy) {
      button.dataset.wasDisabled = button.disabled ? "true" : "false";
      button.disabled = true;
    } else {
      button.disabled = button.dataset.wasDisabled === "true";
      delete button.dataset.wasDisabled;
    }
  });
}

function switchView(view) {
  if (!viewMeta[view]) return;
  state.activeView = view;
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.querySelectorAll(".view").forEach((section) => {
    const active = section.id === `view-${view}`;
    section.classList.toggle("active", active);
    section.hidden = !active;
  });
  $("#topbar-title").textContent = viewMeta[view][0];
  $("#topbar-sub").textContent = viewMeta[view][1];
}

function stageGates(pipeline, stage) {
  const declared = Array.isArray(stage.gates) ? stage.gates : [];
  const definitions = Array.isArray(pipeline.gates) ? pipeline.gates : [];
  return declared.map((gateId) => definitions.find((gate) => gate && gate.id === gateId) || { id: gateId });
}

function renderWorkflowTable(card, pipeline) {
  const scroll = create("div", "table-scroll");
  const table = create("table", "table");
  const head = create("thead");
  const headRow = create("tr");
  ["stage", "role / skill", "maxAttempts", "gates", "evaluator"].forEach((label) => headRow.append(create("th", "", label)));
  head.append(headRow);
  const body = create("tbody");
  (Array.isArray(pipeline.stages) ? pipeline.stages : []).forEach((stage) => {
    const gates = stageGates(pipeline, stage);
    const row = create("tr");
    appendCell(row, stage.id, "mono");
    appendCell(row, stage.role || stage.skill, "mono");
    appendCell(row, stage.maxAttempts);
    appendCell(row, gates.map((gate) => gate.id).join(", ") || "无", "mono");
    appendCell(row, gates.map((gate) => gate.evaluator).filter(Boolean).join(", ") || "—", "mono");
    body.append(row);
  });
  table.append(head, body);
  scroll.append(table);
  card.append(scroll);
}

function renderWorkflow() {
  const list = $("#workflow-list");
  const empty = $("#workflow-empty");
  list.textContent = "";
  const definition = state.pipelineDefinition;
  if (!definition || !Array.isArray(definition.pipelines)) {
    setEmpty(empty, true, "Pipeline Registry 不可用或结构无效，无法显示流水线。");
    return;
  }
  if (!definition.pipelines.length) {
    setEmpty(empty, true, "本机 pipeline registry 中暂无可显示的流水线。");
    return;
  }
  setEmpty(empty, false);
  definition.pipelines.forEach((pipeline) => {
    const card = create("article", "workflow-card");
    const head = create("header", "workflow-head");
    head.append(create("span", "t mono", formatValue(pipeline.id, "未命名 pipeline")));
    head.append(badge(`rev ${formatValue(pipeline.revision)}`, "accent"));
    head.append(badge(pipeline.enabled ? "enabled" : "disabled", pipeline.enabled ? "ok" : "warn"));
    const route = pipeline.route;
    const routeText = route && Array.isArray(route.taskTypes)
      ? `route: ${route.taskTypes.join(", ") || "无 task type"} · priority ${formatValue(route.priority)}`
      : "未配置 route";
    const meta = create("div", "workflow-meta");
    meta.append(create("span", "", `entryStage: ${formatValue(pipeline.entryStage)}`));
    meta.append(create("span", "", routeText));
    head.append(meta);
    card.append(head);

    const canvas = create("div", "workflow-canvas");
    const track = create("div", "workflow-track");
    const stages = Array.isArray(pipeline.stages) ? pipeline.stages : [];
    stages.forEach((stage) => {
      const node = create("div", "stage-node");
      node.append(create("div", "stage-name", formatValue(stage.id)));
      node.append(create("div", "stage-role", `${stage.role ? "role" : "skill"}: ${formatValue(stage.role || stage.skill)}`));
      node.append(create("div", "stage-role", `maxAttempts: ${formatValue(stage.maxAttempts)}`));
      const gates = create("div", "gate-list");
      stageGates(pipeline, stage).forEach((gate) => {
        gates.append(create("div", "gate-node", `${formatValue(gate.id)}${gate.evaluator ? ` · ${gate.evaluator}` : ""}`));
      });
      if (!gates.childElementCount) gates.append(create("div", "gate-node", "无 gate"));
      node.append(gates);
      track.append(node);
    });
    if (!stages.length) track.append(create("div", "empty-state", "此 pipeline 没有 stage。"));
    canvas.append(track);
    card.append(canvas);

    const edges = create("div", "edge-list");
    edges.append(create("div", "workflow-meta", "真实边"));
    const edgeItems = Array.isArray(pipeline.edges) ? pipeline.edges : [];
    edgeItems.forEach((edge) => {
      const from = edge && edge.from ? edge.from : {};
      const to = edge && edge.to ? edge.to : {};
      const row = create("div", "edge-row");
      row.append(create("span", "", `${formatValue(from.stage)} / ${formatValue(from.gate)} / ${formatValue(from.verdict)}`));
      row.append(badge("→", from.verdict === "pass" ? "ok" : from.verdict === "fail" ? "warn" : ""));
      const destination = to.stage !== undefined && to.stage !== null
        ? `stage: ${to.stage}`
        : to.terminal
          ? `terminal: ${to.terminal}`
          : "终止";
      row.append(create("span", "", destination));
      edges.append(row);
    });
    if (!edgeItems.length) edges.append(create("div", "empty-state", "此 pipeline 没有 edge。"));
    card.append(edges);
    renderWorkflowTable(card, pipeline);
    list.append(card);
  });
}

function pipelineRoles() {
  return Array.isArray(state.pipelineDefinition && state.pipelineDefinition.roles)
    ? state.pipelineDefinition.roles
    : [];
}

function isV2() {
  return state.pipelineDefinition && state.pipelineDefinition.version === 2;
}

function renderRoles() {
  const body = $("#role-body");
  const empty = $("#role-empty");
  const notice = $("#role-readonly");
  const newButton = $('[data-action="role-new"]');
  body.textContent = "";
  const editable = isV2();
  notice.hidden = editable;
  notice.textContent = editable ? "" : "当前 Pipeline Registry 不是 v2，role 仅可查看，不能编辑。";
  newButton.disabled = !editable;
  const roles = pipelineRoles();
  setEmpty(empty, roles.length === 0, editable ? "当前没有可显示的 v2 role。" : "当前 registry 没有 role 声明。");
  roles.forEach((role) => {
    const row = create("tr");
    appendCell(row, role.id, "mono");
    appendCell(row, role.provider, "mono");
    appendCell(row, role.model, "mono");
    appendCell(row, role.description);
    const action = create("td");
    const button = create("button", "btn-sm", editable ? "编辑" : "查看");
    button.type = "button";
    button.addEventListener("click", () => {
      state.selectedRole = role.id;
      renderRoleEditor(role, false);
    });
    action.append(button);
    row.append(action);
    body.append(row);
  });
  const selected = roles.find((role) => role.id === state.selectedRole);
  if (selected) renderRoleEditor(selected, false);
  else {
    state.selectedRole = null;
    $("#role-edit-hint").textContent = "选择上方角色后在此编辑";
    $("#role-edit-body").textContent = "";
  }
}

function field(labelText, control, errorText = "") {
  const wrapper = create("div", "field");
  const label = create("label", "", labelText);
  const id = `field-${Math.random().toString(36).slice(2)}`;
  control.id = id;
  label.htmlFor = id;
  wrapper.append(label, control);
  if (errorText) wrapper.append(create("div", "err-msg", errorText));
  return wrapper;
}

function addOption(select, value, label) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  select.append(option);
}

function providerById(providerId) {
  const providers = Array.isArray(state.catalog.providers) ? state.catalog.providers : [];
  return providers.find((provider) => provider.id === providerId);
}

function populateProviders(select, selected) {
  select.textContent = "";
  const providers = Array.isArray(state.catalog.providers) ? state.catalog.providers : [];
  providers.forEach((provider) => addOption(select, provider.id, `${provider.name} (${provider.id})`));
  if (selected && !providers.some((provider) => provider.id === selected)) {
    addOption(select, selected, `${selected} (legacy / unavailable)`);
  }
  if (!providers.length && !selected) addOption(select, "", "无可用 provider");
  select.value = selected || "";
}

function populateModels(select, providerId, selected) {
  select.textContent = "";
  const provider = providerById(providerId);
  const models = provider && Array.isArray(provider.models) ? provider.models : [];
  models.forEach((model) => addOption(select, model.id, `${model.name} (${model.id})`));
  if (selected && !models.some((model) => model.id === selected)) {
    addOption(select, selected, `${selected} (legacy / unavailable)`);
  }
  if (!models.length && !selected) addOption(select, "", "无可用 model");
  select.value = selected || "";
}

function renderRoleEditor(role, isNew) {
  const container = $("#role-edit-body");
  container.textContent = "";
  $("#role-edit-hint").textContent = isNew ? "创建新的 v2 role" : `编辑 ${formatValue(role.id)}`;
  const form = create("form");
  const grid = create("div", "form-grid");
  const idInput = document.createElement("input");
  idInput.value = role.id || "";
  idInput.disabled = !isNew;
  const providerSelect = document.createElement("select");
  const modelSelect = document.createElement("select");
  populateProviders(providerSelect, role.provider || "");
  populateModels(modelSelect, providerSelect.value, role.model || "");
  providerSelect.addEventListener("change", () => populateModels(modelSelect, providerSelect.value, ""));
  const description = document.createElement("input");
  description.value = role.description || "";
  grid.append(
    field("id", idInput, "id 必须匹配 [A-Za-z0-9][A-Za-z0-9._-]*"),
    field("provider", providerSelect),
    field("model", modelSelect),
    field("description", description),
  );
  const save = create("button", "", isNew ? "创建并保存" : "保存 role");
  save.type = "submit";
  form.append(grid, create("div", "field"), save);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const id = idInput.value.trim();
    if (!roleIdPattern.test(id)) {
      idInput.parentElement.classList.add("invalid");
      showToast("无法保存 role", "id 格式不合法");
      return;
    }
    if (isNew && pipelineRoles().some((item) => item.id === id)) {
      showToast("无法保存 role", `role ${id} 已存在`);
      return;
    }
    const definition = clone(state.pipelineDefinition);
    const replacement = {
      id,
      provider: providerSelect.value.trim(),
    };
    if (!replacement.provider) {
      showToast("无法保存 role", "provider 不能为空");
      return;
    }
    const model = modelSelect.value.trim();
    const roleDescription = description.value.trim();
    if (model) replacement.model = model;
    if (roleDescription) replacement.description = roleDescription;
    if (isNew) definition.roles.push(replacement);
    else {
      const index = definition.roles.findIndex((item) => item.id === role.id);
      if (index < 0) {
        showToast("无法保存 role", "原 role 已不存在，请重新加载");
        return;
      }
      definition.roles[index] = replacement;
    }
    await beginSave({ pipeline: definition }, isNew ? `新建 role：${id}` : `更新 role：${id}`);
  });
  container.append(form);
}

function promptMap() {
  const prompts = state.promptRegistry && Array.isArray(state.promptRegistry.prompts)
    ? state.promptRegistry.prompts
    : [];
  return new Map(prompts.map((prompt) => [prompt.id, prompt]));
}

function promptFileMap() {
  return new Map(state.promptFiles.map((file) => [file.path, file.content]));
}

function promptTargets() {
  return state.promptRegistry && Array.isArray(state.promptRegistry.targets)
    ? state.promptRegistry.targets.filter((target) => target && target.kind === "role")
    : [];
}

function renderPrompts() {
  const body = $("#prompt-body");
  const empty = $("#prompt-empty");
  const editor = $("#prompt-edit-body");
  body.textContent = "";
  if (!state.promptRegistry) {
    setEmpty(empty, true, "当前没有可用的 Prompt Registry；本控制台不会凭空创建默认 registry。");
    $("#prompt-edit-hint").textContent = "当前无 Prompt Registry";
    editor.textContent = "当前无 Prompt Registry，无法编辑目标绑定。";
    return;
  }
  const targets = promptTargets();
  setEmpty(empty, targets.length === 0, "Prompt Registry 中暂无 kind=role 的目标绑定。");
  targets.forEach((target) => {
    const row = create("tr");
    appendCell(row, target.id, "mono");
    appendCell(row, target.override ? "override" : "spec");
    appendCell(row, target.prompt, "mono");
    const action = create("td");
    const button = create("button", "btn-sm", "编辑");
    button.type = "button";
    button.addEventListener("click", () => {
      if (state.savePromptDraft) state.savePromptDraft();
      state.selectedPromptTarget = target.id;
      renderPromptEditor(target);
    });
    action.append(button);
    row.append(action);
    body.append(row);
  });
  const selected = targets.find((target) => target.id === state.selectedPromptTarget);
  if (selected) renderPromptEditor(selected);
  else {
    state.selectedPromptTarget = null;
    $("#prompt-edit-hint").textContent = "选择上方目标后在此编辑";
    editor.textContent = "";
  }
}

function renderPromptEditor(target) {
  const container = $("#prompt-edit-body");
  container.textContent = "";
  $("#prompt-edit-hint").textContent = `编辑 role target：${target.id}`;
  const prompts = promptMap();
  const files = promptFileMap();
  const form = create("form");
  const grid = create("div", "form-grid");
  const targetInput = document.createElement("input");
  targetInput.value = target.id;
  targetInput.disabled = true;
  const promptSelect = document.createElement("select");
  prompts.forEach((prompt, id) => addOption(promptSelect, id, id));
  if (target.prompt && !prompts.has(target.prompt)) addOption(promptSelect, target.prompt, `${target.prompt} (unavailable)`);
  promptSelect.value = target.prompt || "";
  const sourceSelect = document.createElement("select");
  addOption(sourceSelect, "spec", "spec");
  addOption(sourceSelect, "override", "override");
  sourceSelect.value = target.override ? "override" : "spec";
  const pathInput = document.createElement("input");
  pathInput.disabled = true;
  const textarea = document.createElement("textarea");
  const warning = create("div", "notice");
  let currentDraftKey = "";

  function selectedPath() {
    if (sourceSelect.value === "override") return target.override;
    const prompt = prompts.get(promptSelect.value);
    return prompt && prompt.template;
  }

  function draftKey() {
    const path = selectedPath();
    return path ? `${sourceSelect.value}:${path}` : "";
  }

  function saveDraft() {
    if (currentDraftKey) state.promptDrafts.set(currentDraftKey, textarea.value);
  }

  function refreshBody() {
    const path = selectedPath();
    pathInput.value = path || "";
    warning.hidden = sourceSelect.value !== "spec";
    warning.textContent = "修改 spec 会影响所有引用该 prompt 的目标。";
    const key = draftKey();
    currentDraftKey = key;
    const sourceFound = Boolean(path && files.has(path));
    textarea.value = key && state.promptDrafts.has(key)
      ? state.promptDrafts.get(key)
      : sourceFound
        ? files.get(path)
        : "";
    textarea.dataset.sourceFound = sourceFound ? "true" : "false";
    textarea.disabled = sourceSelect.value === "override" && !sourceFound;
  }

  state.savePromptDraft = saveDraft;
  promptSelect.addEventListener("change", () => {
    saveDraft();
    refreshBody();
  });
  sourceSelect.addEventListener("change", () => {
    const requestedSource = sourceSelect.value;
    saveDraft();
    if (requestedSource === "override" && (!target.override || !files.has(target.override))) {
      sourceSelect.value = "spec";
      showToast("无法切换到 override", "当前目标没有已声明 override 路径；请先在 Prompt Registry 中声明并创建文件");
    }
    refreshBody();
  });
  grid.append(field("target id", targetInput), field("prompt id", promptSelect), field("来源", sourceSelect), field("正文路径", pathInput));
  refreshBody();
  const bodyField = field("Markdown 正文", textarea);
  const save = create("button", "", "保存 prompt");
  save.type = "submit";
  form.append(grid, warning, bodyField, save);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const registry = clone(state.promptRegistry);
    const targetIndex = registry.targets.findIndex((item) => item.kind === "role" && item.id === target.id);
    const selectedPrompt = registry.prompts.find((item) => item.id === promptSelect.value);
    if (targetIndex < 0 || !selectedPrompt) {
      showToast("无法保存 prompt", "目标或 prompt id 已不存在，请重新加载");
      return;
    }
    const updatedTarget = registry.targets[targetIndex];
    updatedTarget.prompt = selectedPrompt.id;
    let path;
    if (sourceSelect.value === "spec") {
      delete updatedTarget.override;
      path = selectedPrompt.template;
      if (!path || !files.has(path)) {
        showToast("无法保存 prompt", "所选 prompt 的 template 正文或路径找不到");
        return;
      }
    } else {
      path = target.override;
      if (!path || !files.has(path)) {
        showToast("无法保存 prompt", "当前目标没有已声明 override 路径；请先在 Prompt Registry 中声明并创建文件");
        return;
      }
      updatedTarget.override = path;
    }
    const draft = { prompts: registry, files: [{ path, content: textarea.value }] };
    await beginSave(draft, `更新 prompt target：${target.id}`);
  });
  container.append(form);
}

function renderDiff(title, result) {
  const modal = $("#save-modal");
  const body = $("#save-body");
  $("#save-title").textContent = title;
  body.textContent = "";
  const changes = Array.isArray(result.changes) ? result.changes : [];
  if (!changes.length) {
    body.append(create("div", "empty-state", "Diff 没有返回文件变更。"));
  } else {
    const scroll = create("div", "table-scroll");
    const table = create("table", "table");
    const head = create("thead");
    const headRow = create("tr");
    ["path", "operation", "beforeHash", "afterHash"].forEach((label) => headRow.append(create("th", "", label)));
    head.append(headRow);
    const rows = create("tbody");
    changes.forEach((change) => {
      const row = create("tr");
      appendCell(row, change.path, "mono");
      appendCell(row, change.operation);
      appendCell(row, change.beforeHash, "mono");
      appendCell(row, change.afterHash, "mono");
      rows.append(row);
    });
    table.append(head, rows);
    scroll.append(table);
    body.append(scroll);
  }
  modal.hidden = false;
}

async function beginSave(draft, title) {
  if (state.saving) return;
  setBusy(true);
  try {
    await post("/api/v1/draft/validate", { draft });
    const diff = await post("/api/v1/draft/diff", { draft });
    if (!diff.diffId) throw new ApiError("Diff 响应缺少 diffId", 0, diff);
    state.pendingSave = { draft, diffId: diff.diffId };
    renderDiff(title, diff);
  } catch (error) {
    state.pendingSave = null;
    const body = $("#save-body");
    showError(body, "保存前检查失败", error.message);
    $("#save-title").textContent = title;
    $("#save-modal").hidden = false;
    $("#save-only").disabled = true;
    $("#save-confirm").disabled = true;
    showToast("保存前检查失败", error.message);
  } finally {
    setBusy(false);
    $("#save-only").disabled = !state.pendingSave;
    $("#save-confirm").disabled = !state.pendingSave;
  }
}

function cancelSave() {
  state.pendingSave = null;
  $("#save-modal").hidden = true;
  $("#save-body").textContent = "";
}

function isSnapshotConflict(error) {
  return error instanceof ApiError && (error.status === 409 || error.code === "snapshot_conflict");
}

async function confirmSave(syncOpencode) {
  if (!state.pendingSave || state.saving) return;
  const pending = state.pendingSave;
  setBusy(true);
  try {
    const result = await post("/api/v1/draft/apply", {
      draft: pending.draft,
      snapshotHash: state.snapshotHash,
      confirmDiffId: pending.diffId,
      syncOpencode,
    });
    cancelSave();
    setBusy(false);
    const reloaded = await load();
    const reloadDetail = reloaded ? "" : "配置重新读取失败，请手动重新加载。";
    if (!syncOpencode) {
      showToast("已保存，尚未同步到 OpenCode", reloadDetail);
    } else if (result.synced && result.syncStatus === "ok") {
      showToast("已保存并同步，请完整重启 OpenCode", reloadDetail);
    } else {
      const syncMessage = result.syncError && typeof result.syncError.message === "string"
        ? result.syncError.message.trim()
        : "";
      const syncDetail = syncMessage ? `同步失败：${syncMessage}。可重试同步。` : "同步失败，可重试同步。";
      showToast("已保存但未同步", `${syncDetail}${reloadDetail ? ` ${reloadDetail}` : ""}`);
    }
  } catch (error) {
    cancelSave();
    if (isSnapshotConflict(error)) {
      showToast("保存已取消", "配置已被其他会话修改，请重新加载后再编辑");
    } else {
      showToast("保存失败", error.message);
    }
  } finally {
    if (state.saving) setBusy(false);
  }
}

function resetLoadedState() {
  state.snapshotHash = "";
  state.pipelineDefinition = null;
  state.promptRegistry = null;
  state.promptFiles = [];
  state.catalog = { providers: [] };
  state.selectedRole = null;
  state.selectedPromptTarget = null;
  state.promptDrafts.clear();
  state.savePromptDraft = null;
  state.pendingSave = null;
}

async function load() {
  if (state.saving) return;
  $("#skeleton").classList.remove("hidden");
  setConnection("loading", "正在连接");
  cancelSave();
  try {
    const [snapshot, catalog] = await Promise.all([
      request("/api/v1/snapshot"),
      request("/api/v1/provider-catalog"),
    ]);
    state.snapshotHash = snapshot.snapshotHash || "";
    state.pipelineDefinition = snapshot.pipeline && snapshot.pipeline.definition
      ? snapshot.pipeline.definition
      : null;
    state.promptRegistry = snapshot.prompts && snapshot.prompts.registry
      ? snapshot.prompts.registry
      : null;
    state.promptFiles = snapshot.prompts && Array.isArray(snapshot.prompts.files)
      ? snapshot.prompts.files
      : [];
    state.catalog = catalog && Array.isArray(catalog.providers) ? catalog : { providers: [] };
    state.selectedRole = null;
    state.selectedPromptTarget = null;
    state.promptDrafts.clear();
    state.savePromptDraft = null;
    renderWorkflow();
    renderRoles();
    renderPrompts();
    setConnection("ok", "已连接");
    return true;
  } catch (error) {
    resetLoadedState();
    renderWorkflow();
    renderRoles();
    renderPrompts();
    setConnection("error", "连接失败");
    showError($("#workflow-list"), "加载失败", error.message);
    showToast("加载失败", error.message);
    return false;
  } finally {
    $("#skeleton").classList.add("hidden");
  }
}

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});

$('[data-action="role-new"]').addEventListener("click", () => {
  if (!isV2()) return;
  state.selectedRole = null;
  renderRoleEditor({ id: "", provider: "", model: "", description: "" }, true);
});

$("#reload").addEventListener("click", load);
$("#save-cancel").addEventListener("click", cancelSave);
$("#save-only").addEventListener("click", () => confirmSave(false));
$("#save-confirm").addEventListener("click", () => confirmSave(true));

switchView(state.activeView);
load();
