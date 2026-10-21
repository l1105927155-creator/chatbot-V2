#!/usr/bin/env python3
"""Import the legacy meme pack into the shared, category-only image library."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path


def safe_stem(text: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text, flags=re.UNICODE).strip("-_")
    return (text or "表情")[:36]


def import_pack(source: Path, destination: Path, *, category_only: bool) -> dict:
    source = source.resolve(strict=True)
    if not (source / "LICENSE.md").is_file() or not (source / "NOTICE.md").is_file():
        raise ValueError("源素材必须同时包含 LICENSE.md 与 NOTICE.md")
    category_names = json.loads((source / "memes_data.json").read_text("utf-8"))
    semantic = json.loads((source / "semantic_metadata.json").read_text("utf-8"))
    trusted_by_hash: dict[str, dict] = {}
    for item in semantic.get("images", {}).values():
        if isinstance(item, dict) and isinstance(item.get("content_sha256"), str):
            trusted_by_hash[item["content_sha256"]] = item

    image_root = source / "memes"
    if image_root.is_symlink() or not image_root.is_dir():
        raise ValueError("素材 memes 目录无效")
    images = []
    for category_dir in sorted(image_root.iterdir(), key=lambda p: p.name):
        if category_dir.is_symlink() or not category_dir.is_dir():
            continue
        category_key = category_dir.name
        category_label = category_names.get(category_key, category_key)
        category = safe_stem(category_key.split("|", 1)[-1])
        for original in sorted(category_dir.iterdir(), key=lambda p: p.name):
            if original.is_symlink() or not original.is_file() or original.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                continue
            raw = original.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            semantic_record = trusted_by_hash.get(digest)
            if semantic_record is None and not category_only:
                continue
            semantic_verified = semantic_record is not None
            sequence = len([it for it in images if it["category_key"] == category_key]) + 1
            relative = Path(category) / f"{category}-{sequence:02d}-{digest[:4]}{original.suffix.lower()}"
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise ValueError(f"目标文件冲突：{relative}")
            else:
                target.write_bytes(raw)
            images.append({
                "category_key": category_key,
                "category": category,
                "category_label": category_label,
                "name": relative.stem,
                "path": relative.as_posix(),
                "sha256": digest,
                "original_path": original.relative_to(source).as_posix(),
                "original_filename": original.name,
                "semantic_status": "verified" if semantic_verified else "only_category/semantic_unverified",
                "semantic_metadata_entry": semantic_record.get("entry_id") if semantic_verified else None,
            })

    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "LICENSE.md", destination / "LICENSE.md")
    shutil.copy2(source / "NOTICE.md", destination / "NOTICE.md")
    inventory = {
        "format": "chatbot-v2-shared-meme-library",
        "source_pack_id": semantic.get("pack_id"),
        "source_pack": source.name,
        "import_mode": "category-only" if category_only else "hash-matched-only",
        "classification_rule": "physical category directory; semantic labels are added only for matching content_sha256",
        "items": images,
    }
    (destination / "provenance.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return {"imported": len(images), "verified_semantics": sum(x["semantic_status"] == "verified" for x in images),
            "category_only": sum(x["semantic_status"] != "verified" for x in images)}


def main() -> None:
    here = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/home/abasun/chatbot/astrbot/data/plugin_data/meme_manager/packs/ddzs987-semantic-001"))
    parser.add_argument("--destination", type=Path, default=here / "meme")
    parser.add_argument("--category-only", action="store_true", help="allow category-only imports when image hashes do not match semantic metadata")
    args = parser.parse_args()
    if not args.category_only:
        parser.error("当前素材包的语义记录可能与物理文件不同步；确认只采用物理分类时请显式传入 --category-only")
    result = import_pack(args.source, args.destination, category_only=True)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
