"""Safety regressions for scripts/bootstrap.sh.

These tests use disposable repositories and stop before any network fetch. They
must never point bootstrap at this repository's real runtime checkout.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
LOCK_FILE = REPO_ROOT / "upstream.lock.json"


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


def init_repository(path: Path) -> None:
    path.mkdir()
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "bootstrap-test@example.invalid"),
        ("git", "config", "user.name", "Bootstrap Test"),
    ):
        result = run(*command, cwd=path)
        if result.returncode:
            raise RuntimeError(result.stderr)
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    result = run("git", "add", "tracked.txt", cwd=path)
    if result.returncode:
        raise RuntimeError(result.stderr)
    result = run("git", "commit", "-qm", "baseline", cwd=path)
    if result.returncode:
        raise RuntimeError(result.stderr)


class BootstrapIsolationTests(unittest.TestCase):
    def bootstrap(self, runtime_dir: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
        return run(
            str(BOOTSTRAP),
            "--runtime-dir",
            str(runtime_dir),
            "--lock-file",
            str(LOCK_FILE),
            cwd=cwd,
        )

    def test_rejects_a_plain_subdirectory_of_another_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "parent"
            init_repository(parent)
            runtime_dir = parent / "runtime"
            runtime_dir.mkdir()
            before = run("git", "rev-parse", "HEAD", cwd=parent).stdout

            result = self.bootstrap(runtime_dir, parent)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be the Git worktree root", result.stderr)
            self.assertEqual(before, run("git", "rev-parse", "HEAD", cwd=parent).stdout)
            self.assertFalse((parent / ".git" / "refs" / "v2-bootstrap").exists())

    def test_rejects_a_symbolic_link_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sandbox = Path(temporary)
            target = sandbox / "target"
            target.mkdir()
            runtime_link = sandbox / "runtime-link"
            runtime_link.symlink_to(target, target_is_directory=True)

            result = self.bootstrap(runtime_link, sandbox)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not be a symbolic link", result.stderr)

    def test_rejects_tracked_changes_before_fetch_or_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime_dir = Path(temporary) / "runtime"
            init_repository(runtime_dir)
            (runtime_dir / "tracked.txt").write_text("modified\n", encoding="utf-8")

            result = self.bootstrap(runtime_dir, runtime_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tracked or staged changes", result.stderr)
            self.assertEqual("modified\n", (runtime_dir / "tracked.txt").read_text(encoding="utf-8"))

    def test_does_not_adopt_an_existing_repository_without_an_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime_dir = Path(temporary) / "runtime"
            init_repository(runtime_dir)
            before = run("git", "rev-parse", "HEAD", cwd=runtime_dir).stdout

            result = self.bootstrap(runtime_dir, runtime_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("has no origin; refusing to adopt it", result.stderr)
            self.assertEqual(before, run("git", "rev-parse", "HEAD", cwd=runtime_dir).stdout)
            self.assertEqual("", run("git", "remote", cwd=runtime_dir).stdout)

    def test_rejects_staged_changes_before_fetch_or_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime_dir = Path(temporary) / "runtime"
            init_repository(runtime_dir)
            (runtime_dir / "tracked.txt").write_text("staged change\n", encoding="utf-8")
            staged = run("git", "add", "tracked.txt", cwd=runtime_dir)
            self.assertEqual(0, staged.returncode, staged.stderr)

            result = self.bootstrap(runtime_dir, runtime_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tracked or staged changes", result.stderr)
            self.assertEqual("M  tracked.txt\n", run("git", "status", "--short", cwd=runtime_dir).stdout)

    def test_allows_untracked_runtime_data_to_reach_origin_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime_dir = Path(temporary) / "runtime"
            init_repository(runtime_dir)
            (runtime_dir / ".venv").mkdir()
            (runtime_dir / ".venv" / "marker").write_text("runtime data\n", encoding="utf-8")
            run("git", "remote", "add", "origin", "https://example.invalid/not-astrbot.git", cwd=runtime_dir)

            result = self.bootstrap(runtime_dir, runtime_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Runtime origin differs from the lock", result.stderr)
            self.assertNotIn("tracked or staged changes", result.stderr)


if __name__ == "__main__":
    unittest.main()
