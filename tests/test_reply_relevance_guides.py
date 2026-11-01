import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/prepare-reply-relevance-guides.py"
SPEC = importlib.util.spec_from_file_location("reply_relevance_guides", SCRIPT)
guides = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guides)


class ReplyRelevanceGuidePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "existing.md"
        self.template = self.root / "template.md"
        self.output = self.root / "review"
        # Deliberately include non-ASCII text and unrelated clauses on both sides.
        self.source_bytes = (
            "## 固定群资料\n群内原文：保留🙂\n\n## 你的回复习惯\n"
            "旧表达原则\n- 旧任务表达\n\n- **多项推荐或资料：** 保留这段\n"
            "\n## 独立安全约定\n安全正文必须逐字保留\n"
            "\n**普通消息怎么发：** 保留工具契约\n表情包与升级规则保留\n"
        ).encode("utf-8")
        self.template_bytes = (
            "## 模板前文\n\n## 你的回复习惯\n"
            "新原则：按交流意图选择表达。\n"
            "- **闲聊与讨论：** 可以自然展开。\n"
            "- **执行任务：** 如实说明结果，必要时说明风险。\n"
            "- **主动补充：** 只在有助于当前交流时补充。\n"
            "- **消息组织：** 围绕同一件事自然组织，可按需分条。\n"
            "- **多项推荐或资料：** 模板锚点之后的内容不参与替换。\n"
        ).encode("utf-8")
        self.source.write_bytes(self.source_bytes)
        self.template.write_bytes(self.template_bytes)

    def tearDown(self):
        self.temp.cleanup()

    def test_candidate_changes_only_principle_span_and_preserves_utf8_suffix(self):
        result = guides.build(self.source, self.template, self.output, None)
        baseline = (self.output / "baseline.md").read_bytes()
        candidate = (self.output / "candidate.md").read_bytes()
        self.assertEqual(baseline, self.source_bytes)

        source_text = self.source_bytes.decode("utf-8")
        template_text = self.template_bytes.decode("utf-8")
        source_start, source_end, _ = guides.extract_prefix(source_text, "source")
        _, _, template_prefix = guides.extract_prefix(template_text, "template")
        expected = (source_text[:source_start] + template_prefix + source_text[source_end:]).encode("utf-8")
        self.assertEqual(candidate, expected)
        self.assertTrue(candidate.endswith(source_text[source_end:].encode("utf-8")))
        self.assertIn("保留工具契约".encode(), candidate)
        self.assertIn("表情包与升级规则保留".encode(), candidate)
        self.assertIn("## 独立安全约定\n安全正文必须逐字保留".encode(), candidate)
        self.assertNotIn("旧表达原则".encode(), candidate)

        manifest = json.loads((self.output / "manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["source"]["sha256"], hashlib.sha256(self.source_bytes).hexdigest())
        self.assertEqual(manifest["template"]["sha256"], hashlib.sha256(self.template_bytes).hexdigest())
        self.assertEqual(manifest["replacement_boundary"]["before_sha256"], hashlib.sha256(source_text[source_start:source_end].encode()).hexdigest())
        self.assertEqual(manifest["replacement_boundary"]["after_sha256"], hashlib.sha256(template_prefix.encode()).hexdigest())
        self.assertIn("多项推荐或资料", (self.output / "diff.patch").read_text("utf-8"))
        self.assertFalse(any("apply" in line.lower() and "command" in line.lower() for line in (self.output / "diff.patch").read_text("utf-8").splitlines()))
        for path in self.output.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        self.assertEqual(result["purpose"], "review-only isolated guide candidate; no workspace changes or apply command")

    def test_ambiguous_or_missing_boundaries_are_rejected_without_output(self):
        cases = [
            ("duplicate title", self.source_bytes + "\n## 你的回复习惯\n".encode(), self.template_bytes),
            ("missing source anchor", self.source_bytes.replace("- **多项推荐或资料：**".encode(), b"- no anchor"), self.template_bytes),
            ("duplicate template anchor", self.source_bytes, self.template_bytes + "- **多项推荐或资料：** again\n".encode()),
            ("cross-section source anchor", self.source_bytes.replace(
                "- **多项推荐或资料：**".encode(),
                "## 独立安全约定\n不要替换的安全内容\n- **多项推荐或资料：**".encode(),
            ), self.template_bytes),
            ("indented cross-section heading", self.source_bytes.replace(
                "- **多项推荐或资料：**".encode(),
                "   ## 独立安全约定\n保留安全边界\n- **多项推荐或资料：**".encode(),
            ), self.template_bytes),
        ]
        for label, source_bytes, template_bytes in cases:
            with self.subTest(label=label):
                self.source.write_bytes(source_bytes)
                self.template.write_bytes(template_bytes)
                target = self.root / ("out-" + label.replace(" ", "-"))
                with self.assertRaises(guides.PreparationError):
                    guides.build(self.source, self.template, target, None)
                self.assertFalse(target.exists())

    def test_existing_output_and_source_drift_are_rejected(self):
        with self.assertRaisesRegex(guides.PreparationError, "drift"):
            guides.build(self.source, self.template, self.output, "0" * 64)
        self.assertFalse(self.output.exists())

        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("existing", encoding="utf-8")
        with self.assertRaisesRegex(guides.PreparationError, "already exists"):
            guides.build(self.source, self.template, self.output, None)
        self.assertEqual(marker.read_text("utf-8"), "existing")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support unavailable")
    def test_symlink_input_and_output_components_are_rejected(self):
        linked_source = self.root / "linked.md"
        linked_source.symlink_to(self.source)
        with self.assertRaisesRegex(guides.PreparationError, "symlink"):
            guides.build(linked_source, self.template, self.output, None)

        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(guides.PreparationError, "symlink"):
            guides.build(self.source, self.template, linked_parent / "out", None)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
