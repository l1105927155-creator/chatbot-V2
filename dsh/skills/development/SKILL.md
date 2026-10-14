---
name: development
description: Develop and validate a task in the isolated workspace, preserving existing project changes and following its instructions.
---
Load the filesystem skill before taking workspace actions. A scene or persona never grants tools; if the required execution tool is absent, provide analysis within the available capabilities.

Inspect the project and its local instructions before choosing an implementation. Identify the user's intended behavior, relevant files, and the strongest practical validation. Preserve unrelated edits and update the smallest coherent area that fulfills the request.

Use available trusted toolchains. The execution environment has no network and excludes host homes, credentials, and sockets; a dependency or repository must already be present in the workspace. Explain missing prerequisites rather than attempting another host path.

Run focused checks appropriate to the change. Expand verification when a new failure or unresolved risk requires it. Report the resulting behavior, what was actually tested, and any limitation. Commit, push, deploy, or perform destructive actions only within the user's authorized task.
