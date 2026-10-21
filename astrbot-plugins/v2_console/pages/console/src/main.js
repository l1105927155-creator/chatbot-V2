import * as monaco from "monaco-editor/esm/vs/editor/editor.api";
import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import jsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import "monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution";
import "monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution";
import "monaco-editor/esm/vs/basic-languages/python/python.contribution";
import "monaco-editor/esm/vs/basic-languages/javascript/javascript.contribution";
import "monaco-editor/esm/vs/basic-languages/typescript/typescript.contribution";
import "monaco-editor/esm/vs/basic-languages/html/html.contribution";
import "monaco-editor/esm/vs/basic-languages/css/css.contribution";
import "monaco-editor/esm/vs/basic-languages/shell/shell.contribution";
import "monaco-editor/esm/vs/language/json/monaco.contribution";
import "./style.css";

self.MonacoEnvironment = { getWorker: (_moduleId, label) => label === "json" ? new jsonWorker() : new editorWorker() };

const ENDPOINT = "dispatch";
const $ = (id) => document.getElementById(id);
// Plugin iframes intentionally disallow window.prompt/confirm. Use an in-page
// dialog so all actions work without weakening AstrBot's sandbox policy.
function askDialog(message, initialValue, confirmation) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.id = "console-action-dialog";
    const form = document.createElement("form");
    const title = document.createElement("h2");
    title.textContent = message;
    form.append(title);
    const input = document.createElement("input");
    input.id = "console-action-input";
    if (!confirmation) {
      input.value = initialValue || "";
      input.required = true;
      input.setAttribute("aria-label", message);
      input.style.width = "100%";
      form.append(input);
    }
    const actions = document.createElement("div");
    actions.className = "dialog-actions";
    const cancel = document.createElement("button");
    cancel.type = "button"; cancel.className = "button secondary"; cancel.textContent = "取消";
    cancel.addEventListener("click", () => dialog.close("cancel"));
    const accept = document.createElement("button");
    accept.id = "console-action-accept"; accept.type = "submit";
    accept.className = "button primary"; accept.textContent = "确定";
    actions.append(cancel, accept); form.append(actions); dialog.append(form);
    document.body.append(dialog);
    form.addEventListener("submit", (event) => { event.preventDefault(); dialog.close("accept"); });
    dialog.addEventListener("close", () => {
      const value = dialog.returnValue === "accept" ? (confirmation ? true : input.value) : null;
      dialog.remove(); resolve(value);
    }, { once: true });
    dialog.showModal();
    if (!confirmation) { input.focus(); input.select(); }
  });
}
const askText = (message, initialValue = "") => askDialog(message, initialValue, false);
const confirmAction = async (message) => (await askDialog(message, "", true)) === true;
const state = {
  revision: null,
  defaults: null,
  conversations: [],
  templates: [],
  todos: [],
  models: [],
  modelCatalogAvailable: false,
  selectedConversation: null,
  selectedTemplate: null,
  conversationEpoch: 0,
  templateEpoch: 0,
  conversationOpenEpoch: 0,
  templateOpenEpoch: 0,
  conversationListEpoch: 0,
  templateListEpoch: 0,
  file: null,
  templateFile: null,
  selectedEntry: null,
  templateSelectedEntry: null,
  directory: "",
  templateDirectory: "",
  editor: null,
  templateEditor: null,
  editorDirty: false,
  templateEditorDirty: false,
};

async function api(action, payload = {}) {
  const bridge = window.AstrBotPluginPage;
  if (!bridge || typeof bridge.apiPost !== "function") {
    throw new Error("AstrBot 插件页面 bridge 未就绪。请从 AstrBot Dashboard 打开此页面。");
  }
  const response = await bridge.apiPost(ENDPOINT, { action, ...payload });
  if (response?.status === "error") {
    const error = new Error(response.message || "请求失败");
    error.serverResponse = true;
    throw error;
  }
  // Pinned Dashboard bridge unwraps ApiEnvelope.data and rejects errors.
  if (!response || typeof response !== "object") throw new Error("服务端返回格式未识别。");
  return response;
}

function notice(message, type = "info") {
  const node = $("notice");
  node.textContent = message;
  node.className = `notice ${type}`;
  window.clearTimeout(notice.timer);
  notice.timer = window.setTimeout(() => node.classList.add("hidden"), type === "error" ? 9000 : 5000);
}

function busy(on, message = "正在处理…") {
  $("busy-text").textContent = message;
  $("busy").classList.toggle("hidden", !on);
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function withTimeout(promise, timeoutMs, description) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = window.setTimeout(() => reject(new Error(description)), timeoutMs); }),
    ]);
  } finally {
    window.clearTimeout(timer);
  }
}

async function run(action, payload, options = {}) {
  busy(true, options.busy || "正在处理…");
  try {
    const result = await api(action, payload);
    if (options.reload !== false) await loadSnapshot();
    if (options.success) showResult(result, options.success, "success");
    return result;
  } catch (error) {
    notice(error.message || String(error), "error");
    throw error;
  } finally {
    busy(false);
  }
}

