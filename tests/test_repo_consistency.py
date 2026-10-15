"""V2/V3 插件目录与市场清单一致性检查。"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "plugins.v2" / "ptsitesignin"
V3 = ROOT / "plugins.v3" / "ptsitesignin"


def plugin_files(directory: Path):
    return sorted(path.relative_to(directory) for path in directory.rglob("*")
                  if path.is_file() and "__pycache__" not in path.parts)


class RepoConsistencyTests(unittest.TestCase):
    def test_v2_and_v3_plugin_directories_identical(self):
        v2_files = plugin_files(V2)
        self.assertEqual(v2_files, plugin_files(V3))
        for relative in v2_files:
            self.assertEqual((V2 / relative).read_bytes(), (V3 / relative).read_bytes(),
                             f"{relative} differs between plugins.v2 and plugins.v3")

    def test_manifest_version_and_history_match_plugin(self):
        versions = []
        for init_file in (V2 / "__init__.py", V3 / "__init__.py"):
            match = re.search(r'plugin_version\s*=\s*"([^"]+)"',
                              init_file.read_text(encoding="utf-8"))
            self.assertIsNotNone(match, str(init_file))
            versions.append(match.group(1))
        self.assertEqual(versions[0], versions[1])
        version = versions[0]
        for manifest in ("package.json", "package.v2.json", "package.v3.json"):
            data = json.loads((ROOT / manifest).read_text(encoding="utf-8"))
            entry = data["PTSiteSignIn"]
            self.assertEqual(entry["version"], version, manifest)
            self.assertIn(f"v{version}", entry["history"], manifest)


if __name__ == "__main__":
    unittest.main()
