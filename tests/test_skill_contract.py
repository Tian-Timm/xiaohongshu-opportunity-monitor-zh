from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "skills" / "xiaohongshu-opportunity-monitor-zh" / "SKILL.md"


class SkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = SKILL_PATH.read_text(encoding="utf-8")

    def test_web_scan_uses_codex_built_in_browser(self) -> None:
        browser = "browser:control-in-app-browser"
        computer_use = "computer-use:computer-use"
        self.assertIn(browser, self.skill)
        self.assertIn("在 Codex 桌面应用中扫描小红书网站", self.skill)
        self.assertIn("使用侧边栏内置浏览器", self.skill)
        self.assertIn("当前任务已经提供它时直接使用", self.skill)
        self.assertLess(self.skill.index(browser), self.skill.index(computer_use))

    def test_browser_preflight_has_checkable_completion_criteria(self) -> None:
        self.assertIn("浏览器预检", self.skill)
        self.assertIn("读取当前 URL 和 DOM", self.skill)
        self.assertIn("全部满足后才开始搜索", self.skill)

    def test_missing_browser_stops_instead_of_falling_back(self) -> None:
        self.assertIn("Settings > Browser", self.skill)
        self.assertIn("新建任务后重试", self.skill)
        self.assertIn("小红书网站扫描不自动改用 Computer Use", self.skill)

    def test_computer_use_requires_an_explicit_desktop_request(self) -> None:
        self.assertIn("只有用户明确要求操作小红书客户端或其他桌面窗口", self.skill)
        self.assertIn("任一条件无法确认时停止并请用户接管", self.skill)


if __name__ == "__main__":
    unittest.main()