function showResult(result, fallback, kind = "info") {
  const todo = result?.todo;
  const message = result?.message || fallback;
  if (todo) {
    const details = Array.isArray(todo) ? todo.join("；") : String(todo);
    notice(`${message} TODO(待确认): ${details}`, "info");
  } else {
    notice(message, kind);
  }
}

function settingsCopy(settings) {
  return { model: settings?.model ?? null, enabled: typeof settings?.enabled === "boolean" ? settings.enabled : null, editors: [...(settings?.editors ?? [])] };
}

function selectBoolean(select, value) {
  select.value = value === true ? "true" : value === false ? "false" : "";
}

function readBoolean(select, label) {
  if (select.value === "") throw new Error(`请明确选择${label}，未选择时不会保存。`);
  return select.value === "true";
}

function selectedSession() {
  return state.conversations.find((item) => item.id === state.selectedConversation) || null;
}

function selectedTemplate() {
  return state.templates.find((item) => item.id === state.selectedTemplate) || null;
}

function displayConversation(item) {
  const group = item.conversation_key?.startsWith("group:") || item.group_id;
  const numeric = item.group_id || item.conversation_key?.replace(/^group:/, "") || item.conversation_key || item.id;
  return { group, title: item.group_name || (group ? `群 ${numeric}` : `Owner 私聊 ${numeric}`), subtitle: `${item.platform_id || "TODO(待确认): platform"} · ${item.bot_id || "TODO(待确认): bot"}` };
}

function renderSessions() {
  const list = $("sessions");
  list.replaceChildren();
  for (const item of state.conversations) {
    const display = displayConversation(item);
    const button = document.createElement("button");
    button.className = `session-item${item.id === state.selectedConversation ? " selected" : ""}`;
    button.type = "button";
    button.innerHTML = `<span class="session-icon">${display.group ? "群" : "我"}</span><span class="session-copy"><strong></strong><small></small></span><i class="state-dot"></i>`;
    button.querySelector("strong").textContent = display.title;
    button.querySelector("small").textContent = display.subtitle;
    button.querySelector(".state-dot").classList.toggle("off", item.settings?.enabled === false);
    button.querySelector(".state-dot").classList.toggle("unknown", typeof item.settings?.enabled !== "boolean");
    button.addEventListener("click", () => selectConversation(item.id));
    list.append(button);
  }
  renderTemplateOptions();
}

function fillModelSelect(providerSelect, modelSelect, current, models = state.models) {
  const providers = new Map();
  for (const row of models) {
    const provider = row.provider || row.provider_id || row.provider_name || (Array.isArray(row.models) ? row.id || row.name : "");
    if (!provider) continue;
    if (!providers.has(provider)) providers.set(provider, []);
    const model = row.model || row.model_id;
    if (model) providers.get(provider).push({ id: model, label: row.name || row.display_name || model });
    for (const nested of row.models || []) {
      const id = typeof nested === "string" ? nested : nested.id || nested.name;
      if (id) providers.get(provider).push({ id, label: nested.display_name || id });
    }
  }
  const currentProvider = current?.provider || "";
  if (currentProvider && !providers.has(currentProvider)) providers.set(currentProvider, []);
  providerSelect.replaceChildren(new Option(state.modelCatalogAvailable ? "选择 Provider" : "TODO(待确认): 当前仅显示已选模型", ""));
  for (const provider of [...providers.keys()].sort()) providerSelect.add(new Option(provider, provider));
  if (currentProvider) providerSelect.value = currentProvider;
  const populateModels = () => {
    const provider = providerSelect.value;
    const list = providers.get(provider) || [];
    modelSelect.replaceChildren(new Option(list.length ? "选择模型" : "TODO(待确认): 模型选择目录不可用", ""));
    for (const model of list) modelSelect.add(new Option(model.label, model.id));
    if (current?.provider === provider && current?.model) {
      if (![...modelSelect.options].some((option) => option.value === current.model)) modelSelect.add(new Option(`${current.model}（当前）`, current.model));
      modelSelect.value = current.model;
    }
  };
  providerSelect.onchange = populateModels;
  populateModels();
  return { providers, populateModels };
}

async function loadModels(conversationId = state.selectedConversation) {
  conversationId = conversationId || state.conversations[0]?.id;
  if (!conversationId) {
    state.models = [];
    state.modelCatalogAvailable = false;
    return;
  }
  try {
    const result = await api("models", { conversation_id: conversationId });
    state.models = Array.isArray(result) ? result : result?.choices || result?.models || result?.providers || [];
    state.modelCatalogAvailable = result?.available === false ? false : state.models.length > 0;
    if (result?.todo || result?.available === false || !state.modelCatalogAvailable) {
      notice(result?.todo || "TODO(待确认): 服务端尚未提供可供选择的模型目录。", "info");
    }
  } catch (error) {
    state.models = [];
    state.modelCatalogAvailable = false;
    notice(`模型目录暂不可用：${error.message}。TODO(待确认): 确认模型服务适配。`, "error");
  }
}

