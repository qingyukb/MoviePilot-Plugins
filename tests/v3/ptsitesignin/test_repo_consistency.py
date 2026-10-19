"""V2/V3 插件目录与市场清单一致性检查。"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = next((parent for parent in Path(__file__).resolve().parents
              if (parent / "package.json").is_file() and (parent / "plugins.v2").is_dir()),
             Path(__file__).resolve().parents[1])
# 插件目录名 -> 市场清单里的插件 key
PLUGINS = {
    "ptsitesignin": "PTSiteSignIn",
    "mteamkeepalive": "MTeamKeepAlive",
}
MANIFESTS = ("package.json", "package.v2.json", "package.v3.json")


def plugin_files(directory: Path):
    return sorted(path.relative_to(directory) for path in directory.rglob("*")
                  if path.is_file() and "__pycache__" not in path.parts)


class RepoConsistencyTests(unittest.TestCase):
    def test_v2_and_v3_plugin_directories_identical(self):
        for name in PLUGINS:
            v2 = ROOT / "plugins.v2" / name
            v3 = ROOT / "plugins.v3" / name
            if not (v2.is_dir() and v3.is_dir()):
                # 官方仓库只放 plugins.v3 一份，没有 V2 副本时跳过对比
                continue
            with self.subTest(plugin=name):
                v2_files = plugin_files(v2)
                self.assertEqual(v2_files, plugin_files(v3), name)
                for relative in v2_files:
                    self.assertEqual((v2 / relative).read_bytes(), (v3 / relative).read_bytes(),
                                     f"{name}/{relative} differs between plugins.v2 and plugins.v3")

    def test_manifest_version_and_history_match_plugin(self):
        for name, key in PLUGINS.items():
            versions = []
            for init_file in (ROOT / "plugins.v2" / name / "__init__.py",
                              ROOT / "plugins.v3" / name / "__init__.py"):
                if not init_file.is_file():
                    continue
                match = re.search(r'plugin_version\s*=\s*"([^"]+)"',
                                  init_file.read_text(encoding="utf-8"))
                self.assertIsNotNone(match, str(init_file))
                versions.append(match.group(1))
            if len(versions) > 1:
                self.assertEqual(len(set(versions)), 1, name)
            version = versions[0]
            for manifest in MANIFESTS:
                data = json.loads((ROOT / manifest).read_text(encoding="utf-8"))
                entry = data[key]
                self.assertEqual(entry["version"], version, f"{name} @ {manifest}")
                self.assertIn(f"v{version}", entry["history"], f"{name} @ {manifest}")

    def test_every_plugin_directory_is_listed_in_manifests(self):
        for name in sorted(path.name for path in (ROOT / "plugins.v2").iterdir()
                           if path.is_dir()):
            self.assertIn(name, PLUGINS, f"plugins.v2/{name} 未登记到 PLUGINS")
        for name, key in PLUGINS.items():
            self.assertTrue((ROOT / "plugins.v2" / name).is_dir()
                            or (ROOT / "plugins.v3" / name).is_dir(), name)
            for manifest in MANIFESTS:
                data = json.loads((ROOT / manifest).read_text(encoding="utf-8"))
                self.assertIn(key, data, f"{key} 未写入 {manifest}")
                icon = str(data[key].get("icon") or "")
                self.assertIn("/icons/", icon, f"{key} @ {manifest} 缺少图标地址")
                self.assertTrue(icon.endswith(".png"), f"{key} @ {manifest} 图标应为 png")
                icon_file = ROOT / "icons" / icon.rsplit("/", 1)[-1]
                self.assertTrue(icon_file.is_file(), f"{icon_file} 缺失")

    def test_directory_name_matches_class_name(self):
        """官方规则：插件目录名必须是插件主类名的小写形式，主类定义在目录的 __init__.py。"""
        for name, key in PLUGINS.items():
            self.assertEqual(name, key.lower(), f"{name} != {key}.lower()")
            init_file = ROOT / "plugins.v3" / name / "__init__.py"
            source = init_file.read_text(encoding="utf-8")
            self.assertIn(f"class {key}(", source, f"{init_file} 未定义主类 {key}")

    def test_manifest_has_official_fields(self):
        """官方索引字段：name / description / labels / version / icon / author / level / release / history。"""
        required = ("name", "description", "labels", "version", "icon", "author",
                    "level", "release", "history")
        for name, key in PLUGINS.items():
            for manifest in MANIFESTS:
                entry = json.loads((ROOT / manifest).read_text(encoding="utf-8"))[key]
                for field in required:
                    self.assertIn(field, entry, f"{key} @ {manifest} 缺少字段 {field}")
                self.assertIsInstance(entry["release"], bool, f"{key} @ {manifest} release 应为布尔")

    def test_version_history_is_semver_descending(self):
        """官方规则：当前版本置顶，历史按语义版本降序。"""
        def parse(value: str):
            return tuple(int(part) for part in value.lstrip("v").split("."))

        for name, key in PLUGINS.items():
            for manifest in MANIFESTS:
                history = list(json.loads(
                    (ROOT / manifest).read_text(encoding="utf-8"))[key]["history"])
                versions = [parse(item) for item in history]
                self.assertEqual(versions, sorted(versions, reverse=True),
                                 f"{key} @ {manifest} history 未按版本降序")
                self.assertEqual(history[0], f"v{parse(history[0])[0]}."
                                             f"{parse(history[0])[1]}.{parse(history[0])[2]}")

    def test_tests_live_under_tests_v3(self):
        """官方规则：测试放仓库根 tests/v3/<plugin_id>/，不要放进插件源码目录。"""
        for name in PLUGINS:
            test_dir = ROOT / "tests" / "v3" / name
            self.assertTrue(test_dir.is_dir(), f"缺少 {test_dir}")
            self.assertTrue(any(test_dir.glob("test_*.py")), f"{test_dir} 下没有测试文件")
            for plugin_dir in (ROOT / "plugins.v2" / name, ROOT / "plugins.v3" / name):
                self.assertFalse(list(plugin_dir.glob("test_*.py")),
                                 f"{plugin_dir} 里不应放测试文件")


if __name__ == "__main__":
    unittest.main()
