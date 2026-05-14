"""
按月目录整理 CLI 工具。

使用说明（示例）：
1) 交互确认后执行：
   python organize_by_month.py --root "D:/demo/source_dir"
2) 跳过确认并删除空目录：
   python organize_by_month.py --root "D:/demo/source_dir" --yes --remove-empty-dirs
3) 给 GUI 使用的结构化事件输出：
   python organize_by_month.py --root "D:/demo/source_dir" --yes --json-events
"""

from __future__ import annotations

import argparse
import errno
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List


EVENT_PREFIX = "__EVENT__ "


@dataclass
class RunStats:
    total_files: int = 0
    processed_files: int = 0
    moved_files: int = 0
    skipped_same_files: int = 0
    failed_files: int = 0
    removed_empty_dirs: int = 0


def emit_log(message: str, *, json_events: bool = False) -> None:
    if json_events:
        print(
            f"{EVENT_PREFIX}{json.dumps({'event': 'log', 'message': message}, ensure_ascii=True)}",
            flush=True,
        )
        return
    print(message, flush=True)


def emit_event(event_type: str, payload: dict, *, json_events: bool = False) -> None:
    if not json_events:
        return
    body = {"event": event_type, **payload}
    print(f"{EVENT_PREFIX}{json.dumps(body, ensure_ascii=True)}", flush=True)


def confirm_operation(root_dir: Path) -> bool:
    prompt = (
        f"将整理目录：{root_dir}\n"
        "此操作将移动所有文件到月份文件夹，是否继续？[y/N]: "
    )
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def month_folder_name(file_path: Path) -> str:
    modified_dt = datetime.fromtimestamp(file_path.stat().st_mtime)
    return f"{modified_dt.year:04d}_{modified_dt.month:02d}"


def iter_all_files(root_dir: Path) -> List[Path]:
    return [p for p in root_dir.rglob("*") if p.is_file()]


def unique_target_path(target_dir: Path, file_name: str) -> tuple[Path, bool]:
    candidate = target_dir / file_name
    if not candidate.exists():
        return candidate, False

    stem = Path(file_name).stem
    suffix = Path(file_name).suffix
    index = 1
    while True:
        candidate = target_dir / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate, True
        index += 1


def move_file_safely(source_path: Path, target_path: Path) -> None:
    if target_path.exists():
        raise FileExistsError(f"目标文件已存在，拒绝覆盖：{target_path}")

    try:
        source_path.replace(target_path)
        return
    except OSError as exc:
        # EXDEV 常见于跨分区/跨设备移动，回退到复制+校验+删除源文件。
        if exc.errno not in {errno.EXDEV, errno.EACCES, errno.EPERM}:
            raise

    source_size = source_path.stat().st_size
    shutil.copy2(str(source_path), str(target_path))

    target_size = target_path.stat().st_size
    if source_size != target_size:
        try:
            target_path.unlink()
        except OSError:
            pass
        raise IOError(
            f"复制校验失败，源文件大小 {source_size}，目标文件大小 {target_size}：{source_path}"
        )

    source_path.unlink()


def remove_empty_subdirs(
    root_dir: Path,
    *,
    logger: Callable[[str], None],
    json_events: bool = False,
) -> int:
    removed_count = 0
    subdirs = [p for p in root_dir.rglob("*") if p.is_dir()]
    subdirs.sort(key=lambda p: len(p.parts), reverse=True)

    for directory in subdirs:
        try:
            directory.rmdir()
            removed_count += 1
            message = f"删除空目录：{directory}"
            logger(message)
            emit_event("empty_dir_removed", {"path": str(directory)}, json_events=json_events)
        except OSError:
            # 非空目录或权限问题都跳过，不中断流程。
            continue
        except Exception as exc:  # pragma: no cover - 防御性分支
            logger(f"删除目录失败：{directory}，错误：{exc}")
            emit_event(
                "error",
                {"path": str(directory), "message": str(exc), "stage": "remove_empty_dir"},
                json_events=json_events,
            )
    return removed_count


