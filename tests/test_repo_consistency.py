"""V2/V3 插件目录与市场清单一致性检查。"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 插件目录名 -> 市场清单里的插件 key
PLUGINS = {
    "ptsitesignin": "PTSiteSignIn",
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
                match = re.search(r'plugin_version\s*=\s*"([^"]+)"',
                                  init_file.read_text(encoding="utf-8"))
                self.assertIsNotNone(match, str(init_file))
                versions.append(match.group(1))
            self.assertEqual(versions[0], versions[1], name)
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
            self.assertTrue((ROOT / "plugins.v2" / name).is_dir(), name)
            self.assertTrue((ROOT / "plugins.v3" / name).is_dir(), name)
            for manifest in MANIFESTS:
                data = json.loads((ROOT / manifest).read_text(encoding="utf-8"))
                self.assertIn(key, data, f"{key} 未写入 {manifest}")
                icon = str(data[key].get("icon") or "")
                self.assertIn("/icons/", icon, f"{key} @ {manifest} 缺少图标地址")
                self.assertTrue(icon.endswith(".png"), f"{key} @ {manifest} 图标应为 png")
                icon_file = ROOT / "icons" / icon.rsplit("/", 1)[-1]
                self.assertTrue(icon_file.is_file(), f"{icon_file} 缺失")


if __name__ == "__main__":
    unittest.main()
