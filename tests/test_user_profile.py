"""Profile mutations must be exact, scoped, idempotent and truthful."""

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from user_profile import ProfileManager
from tool_schemas import _raw_tool_schemas


# The profile handlers have no third-party dependencies. Load the leaf module
# directly so these persistence regressions also run without the PDF/RAG stack.
_spec = importlib.util.spec_from_file_location(
    "profile_handlers", Path(__file__).resolve().parents[1] / "tools" / "profile_tool.py",
)
_handlers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_handlers)


class TestUserProfile(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "profile.md"
        self.profile = ProfileManager(str(self.path))

    def seed(self, body):
        self.path.write_text(body, encoding="utf-8")

    def call(self, action, content, **kwargs):
        return _handlers.handle_update_profile(
            dict(action=action, content=content, **kwargs), profile_manager=self.profile,
        )

    def test_each_add_writes_one_copy_and_retry_does_not_write(self):
        for action, section, content in (
            ("add_direction", "研究方向", "TSN"),
            ("add_question", "活跃问题", "如何降低抖动？"),
            ("add_paper", "已读论文", "论文 A: 一句话摘要"),
        ):
            with self.subTest(action=action):
                self.assertTrue(self.profile.update_from_agent(action, content))
                self.assertEqual(self.profile.read_section(section), [content])
                before = self.path.read_bytes()
                with patch.object(self.profile, "_write", wraps=self.profile._write) as write:
                    self.assertIn("未变更", self.call(action, f"  {content}  "))
                    write.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)

    def test_add_collapses_legacy_duplicates_only_for_the_target(self):
        self.seed("## 研究方向\n- TSN\n- TSN2\n- TSN  \n- other\n- other\n\n## 研究方向备忘\n- TSN\n")
        self.profile.add_research_direction("TSN")
        self.assertEqual(self.profile.read_section("研究方向"), ["TSN", "TSN2", "other", "other"])
        self.assertIn("## 研究方向备忘\n- TSN\n", self.profile.read())

    def test_remove_is_exact_scoped_and_removes_all_legacy_copies(self):
        self.seed("## 研究方向\n- TSN\n- TSN2\n- TSN\n\n## 活跃问题\n- TSN\n")
        self.assertIn("已更新", self.call("remove_entry", "TSN", section="研究方向"))
        self.assertEqual(self.profile.read_section("研究方向"), ["TSN2"])
        self.assertEqual(self.profile.read_section("活跃问题"), ["TSN"])
        before = self.path.read_bytes()
        self.assertIn("未找到精确匹配", self.call("remove_entry", "TS", section="研究方向"))
        self.assertIn("未找到精确匹配", self.call("remove_entry", "TSN", section="研究方向"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_all_four_sections_support_exact_replace_and_remove(self):
        for section in ProfileManager.SECTIONS:
            with self.subTest(section=section):
                self.seed(f"## {section}\n- key: old\n- key: old\n- keep: value\n")
                self.assertTrue(self.profile.replace_entry(section, "key: old", "key: new"))
                self.assertEqual(self.profile.read_section(section), ["key: new", "keep: value"])
                self.assertTrue(self.profile.remove_entry(section, "key: new"))
                self.assertEqual(self.profile.read_section(section), ["keep: value"])

    def test_indented_and_subsection_headings_bound_the_mutation(self):
        for heading in ("  ## 活跃问题", "### 手写备注"):
            with self.subTest(heading=heading):
                self.seed(f"## 研究方向\n- TSN\n{heading}\n- TSN\n")
                self.profile.remove_entry("研究方向", "TSN")
                self.assertEqual(self.profile.read_section("研究方向"), [])
                self.assertIn(f"{heading}\n- TSN\n", self.profile.read())

    def test_replace_keeps_order_and_coalesces_existing_destination(self):
        self.seed("## 活跃问题\n- first\n- old\n- middle\n- new\n- old\n- last\n")
        self.profile.replace_entry("活跃问题", "old", "new")
        self.assertEqual(self.profile.read_section("活跃问题"), ["first", "new", "middle", "last"])

    def test_identical_replacement_is_noop_and_stale_replacement_fails(self):
        self.profile.add_research_direction("TSN")
        before = self.path.read_bytes()
        self.assertIn("未变更", self.call("replace_entry", "TSN", section="研究方向", replacement="TSN"))
        self.assertIn("未找到精确匹配", self.call("replace_entry", "stale", section="研究方向", replacement="new"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_set_preference_updates_only_its_section_and_collapses_same_key(self):
        self.seed("## 研究方向\n- 语言: 保留\n## 偏好设置\n- 语言: 中文\n- 语言: 英文\n- 语言模型: 保留\n## 已读论文\n- 语言: 保留\n")
        self.assertTrue(self.profile.set_preference("语言", "双语"))
        self.assertEqual(self.profile.read_section("偏好设置"), ["语言: 双语", "语言模型: 保留"])
        self.assertEqual(self.profile.read_section("研究方向"), ["语言: 保留"])
        self.assertEqual(self.profile.read_section("已读论文"), ["语言: 保留"])
        self.assertFalse(self.profile.set_preference("语言", "双语"))

    def test_preference_rename_collision_does_not_write(self):
        self.seed("## 偏好设置\n- 语言: 中文\n- 风格: 简洁\n")
        before = self.path.read_bytes()
        self.assertIn("已有其他值", self.call(
            "replace_entry", "语言: 中文", section="偏好设置", replacement="风格: 详细",
        ))
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_action_errors_at_manager_and_handler(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "未知画像操作"):
            self.profile.update_from_agent("remove_direction", "TSN")
        self.assertIn("❌", self.call("remove_direction", "TSN"))
        self.assertIn("未知画像操作", self.call("remove_direction", "TSN"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_arguments_cannot_change_profile_structure(self):
        self.profile.add_research_direction("TSN")
        before = self.path.read_bytes()
        invalid = [
            ("add_direction", "", {}),
            ("add_question", "  ", {}),
            ("add_direction", ["TSN"], {}),
            ("add_direction", "a\n## 偏好设置\n- 语言: 英文", {}),
            ("add_direction", "a\rhidden", {}),
            ("add_paper", "missing summary", {}),
            ("add_paper", ": summary", {}),
            ("set_preference", "语言", {}),
            ("set_preference", "语言: ", {}),
            ("set_preference", "bad:key: value", {}),
            ("remove_entry", "TSN", {}),
            ("remove_entry", "TSN", {"section": "unknown"}),
            ("remove_entry", "TSN", {"section": "研究方向", "replacement": "new"}),
            ("replace_entry", "TSN", {"section": "研究方向"}),
            ("replace_entry", "TSN", {"section": "研究方向", "replacement": "new\nline"}),
            ("replace_entry", "语言: 中文", {"section": "偏好设置", "replacement": "bad:key: value"}),
            ("add_direction", "TSN", {"section": "活跃问题"}),
        ]
        for action, content, kwargs in invalid:
            with self.subTest(action=action, content=content, kwargs=kwargs):
                self.assertIn("❌", self.call(action, content, **kwargs))
                self.assertEqual(self.path.read_bytes(), before)

    def test_missing_or_ambiguous_section_errors_without_writing(self):
        for body in ("## 研究方向备忘\n- TSN\n", "## 研究方向\n- TSN\n## 研究方向\n- other\n"):
            self.seed(body)
            before = self.path.read_bytes()
            for action in ("add_direction", "remove_entry", "replace_entry"):
                kwargs = {} if action == "add_direction" else {"section": "研究方向"}
                if action == "replace_entry":
                    kwargs["replacement"] = "new"
                self.assertIn("缺失或重复", self.call(action, "TSN", **kwargs))
                self.assertEqual(self.path.read_bytes(), before)

    def test_preserves_notes_other_sections_and_literal_timestamp_text(self):
        self.seed("# 用户画像\n\n## 研究方向\n自定义备注\n- TSN\n\n## 其他\n- untouched\n*最后更新: 2000-01-01 00:00*\n")
        self.profile.replace_entry("研究方向", "TSN", "文本含 *最后更新: 原文* 标记")
        text = self.profile.read()
        self.assertIn("自定义备注\n- 文本含 *最后更新: 原文* 标记\n\n## 其他\n- untouched\n", text)
        self.assertNotIn("2000-01-01", text)

    def test_read_profile_exposes_papers_and_questions_beyond_summary(self):
        self.profile.add_paper("Paper A", "Evidence")
        for i in range(4):
            self.profile.add_active_question(f"q{i}")
        before = self.path.read_bytes()
        self.assertIn("- Paper A: Evidence", _handlers.handle_read_profile(
            {"section": "已读论文"}, profile_manager=self.profile,
        ))
        self.assertIn("- q0", _handlers.handle_read_profile(
            {"section": "活跃问题"}, profile_manager=self.profile,
        ))
        self.assertNotIn("q0", self.profile.summary())
        self.assertEqual(self.path.read_bytes(), before)

    def test_handlers_report_disabled_profile_and_invalid_read(self):
        self.assertIn("未启用", _handlers.handle_read_profile({"section": "研究方向"}))
        self.assertIn("未启用", _handlers.handle_update_profile({"action": "add_direction", "content": "TSN"}))
        self.assertIn("未知画像章节", _handlers.handle_read_profile({}, profile_manager=self.profile))

    def test_concurrent_adds_keep_every_distinct_entry_once(self):
        values = [f"question-{i}" for i in range(20)] * 3
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(self.profile.add_active_question, values))
        entries = self.profile.read_section("活跃问题")
        self.assertEqual(len(entries), 20)
        self.assertEqual(set(entries), set(values))
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_failed_atomic_write_leaves_original_profile(self):
        before = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("write failed")):
            self.assertIn("画像更新失败", self.call("add_direction", "TSN"))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_tool_schemas_expose_supported_actions_and_read_sections(self):
        schemas = {s["function"]["name"]: s["function"] for s in _raw_tool_schemas()}
        params = schemas["update_profile"]["parameters"]
        self.assertEqual(set(params["properties"]["action"]["enum"]), set(ProfileManager.ACTIONS))
        self.assertEqual(set(params["properties"]["section"]["enum"]), set(ProfileManager.SECTIONS))
        self.assertIn("replacement", params["properties"])
        self.assertEqual(schemas["read_profile"]["parameters"]["required"], ["section"])


if __name__ == "__main__":
    unittest.main()
