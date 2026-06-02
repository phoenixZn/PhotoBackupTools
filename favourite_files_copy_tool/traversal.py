"""DFS 媒体遍历：先本目录文件，再子目录（深度优先）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

MediaKind = Literal["image", "video"]


def is_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS


def is_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def is_media(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS


def media_kind_for(path: Path) -> MediaKind:
    if is_video(path):
        return "video"
    return "image"


def rel_to_base_str(path: Path, base: Path) -> str:
    return str(path.relative_to(base)).replace("\\", "/")


@dataclass(frozen=True)
class ImageEntry:
    absolute_path: Path
    container_dir: Path
    rel_to_base: str
    media_kind: MediaKind


def iter_images_dfs(root: Path) -> list[Path]:
    """深度优先收集媒体文件：本目录文件（按名排序）→ 各子目录。"""
    files = sorted(
        (p for p in root.iterdir() if is_media(p)),
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
            media_kind=media_kind_for(p),
        )
        for p in paths
    ]


def build_dir_first_indices(entries: list[ImageEntry]) -> list[int]:
    """按 DFS 顺序，记录每个「含媒体文件子目录」在扁平列表中首个条目的索引。"""
    indices: list[int] = []
    seen: set[Path] = set()
    for idx, entry in enumerate(entries):
        cdir = entry.container_dir.resolve()
        if cdir not in seen:
            seen.add(cdir)
            indices.append(idx)
    return indices


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
