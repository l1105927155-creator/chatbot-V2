import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const dist = resolve(root, "astrbot-plugins/v2_console/pages/console/dist");
const { chromium } = await import(new URL("../.runtime/sandbox-browser/node_modules/playwright-core/index.mjs", import.meta.url).href);
const mime = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml", ".woff2": "font/woff2" };
const identity = ["napcat", "bot-1", "group:12345"];
const scheduledTask = {
  id: "f87b3b55-ef0b-485a-8c19-7ba3a4b1c6c0", name: "暂停任务", enabled: false, kind: "agent",
  schedule: { type: "cron", expression: "0 9 * * *", timezone: "Asia/Shanghai" }, prompt: "写日报", parameters: {},
};
const activeRuns = [
  { run_id: "run-new", status: "running", started_at: "2026-10-28T02:00:00Z", finished_at: null },
  { run_id: "run-old", status: "running", started_at: "2026-10-28T01:00:00Z", finished_at: null },
  ...Array.from({ length: 9 }, (_, index) => ({ run_id: `run-${index}`, status: "failed", started_at: "2026-10-27T01:00:00Z", finished_at: "2026-10-27T01:01:00Z", error_code: "provider_error" })),
];
const taskRows = [{ task: scheduledTask, registered: true, scheduled: false, next_run: null, creator_id: "owner", recent_runs: activeRuns }];
const groups = [
  { identity, title: "验收群", enabled: false, managed: true, revision: 7, tasks: taskRows },
  { identity: ["napcat", "bot-1", "group:67890"], title: "未接管群", enabled: false, managed: false, revision: 3, tasks: [] },
  { identity: ["napcat", "bot-1", "private:owner"], title: "私聊不应出现", enabled: true, managed: true, revision: 1, tasks: [] },
];
const policy = { revision: 1, default_enabled: false, overrides: [] };
const calls = [];
let holdSnapshot = false;
let pendingSnapshot = null;
let conflictNextManage = false;
let generatedId = 0;
const clone = (value) => JSON.parse(JSON.stringify(value));
const getGroup = (wanted) => groups.find((item) => JSON.stringify(item.identity) === JSON.stringify(wanted));
const recalc = () => {
  for (const group of groups) {
    if (group.identity[2].startsWith("private:")) continue;
    const override = policy.overrides.find((item) => JSON.stringify(item.identity) === JSON.stringify(group.identity));
    group.enabled = override ? override.enabled : policy.default_enabled;
  }
};
const serverSnapshot = () => ({
  policy: clone(policy),
  groups: clone(groups.filter((item) => item.identity[2].startsWith("group:"))),
});
const browserErrors = [];
const server = createServer(async (request, response) => {
  try {
    const urlPath = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
    if (urlPath === "/favicon.ico") { response.writeHead(204); response.end(); return; }
    const target = resolve(dist, `.${urlPath}`);
    if (target !== dist && !target.startsWith(`${dist}${sep}`)) throw new Error("path escaped dist");
    const info = await stat(target);
    const path = info.isDirectory() ? resolve(target, "index.html") : target;
    response.writeHead(200, { "content-type": mime[extname(path)] || "application/octet-stream", "cache-control": "no-store" });
    response.end(await readFile(path));
  } catch {
    response.writeHead(404);
    response.end("not found");
  }
});
await new Promise((resolveListen) => server.listen(0, "127.0.0.1", resolveListen));
const address = server.address();
const browser = await chromium.launch({ executablePath: "/snap/bin/chromium", headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") process.stderr.write(`browser console: ${message.text()}\n`); });
  await page.addInitScript(() => {
    window.__taskCalls = [];
    window.__pendingTaskReads = [];
    window.__holdTaskSnapshot = false;
    window.__conflictNextManage = false;
    window.AstrBotPluginPage = {
      apiPost(_endpoint, payload) {
        window.__taskCalls.push(structuredClone(payload));
        if (payload.action === "snapshot") return Promise.resolve({ revision: 1, defaults: { enabled: false, model: null, editors: [] }, conversations: [], templates: [], todos: [], health: { ready: true, onebot_connected: true } });
        if (payload.action === "models") return Promise.resolve({ available: false, choices: [] });
        if (payload.action === "tasks.snapshot") {
          if (window.__holdTaskSnapshot) {
            window.__holdTaskSnapshot = false;
            return new Promise((resolve) => window.__pendingTaskReads.push(resolve));
          }
          return Promise.resolve(window.__taskState());
        }
        if (payload.action === "tasks.policy") {
          const state = window.__taskState();
          if (payload.default_enabled !== undefined) {
            state.policy.default_enabled = payload.default_enabled;
            for (const item of state.groups) {
              const override = state.policy.overrides.find((entry) => JSON.stringify(entry.identity) === JSON.stringify(item.identity));
              if (!override) item.enabled = payload.default_enabled;
            }
          }
          if (payload.identity) {
            const key = JSON.stringify(payload.identity);
            const index = state.policy.overrides.findIndex((item) => JSON.stringify(item.identity) === key);
            if (payload.enabled === null) { if (index >= 0) state.policy.overrides.splice(index, 1); }
            else if (index >= 0) state.policy.overrides[index].enabled = payload.enabled;
            else state.policy.overrides.push({ identity: payload.identity, enabled: payload.enabled });
          }
          state.policy.revision += 1;
          const group = state.groups.find((item) => JSON.stringify(item.identity) === JSON.stringify(payload.identity));
          if (group) { group.revision += 1; group.enabled = payload.enabled ?? state.policy.default_enabled; }
          window.__setTaskState(state);
          return Promise.resolve({ ok: true });
        }
        if (payload.action === "tasks.manage") {
          if (window.__conflictNextManage) {
            window.__conflictNextManage = false;
            return Promise.resolve({ status: "error", error_type: "revision_conflict", message: "任务已被修改，请刷新后重试。" });
          }
          const state = window.__taskState();
          const group = state.groups.find((item) => JSON.stringify(item.identity) === JSON.stringify(payload.identity));
          if (payload.operation === "create") {
            const task = { id: `created-${++window.__createdId}`, ...payload.definition };
            group.tasks.push({ task, registered: true, scheduled: task.enabled && group.enabled, next_run: null, creator_id: "owner", recent_runs: [] });
          } else {
            const row = group.tasks.find((item) => item.task.id === payload.task_id);
            if (payload.operation === "delete") group.tasks = group.tasks.filter((item) => item !== row);
            else if (row && payload.operation === "update") row.task = { ...row.task, ...payload.definition };
            else if (row && ["enable", "disable"].includes(payload.operation)) row.task.enabled = payload.operation === "enable";
          }
          group.revision += 1;
          window.__setTaskState(state);
          return Promise.resolve({ ok: true });
        }
        if (payload.action === "tasks.run") {
          window.__taskCalls[window.__taskCalls.length - 1].request_id = payload.request_id;
          return Promise.resolve({ run_id: "run-created", status: "running" });
        }
        if (payload.action === "tasks.cancel") return Promise.resolve({ status: "cancelled", run_id: payload.run_id });
        return Promise.resolve({});
      },
    };
  });
  await page.addInitScript((initial) => {
    let taskState = initial;
    window.__createdId = 0;
    window.__taskState = () => structuredClone(taskState);
    window.__setTaskState = (state) => { taskState = structuredClone(state); };
    window.__resolveTaskSnapshot = (state) => {
      const resolve = window.__pendingTaskReads.shift();
      if (!resolve) throw new Error("no pending task snapshot");
      resolve(structuredClone(state));
    };
  }, serverSnapshot());

  await page.goto(`http://127.0.0.1:${address.port}/`);
  await page.locator('[data-view="tasks"]').click();
  try { await page.locator("#task-group-list .task-group-item").filter({ hasText: "验收群" }).waitFor({ timeout: 4000 }); }
  catch (error) { process.stderr.write(`task page debug: ${await page.locator("#app").innerText()}\npage errors: ${browserErrors.join("; ")}\n`); throw error; }
  if (await page.locator("#task-group-list").innerText().then((text) => text.includes("私聊不应出现"))) throw new Error("private conversations appeared in the task group list");
  await page.locator("#task-group-list .task-group-item").filter({ hasText: "未接管群" }).click();
  if (!(await page.locator("#task-detail-content").innerText()).includes("后台增量记忆在群聊记忆页独立管理")) throw new Error("independent background memory configuration was not explained");
  await page.locator("#task-group-list .task-group-item").filter({ hasText: "验收群" }).click();
  await page.evaluate(() => { window.__holdTaskSnapshot = true; });
  await page.locator("#tasks-refresh").click();
  await page.waitForFunction(() => window.__pendingTaskReads.length === 1);
  await page.locator(".task-card").filter({ hasText: "暂停任务" }).getByRole("button", { name: "编辑" }).click();
  const dialog = page.locator("#task-editor-dialog");
  await dialog.waitFor({ state: "visible" });
  if (!(await dialog.locator('[name="kind"]').isDisabled())) throw new Error("editing a task allowed changing kind");
  await dialog.locator('[name="name"]').fill("编辑后的停用任务");
  const late = await page.evaluate(() => window.__taskState());
  late.groups[0].revision = 19;
  await page.evaluate((state) => window.__resolveTaskSnapshot(state), late);
  if (await dialog.locator('[name="name"]').inputValue() !== "编辑后的停用任务") throw new Error("late refresh overwrote edits in the open task editor");
  await dialog.locator('button[type="submit"]').click();
  await page.waitForFunction(() => window.__taskCalls.some((call) => call.action === "tasks.manage"));
  let update = await page.evaluate(() => window.__taskCalls.find((call) => call.action === "tasks.manage"));
  if (update.expected_revision !== 7 || update.definition.enabled !== false || update.operation !== "update") throw new Error("editing silently enabled a disabled task or used a refreshed revision");
  await dialog.waitFor({ state: "hidden" });
  process.stdout.write("PASS: delayed refresh preserves editor text and its original CAS revision; disabled edit stays disabled\n");

  await page.evaluate(() => { window.__conflictNextManage = true; });
  await page.locator(".task-card").filter({ hasText: "编辑后的停用任务" }).getByRole("button", { name: "编辑" }).click();
  await dialog.locator('[name="name"]').fill("并发冲突编辑");
  await dialog.locator('button[type="submit"]').click();
  try { await page.locator("#notice").filter({ hasText: "已被其他操作修改" }).waitFor({ timeout: 5000 }); }
  catch (error) { process.stderr.write(`conflict debug: ${await page.locator("#notice").innerText()}\n${JSON.stringify(await page.evaluate(() => window.__taskCalls.filter(call => call.action === "tasks.manage")))}\n`); throw error; }
  await dialog.waitFor({ state: "visible" });
  await dialog.locator('[name="name"]').fill("通过冲突后的更新");
  await dialog.locator('button[type="submit"]').click();
  await dialog.waitFor({ state: "hidden" });

  await page.locator("#tasks-default-enabled").check();
  await page.waitForFunction(() => window.__taskCalls.some((call) => call.action === "tasks.policy" && call.default_enabled === true));
  await page.locator(".task-card").filter({ hasText: "通过冲突后的更新" }).getByRole("button", { name: "立即执行" }).click();
  await page.waitForFunction(() => window.__taskCalls.some((call) => call.action === "tasks.run"));
  const runCall = await page.evaluate(() => window.__taskCalls.find((call) => call.action === "tasks.run"));
  if (!runCall.request_id || runCall.request_id.length < 30) throw new Error("run request did not include an idempotency UUID");
  const history = page.locator(".task-card").filter({ hasText: "通过冲突后的更新" }).locator("details.task-history");
  await history.locator("summary").click();
  await history.locator(".task-history-row").filter({ hasText: "运行中" }).nth(1).getByRole("button", { name: "取消" }).click();
  await page.waitForFunction(() => window.__taskCalls.some((call) => call.action === "tasks.cancel" && call.run_id === "run-old"));
  process.stdout.write("PASS: conflict prompts refresh, immediate run carries UUID, and any running history row can be cancelled\n");

  await page.locator(".task-card").filter({ hasText: "通过冲突后的更新" }).getByRole("button", { name: "删除" }).click();
  await page.locator("#console-action-dialog").getByRole("button", { name: "确定" }).click();
  await page.waitForFunction(() => window.__taskCalls.some((call) => call.action === "tasks.manage" && call.operation === "delete"));
  await page.getByRole("button", { name: "新建任务" }).click();
  await dialog.locator('[name="name"]').fill("新任务");
  await dialog.locator('[name="prompt"]').fill("生成并发送日报");
  await dialog.locator('button[type="submit"]').click();
  await page.waitForFunction(() => window.__taskCalls.some((call) => call.action === "tasks.manage" && call.operation === "create"));
  if (browserErrors.length) throw new Error(`browser errors: ${browserErrors.join("; ")}`);
  process.stdout.write("PASS: delete and create are available through the console task controls\n");
} finally {
  await browser.close();
  await new Promise((resolveClose) => server.close(resolveClose));
}
