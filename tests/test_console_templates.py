from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[1] / "astrbot-plugins/v2_console"
sys.path.insert(0, str(PLUGIN))
from files import WorkspaceFiles  # noqa: E402 - explicit isolated plugin path
from templates import TemplateService  # noqa: E402


@pytest.fixture
def template_service(tmp_path: Path):
    root = tmp_path / "templates"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root, TemplateService(root)


def _template(service: TemplateService, template_id: str) -> WorkspaceFiles:
    service.create_directory(template_id)
    return service.files(template_id)


def test_template_copy_initializes_structure_and_never_overwrites(template_service, tmp_path):
    _, service = template_service
    template_id = "c70a4d41-13ce-4c0e-84b4-2dcd2aa23d8a"
    with _template(service, template_id) as template:
        template.mkdir("skills")
        template.mkdir("skills/example")
        template.write("AGENTS.md", b"guide", None)
        template.write("existing.txt", b"keep", None)
        template.write("skills/example/SKILL.md", b"skill", None)

    target = tmp_path / "workspace"
    target.mkdir()
    (target / "existing.txt").write_bytes(b"keep")

    result = service.copy_to(template_id, target)
    assert result == {"copied_files": 2, "skipped_files": 1, "created_directories": 2}
    assert (target / "AGENTS.md").read_bytes() == b"guide"
    assert (target / "skills/example/SKILL.md").read_bytes() == b"skill"
    assert (target / "existing.txt").read_bytes() == b"keep"
    with service.files(template_id) as template:
        current = template.read("existing.txt")
        template.write("existing.txt", b"new template file", current["version"])
    with pytest.raises(FileExistsError, match="content differs"):
        service.copy_to(template_id, target)
    assert (target / "existing.txt").read_bytes() == b"keep"


def test_partial_initialization_can_be_retried_without_overwrite(template_service, tmp_path):
    _, service = template_service
    template_id = "965de41c-206e-4b98-94d1-ecc7527e6e4a"
    with _template(service, template_id) as template:
        template.write("a.txt", b"template-a", None)
        template.write("b.txt", b"template-b", None)
    target = tmp_path / "workspace"
    target.mkdir()
    (target / "b.txt").write_bytes(b"owner-content")

    with pytest.raises(FileExistsError, match="content differs"):
        service.copy_to(template_id, target)
    assert (target / "a.txt").read_bytes() == b"template-a"
    assert (target / "b.txt").read_bytes() == b"owner-content"

    with WorkspaceFiles(target) as files:
        current = files.read("b.txt")
        files.write("b.txt", b"template-b", current["version"])
    result = service.copy_to(template_id, target)
    assert result == {"copied_files": 0, "skipped_files": 2, "created_directories": 0}
    assert (target / "a.txt").read_bytes() == b"template-a"
    assert (target / "b.txt").read_bytes() == b"template-b"


def test_copy_rejects_links_and_special_files_without_following_them(template_service, tmp_path):
    _, service = template_service
    template_id = "3c2d5366-b5c9-4e46-85ef-363b5b018d1b"
    with _template(service, template_id) as template:
        template.write("a-safe.txt", b"safe", None)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "templates" / template_id / "escape").symlink_to(outside)
    target = tmp_path / "workspace"
    target.mkdir()

    with pytest.raises(PermissionError, match="link or special"):
        service.copy_to(template_id, target)
    assert outside.read_text() == "secret"
    assert (target / "a-safe.txt").read_bytes() == b"safe"
    assert not (target / "escape").exists()


def test_rejects_noncanonical_or_non_uuid_template_ids(template_service, tmp_path):
    _, service = template_service
    target = tmp_path / "workspace"
    target.mkdir()
    for value in ("", "../workspace", "A65a4d41-13ce-4c0e-84b4-2dcd2aa23d8a",
                  "c70a4d41-13ce-4c0e-84b4-2dcd2aa23d8a/child"):
        with pytest.raises(ValueError):
            service.copy_to(value, target)


def test_template_root_must_be_private_and_owned(tmp_path):
    root = tmp_path / "public"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    with pytest.raises(PermissionError):
        TemplateService(root)
