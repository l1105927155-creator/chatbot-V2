# doodle Skill — 部署说明

仓库中的 `dsh/skills/doodle/SKILL.md` 是可被群会话自动发现的共享 Skill，但群沙箱仅挂载本群 `/workspace`，**不会直接获得共享 Skill 旁边的 JS 脚本**，也不能联网装包。使用前需把可执行资料离线安装到目标群工作区。此步骤不修改沙箱权限、MCP 或消息链路。

## 准备 Rough.js（宿主执行一次）

```bash
# 在部署 Linux 宿主上执行；不要在群沙箱执行 npm
mkdir -p .runtime/doodle-deps
npm install --prefix .runtime/doodle-deps --no-save --save-exact roughjs@4.6.6
```

Rough.js 的 CommonJS bundle 在 `.runtime/doodle-deps/node_modules/roughjs/bundled/rough.cjs.js`，本 Skill 按 4.6.6 编写。将它和 Skill 安装到指定**已挂载**群卷：

```bash
python3 scripts/install-doodle-skill.py \
  --workspace /path/to/group-storage/<group-key>/workspace \
  --bundle .runtime/doodle-deps/node_modules/roughjs/bundled/rough.cjs.js
```

安装器拒绝覆盖已有 `skills/doodle`（包括群成员自行编辑的版本），不会自动修改任何现有群。群卷未挂载或未通过 V2 的群卷校验时拒绝操作；在受控维护时间执行。安装后下一次模型请求即可发现本群版本；要复测发现与工具使用，需在真实群里请求一张画。这个步骤只复制本地受信任文件，执行时群内仍断网。

## 使用和验收

```bash
node /workspace/skills/doodle/render.cjs /workspace/example.json /workspace/result.svg
# 若存在 rsvg-convert，可按实际可用选项把 SVG 转为 PNG：
rsvg-convert -o /workspace/result.png /workspace/result.svg
```

`render.cjs` 不覆盖已经存在的输出。渲染器使用 Rough.js 原生 `generator.path` 和 `toPaths`，在任何路径上显式关闭双笔描边。样例的轮子和脸部本身就不对称，以免靠单纯抖动模拟画技。依赖和真实 QQ 文件/图片发送仍需部署后的环境测试；SVG 可以作为 QQ 文件交付，PNG 转换取决于宿主提供的可用命令。网页或 QQ 端是否能直接预览 SVG 不作承诺。
