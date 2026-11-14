#!/usr/bin/env python3
"""检查 V2/V3 插件源码是否一致，并允许入口文件的平台差异。"""

from __future__ import annotations

import difflib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
V2_ROOT = ROOT / "plugins.v2"
V3_ROOT = ROOT / "plugins.v3"

# 这些插件的 __init__.py 需要兼容不同 MoviePilot SDK 路径或使用独立发布版本号，
# 不能逐字节相同；其余文件仍严格比较。入口行为由 unit tests 覆盖。
PLATFORM_ENTRYPOINTS = {"mteamalive", "ptsitesignin"}


def source_files(root: Path) -> dict[Path, Path]:
    return {
        path.relative_to(root): path
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def comparable(path: Path, relative: Path, plugin: str) -> str | bytes:
    if relative.name == "__init__.py" and plugin in PLATFORM_ENTRYPOINTS:
        return b""
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        return raw


def main() -> int:
    failed = False
    names = sorted(path.name for path in V2_ROOT.iterdir() if path.is_dir())
    v3_names = sorted(path.name for path in V3_ROOT.iterdir() if path.is_dir())
    if names != v3_names:
        print(f"插件目录集合不一致：V2={names} V3={v3_names}")
        failed = True

    for name in names:
        v2_dir = V2_ROOT / name
        v3_dir = V3_ROOT / name
        if not v3_dir.is_dir():
            print(f"V3 缺少插件目录：{name}")
            failed = True
            continue
        v2_files = source_files(v2_dir)
        v3_files = source_files(v3_dir)
        for relative in sorted(set(v2_files) | set(v3_files), key=str):
            v2_file = v2_files.get(relative)
            v3_file = v3_files.get(relative)
            if v2_file is None or v3_file is None:
                print(f"{name}/{relative}: 文件只存在于一侧")
                failed = True
                continue
            v2_value = comparable(v2_file, relative, name)
            v3_value = comparable(v3_file, relative, name)
            if v2_value == v3_value:
                continue
            print(f"{name}/{relative}: 内容不一致")
            if isinstance(v2_value, str) and isinstance(v3_value, str):
                diff = difflib.unified_diff(
                    v2_value.splitlines(),
                    v3_value.splitlines(),
                    fromfile=f"plugins.v2/{name}/{relative}",
                    tofile=f"plugins.v3/{name}/{relative}",
                    lineterm="",
                )
                print("\n".join(diff))
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