function renderSettings() {
  const session = selectedSession();
  const defaults = settingsCopy(state.defaults || {});
  selectBoolean($("default-enabled"), defaults.enabled);
  $("default-editors").value = defaults.editors.join("\n");
  fillModelSelect($("default-provider"), $("default-model"), defaults.model);
  if (!session) return;
  const settings = settingsCopy(session.settings || {});
  selectBoolean($("session-enabled-input"), settings.enabled);
  $("session-editors").value = settings.editors.join("\n");
  fillModelSelect($("session-provider"), $("session-model"), settings.model);
  $("session-enabled").textContent = settings.enabled === null ? "先配置 Agent 状态" : settings.enabled ? "停用 Agent" : "启用 Agent";
  $("session-enabled").disabled = settings.enabled === null;
  $("session-enabled").classList.remove("hidden");
}

function renderSession() {
  const session = selectedSession();
  const empty = !session;
  $("empty-session").classList.toggle("hidden", !empty);
  $("session-workspace").classList.toggle("hidden", empty);
  $("session-enabled").classList.toggle("hidden", empty);
  if (!session) {
    $("session-title").textContent = "选择一个会话";
    $("session-subtitle").textContent = "";
    return;
  }
  const display = displayConversation(session);
  $("session-title").textContent = display.title;
  $("session-subtitle").textContent = `${display.subtitle} · ${session.conversation_key || "TODO(待确认): conversation key"}`;
  renderSettings();
  listFiles("conversation").catch(() => {});
}

function captureFileScope(kind) {
  const context = fileContext(kind);
  return {
    context,
    epoch: kind === "conversation" ? state.conversationEpoch : state.templateEpoch,
    directory: currentDirectory(kind),
  };
}

function isCurrentFileScope(kind, scope) {
  return scope.epoch === (kind === "conversation" ? state.conversationEpoch : state.templateEpoch)
    && scope.directory === currentDirectory(kind)
    && (kind === "conversation" ? scope.context.conversation_id === state.selectedConversation : scope.context.template_id === state.selectedTemplate);
}

function nextOpenEpoch(kind) {
  const field = kind === "conversation" ? "conversationOpenEpoch" : "templateOpenEpoch";
  state[field] += 1;
  return state[field];
}

async function selectConversation(id) {
  if (id === state.selectedConversation) {
    showView("session");
    return;
  }
  if (state.editorDirty && !await confirmAction("当前文件有未保存修改。切换会话会丢弃这些修改，继续吗？")) return;
  showView("session");
  state.conversationEpoch += 1;
  state.selectedConversation = id;
  state.directory = "";
  state.file = null;
  state.selectedEntry = null;
  state.editorDirty = false;
  renderSessions();
  renderSession();
  loadModels(id).then(renderSettings).catch(() => {});
}

function renderTemplateOptions() {
  const select = document.querySelector('#add-group-form select[name="template_id"]');
  if (!select) return;
  const selected = select.value;
  select.replaceChildren(new Option("选择 Workspace 模板", ""));
  for (const template of state.templates) select.add(new Option(template.name, template.id));
  if (state.templates.some((item) => item.id === selected)) select.value = selected;
}

async function loadSnapshot() {
  const snapshot = await api("snapshot");
  if (!snapshot || typeof snapshot.revision !== "number" || !Array.isArray(snapshot.conversations) || !Array.isArray(snapshot.templates)) {
    throw new Error("Snapshot 响应不符合约定；控制台保持只读。TODO(待确认): 检查后端接口。");
  }
  state.revision = snapshot.revision;
  state.defaults = settingsCopy(snapshot.defaults || {});
  state.conversations = snapshot.conversations;
  state.templates = snapshot.templates;
  state.todos = snapshot.todos || [];
  if (!state.models.length && state.conversations.length) await loadModels(state.conversations[0].id);
  if (state.selectedConversation && !state.conversations.some((item) => item.id === state.selectedConversation)) state.selectedConversation = null;
  if (state.selectedTemplate && !state.templates.some((item) => item.id === state.selectedTemplate)) state.selectedTemplate = null;
  const health = snapshot.health;
  const healthLabel = health
    ? ` · Bot ${health.ready === true ? "就绪" : "启动中"} · QQ ${health.onebot_connected === true ? "已连接" : health.onebot_connected === false ? "未连接" : "状态未知"}`
    : " · TODO(待确认): 服务健康状态未提供";
  $("connection").textContent = `已连接 · 配置版本 ${state.revision}${healthLabel}`;
  $("connection").classList.add("connected");
  renderSessions();
  renderSettings();
  renderSession();
  renderTemplates();
  if (state.todos.length) notice(`后端待确认项：${state.todos.join("；")}`, "info");
}

function parseEditors(value) {
  return [...new Set(value.split(/[\s,，;；]+/).map((part) => part.trim()).filter(Boolean))];
}

function modelFrom(providerId, modelId) {
  return providerId && modelId ? { provider: providerId, model: modelId } : null;
}

function readDefaultForm() {
  return { model: modelFrom($("default-provider").value, $("default-model").value), enabled: readBoolean($("default-enabled"), "新会话 Agent 状态"), editors: parseEditors($("default-editors").value) };
}

function readSessionForm() {
  return { model: modelFrom($("session-provider").value, $("session-model").value), enabled: readBoolean($("session-enabled-input"), "会话 Agent 状态"), editors: parseEditors($("session-editors").value) };
}

