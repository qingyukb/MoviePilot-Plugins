"""多 Token 轮换 + LuckPT 签到规则的用例。"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = next((parent for parent in Path(__file__).resolve().parents
              if (parent / "package.json").is_file() and (parent / "plugins.v2").is_dir()),
             Path(__file__).resolve().parents[1])
PLUGIN_DIRS = tuple(directory for directory in (
    ROOT / "plugins.v2" / "ptsitesignin",
    ROOT / "plugins.v3" / "ptsitesignin",
) if directory.is_dir())


def load_core(directory: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, directory / "core.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CORES = [load_core(directory, f"ptsignin_pool_{index}")
         for index, directory in enumerate(PLUGIN_DIRS)]


class BrowserlessPoolTests(unittest.TestCase):
    """多个 Token：额度/鉴权失败时自动换下一个，站点自身失败不轮换。"""

    def _pool(self, core, results):
        """构造一个 pool，sign 时依次返回 results 里的行为（不联网）。"""
        pool = core.BrowserlessPool("https://example.test", ["k1", "k2", "k3"], 60)
        calls = {"n": 0}

        def fake_sign(self, site, rule):
            index = calls["n"]
            calls["n"] += 1
            return results[min(index, len(results) - 1)]

        for _, signer in pool._signers:
            signer.sign = lambda site, rule, _s=signer: fake_sign(_s, site, rule)
        return pool, calls

    def test_switches_token_on_quota_error(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                quota = core.SignResult("failed", "Browserless 额度不足或请求过于频繁（HTTP 429）")
                ok = core.SignResult("success", "签到成功")
                pool, calls = self._pool(core, [quota, ok])
                result = pool.sign({}, {"path": "/attendance.php"})
                self.assertEqual(result.status, "success")
                self.assertEqual(calls["n"], 2)          # 第一个失败后换了第二个
                self.assertEqual(pool.active_index, 2)   # 记住这次成功的序号

    def test_keeps_token_on_site_failure(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                site_fail = core.SignResult("failed", "签到页返回 500，站点自身错误")
                pool, calls = self._pool(core, [site_fail])
                result = pool.sign({}, {"path": "/attendance.php"})
                self.assertEqual(result.status, "failed")
                self.assertEqual(calls["n"], 1)          # 不因站点错误切换 Token

    def test_all_tokens_busy_returns_last_failure(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                quota = core.SignResult("failed", "Browserless 鉴权失败（HTTP 401），请检查 Token")
                pool, calls = self._pool(core, [quota])
                result = pool.sign({}, {"path": "/attendance.php"})
                self.assertEqual(result.status, "failed")
                self.assertEqual(calls["n"], 3)          # 三个 Token 都试过
                self.assertIn("401", result.message)

    def test_empty_tokens_gracefully_fail(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                pool = core.BrowserlessPool("https://example.test", ["", "  "], 60)
                self.assertFalse(pool)
                self.assertEqual(pool.sign({}, {}).status, "failed")

    def test_token_dedup(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                pool = core.BrowserlessPool("https://example.test", ["a", " a ", "b"], 60)
                self.assertEqual(pool.tokens, ["a", "b"])


class LuckPTRuleTests(unittest.TestCase):
    """LuckPT 只做日常签到，不再领「纪念套装」幸运星。"""

    def test_luckpt_uses_attendance(self):
        for core in CORES:
            with self.subTest(core=core.__name__):
                rule = core.DEFAULT_RULES["pt.luckpt.de"]
                self.assertEqual(rule["path"], "/attendance.php")
                self.assertNotIn("medal_collection", rule["path"])
                self.assertNotIn("幸运星", str(rule.get("submit_text") or ""))
                self.assertEqual(rule["submit_method"], "dom_click")


if __name__ == "__main__":
    unittest.main()
