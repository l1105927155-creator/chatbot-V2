---
name: filesystem
description: Inspect and modify files inside the isolated owner workspace using the available confined Bash tool.
---
Check which tools are actually available. This skill grants no file or shell access. If Bash is absent, explain the available conversation capabilities instead of claiming a file operation.

Use the default workspace, which appears as /workspace inside Bash. Use relative paths or /workspace paths in commands; do not copy the host's private runtime paths into shell commands. A Bash workdir may name an existing relative workspace subdirectory.

Inspect the current tree and relevant files before editing. Read project instructions in the workspace, preserve existing changes, and use literal paths and appropriate shell quoting. Prefer a focused change and inspect its result.

Commands have no inherited credentials or network access. Background execution and sandbox escalation are unavailable. Inspect the actual exit/timeout/denial result before claiming success. If required files or executables are absent, report the missing input or runtime dependency.