async function saveDefaults() {
  await run("defaults.save", { settings: readDefaultForm(), expected_revision: state.revision }, { busy: "保存默认设置…", success: "默认设置已保存，新会话将复制此版本。" });
}

async function saveSession() {
  const session = selectedSession();
  if (!session) return;
  await run("conversation.save", { conversation_id: session.id, settings: readSessionForm(), expected_revision: state.revision }, { busy: "保存会话设置…", success: "会话设置已保存。" });
}

async function restoreField(field) {
  const session = selectedSession();
  if (!session) return;
  const sessionId = session.id;
  const epoch = state.conversationEpoch;
  try {
    const latest = await api("snapshot");
    if (state.selectedConversation !== sessionId || state.conversationEpoch !== epoch) return;
    const current = latest.conversations.find((item) => item.id === session.id);
    if (!current || JSON.stringify(current.settings) !== JSON.stringify(session.settings)) {
      throw new Error("会话设置已被修改，请先刷新后再恢复默认。");
    }
    state.revision = latest.revision;
    state.defaults = latest.defaults;
    const value = latest.defaults[field];
    if (field === "model") fillModelSelect($("session-provider"), $("session-model"), value);
    else if (field === "enabled") selectBoolean($("session-enabled-input"), value);
    else if (field === "editors") $("session-editors").value = value.join("\n");
    notice(`已填入当前默认${fieldLabel(field)}，点击“保存会话设置”后生效。`, "info");
  } catch (error) { notice(error.message || String(error), "error"); }
}

function fieldLabel(field) { return ({ model: "模型", enabled: "启用状态", editors: "修改白名单" })[field] || field; }

function renderTemplates() {
  const list = $("template-list");
  list.replaceChildren();
  if (!state.templates.length) {
    const empty = document.createElement("p");
    empty.className = "muted empty-template";
    empty.textContent = "还没有模板。创建空模板后，可在右侧编辑初始目录和文件。";
    list.append(empty);
  }
  for (const template of state.templates) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `template-item${template.id === state.selectedTemplate ? " selected" : ""}`;
    button.textContent = template.name;
    button.addEventListener("click", async () => {
      if (state.templateEditorDirty && !await confirmAction("模板文件有未保存修改，继续将丢弃修改。继续吗？")) return;
      if (button.isConnected === false) return;
      state.templateEpoch += 1;
      state.selectedTemplate = template.id;
      state.templateDirectory = "";
      state.templateFile = null;
      state.templateSelectedEntry = null;
      state.templateEditorDirty = false;
      renderTemplates();
      listFiles("template").catch(() => {});
    });
    list.append(button);
  }
  const template = selectedTemplate();
  $("template-title").textContent = template?.name || "选择模板";
  for (const id of ["rename-template", "delete-template", "template-new-file", "template-new-directory", "template-upload-file"]) $(id).disabled = !template;
}

function fileContext(kind) {
  const conversation = kind === "conversation" ? selectedSession() : null;
  const template = kind === "template" ? selectedTemplate() : null;
  if (kind === "conversation" && !conversation) throw new Error("请先选择会话。");
  if (kind === "template" && !template) throw new Error("请先选择模板。");
  return conversation ? { conversation_id: conversation.id } : { template_id: template.id };
}

function currentDirectory(kind) { return kind === "conversation" ? state.directory : state.templateDirectory; }
function setDirectory(kind, value) { if (kind === "conversation") state.directory = value; else state.templateDirectory = value; }
function currentFile(kind) { return kind === "conversation" ? state.file : state.templateFile; }
function setCurrentFile(kind, value) { if (kind === "conversation") state.file = value; else state.templateFile = value; }
function selectedEntry(kind) { return kind === "conversation" ? state.selectedEntry : state.templateSelectedEntry; }
function setSelectedEntry(kind, value) { if (kind === "conversation") state.selectedEntry = value; else state.templateSelectedEntry = value; }
function activeEditor(kind) { return kind === "conversation" ? state.editor : state.templateEditor; }
function isDirty(kind) { return kind === "conversation" ? state.editorDirty : state.templateEditorDirty; }
function setDirty(kind, value) { if (kind === "conversation") state.editorDirty = value; else state.templateEditorDirty = value; }

function languageFor(path) {
  const ext = path.split(".").pop().toLowerCase();
  return ({ md: "markdown", markdown: "markdown", json: "json", yaml: "yaml", yml: "yaml", py: "python", js: "javascript", mjs: "javascript", cjs: "javascript", ts: "typescript", html: "html", htm: "html", css: "css", txt: "plaintext", sh: "shell" })[ext] || "plaintext";
}

function getEditor(kind) {
  const field = kind === "conversation" ? "editor" : "templateEditor";
  if (!state[field]) {
    const id = kind === "conversation" ? "editor" : "template-editor";
    state[field] = monaco.editor.create($(id), { value: "", language: "plaintext", theme: document.documentElement.dataset.theme === "dark" ? "vs-dark" : "vs", automaticLayout: true, minimap: { enabled: false }, fontSize: 13, wordWrap: "on", scrollBeyondLastLine: false });
    state[field].onDidChangeModelContent(() => setDirty(kind, true));
  }
  return state[field];
}

