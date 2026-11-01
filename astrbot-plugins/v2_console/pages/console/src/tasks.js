const escapeHtml = (value) => String(value ?? "—").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

const statusLabels = {
  running: "运行中", completed: "已完成", skipped: "已跳过", failed: "失败",
  cancelled: "已取消", interrupted: "中断", below_threshold: "消息不足，跳过",
  unavailable: "记忆服务不可用", capture_degraded: "消息采集异常", binding_changed: "群状态已变化",
};

function localDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function scheduleSummary(schedule = {}) {
  if (schedule.type === "cron") return `${schedule.expression} · ${schedule.timezone}`;
  if (schedule.type === "interval") return `每 ${schedule.seconds} 秒`;
  if (schedule.type === "once") return `一次 · ${schedule.run_at || "时间未设置"}`;
  return "时间未设置";
}

function dateLabel(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export function createTasksView({ root, api, notice, confirmAction }) {
  const state = { snapshot: null, selected: null, dirty: false, busy: false, loaded: false };
  root.innerHTML = `
    <div class="view-heading"><div><div class="eyebrow">工作区调度</div><h1>自动任务</h1><div class="muted">群可以单独启用、停用或恢复继承。日常记忆整理由群聊记忆页的后台增量开关管理。</div></div><button id="tasks-refresh" class="button secondary">刷新任务</button></div>
    <section class="card task-policy-card"><div class="card-heading"><div><h2>统一默认状态</h2><p>影响继承默认的现有群及新增群的工作区任务。</p></div><label class="task-switch"><input id="tasks-default-enabled" type="checkbox"><span>继承群默认启用自动任务</span></label></div></section>
    <div class="task-layout"><section class="card task-groups"><div class="card-heading"><div><h2>群列表</h2><p>只显示已准入群。</p></div></div><div id="task-group-list" class="task-group-list"></div></section>
      <section class="card task-detail"><div id="task-detail-content" class="empty-state">选择一个群查看自动任务。</div></section></div>
    <dialog id="task-editor-dialog" class="task-dialog"><form id="task-editor-form"><div class="dialog-heading"><h2 id="task-editor-title">新建任务</h2><button type="button" class="icon-button" data-close-dialog aria-label="关闭">×</button></div>
      <label class="field"><span>任务名称</span><input name="name" required maxlength="160"></label>
      <label class="field"><span>任务类型</span><select name="kind"><option value="agent">助理任务</option><option value="memory">自动整理记忆</option></select></label>
      <label class="field task-prompt-field"><span>任务提示词</span><textarea name="prompt" rows="7" placeholder="描述触发后助理要完成的工作"></textarea></label>
      <label class="field task-memory-field"><span>最少新增群友消息数</span><input name="min_new_messages" type="number" min="1" step="1" value="99"></label>
      <label class="field"><span>计划类型</span><select name="schedule_type"><option value="cron">Cron 定时</option><option value="interval">固定间隔</option><option value="once">单次时间</option></select></label>
      <label class="field task-cron-field"><span>Cron 表达式（分 时 日 月 周）</span><input name="expression" placeholder="0 9 * * *"></label>
      <label class="field task-cron-field"><span>时区</span><input name="timezone" value="Asia/Shanghai"></label>
      <label class="field task-interval-field"><span>间隔秒数</span><input name="seconds" type="number" min="1" step="1" value="3600"></label>
      <label class="field task-interval-field"><span>开始时间</span><input name="start_at" type="datetime-local"></label>
      <label class="field task-once-field"><span>执行时间</span><input name="run_at" type="datetime-local"></label>
      <div class="dialog-actions"><button type="button" class="button secondary" data-close-dialog>取消</button><button class="button primary" type="submit">保存任务</button></div>
    </form></dialog>`;

  const $ = (selector) => root.querySelector(selector);
  const policy = () => state.snapshot?.policy;
  const selectedGroup = () => state.snapshot?.groups?.find((item) => JSON.stringify(item.identity) === JSON.stringify(state.selected)) || null;
  const isPrivate = (identity) => !Array.isArray(identity) || identity.length !== 3 || !String(identity[2]).startsWith("group:");
  const groupLabel = (group) => {
    const label = group.name || group.title || group.conversation_key;
    if (label) return label;
    return `群 ${String(group.identity?.[2] || "").replace(/^group:/, "") || "未命名"}`;
  };
  const groupRevision = (group) => group.revision;

  async function refresh({ quiet = false } = {}) {
    const previous = state.selected;
    const snapshot = await api("tasks.snapshot");
    if (!snapshot || !snapshot.policy || !Array.isArray(snapshot.policy.overrides) || !Array.isArray(snapshot.groups)) {
      throw new Error("自动任务状态响应格式错误。");
    }
    snapshot.groups = snapshot.groups.filter((item) => !isPrivate(item.identity));
    state.snapshot = snapshot;
    if (!snapshot.groups.some((item) => JSON.stringify(item.identity) === JSON.stringify(previous))) {
      state.selected = snapshot.groups[0]?.identity || null;
    }
    render();
    state.loaded = true;
    if (!quiet) notice("自动任务状态已刷新。", "success");
  }

  function render() {
    const snap = state.snapshot;
    if (!snap) return;
    $("#tasks-default-enabled").checked = snap.policy.default_enabled === true;
    $("#tasks-default-enabled").disabled = state.busy;
    const list = $("#task-group-list");
    list.replaceChildren();
    if (!snap.groups.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "当前没有已准入群，自动任务管理不可用。";
      list.append(empty);
    }
    for (const group of snap.groups) {
      const override = snap.policy.overrides.find((entry) => JSON.stringify(entry.identity) === JSON.stringify(group.identity));
      const button = document.createElement("button");
      button.type = "button";
      button.className = `task-group-item${JSON.stringify(group.identity) === JSON.stringify(state.selected) ? " selected" : ""}`;
      const title = document.createElement("strong");
      title.textContent = groupLabel(group);
      const stateLine = document.createElement("small");
      stateLine.textContent = override ? (override.enabled ? "单独启用" : "单独停用") : `继承默认 · ${snap.policy.default_enabled ? "启用" : "停用"}`;
      button.append(title, stateLine);
      button.addEventListener("click", () => { state.selected = group.identity; render(); });
      list.append(button);
    }
    renderDetail();
  }

  function renderDetail() {
    const host = $("#task-detail-content");
    const group = selectedGroup();
    if (!group) {
      host.className = "empty-state";
      host.textContent = "当前没有可管理的群。";
      return;
    }
    const override = policy().overrides.find((entry) => JSON.stringify(entry.identity) === JSON.stringify(group.identity));
    const enabled = group.enabled === true;
    host.className = "task-detail-inner";
    host.replaceChildren();
    const heading = document.createElement("div");
    heading.className = "view-heading task-detail-heading";
    const copy = document.createElement("div");
    const eyebrow = document.createElement("div"); eyebrow.className = "eyebrow"; eyebrow.textContent = "群任务";
    const title = document.createElement("h1"); title.textContent = groupLabel(group);
    const status = document.createElement("div"); status.className = "muted";
    status.textContent = !group.managed
      ? `${override ? "群级覆盖" : "继承统一默认"} · 自动任务管理尚未接管此群`
      : `${override ? "群级覆盖" : "继承统一默认"} · 自动任务${enabled ? "已启用" : "已停用"}`;
    copy.append(eyebrow, title, status);
    const actions = document.createElement("div"); actions.className = "heading-actions";
    const toggle = document.createElement("button"); toggle.type = "button"; toggle.className = "button secondary";
    toggle.textContent = override ? (override.enabled ? "停用此群" : "启用此群") : (enabled ? "单独停用" : "单独启用");
    toggle.disabled = state.busy;
    toggle.addEventListener("click", () => saveGroupPolicy(group, override ? !override.enabled : !enabled).catch((error) => { if (!error.taskConflict) notice(error.message || String(error), "error"); }));
    const inherit = document.createElement("button"); inherit.type = "button"; inherit.className = "button outline"; inherit.textContent = "恢复继承";
    inherit.disabled = !override || state.busy;
    inherit.addEventListener("click", () => saveGroupPolicy(group, null).catch((error) => { if (!error.taskConflict) notice(error.message || String(error), "error"); }));
    const create = document.createElement("button"); create.type = "button"; create.className = "button primary"; create.textContent = "新建任务";
    create.disabled = state.busy;
    create.addEventListener("click", () => openEditor());
    actions.append(toggle, inherit, create);
    heading.append(copy, actions);
    host.append(heading);
    if (!group.managed) {
      const note = document.createElement("p"); note.className = "task-paused-note"; note.textContent = "此群尚未迁入自动任务管理。单独启用或停用此群后，将由任务清单接管；后台增量记忆在群聊记忆页独立管理。"; host.append(note);
    } else if (!enabled) {
      const paused = document.createElement("p"); paused.className = "task-paused-note"; paused.textContent = "此群的工作区自动任务已暂停。恢复启用后，已启用任务会重新交给调度器；后台增量记忆在群聊记忆页独立管理。"; host.append(paused);
    }
    if (!Array.isArray(group.tasks) || !group.tasks.length) {
      const empty = document.createElement("div"); empty.className = "empty-state task-empty"; empty.textContent = "这个群还没有自动任务。"; host.append(empty); return;
    }
    const table = document.createElement("div"); table.className = "task-cards";
    for (const row of group.tasks) table.append(renderTask(group, row));
    host.append(table);
  }

  function renderTask(group, row) {
    const task = row.task || {};
    const enabled = group.enabled === true;
    const card = document.createElement("article"); card.className = "task-card";
    const top = document.createElement("div"); top.className = "task-card-top";
    const details = document.createElement("div");
    const name = document.createElement("h3"); name.textContent = task.name || "未命名任务";
    const meta = document.createElement("div"); meta.className = "muted";
    meta.textContent = `${task.kind === "memory" ? "自动整理记忆" : "助理任务"} · ${scheduleSummary(task.schedule)}`;
    details.append(name, meta);
    const badge = document.createElement("span"); badge.className = `task-badge${row.scheduled ? " active" : ""}`;
    badge.textContent = group.managed === false ? "任务管理未接管" : !enabledLabel(group) ? "群已停用" : row.scheduled ? "已排程" : task.enabled ? "未排程" : "已停用";
    top.append(details, badge); card.append(top);
    if (task.kind === "memory") {
      const threshold = document.createElement("p"); threshold.className = "task-prompt muted";
      threshold.textContent = `新消息门槛：${task.parameters?.min_new_messages ?? "—"} 条（不计 Bot 自己的消息）`;
      card.append(threshold);
    } else if (task.prompt) {
      const prompt = document.createElement("p"); prompt.className = "task-prompt"; prompt.textContent = task.prompt; card.append(prompt);
    }
    const next = document.createElement("p"); next.className = "task-next muted";
    next.textContent = `下次执行：${row.scheduled ? dateLabel(row.next_run) : "暂无"}`; card.append(next);
    const recentRuns = Array.isArray(row.recent_runs) ? row.recent_runs.slice(0, 10) : [];
    const latest = recentRuns[0];
    if (latest) {
      const runLine = document.createElement("p"); runLine.className = `task-run-summary${latest.status === "failed" ? " failed" : ""}`;
      runLine.textContent = `最近：${statusLabels[latest.status] || latest.status || "未知"} · ${dateLabel(latest.finished_at || latest.started_at)}${latest.error_code ? ` · ${latest.error_code}` : ""}`;
      card.append(runLine);
    }
    const buttons = document.createElement("div"); buttons.className = "task-actions";
    const toggle = button(task.enabled ? "停用" : "启用", "secondary", () => manage(group, task.enabled ? "disable" : "enable", task.id));
    const edit = button("编辑", "outline", () => openEditor(task, group));
    const run = button("立即执行", "secondary", () => runNow(group, task));
    const remove = button("删除", "danger", async () => {
      if (await confirmAction(`删除任务“${task.name}”？此操作不能自动恢复。`)) await manage(group, "delete", task.id);
    });
    for (const control of [toggle, edit, remove]) control.disabled = state.busy;
    run.disabled = state.busy || !enabled;
    buttons.append(toggle, edit, run, remove); card.append(buttons);
    if (recentRuns.length) {
      const history = document.createElement("details"); history.className = "task-history";
      const summary = document.createElement("summary"); summary.textContent = `最近执行记录（${recentRuns.length}）`; history.append(summary);
      const list = document.createElement("div"); list.className = "task-history-list";
      for (const item of recentRuns) {
        const line = document.createElement("div"); line.className = "task-history-row";
        const info = document.createElement("span");
        info.textContent = `${statusLabels[item.status] || item.status || "未知"} · ${dateLabel(item.finished_at || item.started_at)}${item.error_code ? ` · ${item.error_code}` : ""}`;
        line.append(info);
        if (item.status === "running" && item.run_id) {
          const cancel = button("取消", "outline", () => cancelRun(group, item));
          cancel.disabled = state.busy;
          line.append(cancel);
        }
        list.append(line);
      }
      history.append(list); card.append(history);
    }
    return card;
  }

  function enabledLabel(group) { return group.enabled === true; }

  function button(text, style, handler) {
    const node = document.createElement("button"); node.type = "button"; node.className = `button ${style}`; node.textContent = text;
    node.addEventListener("click", () => Promise.resolve(handler()).catch((error) => { if (!error.taskConflict) notice(error.message || String(error), "error"); }));
    return node;
  }

  function conflict(error) {
    const text = error?.message || String(error);
    return /conflict|revision|stale|冲突|版本|已被修改/i.test(text)
      || ["revision_conflict", "conflict", "stale_revision"].includes(error?.error_type || error?.response?.error_type)
      || (error?.serverResponse === true && /修改|刷新|revision/i.test(text));
  }

  async function mutate(action, payload, fallback) {
    state.busy = true; render();
    try {
      const result = await api(action, payload);
      await refresh({ quiet: true });
      notice(result?.message || fallback, "success");
      return result;
    } catch (error) {
      if (conflict(error)) {
        error.taskConflict = true;
        await refresh({ quiet: true }).catch(() => {});
        notice("任务或群设置已被其他操作修改。已刷新当前数据，请核对后重试。", "error");
      } else notice(error.message || String(error), "error");
      throw error;
    } finally { state.busy = false; render(); }
  }

  async function saveGroupPolicy(group, enabled) {
    const current = policy();
    const payload = { expected_revision: current.revision, identity: group.identity, enabled };
    await mutate("tasks.policy", payload, enabled === null ? "已恢复继承统一默认。" : enabled ? "已启用此群自动任务管理。" : "已停用此群工作区自动任务。");
  }

  async function manage(group, operation, taskId, definition, boundRevision = groupRevision(group)) {
    const payload = { identity: group.identity, operation, expected_revision: boundRevision };
    if (taskId) payload.task_id = taskId;
    if (definition) payload.definition = definition;
    await mutate("tasks.manage", payload, ({ create: "任务已创建。", update: "任务已更新。", enable: "任务已启用。", disable: "任务已停用。", delete: "任务已删除。" })[operation]);
  }

  async function runNow(group, task) {
    const requestId = crypto.randomUUID();
    await mutate("tasks.run", { identity: group.identity, task_id: task.id, expected_revision: groupRevision(group), request_id: requestId }, "已提交立即执行请求。");
  }

  async function cancelRun(group, latest) {
    if (!latest?.run_id) throw new Error("执行记录没有可取消的运行标识，请刷新后检查状态。");
    await mutate("tasks.cancel", { identity: group.identity, run_id: latest.run_id, request_id: crypto.randomUUID() }, "已提交取消请求。");
  }

  function openEditor(task = null, group = selectedGroup()) {
    const dialog = $("#task-editor-dialog");
    const form = $("#task-editor-form");
    form.reset();
    form.dataset.taskId = task?.id || "";
    form.dataset.identity = JSON.stringify(group?.identity || []);
    form.dataset.expectedRevision = String(groupRevision(group));
    form.dataset.enabled = task ? String(task.enabled === true) : "true";
    $("#task-editor-title").textContent = task ? "编辑任务" : "新建任务";
    form.elements.name.value = task?.name || "";
    form.elements.kind.value = task?.kind || "agent";
    form.elements.kind.disabled = Boolean(task);
    form.elements.prompt.value = task?.prompt || "";
    form.elements.min_new_messages.value = task?.parameters?.min_new_messages ?? 99;
    const schedule = task?.schedule || { type: "cron", expression: "0 9 * * *", timezone: "Asia/Shanghai" };
    form.elements.schedule_type.value = schedule.type;
    form.elements.expression.value = schedule.expression || "";
    form.elements.timezone.value = schedule.timezone || "Asia/Shanghai";
    form.elements.seconds.value = schedule.seconds || 3600;
    form.elements.start_at.value = localDateTime(schedule.start_at);
    form.elements.run_at.value = localDateTime(schedule.run_at);
    updateFormFields();
    dialog.showModal();
  }

  function updateFormFields() {
    const form = $("#task-editor-form");
    const kind = form.elements.kind.value;
    const schedule = form.elements.schedule_type.value;
    root.querySelectorAll(".task-prompt-field").forEach((node) => node.classList.toggle("hidden", kind !== "agent"));
    root.querySelectorAll(".task-memory-field").forEach((node) => node.classList.toggle("hidden", kind !== "memory"));
    root.querySelectorAll(".task-cron-field").forEach((node) => node.classList.toggle("hidden", schedule !== "cron"));
    root.querySelectorAll(".task-interval-field").forEach((node) => node.classList.toggle("hidden", schedule !== "interval"));
    root.querySelectorAll(".task-once-field").forEach((node) => node.classList.toggle("hidden", schedule !== "once"));
    form.elements.prompt.required = kind === "agent";
    form.elements.min_new_messages.required = kind === "memory";
    form.elements.expression.required = schedule === "cron";
    form.elements.timezone.required = schedule === "cron";
    form.elements.seconds.required = schedule === "interval";
    form.elements.run_at.required = schedule === "once";
  }

  function localIso(value, label) {
    if (!value) throw new Error(`请填写${label}。`);
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) throw new Error(`${label}格式无效。`);
    return date.toISOString();
  }

  $("#tasks-refresh").addEventListener("click", () => refresh().catch((error) => notice(error.message || String(error), "error")));
  $("#tasks-default-enabled").addEventListener("change", async (event) => {
    const current = policy();
    try { await mutate("tasks.policy", { expected_revision: current.revision, default_enabled: event.currentTarget.checked }, event.currentTarget.checked ? "统一默认状态已启用。" : "统一默认状态已停用；已接管且继承默认的群将暂停工作区自动任务。"); }
    catch (_) { /* surfaced by mutate */ }
  });
  root.querySelectorAll("[data-close-dialog]").forEach((node) => node.addEventListener("click", () => $("#task-editor-dialog").close()));
  $("#task-editor-form").elements.kind.addEventListener("change", updateFormFields);
  $("#task-editor-form").elements.schedule_type.addEventListener("change", updateFormFields);
  $("#task-editor-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      const kind = form.elements.kind.value;
      const scheduleType = form.elements.schedule_type.value;
      const schedule = scheduleType === "cron"
        ? { type: "cron", expression: form.elements.expression.value.trim(), timezone: form.elements.timezone.value.trim() }
        : scheduleType === "interval"
          ? { type: "interval", seconds: Number(form.elements.seconds.value), start_at: localIso(form.elements.start_at.value || localDateTime(new Date().toISOString()), "开始时间") }
          : { type: "once", run_at: localIso(form.elements.run_at.value, "执行时间") };
      const definition = {
        name: form.elements.name.value.trim(), enabled: form.dataset.enabled === "true", kind, schedule,
        prompt: kind === "agent" ? form.elements.prompt.value : null,
        parameters: kind === "memory" ? { min_new_messages: Number(form.elements.min_new_messages.value) } : {},
      };
      const identity = JSON.parse(form.dataset.identity || "[]");
      const group = state.snapshot?.groups?.find((item) => JSON.stringify(item.identity) === JSON.stringify(identity));
      if (!group) throw new Error("编辑期间所选群已不可用，请关闭窗口并刷新。");
      await manage(group, form.dataset.taskId ? "update" : "create", form.dataset.taskId || undefined, definition, Number(form.dataset.expectedRevision));
      $("#task-editor-dialog").close();
    } catch (error) { if (!error.taskConflict) notice(error.message || String(error), "error"); }
  });

  let refreshTimer;
  const editorDialog = $("#task-editor-dialog");
  editorDialog.addEventListener("close", () => { state.dirty = false; });
  $("#task-editor-form").addEventListener("input", () => { state.dirty = true; });
  return {
    async show() {
      if (!state.loaded) await refresh({ quiet: true });
      if (refreshTimer) window.clearInterval(refreshTimer);
      refreshTimer = window.setInterval(() => {
        if (state.dirty || $("#task-editor-dialog").open || document.hidden) return;
        refresh({ quiet: true }).catch((error) => notice(`自动任务状态刷新失败：${error.message || error}`, "error"));
      }, 30000);
    },
    dispose() { if (refreshTimer) window.clearInterval(refreshTimer); },
  };
}
