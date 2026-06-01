"""DFS 图片遍历：先本目录文件，再子目录（深度优先）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif"}


def is_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS


def rel_to_base_str(path: Path, base: Path) -> str:
    return str(path.relative_to(base)).replace("\\", "/")


@dataclass(frozen=True)
class ImageEntry:
    absolute_path: Path
    container_dir: Path
    rel_to_base: str


def iter_images_dfs(root: Path) -> list[Path]:
    """深度优先收集图片：本目录文件（按名排序）→ 各子目录。"""
    files = sorted(
        (p for p in root.iterdir() if is_image(p)),
        key=lambda p: p.name.lower(),
    )
    result: list[Path] = list(files)
    subdirs = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.name.lower(),
    )
    for sub in subdirs:
        result.extend(iter_images_dfs(sub))
    return result


def build_image_entries(base_dir: Path) -> list[ImageEntry]:
    base = base_dir.resolve()
    paths = iter_images_dfs(base)
    return [
        ImageEntry(
            absolute_path=p.resolve(),
            container_dir=p.parent.resolve(),
            rel_to_base=rel_to_base_str(p, base),
        )
        for p in paths
    ]


def iter_all_directories_dfs(root: Path) -> list[Path]:
    """深度优先列出 Base 下所有目录（含 root 自身）。"""
    root = root.resolve()
    result = [root]
    subdirs = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.name.lower(),
    )
    for sub in subdirs:
        result.extend(iter_all_directories_dfs(sub))
    return result
