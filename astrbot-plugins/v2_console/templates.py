"""Owner-managed workspace template directories and safe initialization."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

try:
    from .files import WorkspaceFiles
except ImportError:  # Direct-file contract tests.
    from files import WorkspaceFiles

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _template_id(value: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ValueError("template id must be a canonical UUID")
    return value


class TemplateService:
    """Manage files under a trusted private template root.

    Template metadata and owner authorization belong to the caller. Before
    exposing a template, callers must verify that its UUID exists in trusted
    console metadata. This class only enforces the identifier shape and path
    confinement.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).absolute()
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise PermissionError("template root must be a private directory owned by the runtime user")

    def create_directory(self, template_id: str) -> None:
        """Create a private directory for an ID already committed to metadata."""
        template_id = _template_id(template_id)
        with WorkspaceFiles(self.root) as templates:
            templates.mkdir(template_id)

    def files(self, template_id: str) -> WorkspaceFiles:
        """Return a confined file service; caller must validate metadata first."""
        template_id = _template_id(template_id)
        return WorkspaceFiles(self.root / template_id)

    def copy_to(self, template_id: str, workspace_root: str | os.PathLike[str], *,
                replace_pristine_guides: dict[str, bytes] | None = None) -> dict[str, Any]:
        """Initialize a workspace without replacing any existing file.

        Existing directories are reused. Existing regular files are skipped
        only when their bytes match the template; a differing object is a
        conflict. This makes a partially completed initialization retryable.
        A newly provisioned group may already contain framework-seeded guides;
        these may be replaced only if bytes still match the trusted stock guides.
        """
        template_id = _template_id(template_id)
        copied = 0
        skipped = 0
        created_directories = 0
        with self.files(template_id) as source, WorkspaceFiles(workspace_root) as target:
            stack: list[str] = [""]
            while stack:
                relative_dir = stack.pop()
                source_entries = source.list(relative_dir)
                target_entries = target.list(relative_dir)
                existing = {entry["name"]: entry for entry in target_entries}
                for entry in source_entries:
                    name = entry["name"]
                    relative = f"{relative_dir}/{name}" if relative_dir else name
                    kind = entry.get("type")
                    if kind == "other":
                        raise PermissionError(f"template contains a link or special file: {relative}")
                    current = existing.get(name)
                    if kind == "directory":
                        if current is None:
                            target.mkdir(relative)
                            created_directories += 1
                        elif current.get("type") != "directory":
                            raise FileExistsError(f"template destination conflicts: {relative}")
                        stack.append(relative)
                        continue
                    if kind != "file":
                        raise PermissionError(f"template contains an unsupported entry: {relative}")
                    source_data = source.download(relative)[0]
                    if current is None:
                        target.write(relative, source_data, None)
                        copied += 1
                        continue
                    if current.get("type") != "file":
                        raise FileExistsError(f"template destination conflicts: {relative}")
                    target_data, metadata = target.download(relative)
                    if target_data != source_data:
                        if (replace_pristine_guides is None
                                or relative not in replace_pristine_guides
                                or target_data != replace_pristine_guides[relative]):
                            raise FileExistsError(f"template destination content differs: {relative}")
                        target.write(relative, source_data, metadata["version"])
                        copied += 1
                    else:
                        skipped += 1
        return {"copied_files": copied, "skipped_files": skipped,
                "created_directories": created_directories}
