#!/usr/bin/env python3
"""Install the locked local Bilibili/Twitter plugin snapshots into V2 AstrBot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "business-plugins.lock.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_source(plugin: dict[str, Any]) -> Path:
    source = Path(plugin["source_directory"]).resolve(strict=True)
    expected = plugin["files"]
    for relative, digest in expected.items():
        candidate = source / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise RuntimeError(f"locked plugin file is missing or unsafe: {plugin['plugin_id']}/{relative}")
        if sha256(candidate) != digest:
            raise RuntimeError(f"locked plugin file changed: {plugin['plugin_id']}/{relative}")
    return source


def verify_install(target: Path, plugin: dict[str, Any]) -> bool:
    if not target.exists():
        return False
    if target.is_symlink() or not target.is_dir():
        raise RuntimeError(f"plugin target is not a real directory: {target}")
    expected = plugin["files"]
    actual = {
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.is_file() or path.is_symlink()
        if "__pycache__" not in path.relative_to(target).parts
        and path.suffix != ".pyc"
    }
    if actual != set(expected):
        raise RuntimeError(f"existing plugin directory differs from lock; refusing to overwrite: {target}")
    for relative, digest in expected.items():
        path = target / relative
        if path.is_symlink() or sha256(path) != digest:
            raise RuntimeError(f"installed plugin file differs from lock: {target.name}/{relative}")
    return True


def install_plugin(source: Path, target: Path, plugin: dict[str, Any]) -> None:
    if verify_install(target, plugin):
        print(f"verified existing {plugin['plugin_id']} {plugin['metadata_version']}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.install-", dir=target.parent))
    try:
        for relative, digest in plugin["files"].items():
            source_file = source / relative
            if sha256(source_file) != digest:
                raise RuntimeError(f"source changed during copy: {plugin['plugin_id']}/{relative}")
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, destination)
            if sha256(destination) != digest:
                raise RuntimeError(f"copy verification failed: {plugin['plugin_id']}/{relative}")
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    verify_install(target, plugin)
    print(f"installed {plugin['plugin_id']} {plugin['metadata_version']}")


def requirements_install(python: Path, plugin_dirs: list[Path], report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    before = subprocess.run(
        [str(python), "-m", "pip", "freeze", "--all"],
        check=True, text=True, capture_output=True,
    ).stdout
    (report_dir / "pip-freeze-before.txt").write_text(before)
    requirements = [str(path / "requirements.txt") for path in plugin_dirs]
    dry_report = report_dir / "pip-resolve-report.txt"
    uv = shutil.which("uv") or "/home/abasun/.local/bin/uv"
    dry_run = subprocess.run(
        [uv, "pip", "install", "--dry-run", "--python", str(python),
         "-r", requirements[0], "-r", requirements[1]],
        check=True, text=True, capture_output=True,
    )
    dry_report.write_text(dry_run.stdout + dry_run.stderr)
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), "-r", requirements[0], "-r", requirements[1]],
        check=True,
    )
    after = subprocess.run(
        [str(python), "-m", "pip", "freeze", "--all"],
        check=True, text=True, capture_output=True,
    ).stdout
    (report_dir / "pip-freeze-after.txt").write_text(after)
    (report_dir / "pip-install-summary.json").write_text(json.dumps({
        "resolver_report": dry_report.name,
        "freeze_changed": before != after,
    }, indent=2) + "\n")
    if before != after:
        old = {line.split("==", 1)[0].lower(): line for line in before.splitlines() if "==" in line}
        new = {line.split("==", 1)[0].lower(): line for line in after.splitlines() if "==" in line}
        changes = [f"{old.get(name, '<new>')} -> {new.get(name, '<removed>')}"
                   for name in sorted(old.keys() | new.keys()) if old.get(name) != new.get(name)]
        print("dependency environment changes:")
        for change in changes:
            print(f"  {change}")
    print(f"dependency install report: {report_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--astrbot-root", type=Path, default=ROOT / ".runtime/astrbot")
    parser.add_argument("--python", type=Path, default=ROOT / ".runtime/astrbot/.venv/bin/python")
    parser.add_argument("--skip-dependencies", action="store_true")
    args = parser.parse_args()
    lock = json.loads(LOCK.read_text())
    plugins_dir = args.astrbot_root / "data/plugins"
    staged: list[tuple[Path, Path, dict[str, Any]]] = []
    for plugin in lock["plugins"].values():
        source = verify_source(plugin)
        target = plugins_dir / plugin["plugin_id"]
        staged.append((source, target, plugin))
    # Complete and validate every source before writing any installed plugin.
    for source, target, plugin in staged:
        install_plugin(source, target, plugin)
    if not args.skip_dependencies:
        requirements_install(
            args.python.absolute(),
            [target for _, target, _ in staged],
            ROOT / ".runtime/business-plugin-install",
        )
    print("No plugin-data directories, subscriptions, account credentials, or old configuration were copied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
