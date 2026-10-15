"""Real bounded-volume lifecycle and fail-closed identity checks, without QQ."""
from __future__ import annotations

import errno
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("v2_owner_volume", ROOT / "dsh/plugins/agent-profile/owner_volume.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.path.insert(0, str(ROOT / "dsh/plugins/agent-profile"))
runner_spec = importlib.util.spec_from_file_location("v2_owner_scope_runner", ROOT / "dsh/plugins/agent-profile/owner-scope-runner.py")
runner = importlib.util.module_from_spec(runner_spec)
runner_spec.loader.exec_module(runner)


@pytest.fixture
def volume(tmp_path):
    work, image = tmp_path / "workspace", tmp_path / "workspace.ext4"
    module.prepare(work, image, module.MIN_BYTES)
    try:
        yield work, image
    finally:
        if os.path.ismount(work):
            module.unmount(work)


def test_fixed_capacity_full_recovery_idempotence_and_remount(volume):
    work, image = volume
    assert module.prepare(work, image, module.MIN_BYTES)["bytes"] == module.MIN_BYTES
    (work / "sentinel").write_text("persistent")
    with pytest.raises(OSError) as failure:
        with (work / "fill").open("wb", buffering=0) as stream:
            for _ in range(40):
                stream.write(b"x" * (1024 ** 2))
    assert failure.value.errno == errno.ENOSPC
    assert image.stat().st_size == module.MIN_BYTES
    (work / "fill").unlink()
    (work / "recovered").write_text("after disk full")
    module.unmount(work)
    with pytest.raises(RuntimeError, match="dedicated workspace mount"):
        module.verified_workspace(work)
    module.prepare(work, image, module.MIN_BYTES)
    assert (work / "sentinel").read_text() == "persistent"
    assert (work / "recovered").read_text() == "after disk full"


def test_manifest_missing_symlink_wrong_capacity_and_foreign_backing_are_denied(volume):
    work, image = volume
    manifest = module.manifest_path(work)
    original = manifest.read_bytes()
    saved = manifest.with_suffix(".saved")
    manifest.rename(saved)
    try:
        with pytest.raises(FileNotFoundError):
            module.verified_workspace(work)
        manifest.symlink_to(saved)
        with pytest.raises(OSError):
            module.verified_workspace(work)
        manifest.unlink()
    finally:
        saved.rename(manifest)
    try:
        data = json.loads(original)
        data["bytes"] += 1024 ** 2
        manifest.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="capacity or reservation"):
            module.verified_workspace(work)
        other_image = image.with_name("foreign.ext4")
        with other_image.open("wb") as stream:
            os.posix_fallocate(stream.fileno(), 0, module.MIN_BYTES)
        other_image.chmod(0o600)
        data["bytes"], data["image"] = module.MIN_BYTES, str(other_image)
        manifest.write_text(json.dumps(data))
        with pytest.raises(RuntimeError, match="backing does not match"):
            module.verified_workspace(work)
        data["image"] = str(image)
        manifest.write_text(json.dumps(data));manifest.chmod(0o644)
        with pytest.raises(PermissionError, match="private owned"):
            module.verified_workspace(work)
    finally:
        manifest.chmod(0o600);manifest.write_bytes(original)


def test_existing_files_and_orphan_image_are_never_hidden_or_formatted(tmp_path):
    work, image = tmp_path / "workspace", tmp_path / "workspace.ext4"
    work.mkdir(mode=0o700)
    (work / "existing").write_text("preserve")
    with pytest.raises(ValueError, match="empty unmounted"):
        module.prepare(work, image, module.MIN_BYTES)
    assert (work / "existing").read_text() == "preserve" and not image.exists()
    (work / "existing").unlink()
    image.write_bytes(b"existing image")
    with pytest.raises(ValueError, match="without a matching manifest"):
        module.prepare(work, image, module.MIN_BYTES)
    assert image.read_bytes() == b"existing image"


def test_mount_initialization_failure_rolls_back_and_prepare_can_retry(tmp_path, monkeypatch):
    work, image = tmp_path / "workspace", tmp_path / "workspace.ext4"
    privileged = module._privileged
    def fail_chown(*args):
        if args[0] == "/usr/bin/chown":
            raise RuntimeError("injected chown failure")
        return privileged(*args)
    with monkeypatch.context() as patch:
        patch.setattr(module, "_privileged", fail_chown)
        with pytest.raises(RuntimeError, match="injected chown failure"):
            module.prepare(work, image, module.MIN_BYTES)
    assert not os.path.ismount(work)
    assert image.exists() and module.manifest_path(work).exists()
    module.prepare(work, image, module.MIN_BYTES)
    try:
        fd = module.verified_workspace(work)
        os.close(fd)
    finally:
        module.unmount(work)


def test_path_replacement_is_denied_before_model_command_executes(volume):
    work, image = volume
    (work / "sentinel").write_text("original-volume")
    argv, fd = runner.bounded_argv([
        "/usr/bin/bwrap", "--unshare-net", "--unshare-pid", "--die-with-parent",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
        "--bind", str(work), "/workspace", "--cap-drop", "ALL", "--",
        "/usr/bin/bash", "-c", "printf unsafe > /workspace/model-command-ran",
    ], work)
    try:
        # Simulate a privileged host changing the pathname between preflight
        # and bwrap. This intentionally detaches only this disposable volume.
        subprocess.run(["sudo", "-n", "umount", "-l", str(work)], check=True, capture_output=True)
        (work / "foreign").write_text("unbounded-host-directory")
        result = subprocess.run(argv, pass_fds=(fd,), capture_output=True, text=True, timeout=5)
        assert result.returncode != 0
        assert "containment unavailable" in result.stderr or "bwrap:" in result.stderr
        assert not (work / "model-command-ran").exists()
        with pytest.raises(RuntimeError, match="dedicated workspace mount"):
            module.verified_workspace(work)
    finally:
        os.close(fd)
        (work / "foreign").unlink(missing_ok=True)
        if not os.path.ismount(work):
            module.prepare(work, image, module.MIN_BYTES)
