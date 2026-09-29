# chatbot-V2

A clean integration project for **AstrBot + DeepSeek Harness (DSH)**.

The repository intentionally does **not** copy the old `chatbot` codebase or vendor full upstream AstrBot/DSH sources. Upstream versions are pinned in [`upstream.lock.json`](upstream.lock.json); this repository will contain only the integration code, plugins, MCP boundary, DSH profile/policies, tests, and bootstrap/deployment assets owned by this project.

The architecture baseline and phased implementation plan are in [`PLAN.md`](PLAN.md).

Current status: **Phase 0 — architecture baseline. No production bot code has been migrated yet.**
