"""One-time bundled workspace persona presets; migrate stock v1 entries without deleting user data."""
from __future__ import annotations

import hashlib
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

_MARKER = ".workspace-persona-presets-v2"
_LEGACY_MARKER = ".workspace-persona-presets-v1"
_SLUG = re.compile(r"^[a-z][a-z0-9-]*$")

# Exact Git blob IDs of the bundled v1 files. Compare bytes rather than
# assuming that a known template UUID still contains the original content.
_LEGACY_V1 = {
    "877a623b-6139-4367-b4c6-c58ad3ebd613":
        ("冷面吐槽 · 观察系", "a1f9a36416e12fe9db902546ca3c4b11874b9da1"),
    "0fdd9f91-e10a-4466-bdbd-3b178e19f08d":
        ("电波脑洞 · 幻想系", "eb903bd7f47cfbc3aad9d48b0d8d56ba565b0de1"),
    "65eeb7fd-2eb7-4546-b6cc-0152ac38eb25":
        ("温柔腹黑 · 细腻系", "db33c1f190d64e5397a1133ae94f353e02649571"),
    "a47f91de-aa16-43b6-9b82-bac59c55d0ee":
        ("宅系考据党 · 同好系", "5c3a35786f350dc4be81e762a9f1e5dfd53e62bb"),
}
_LEGACY_RENAMES = {
    "5b387989-9f5e-4373-b39a-03e2eac99b1f":
        ("嘴硬心软 · 傲娇系", "傲娇-嘴硬心软", "2fe9e7a9a3663fef3fc9d6a563f0b909cb8028c6"),
    "6e2ac9e8-a8ec-430a-9b75-7d9d9120c253":
        ("元气乐子人 · 气氛系", "元气-活泼热情", "3ae30e62c3e9bf578155f02022316ee6cd79df0c"),
}
_LEGACY_GUIDE_BLOB = "9e843480f46a1ccc951dd774b5230cf7386b0a7e"


def _git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def _installed(marker: Path, version: int) -> bool:
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise PermissionError("preset marker must be private")
        if os.read(fd, 32) != f"installed-v{version}\n".encode():
            raise ValueError("invalid preset marker")
        return True
    finally:
        os.close(fd)


def _mark_installed(marker: Path, version: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".preset-", dir=marker.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(f"installed-v{version}\n".encode())
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


def _stock_v1(service: TemplateService, template_id: str, persona_blob: str, *,
              for_retirement: bool) -> bool:
    """Only consider a v1 template untouched when the stored bytes prove it."""
    try:
        with service.files(template_id) as files:
            entries = files.list("")
            if for_retirement:
                if {row["name"] for row in entries} != {"persona.md", "AGENTS.md"}:
                    return False
                if any(row["type"] != "file" for row in entries):
                    return False
                if _git_blob(files.download("AGENTS.md")[0]) != _LEGACY_GUIDE_BLOB:
                    return False
            return _git_blob(files.download("persona.md")[0]) == persona_blob
    except (OSError, ValueError):
        # Missing, edited or inaccessible data must not be removed by migration.
        return False


def install_workspace_persona_presets(store, templates_root: Path, repo_root: Path) -> int:
    """Install the six current personas and conservatively retire untouched v1 stock.

    This does not update any group workspace, and a v2 marker prevents
    intentionally deleted templates from coming back on later restarts.
    """
    if _installed(store.root / _MARKER, 2):
        return 0
    # Validate the predecessor's marker before touching its catalog.
    _installed(store.root / _LEGACY_MARKER, 1)

    source = repo_root / "dsh/templates/workspace-presets"
    data = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if data.get("version") != 2 or not isinstance(data.get("presets"), list):
        raise ValueError("unsupported workspace preset manifest")
    guide = (repo_root / "dsh/templates/group/AGENTS.md").read_bytes()
    prepared = []
    ids = set()
    for item in data["presets"]:
        if not isinstance(item, dict) or set(item) != {"id", "slug", "name"}:
            raise ValueError("invalid preset item")
        template_id, slug, name = item["id"], item["slug"], item["name"]
        if (not isinstance(slug, str) or not _SLUG.fullmatch(slug)
                or template_id in ids or not isinstance(name, str)):
            raise ValueError("invalid or duplicate preset slug or ID")
        ids.add(template_id)
        persona = (source / slug / "persona.md").read_bytes()
        if not persona.strip():
            raise ValueError("empty preset persona")
        prepared.append((template_id, name, persona))

    templates_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    service = TemplateService(templates_root)
    snapshot = store.read()
    registered = {item["id"]: item["name"] for item in snapshot["templates"]}

    # Prepare content first; an incomplete import can retry with the same IDs.
    for template_id, _name, persona in prepared:
        if template_id in registered:
            continue  # Do not touch manually edited, renamed or referenced templates.
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

    referenced = {row.get("template_id") for row in snapshot["sessions"].values()}
    retire = []
    for template_id, (old_name, blob) in _LEGACY_V1.items():
        if (registered.get(template_id) == old_name and template_id not in referenced
                and _stock_v1(service, template_id, blob, for_retirement=True)):
            retire.append({"id": template_id, "name": old_name})
    rename = []
    for template_id, (old_name, new_name, blob) in _LEGACY_RENAMES.items():
        if (registered.get(template_id) == old_name
                and _stock_v1(service, template_id, blob, for_retirement=False)):
            rename.append({"id": template_id, "old_name": old_name, "new_name": new_name})

    store.seed_templates(
        [{"id": template_id, "name": name} for template_id, name, _ in prepared],
        retire=retire, rename=rename, expected_revision=snapshot["revision"],
    )
    _mark_installed(store.root / _MARKER, 2)
    return sum(template_id not in registered for template_id, _name, _ in prepared)
