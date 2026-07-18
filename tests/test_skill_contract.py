from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "skills" / "xiaohongshu-opportunity-monitor-zh" / "SKILL.md"


class SkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = SKILL_PATH.read_text(encoding="utf-8")

    def test_web_scan_recognizes_current_codex_browser_paths(self) -> None:
        self.assertIn("Browser Plugin", self.skill)
        self.assertIn("Chrome Plugin", self.skill)
        self.assertIn("node_repl", self.skill)
        self.assertIn("Playwright", self.skill)
        self.assertNotIn("browser:control-in-app-browser", self.skill)

    def test_browser_preflight_has_checkable_completion_criteria(self) -> None:
        self.assertIn("浏览器预检", self.skill)
        self.assertIn("读取当前 URL 和 DOM", self.skill)
        self.assertIn("全部满足后才开始搜索", self.skill)

    def test_computer_use_requires_an_explicit_desktop_branch(self) -> None:
        self.assertIn("只有用户随后明确同意改用桌面操作", self.skill)
        self.assertIn("用户明确要求操作小红书客户端、桌面窗口", self.skill)
        self.assertIn("任一条件无法确认时停止并请用户接管", self.skill)


if __name__ == "__main__":
    unittest.main()
