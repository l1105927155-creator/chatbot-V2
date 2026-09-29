# chatbot-V2

A clean integration project for **AstrBot + DeepSeek Harness (DSH)**.

The repository intentionally does **not** copy the old `chatbot` codebase or vendor full upstream AstrBot/DSH sources. Upstream versions are pinned in [`upstream.lock.json`](upstream.lock.json); this repository will contain only the integration code, plugins, MCP boundary, DSH profile/policies, tests, and bootstrap/deployment assets owned by this project.

Before development, read:

- [`PLAN.md`](PLAN.md) — architecture baseline and phased roadmap.
- [`AGENTS.md`](AGENTS.md) — repository development constraints.
- [`docs/phases/phase-1.md`](docs/phases/phase-1.md) — current implementation scope.

Current status: **Phase 1 ready for implementation. No production bot code has been migrated yet.**