function treeIds(kind) {
  return kind === "conversation"
    ? { path: "current-path", tree: "file-tree", filename: "editor-file-name", version: "file-version" }
    : { path: "template-current-path", tree: "template-file-tree", filename: "template-editor-file-name", version: "template-file-version" };
}

function joinPath(parent, name) { return parent ? `${parent}/${name}` : name; }

async function listFiles(kind) {
  const ids = treeIds(kind);
  const scope = captureFileScope(kind);
  const listEpochField = kind === "conversation" ? "conversationListEpoch" : "templateListEpoch";
  const listEpoch = ++state[listEpochField];
  const directory = scope.directory;
  $(ids.path).textContent = `/${directory}`;
  $(ids.tree).textContent = "正在读取…";
  const data = await api("files.list", { ...scope.context, path: directory });
  if (!isCurrentFileScope(kind, scope) || state[listEpochField] !== listEpoch) return;
  const entries = data?.entries || data?.files || [];
  if (!Array.isArray(entries)) throw new Error("文件列表响应格式错误。");
  const tree = $(ids.tree);
  tree.replaceChildren();
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "tree-empty";
    empty.textContent = "此目录为空";
    tree.append(empty);
  }
  for (const entry of entries) {
    const path = entry.path || joinPath(directory, entry.name);
    const button = document.createElement("button");
    button.type = "button";
    button.className = `file-item${entry.is_dir ? " directory" : ""}${selectedEntry(kind)?.path === path ? " selected" : ""}`;
    button.textContent = `${entry.is_dir ? "▸" : "·"} ${entry.name}`;
    button.title = path;
    button.addEventListener("click", async () => {
      if (!isCurrentFileScope(kind, scope)) return;
      if (entry.is_dir) {
        if (isDirty(kind) && !await confirmAction("当前文件有未保存修改，继续将丢弃修改。继续吗？")) return;
        if (!isCurrentFileScope(kind, scope)) return;
        nextOpenEpoch(kind);
        setSelectedEntry(kind, { path, version: entry.version, is_dir: true });
        setDirectory(kind, path);
        setCurrentFile(kind, null);
        setDirty(kind, false);
        renderFileEmpty(kind);
        listFiles(kind).catch((error) => notice(error.message, "error"));
      } else openFile(kind, path).catch((error) => notice(error.message, "error"));
    });
    tree.append(button);
  }
}

function renderFileEmpty(kind) {
  nextOpenEpoch(kind);
  const ids = treeIds(kind);
  $(ids.filename).textContent = "未选择文件";
  $(ids.version).textContent = "";
  const editor = getEditor(kind);
  editor.setValue("");
  setDirty(kind, false);
}

async function openFile(kind, path, options = {}) {
  const scope = options.scope || captureFileScope(kind);
  const openEpoch = nextOpenEpoch(kind);
  if (!isCurrentFileScope(kind, scope)) return;
  if (isDirty(kind) && !await confirmAction("当前文件有未保存修改，切换会丢弃修改。继续吗？")) return;
  if (!isCurrentFileScope(kind, scope) || (kind === "conversation" ? state.conversationOpenEpoch : state.templateOpenEpoch) !== openEpoch) return;
  const data = await api("files.read", { ...scope.context, path });
  if (!isCurrentFileScope(kind, scope) || (kind === "conversation" ? state.conversationOpenEpoch : state.templateOpenEpoch) !== openEpoch) return;
  const raw = data?.content;
  if (typeof raw !== "string") throw new Error("此文件为二进制内容，请使用“下载”保存到本地查看。");
  setCurrentFile(kind, { path, version: data.version });
  setSelectedEntry(kind, { path, version: data.version, is_dir: false });
  setDirty(kind, false);
  const ids = treeIds(kind);
  $(ids.filename).textContent = path;
  $(ids.version).textContent = `版本 ${String(data.version || "").slice(0, 12)}`;
  const editor = getEditor(kind);
  editor.setValue(raw);
  monaco.editor.setModelLanguage(editor.getModel(), languageFor(path));
  setDirty(kind, false);
  await listFiles(kind);
}

async function saveFile(kind) {
  const scope = captureFileScope(kind);
  const file = currentFile(kind);
  if (!file) return notice("请先选择文件。", "error");
  if (!isDirty(kind)) return notice("文件没有未保存修改。", "info");
  const editor = activeEditor(kind);
  const model = editor.getModel();
  const savedModelVersion = model?.getVersionId();
  const content = editor.getValue();
  const result = await api("files.save", { ...scope.context, path: file.path, content, expected_version: file.version });
  if (!isCurrentFileScope(kind, scope) || currentFile(kind) !== file) return;
  if (result?.version) file.version = result.version;
  const entry = selectedEntry(kind);
  if (entry?.path === file.path && result?.version) setSelectedEntry(kind, { ...entry, version: result.version });
  if (model?.getVersionId() !== savedModelVersion) {
    setDirty(kind, true);
    notice("已保存当前版本；保存期间又有新修改，请再次保存。", "info");
    return;
  }
  setDirty(kind, false);
  $(treeIds(kind).version).textContent = `版本 ${String(file.version || "").slice(0, 12)}`;
  await listFiles(kind);
  if (!isCurrentFileScope(kind, scope)) return;
  notice(kind === "conversation"
    ? "文件已保存。指南和人格按现有热加载机制生效；代码是否热加载取决于应用。"
    : "模板文件已保存；只影响之后创建并复制该模板的会话。", "success");
}

