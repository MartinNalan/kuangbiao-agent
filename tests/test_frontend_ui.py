from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FrontendUiTests(unittest.TestCase):
    def test_markdown_renderer_supports_tables_and_escapes_html(self) -> None:
        script = r"""
const renderer = require('./web/static/markdown.js');
const input = '**表 E.1**\n\n| 项目 | 数值 |\n| --- | ---: |\n| 大型 | >500 |\n\n<script>alert(1)</script>';
process.stdout.write(JSON.stringify({html: renderer.render(input, {baseUrl: 'http://localhost/'})}));
"""
        completed = subprocess.run(
            ["node", "-e", script],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        html = json.loads(completed.stdout)["html"]

        self.assertIn('<table class="markdown-table">', html)
        self.assertIn('style="text-align:right"', html)
        self.assertIn("&gt;500", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_markdown_renderer_loads_before_application(self) -> None:
        html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")

        self.assertLess(html.index("/static/markdown.js"), html.index("/static/app.js"))

    def test_secret_copy_buttons_use_http_compatible_fallback(self) -> None:
        script = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('document.addEventListener("copy", handleCopy)', script)
        self.assertIn('document.execCommand("copy")', script)
        self.assertIn('sourceElement: $("#newKeyValue")', script)
        self.assertIn('sourceElement: $("#newInviteValue")', script)

    def test_revoked_api_keys_are_not_rendered_as_actionable(self) -> None:
        script = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("const revoked = Boolean(item.revoked_at)", script)
        self.assertIn("已吊销", script)
        self.assertIn("if (!revoked)", script)

    def test_dual_mode_controls_and_deep_progress_are_present(self) -> None:
        html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        script = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="qaModeControl"', html)
        self.assertIn("基本模式 · 快速查证", html)
        self.assertIn("深度模式 · 综合研究", script)
        self.assertIn('apiRequest("/api/research/tasks"', script)
        self.assertIn("updateResearchProgress", script)
        self.assertIn("转深度研究 · 追加 2 次", script)

    def test_quota_label_uses_backend_consumed_units(self) -> None:
        script = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("quota.consumed_units", script)
        self.assertIn("本次使用 ${displayCount(units)} 次", script)


if __name__ == "__main__":
    unittest.main()
