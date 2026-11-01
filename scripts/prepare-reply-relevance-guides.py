#!/usr/bin/env python3
"""Build review-only baseline/candidate guides without touching a workspace."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


TITLE = "## 你的回复习惯"
ANCHOR = "- **多项推荐或资料：**"


class PreparationError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reject_symlink_path(path: Path, *, include_leaf: bool = True) -> None:
    """Reject symlinks in every existing lexical component (including dangling links)."""
    absolute = Path(os.path.abspath(path))
    parts = absolute.parts if include_leaf else absolute.parts[:-1]
    cursor = Path(absolute.anchor)
    for part in parts[1:]:
        cursor = cursor / part
        try:
            if stat.S_ISLNK(cursor.lstat().st_mode):
                raise PreparationError(f"symlink path component is not allowed: {cursor}")
        except FileNotFoundError:
            continue


def read_regular(path: Path, label: str) -> bytes:
    reject_symlink_path(path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise PreparationError(f"{label} file does not exist: {path}") from exc
    if not stat.S_ISREG(mode):
        raise PreparationError(f"{label} must be a regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PreparationError(f"cannot read {label}: {exc}") from exc


def decode(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PreparationError(f"{label} is not valid UTF-8") from exc


def unique_line(text: str, needle: str, label: str) -> int:
    positions = [i for i in range(len(text)) if text.startswith(needle, i)]
    if len(positions) != 1:
        raise PreparationError(f"expected exactly one {label}; found {len(positions)}")
    return positions[0]


def extract_prefix(text: str, label: str) -> tuple[int, int, str]:
    title_at = unique_line(text, TITLE, f"{label} title {TITLE!r}")
    if title_at and text[title_at - 1] != "\n":
        raise PreparationError(f"{label} title is not a standalone line")
    title_end = title_at + len(TITLE)
    if title_end < len(text) and text[title_end] not in "\r\n":
        raise PreparationError(f"{label} title is not a standalone line")

    anchor_at = unique_line(text, ANCHOR, f"{label} anchor {ANCHOR!r}")
    if anchor_at <= title_end or (anchor_at and text[anchor_at - 1] != "\n"):
        raise PreparationError(f"{label} anchor must follow the title on its own line")
    intervening = text[title_end:anchor_at]
    if re.search(r"(?m)^ {0,3}#{1,2}(?:[ \t]+|$)", intervening):
        raise PreparationError(
            f"{label} has a level-1 or level-2 heading before the anchor; refusing cross-section replacement"
        )
    section_start = title_end
    # Preserve the title line ending; replacement begins immediately after it.
    while section_start < len(text) and text[section_start] in "\r\n":
        section_start += 1
    prefix = text[section_start:anchor_at]
    if not prefix.strip():
        raise PreparationError(f"{label} has no core reply-principle content before the anchor")
    return section_start, anchor_at, prefix


def build(agents: Path, template: Path, output_dir: Path, expected_source_sha: str | None) -> dict:
    source_bytes = read_regular(agents, "source AGENTS")
    template_bytes = read_regular(template, "template AGENTS")
    source_sha, template_sha = sha256(source_bytes), sha256(template_bytes)
    if expected_source_sha and source_sha.lower() != expected_source_sha.lower():
        raise PreparationError(
            f"source SHA-256 drift: expected {expected_source_sha.lower()}, got {source_sha}"
        )
    source, template_text = decode(source_bytes, "source AGENTS"), decode(template_bytes, "template AGENTS")
    source_start, source_end, _ = extract_prefix(source, "source AGENTS")
    _, _, template_prefix = extract_prefix(template_text, "template AGENTS")
    candidate = source[:source_start] + template_prefix + source[source_end:]
    baseline_bytes, candidate_bytes = source.encode("utf-8"), candidate.encode("utf-8")

    reject_symlink_path(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise PreparationError(f"output directory already exists; refusing overwrite: {output_dir}")
    try:
        output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    except OSError as exc:
        raise PreparationError(f"cannot create new output directory: {exc}") from exc

    before = source[source_start:source_end]
    after = template_prefix
    before_bytes = before.encode("utf-8")
    after_bytes = after.encode("utf-8")
    diff = "".join(difflib.unified_diff(
        source.splitlines(keepends=True), candidate.splitlines(keepends=True),
        fromfile="baseline.md", tofile="candidate.md",
    ))
    manifest = {
        "format": 1,
        "purpose": "review-only isolated guide candidate; no workspace changes or apply command",
        "source": {"path": str(agents), "sha256": source_sha},
        "template": {"path": str(template), "sha256": template_sha},
        "outputs": {
            "baseline.md": {"sha256": sha256(baseline_bytes)},
            "candidate.md": {"sha256": sha256(candidate_bytes)},
            "diff.patch": {"sha256": sha256(diff.encode("utf-8"))},
        },
        "replacement_boundary": {
            "section_title": TITLE,
            "exclusive_end_anchor": ANCHOR,
            "source_character_offsets": {"start": source_start, "end": source_end},
            "source_byte_offsets_utf8": {
                "start": len(source[:source_start].encode("utf-8")),
                "end": len(source[:source_end].encode("utf-8")),
            },
            "before_sha256": sha256(before_bytes),
            "after_sha256": sha256(after_bytes),
            "before_bytes": len(before_bytes),
            "after_bytes": len(after_bytes),
        },
    }
    files = {
        "baseline.md": baseline_bytes,
        "candidate.md": candidate_bytes,
        "diff.patch": diff.encode("utf-8"),
        "manifest.json": (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    }
    try:
        for name, content in files.items():
            fd = os.open(output_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
    except OSError as exc:
        # Remove only files created by this invocation; never follow links.
        for name in files:
            try:
                (output_dir / name).unlink()
            except FileNotFoundError:
                pass
        try:
            output_dir.rmdir()
        except OSError:
            pass
        raise PreparationError(f"failed writing review artifacts: {exc}") from exc
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", required=True, type=Path, help="existing group AGENTS.md")
    parser.add_argument("--template", type=Path, default=Path("dsh/templates/group/AGENTS.md"))
    parser.add_argument("--output-dir", required=True, type=Path, help="new directory for review-only artifacts")
    parser.add_argument("--expected-source-sha", help="optional SHA-256 guard against source drift")
    args = parser.parse_args(argv)
    if args.expected_source_sha and (len(args.expected_source_sha) != 64 or any(c not in "0123456789abcdefABCDEF" for c in args.expected_source_sha)):
        parser.error("--expected-source-sha must be 64 hexadecimal characters")
    try:
        manifest = build(args.agents, args.template, args.output_dir, args.expected_source_sha)
    except PreparationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output_dir": str(args.output_dir), "manifest": manifest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
