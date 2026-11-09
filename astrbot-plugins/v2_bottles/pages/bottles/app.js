// AstrBot injects the plugin-page bridge SDK right before </body>. An inline
// classic script would run before that SDK exists, so this page loads its logic
// as a deferred module script instead.
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "—").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const time = (value) => {
  if (!value) return "—";
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
};
const displayTime = (row) =>
  String(row.created_at_text || "").trim() || time(row.created_at);
const bytes = (value) => {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MiB`;
};
function say(text, kind = "muted") {
  $("notice").textContent = text;
  $("notice").className = kind;
}
async function call(action, fields = {}) {
  const bridge = window.AstrBotPluginPage;
  if (!bridge || typeof bridge.apiPost !== "function") {
    throw new Error("页面接口尚未就绪，请刷新页面或从 AstrBot 控制台重新打开。");
  }
  await bridge.ready();
  const result = await bridge.apiPost("api", { action, ...fields });
  if (result?.status === "error") throw new Error(result.message || "操作未完成");
  return result?.data ?? result;
}
function renderStatus(data) {
  $("status").innerHTML = [
    ["漂流瓶总数", data.count ?? 0],
    ["总条数上限", data.max_bottles ?? "—"],
    ["单条字数上限", data.max_chars ?? "—"],
    ["存储占用", `${bytes(data.disk_bytes)} / ${bytes(data.max_disk_bytes)}`],
    ["启用群", (data.enabled_groups || []).join("、") || "无（未启用）"],
  ].map(([label, value]) =>
    `<div class="card"><span class="muted">${esc(label)}</span><strong>${esc(value)}</strong></div>`
  ).join("");
}
function renderRows(data) {
  const rows = data.bottles || [];
  resetDeleteButton();
  if (!rows.length) {
    $("rows").innerHTML = '<tr><td colspan="7" class="muted">没有匹配的漂流瓶。</td></tr>';
    syncSelectAll();
    return;
  }
  $("rows").innerHTML = rows.map((row) => `<tr>
    <td><input type="checkbox" class="row-select" value="${esc(row.id)}" aria-label="选择漂流瓶"></td>
    <td>${esc(displayTime(row))}</td>
    <td>${esc(row.group_id)}</td>
    <td>${esc(row.sender_qq)}</td>
    <td>${esc(row.sender_name || "未知昵称")}</td>
    <td>${row.anonymous ? "是" : "否"}</td>
    <td class="content">${esc(row.content)}</td>
  </tr>`).join("");
  syncSelectAll();
}
function selectedIds() {
  return [...$("rows").querySelectorAll("input.row-select:checked")]
    .map((box) => Number(box.value))
    .filter((value) => Number.isInteger(value) && value > 0);
}
function syncSelectAll() {
  const boxes = [...$("rows").querySelectorAll("input.row-select")];
  const selectAll = $("select-all");
  selectAll.checked = boxes.length > 0 && boxes.every((box) => box.checked);
  selectAll.indeterminate = boxes.some((box) => box.checked) && !selectAll.checked;
}
let pendingDeleteIds = null;
let deleteTimer = null;
function resetDeleteButton() {
  pendingDeleteIds = null;
  clearTimeout(deleteTimer);
  deleteTimer = null;
  const button = $("delete-selected");
  button.textContent = "删除选中";
  button.classList.remove("danger");
}
function armDelete(ids) {
  pendingDeleteIds = ids;
  const button = $("delete-selected");
  button.textContent = `确认删除 ${ids.length} 条`;
  button.classList.add("danger");
  clearTimeout(deleteTimer);
  deleteTimer = setTimeout(resetDeleteButton, 5000);
}
async function deleteSelected() {
  const ids = selectedIds();
  if (!ids.length) {
    resetDeleteButton();
    say("先勾选要删除的漂流瓶。", "error");
    return;
  }
  if (pendingDeleteIds === null || pendingDeleteIds.join(",") !== ids.join(",")) {
    armDelete(ids);
    say(`再次点击“确认删除 ${ids.length} 条”才会执行。`, "muted");
    return;
  }
  resetDeleteButton();
  const data = await call("delete", { ids });
  await loadStatus();
  await loadRows();
  say(`已删除 ${data?.deleted ?? 0} 条漂流瓶。`, "ok");
}
async function loadStatus() {
  const status = await call("status");
  renderStatus(status);
  return status;
}
async function loadRows() {
  const data = await call("list", {
    group_id: $("group").value.trim(),
    qq: $("qq").value.trim(),
    limit: Number($("limit").value),
  });
  renderRows(data);
  return data;
}
async function refresh() {
  say("正在读取…");
  try {
    await loadStatus();
    await loadRows();
    say("已刷新。", "ok");
  } catch (error) {
    say(error.message || "读取失败", "error");
  }
}
function renderSettings(data) {
  const container = $("settings");
  container.innerHTML = "";
  for (const field of data.fields || []) {
    const label = document.createElement("label");
    label.className = "field";
    const title = document.createElement("span");
    title.textContent = field.label || field.key;
    const input = document.createElement("input");
    input.type = "number";
    input.dataset.key = field.key;
    input.min = field.min ?? "";
    input.max = field.max ?? "";
    input.step = field.step ?? "1";
    input.value = data.settings?.[field.key] ?? "";
    const hint = document.createElement("small");
    hint.className = "muted";
    hint.textContent = field.hint || "";
    label.append(title, input, hint);
    container.append(label);
  }
}
function collectSettings() {
  const values = {};
  for (const input of $("settings").querySelectorAll("input[data-key]")) {
    values[input.dataset.key] = Number(input.value);
  }
  return values;
}
async function loadSettings() {
  const data = await call("settings");
  renderSettings(data);
  return data;
}
async function saveSettings() {
  await call("settings.save", { settings: collectSettings() });
  await loadSettings();
  await loadStatus();
  say("运行设置已保存，立即生效。", "ok");
}
function renderTexts(data) {
  const container = $("texts");
  container.innerHTML = "";
  for (const field of data.fields || []) {
    const label = document.createElement("label");
    label.className = "field";
    const title = document.createElement("span");
    title.textContent = field.label || field.key;
    const textarea = document.createElement("textarea");
    textarea.dataset.key = field.key;
    textarea.rows = Number(field.rows) || 2;
    textarea.value = data.texts?.[field.key] ?? "";
    const hint = document.createElement("small");
    hint.className = "muted";
    hint.textContent = field.hint || "";
    label.append(title, textarea, hint);
    container.append(label);
  }
}
function collectTexts() {
  const values = {};
  for (const textarea of $("texts").querySelectorAll("textarea[data-key]")) {
    values[textarea.dataset.key] = textarea.value;
  }
  return values;
}
async function loadTexts() {
  const data = await call("texts");
  renderTexts(data);
  return data;
}
async function saveTexts() {
  await call("texts.save", { texts: collectTexts() });
  await loadTexts();
  say("提示文本已保存，立即生效。", "ok");
}
$("reload").addEventListener("click", refresh);
$("load").addEventListener("click", async () => {
  try {
    await loadRows();
    say("查询完成。", "ok");
  } catch (error) {
    say(error.message || "查询失败", "error");
  }
});
$("save-texts").addEventListener("click", async () => {
  try {
    await saveTexts();
  } catch (error) {
    say(error.message || "保存失败", "error");
  }
});
$("save-settings").addEventListener("click", async () => {
  try {
    await saveSettings();
  } catch (error) {
    say(error.message || "保存失败", "error");
  }
});
$("select-all").addEventListener("change", () => {
  const checked = $("select-all").checked;
  for (const box of $("rows").querySelectorAll("input.row-select")) {
    box.checked = checked;
  }
  syncSelectAll();
});
$("rows").addEventListener("change", (event) => {
  if (event.target instanceof HTMLInputElement
      && event.target.classList.contains("row-select")) {
    syncSelectAll();
  }
});
$("delete-selected").addEventListener("click", async () => {
  try {
    await deleteSelected();
  } catch (error) {
    resetDeleteButton();
    say(error.message || "删除失败", "error");
  }
});
refresh();
loadSettings().catch((error) => say(error.message || "运行设置读取失败", "error"));
loadTexts().catch((error) => say(error.message || "提示文本读取失败", "error"));