async function createFile(kind, directory = false) {
  const scope = captureFileScope(kind);
  const input = await askText(directory ? "新目录名称" : "新文件名称");
  if (input === null) return;
  if (!isCurrentFileScope(kind, scope)) return;
  const path = joinPath(scope.directory, input.trim());
  if (!input.trim()) return;
  await api(directory ? "files.mkdir" : "files.save", directory
    ? { ...scope.context, path }
    : { ...scope.context, path, content: "", expected_version: null });
  if (!isCurrentFileScope(kind, scope)) return;
  await listFiles(kind);
  if (!isCurrentFileScope(kind, scope)) return;
  if (!directory) await openFile(kind, path, { scope });
  notice(directory ? "目录已创建。" : "文件已创建。", "success");
}

async function renameFile(kind) {
  const scope = captureFileScope(kind);
  const entry = selectedEntry(kind);
  if (!entry) return notice("请先选择文件或目录。", "error");
  const name = await askText("输入新的相对路径", entry.path);
  if (name === null || !name.trim()) return;
  if (!isCurrentFileScope(kind, scope) || selectedEntry(kind) !== entry) return;
  await api("files.rename", { ...scope.context, path: entry.path, new_path: name.trim(), expected_version: entry.version });
  if (!isCurrentFileScope(kind, scope)) return;
  setCurrentFile(kind, null);
  setSelectedEntry(kind, null);
  renderFileEmpty(kind);
  await listFiles(kind);
  notice("文件已重命名。", "success");
}

async function deleteFile(kind) {
  const scope = captureFileScope(kind);
  const entry = selectedEntry(kind);
  if (!entry) return notice("请先选择文件或目录。", "error");
  if (!await confirmAction(`删除 ${entry.path}？${entry.is_dir ? "目录必须为空才能删除。" : "此操作不能自动恢复。"}`)) return;
  if (!isCurrentFileScope(kind, scope) || selectedEntry(kind) !== entry) return;
  await api("files.delete", { ...scope.context, path: entry.path, expected_version: entry.version });
  if (!isCurrentFileScope(kind, scope)) return;
  setCurrentFile(kind, null);
  setSelectedEntry(kind, null);
  renderFileEmpty(kind);
  await listFiles(kind);
  notice("文件已删除。", "success");
}

async function uploadFile(kind, file) {
  if (!file) return;
  const scope = captureFileScope(kind);
  const path = joinPath(scope.directory, file.name);
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let start = 0; start < bytes.length; start += 0x8000) binary += String.fromCharCode(...bytes.subarray(start, start + 0x8000));
  const data_base64 = btoa(binary);
  await api("files.upload", { ...scope.context, path, data_base64 });
  if (!isCurrentFileScope(kind, scope)) return;
  await listFiles(kind);
  notice(`已上传 ${file.name}。`, "success");
}

async function downloadFile(kind) {
  const scope = captureFileScope(kind);
  const file = selectedEntry(kind);
  if (!file || file.is_dir) return notice("请先选择文件。", "error");
  const data = await api("files.download", { ...scope.context, path: file.path });
  if (!isCurrentFileScope(kind, scope)) return;
  const bytes = Uint8Array.from(atob(data.data_base64), (char) => char.charCodeAt(0));
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
  link.download = file.path.split("/").pop();
  link.click();
  URL.revokeObjectURL(link.href);
}

