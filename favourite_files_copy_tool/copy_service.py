"""PC / 安卓文件复制服务。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from adb_push import (
    adb_push_with_optional_sync,
    ensure_adb_ready,
    ensure_remote_directory,
    ensure_remote_parent_for_file,
    normalize_phone_dir,
)
from favourites import FAVOURITE_FILE, read_favourite_names
from traversal import ImageEntry, iter_all_directories_dfs

LogFn = Callable[[str], None]


@dataclass
class CopyStats:
    ok: int = 0
    conflict: int = 0
    missing: int = 0
    failed: int = 0
    skipped: int = 0
    messages: list[str] = field(default_factory=list)


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


def compute_dest_path(
    src: Path, base: Path, target_root: Path | str, keep_path: bool, *, android: bool
) -> str | Path:
    rel = src.resolve().relative_to(base.resolve())
    if android:
        root = normalize_phone_dir(str(target_root))
        if keep_path:
            rel_posix = str(rel).replace("\\", "/")
            return f"{root}/{rel_posix}"
        return f"{root}/{rel.name}"
    root_path = Path(str(target_root))
    if keep_path:
        return root_path / rel
    return root_path / rel.name


def copy_file_pc(
    src: Path,
    dest: Path,
    log: LogFn,
) -> str:
    """
    PC 复制单文件。返回 'ok' | 'conflict' | 'failed'。
    目标已存在则不覆盖并记录冲突。
    """
    if dest.exists():
        try:
            src_size = src.stat().st_size
            dest_size = dest.stat().st_size
        except OSError as exc:
            log(f"冲突：无法读取文件大小：{exc}")
            return "conflict"
        log("冲突：目标已存在同名文件，拒绝覆盖")
        log(f"  源: {src} ({human_size(src_size)})")
        log(f"  目标: {dest} ({human_size(dest_size)})")
        return "conflict"

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest))
        log(f"已复制: {src} -> {dest}")
        return "ok"
    except OSError as exc:
        log(f"复制失败: {src} -> {dest} | {exc}")
        return "failed"


def copy_file_android(
    src: Path,
    phone_dest: str,
    log: LogFn,
    *,
    sync_warned: list[bool],
) -> str:
    """安卓推送单文件。返回 'ok' | 'failed'。"""
    ensure_remote_parent_for_file(phone_dest)
    used_sync, proc, msg = adb_push_with_optional_sync(str(src), phone_dest)
    if not used_sync and msg.startswith("OK") and not sync_warned[0]:
        log("警告: 当前 adb 不支持 --sync，已降级为普通 push。")
        sync_warned[0] = True
    if proc.returncode == 0 or msg.startswith("OK"):
        log(f"已推送: {src.name} -> {phone_dest}")
        return "ok"
    log(f"推送失败: {src} -> {phone_dest} | {msg}")
    return "failed"


def copy_one(
    src: Path,
    base: Path,
    target: str,
    keep_path: bool,
    android: bool,
    log: LogFn,
) -> str:
    dest = compute_dest_path(src, base, target, keep_path, android=android)
    if android:
        ensure_adb_ready()
        ensure_remote_directory(normalize_phone_dir(target))
        sync_warned = [False]
        return copy_file_android(src, str(dest), log, sync_warned=sync_warned)
    return copy_file_pc(src, Path(dest), log)


def copy_all_favourites(
    base: Path,
    target: str,
    keep_path: bool,
    android: bool,
    log: LogFn,
) -> CopyStats:
    """遍历 Base 树，复制各子目录 favourite_list.txt 中列出的文件。"""
    stats = CopyStats()
    base = base.resolve()

    if android:
        try:
            ensure_adb_ready()
            ensure_remote_directory(normalize_phone_dir(target))
        except Exception as exc:
            log(f"ADB 初始化失败: {exc}")
            stats.failed += 1
            return stats

    sync_warned = [False]

    for directory in iter_all_directories_dfs(base):
        fav_path = directory / FAVOURITE_FILE
        if not fav_path.exists():
            continue
        names = read_favourite_names(directory)
        if not names:
            stats.skipped += 1
            continue

        log(f"处理目录: {directory}（{len(names)} 条喜爱）")
        for name in sorted(names, key=str.lower):
            src = directory / name
            if not src.is_file():
                log(f"  跳过（文件不存在）: {name}")
                stats.missing += 1
                continue

            dest = compute_dest_path(src, base, target, keep_path, android=android)
            if android:
                status = copy_file_android(
                    src, str(dest), log, sync_warned=sync_warned
                )
            else:
                status = copy_file_pc(src, Path(dest), log)

            if status == "ok":
                stats.ok += 1
            elif status == "conflict":
                stats.conflict += 1
            else:
                stats.failed += 1

    log(
        f"批量复制完成: 成功={stats.ok}, 冲突={stats.conflict}, "
        f"缺失={stats.missing}, 失败={stats.failed}"
    )
    return stats


def copy_current_entry(
    entry: ImageEntry,
    base: Path,
    target: str,
    keep_path: bool,
    android: bool,
    log: LogFn,
) -> str:
    return copy_one(
        entry.absolute_path,
        base,
        target,
        keep_path,
        android,
        log,
    )