def organize_files(
    root_dir: Path,
    *,
    remove_empty_dirs: bool,
    json_events: bool = False,
) -> RunStats:
    stats = RunStats()
    backup_root = root_dir.parent / f"{root_dir.name}_YM"
    backup_root.mkdir(parents=True, exist_ok=True)

    files = iter_all_files(root_dir)
    stats.total_files = len(files)
    emit_log(f"待处理文件总数：{stats.total_files}", json_events=json_events)
    emit_event(
        "start",
        {"total": stats.total_files, "backup_root": str(backup_root)},
        json_events=json_events,
    )

    for file_path in files:
        stats.processed_files += 1
        try:
            month_dir_name = month_folder_name(file_path)
            month_dir = backup_root / month_dir_name
            month_dir.mkdir(parents=True, exist_ok=True)

            base_target_path = month_dir / file_path.name
            if base_target_path.exists():
                source_size = file_path.stat().st_size
                target_size = base_target_path.stat().st_size
                if source_size == target_size:
                    relative_source = file_path.relative_to(root_dir)
                    display_source = str(Path(root_dir.name) / relative_source)
                    skip_message = (
                        f"[提示] 同名且同大小，视为同文件并跳过：{display_source} "
                        f"(size={source_size})"
                    )
                    emit_log(skip_message, json_events=json_events)
                    emit_event(
                        "duplicate_skipped",
                        {
                            "source": str(file_path),
                            "target": str(base_target_path),
                            "size": source_size,
                            "processed": stats.processed_files,
                            "total": stats.total_files,
                        },
                        json_events=json_events,
                    )
                    stats.skipped_same_files += 1
                    continue

            target_path, renamed = unique_target_path(month_dir, file_path.name)
            if renamed:
                emit_log(
                    f"[警告] 发现重名，自动重命名：{file_path.name} -> {target_path.name}",
                    json_events=json_events,
                )
                emit_event(
                    "name_conflict",
                    {
                        "original_name": file_path.name,
                        "renamed_to": target_path.name,
                        "month": month_dir_name,
                    },
                    json_events=json_events,
                )

            move_file_safely(file_path, target_path)
            stats.moved_files += 1

            relative_source = file_path.relative_to(root_dir)
            display_source = str(Path(root_dir.name) / relative_source)
            message = f"正在移动 {display_source} -> {month_dir_name}"
            emit_log(message, json_events=json_events)
            emit_event(
                "file_moved",
                {
                    "source": str(file_path),
                    "target": str(target_path),
                    "processed": stats.processed_files,
                    "total": stats.total_files,
                },
                json_events=json_events,
            )
        except Exception as exc:
            stats.failed_files += 1
            error_message = f"移动失败：{file_path}，错误：{exc}"
            emit_log(error_message, json_events=json_events)
            emit_event(
                "error",
                {
                    "path": str(file_path),
                    "message": str(exc),
                    "stage": "move_file",
                    "processed": stats.processed_files,
                    "total": stats.total_files,
                },
                json_events=json_events,
            )
        finally:
            emit_event(
                "progress",
                {"processed": stats.processed_files, "total": stats.total_files},
                json_events=json_events,
            )

    if remove_empty_dirs:
        stats.removed_empty_dirs = remove_empty_subdirs(
            root_dir, logger=lambda msg: emit_log(msg, json_events=json_events), json_events=json_events
        )

    summary = (
        f"整理完成：共 {stats.total_files} 个文件，成功 {stats.moved_files}，"
        f"跳过同文件 {stats.skipped_same_files}，失败 {stats.failed_files}，"
        f"删除空目录 {stats.removed_empty_dirs}。"
    )
    emit_log(summary, json_events=json_events)
    emit_event(
        "done",
        {
            "total": stats.total_files,
            "moved": stats.moved_files,
            "skipped_same_files": stats.skipped_same_files,
            "failed": stats.failed_files,
            "removed_empty_dirs": stats.removed_empty_dirs,
        },
        json_events=json_events,
    )
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将目录中的文件按修改时间（月）整理到同级备份目录。")
    parser.add_argument("--root", required=True, help="待整理的根目录路径")
    parser.add_argument(
        "--remove-empty-dirs",
        action="store_true",
        help="处理后删除原根目录下已变为空目录的子目录",
    )
    parser.add_argument("--yes", action="store_true", help="跳过交互确认，直接执行")
    parser.add_argument(
        "--json-events",
        action="store_true",
        help="仅输出结构化事件（供 GUI 解析）",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    root_dir = Path(args.root).expanduser().resolve()
    if not root_dir.exists():
        print(f"错误：目录不存在：{root_dir}", file=sys.stderr)
        return 2
    if not root_dir.is_dir():
        print(f"错误：不是目录：{root_dir}", file=sys.stderr)
        return 2

    if not args.yes:
        if not confirm_operation(root_dir):
            print("已取消操作。")
            return 0

    try:
        organize_files(
            root_dir,
            remove_empty_dirs=args.remove_empty_dirs,
            json_events=args.json_events,
        )
        return 0
    except Exception as exc:
        print(f"发生未预期错误：{exc}", file=sys.stderr)
        if args.json_events:
            emit_event(
                "fatal",
                {"message": str(exc), "stage": "main"},
                json_events=True,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