async function exportWorkspace() {
  const session = selectedSession();
  if (!session) return notice("请先选择会话。", "error");
  const button = $("export-workspace");
  button.disabled = true;
  notice("正在打包会话工作区，完成后将下载 ZIP。导出内容以服务器已保存的文件为准。", "info");
  try {
    await window.AstrBotPluginPage.download("export", { conversation_id: session.id });
    notice("会话工作区 ZIP 已生成并开始下载。", "success");
  } catch (error) {
    notice(`导出失败：${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
}

function bindFiles(prefix, kind) {
  const isSession = kind === "conversation";
  $(isSession ? "up-directory" : "template-up-directory").addEventListener("click", async () => {
    if (isDirty(kind) && !await confirmAction("当前文件有未保存修改，继续将丢弃修改。继续吗？")) return;
    setDirectory(kind, currentDirectory(kind).split("/").slice(0, -1).join("/"));
    setSelectedEntry(kind, null);
    renderFileEmpty(kind);
    listFiles(kind).catch((error) => notice(error.message, "error"));
  });
  $(isSession ? "new-file" : "template-new-file").addEventListener("click", () => createFile(kind).catch((error) => notice(error.message, "error")));
  $(isSession ? "new-directory" : "template-new-directory").addEventListener("click", () => createFile(kind, true).catch((error) => notice(error.message, "error")));
  const uploadInput = isSession ? $("upload-input") : $("template-upload-input");
  $(isSession ? "upload-file" : "template-upload-file").addEventListener("click", () => uploadInput.click());
  uploadInput.addEventListener("change", async (event) => {
    try { await uploadFile(kind, event.target.files?.[0]); } catch (error) { notice(error.message, "error"); }
    event.target.value = "";
  });
  $(isSession ? "save-file" : "template-save-file").addEventListener("click", () => saveFile(kind).catch((error) => notice(error.message, "error")));
  $(isSession ? "rename-file" : "template-rename-file").addEventListener("click", () => renameFile(kind).catch((error) => notice(error.message, "error")));
  $(isSession ? "delete-file" : "template-delete-file").addEventListener("click", () => deleteFile(kind).catch((error) => notice(error.message, "error")));
  $(isSession ? "download-file" : "template-download-file").addEventListener("click", () => downloadFile(kind).catch((error) => notice(error.message, "error")));
}

async function addTemplate() {
  const name = await askText("模板名称");
  if (!name?.trim()) return;
  const previous = new Set(state.templates.map((template) => template.id));
  await api("template.create", { name: name.trim(), expected_revision: state.revision });
  await loadSnapshot();
  state.selectedTemplate = state.templates.find((template) => !previous.has(template.id))?.id || null;
  renderTemplates();
  if (state.selectedTemplate) await listFiles("template");
  notice("模板元数据已创建；初始结构仍需在模板工作区编辑。", "success");
}

async function renameTemplate() {
  const template = selectedTemplate();
  if (!template) return;
  const name = await askText("模板名称", template.name);
  if (!name?.trim()) return;
  await api("template.rename", { template_id: template.id, name: name.trim(), expected_revision: state.revision });
  await loadSnapshot();
  notice("模板已重命名。", "success");
}

async function deleteTemplate() {
  const template = selectedTemplate();
  if (!template || !await confirmAction(`删除模板“${template.name}”？`)) return;
  await api("template.delete", { template_id: template.id, expected_revision: state.revision });
  state.selectedTemplate = null;
  await loadSnapshot();
  notice("模板已删除。", "success");
}

function showView(name) {
  document.querySelectorAll(".nav-button").forEach((node) => node.classList.toggle("active", node.dataset.view === name));
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("hidden", view.id !== `view-${name}`));
}

function bindNavigation() {
  document.querySelectorAll(".nav-button").forEach((button) => button.addEventListener("click", async () => {
    showView(button.dataset.view);
    if (button.dataset.view === "defaults") loadModels(state.selectedConversation).then(renderSettings).catch(() => {});
  }));
}

function bindActions() {
  $("refresh").addEventListener("click", () => loadSnapshot().then(() => notice("已刷新。", "success")).catch((error) => notice(error.message, "error")));
  $("save-defaults").addEventListener("click", () => saveDefaults().catch(() => {}));
  $("save-session").addEventListener("click", () => saveSession().catch(() => {}));
  document.querySelectorAll(".restore").forEach((button) => button.addEventListener("click", () => restoreField(button.dataset.field)));
  $("session-enabled").addEventListener("click", async () => {
    const session = selectedSession();
    if (!session) return;
    const settings = settingsCopy(session.settings);
    settings.enabled = !settings.enabled;
    await run("conversation.save", { conversation_id: session.id, settings, expected_revision: state.revision }, { busy: settings.enabled ? "启用会话 Agent…" : "停用会话 Agent…", success: settings.enabled ? "已启用。禁用期间消息将仅作为后续背景，不补执行旧请求。" : "已停用。服务端应取消当前任务并保留确定性插件和 Journal。" });
  });
  $("show-add-group").addEventListener("click", () => {
    const identity = state.conversations.find((row) => row.conversation_key?.startsWith("private:")) || state.conversations[0];
    if (identity) {
      $("add-group-form").elements.platform_id.value = identity.platform_id;
      $("add-group-form").elements.bot_id.value = identity.bot_id;
    }
    $("add-group-dialog").showModal();
  });
  $("add-group-form").addEventListener("submit", async (event) => {
    if (event.submitter?.value === "cancel") return;
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const identity = { platform_id: String(form.get("platform_id")).trim(), bot_id: String(form.get("bot_id")).trim(), group_id: String(form.get("group_id")).trim(), template_id: String(form.get("template_id")).trim() };
      const result = await run("group.add", { ...identity, expected_revision: state.revision }, { reload: false, busy: "同步平台准入、Agent 会话并初始化工作区…" });
      await loadSnapshot();
      const appeared = state.conversations.some((item) => item.platform_id === identity.platform_id && item.bot_id === identity.bot_id && String(item.conversation_key || "").includes(identity.group_id));
      const completed = Array.isArray(result?.completed) ? ` 已完成步骤：${result.completed.join("；")}。` : "";
      showResult(result, appeared
        ? `群会话已出现在控制台列表；接入 API 已返回成功。${completed}`
        : "接口已返回，但无法在会话列表中核对新群。TODO(待确认): 核对平台放行、Agent 准入与工作区初始化状态。", appeared ? "success" : "info");
      $("add-group-dialog").close();
      event.currentTarget.reset();
      $("add-group-form").elements.platform_id.value = "napcat";
    } catch (_) { /* the backend message describes actual partial progress */ }
  });
  $("add-template").addEventListener("click", () => addTemplate().catch((error) => notice(error.message, "error")));
  $("rename-template").addEventListener("click", () => renameTemplate().catch((error) => notice(error.message, "error")));
  $("delete-template").addEventListener("click", () => deleteTemplate().catch((error) => notice(error.message, "error")));
  $("restart").addEventListener("click", async () => {
    if (!await confirmAction("重启整个 chatbot-v2-agent.service？当前 Agent 任务会取消，工作区、会话和 Journal 保留。")) return;
    busy(true, "读取当前服务状态…");
    let previousPid;
    try {
      const before = await withTimeout(api("snapshot"), 8000, "读取服务状态超时。");
      previousPid = before?.health?.pid;
      if (previousPid === undefined || previousPid === null || previousPid === "") {
        notice("TODO(待确认): 服务端未提供当前进程 PID，无法验证重启结果；未提交重启。", "error");
        return;
      }
      let restartResult;
      try {
        busy(true, "提交一次服务重启请求…");
        restartResult = await withTimeout(api("restart"), 10000, "重启请求等待超时；将继续检查服务状态，不会重复提交。");
        previousPid = restartResult?.previous_pid ?? previousPid;
      } catch (error) {
        if (error.serverResponse) {
          notice(`重启请求被服务端拒绝：${error.message}`, "error");
          return;
        }
        notice(`重启请求连接中断，正在核对服务状态；不会重复提交。${error.message}`, "info");
      }

      const deadline = Date.now() + 900000;
      let lastHealth = null;
      let polls = 0;
      while (Date.now() < deadline) {
        polls += 1;
        busy(true, `等待服务重新启动并连接 QQ…（检查 ${polls}）`);
        await sleep(Math.min(2500, Math.max(0, deadline - Date.now())));
        try {
          const snapshot = await withTimeout(
            api("snapshot"), Math.min(6000, Math.max(1000, deadline - Date.now())),
            "服务状态检查超时。",
          );
          lastHealth = snapshot?.health || null;
          const newProcess = lastHealth?.pid !== undefined && String(lastHealth.pid) !== String(previousPid);
          const ready = lastHealth?.ready === true
            && lastHealth?.core_ready === true
            && lastHealth?.onebot_connected === true;
          if (newProcess && ready) {
            loadSnapshot().catch((error) => notice(`服务健康检查通过，但配置刷新失败：${error.message}`, "error"));
            notice(`Bot 服务已在新进程（PID ${lastHealth.pid}）恢复，核心与 QQ 连接均就绪。`, "success");
            return;
          }
        } catch (_) {
          // AstrBot can be unavailable while systemd replaces the process.
          // Keep checking until the bounded deadline, without resubmitting.
        }
      }
      const observed = lastHealth
        ? `当前 PID=${lastHealth.pid ?? "未知"}, core_ready=${lastHealth.core_ready ?? "未知"}, onebot_connected=${lastHealth.onebot_connected ?? "未知"}。`
        : "轮询期间没有取得服务健康状态。";
      notice(`未能在服务启停的 15 分钟等待窗口内确认新 PID、核心和 QQ 连接均就绪。${observed}请人工核对服务；没有重复提交重启。`, "error");
    } catch (error) {
      notice(`重启前状态检查失败，未提交重启：${error.message}`, "error");
    } finally {
      busy(false);
    }
  });
  $("compress").addEventListener("click", async () => {
    const session = selectedSession(); if (!session) return notice("请先选择会话。", "error");
    busy(true, "由当前模型压缩上下文…");
    try {
      const result = await api("compact", { conversation_id: session.id });
      if (result.compacted === true) notice("上下文压缩已完成，工作区保持不变。", "success");
      else if (result.compacted === false) notice("当前没有适合压缩的历史区间，未作更改。", "info");
      else showResult(result, "未取得明确压缩结果，请核对服务状态。", "info");
    }
    catch (error) { notice(`手动压缩未完成：${error.message}`, "error"); }
    finally { busy(false); }
  });
  $("new-context").addEventListener("click", async () => {
    const session = selectedSession(); if (!session) return notice("请先选择会话。", "error");
    if (!await confirmAction("新建对话上下文？工作区和 Journal 会保留。")) return;
    try { const result = await api("new", { conversation_id: session.id }); showResult(result, result.reset === true ? "已开始新的对话上下文，工作区和 Journal 已保留。" : "未取得明确重置结果，请核对服务状态。", result.reset === true ? "success" : "info"); }
    catch (error) { notice(`新建上下文未确认成功：${error.message}`, "error"); }
  });
}

bindNavigation();
bindActions();
$("export-workspace").addEventListener("click", exportWorkspace);
bindFiles("", "conversation");
bindFiles("template-", "template");
loadSnapshot().catch((error) => {
  $("connection").textContent = "连接失败";
  $("connection").classList.add("failed");
  notice(`无法载入控制台：${error.message}`, "error");
});
