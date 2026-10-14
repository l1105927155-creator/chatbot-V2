import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "tests/native/phase5-authority.mjs"
NODE_MODULES_LINK = REPO_ROOT / "dsh/node_modules"
PINNED_NODE_MODULES = REPO_ROOT / ".runtime/dsh-runtime/node_modules"


def test_native_authority_and_scope_tools_use_pinned_tool_runtime():
    created_link = False
    if not NODE_MODULES_LINK.exists() and not NODE_MODULES_LINK.is_symlink():
        NODE_MODULES_LINK.symlink_to(os.path.relpath(PINNED_NODE_MODULES, NODE_MODULES_LINK.parent), target_is_directory=True)
        created_link = True
    try:
        result = subprocess.run(
            ["node", str(HELPER)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        if created_link:
            NODE_MODULES_LINK.unlink(missing_ok=True)
    assert result.returncode == 0, f"Native Cordis Authority test failed:\n{result.stdout}\n{result.stderr}"
    assert "native Cordis authority/configuration ToolRuntime contract passed" in result.stdout
