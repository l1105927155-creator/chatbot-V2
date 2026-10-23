"""Install bundled group-workspace persona presets once, without replacing user data."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path

try:
    from .templates import TemplateService
except ImportError:  # Isolated contract tests.
    from templates import TemplateService

_MARKER = ".workspace-persona-presets-v1"
_SLUG = re.compile(r"^[a-z][a-z0-9-]*$")


def _installed(marker: Path) -> bool:
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise PermissionError("preset marker must be private")
        if os.read(fd, 32) != b"installed-v1\n":
            raise ValueError("invalid preset marker")
        return True
    finally:
        os.close(fd)


def _mark_installed(marker: Path) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".preset-", dir=marker.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"installed-v1\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
        directory = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_workspace_persona_presets(store, templates_root: Path, repo_root: Path) -> int:
    """Seed the console catalog exactly once; existing templates and groups stay untouched.

    The marker prevents intentionally deleted presets from returning after a restart.
    If installation stops before that marker, stable UUIDs allow safe retry.
    """
    marker = store.root / _MARKER
    if _installed(marker):
        return 0

    source = repo_root / "dsh/templates/workspace-presets"
    data = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("presets"), list):
        raise ValueError("unsupported workspace preset manifest")
    guide = (repo_root / "dsh/templates/group/AGENTS.md").read_bytes()
    prepared = []
    ids = set()
    for item in data["presets"]:
        if not isinstance(item, dict) or set(item) != {"id", "slug", "name"}:
            raise ValueError("invalid preset item")
        template_id, slug, name = item["id"], item["slug"], item["name"]
        if (not isinstance(slug, str) or not _SLUG.fullmatch(slug)
                or template_id in ids):
            raise ValueError("invalid or duplicate preset slug or ID")
        ids.add(template_id)
        persona = (source / slug / "persona.md").read_bytes()
        if not persona.strip():
            raise ValueError("empty preset persona")
        prepared.append((template_id, name, persona))

    templates_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    service = TemplateService(templates_root)
    registered = {item["id"] for item in store.read()["templates"]}
    for template_id, _name, persona in prepared:
        if template_id in registered:
            # Preserve an edited template after an interrupted installation.
            continue
        directory = templates_root / template_id
        if not directory.exists():
            service.create_directory(template_id)
        with service.files(template_id) as files:
            existing = {entry["name"]: entry for entry in files.list("")}
            for filename, content in (("AGENTS.md", guide), ("persona.md", persona)):
                entry = existing.get(filename)
                if entry is None:
                    files.write(filename, content, None)
                elif entry["type"] != "file" or files.download(filename)[0] != content:
                    raise FileExistsError(f"preset bootstrap conflicts: {filename}")

    store.seed_templates([{"id": template_id, "name": name}
                          for template_id, name, _persona in prepared])
    _mark_installed(marker)
    return len(prepared) - len(ids & registered)
