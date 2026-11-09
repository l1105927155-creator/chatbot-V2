---
name: doodle
description: 用户要画简笔画、火柴人、歪扭手绘图或搞怪涂鸦时，生成单笔、不规整的 SVG；用 Rough.js 对自由路径做轻度笔迹扰动。
---

# 笨拙简笔画（Rough.js）

当用户想让你画画时使用；不必让每张聊天图片都变成涂鸦。目标是**有意图、比例不准、线条不稳，但仍能辨认主题**，不是给规则圆形加双重描线。根据当前请求确定构图，不照搬样例场景。

## 画法

1. 先考虑主体、姿势与少量关键物件；宁可画得简单，也不要靠堆积几何图形增加精致感。
2. 用 SVG `path` 描述各个独立笔画，优先 `M/L/Q/C`；画头部、车轮等轮廓时直接构思不对称的路径，不要先生成规则圆再依赖扰动。故意允许部分接头错位、不闭合、比例夸张。
3. 默认一笔只画一次，**不要重描、补笔、素描排线**。粗糙度以适度为主，不为“更像人画”而一味加大噪声。
4. 把画布和笔画写成 JSON（参考 [example.json](example.json)），渲染器用 Rough.js 的 `generator.path`、`disableMultiStroke: true` 输出 SVG，默认黑色线条和浅色纸面。`seed` 用于稳定复现。
5. 执行前先检查 `/workspace/skills/doodle/render.cjs`、其 `vendor/rough.cjs.js` 以及 `node` 是否存在。群聊沙箱断网，不要在群里尝试 `npm install` 或在线 CDN。群内执行：`node /workspace/skills/doodle/render.cjs scene.json drawing.svg`；运行前确认文件名未占用。若文件缺失，说明需要宿主管理员按 [README.md](README.md) 做一次离线部署，不要谎称渲染成功。Owner 主机可以在已有 Rough.js 依赖的环境运行此脚本。
6. 核验输出文件和实际效果，必要时修改笔画路径；优先把 SVG 文件通过 `qq_file_send` 交付。若环境确实有 SVG→PNG 转换工具，可额外生成 PNG 再发送；不要假设 QQ 能内联显示 SVG，也不要把“生成了文件”当成“已经发送”。

## 数据约束

JSON 根字段支持 `width`、`height`（64–2048）、`seed`、`ink`、`paper`、`roughness`（0–3）、`bowing`（0–3）、`strokeWidth`、`paths`、`labels`。`paths` 非空，各项包含 `d`，可单独覆写 `roughness`、`bowing`、`strokeWidth`、`maxRandomnessOffset`、`ink`；`labels` 可选，含 `text`、`x`、`y`、`size`。背景和画笔颜色只接受六位十六进制；不要在路径中嵌入任意 XML、脚本或外部资源。

画面不规整主要由**你生成的路径**决定；Rough.js 只负责单笔的轻微抖动，不能凭空修复构图或模拟真实初学者的全部笔触。
