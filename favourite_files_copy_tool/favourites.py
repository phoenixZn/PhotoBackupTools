"""分目录喜爱清单与 Base 级浏览位置读写。"""

from __future__ import annotations

from pathlib import Path

from traversal import ImageEntry

FAVOURITE_FILE = "favourite_list.txt"
PREVIEW_POS_FILE = "preview_pos.txt"


def read_favourite_names(container_dir: Path) -> set[str]:
    fav_path = container_dir / FAVOURITE_FILE
    if not fav_path.exists():
        return set()
    try:
        lines = fav_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {line.strip() for line in lines if line.strip()}


def write_favourite_names(
    container_dir: Path,
    names: set[str],
    order_hint: list[str] | None = None,
) -> None:
    """写入喜爱清单；order_hint 为优先排序键（如 DFS 中该目录文件名顺序）。"""
    ordered = _ordered_names(names, order_hint)
    content = "\n".join(ordered)
    if content:
        content += "\n"
    fav_path = container_dir / FAVOURITE_FILE
    fav_path.write_text(content, encoding="utf-8")


def _ordered_names(names: set[str], order_hint: list[str] | None) -> list[str]:
    if not order_hint:
        return sorted(names, key=str.lower)
    order_map = {name: idx for idx, name in enumerate(order_hint)}
    return sorted(names, key=lambda n: order_map.get(n, 10**9))


def read_preview_index(entries: list[ImageEntry], base_dir: Path) -> int:
    pos_path = base_dir / PREVIEW_POS_FILE
    if not pos_path.exists() or not entries:
        return 0
    try:
        saved_rel = pos_path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    if not saved_rel:
        return 0
    for idx, entry in enumerate(entries):
        if entry.rel_to_base == saved_rel:
            return idx
    return 0


def write_preview_position(base_dir: Path, rel_to_base: str) -> None:
    pos_path = base_dir / PREVIEW_POS_FILE
    pos_path.write_text(rel_to_base + "\n", encoding="utf-8")


def filenames_in_container(entries: list[ImageEntry], container_dir: Path) -> list[str]:
    """返回 DFS 列表中属于某目录的文件名顺序。"""
    container = container_dir.resolve()
    return [
        e.absolute_path.name
        for e in entries
        if e.container_dir.resolve() == container
    ]
