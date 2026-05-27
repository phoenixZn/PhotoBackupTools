"""
目录整理 CLI 工具（支持按年 / 按月 / 按日）。

使用说明（示例）：
1) 交互确认后执行（默认按月）：
   python organize_by_month.py --root "D:/demo/source_dir"
2) 跳过确认并删除空目录（按年）：
   python organize_by_month.py --root "D:/demo/source_dir" --yes --remove-empty-dirs --group-by year
3) 指定输出目录（跳过默认后缀命名）：
   python organize_by_month.py --root "D:/demo/source_dir" --output "D:/demo/archive" --yes
4) 给 GUI 使用的结构化事件输出（按日）：
   python organize_by_month.py --root "D:/demo/source_dir" --yes --json-events --group-by day
"""

from __future__ import annotations

import argparse
import errno
import json
import re
import shlex
import subprocess
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Literal


EVENT_PREFIX = "__EVENT__ "
GroupByMode = Literal["year", "month", "day"]

# 统一定义展示文案和输出目录后缀，避免 CLI/GUI/日志口径不一致。
GROUP_BY_LABELS: dict[GroupByMode, str] = {
    "year": "按年",
    "month": "按月",
    "day": "按日",
}
GROUP_BY_FOLDER_SUFFIX: dict[GroupByMode, str] = {
    "year": "Y",
    "month": "YM",
    "day": "YMD",
}


@dataclass
class RunStats:
    total_files: int = 0
    processed_files: int = 0
    filtered_by_whitelist: int = 0
    moved_files: int = 0
    skipped_same_files: int = 0
    renamed_files: int = 0
    failed_files: int = 0
    removed_empty_dirs: int = 0
    bucket_moved_counts: Dict[str, int] = field(default_factory=dict)


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


def resolve_backup_root(
    root_dir: Path,
    *,
    group_by: GroupByMode,
    output_dir: Path | None,
) -> Path:
    """解析文件归档根目录：指定有效输出目录时直接使用，否则按模式后缀命名。"""
    if output_dir is not None:
        resolved = output_dir.expanduser().resolve()
        if resolved.exists() and not resolved.is_dir():
            raise ValueError(f"输出目录不是有效目录：{resolved}")
        return resolved
    return root_dir.parent / f"{root_dir.name}_{GROUP_BY_FOLDER_SUFFIX[group_by]}"


def confirm_operation(
    root_dir: Path,
    *,
    group_by: GroupByMode,
    backup_root: Path,
) -> bool:
    mode_label = GROUP_BY_LABELS[group_by]
    prompt = (
        f"将整理目录：{root_dir}\n"
        f"输出目录：{backup_root}\n"
        f"整理模式：{mode_label}\n"
        "此操作将移动所有文件到目标时间目录，是否继续？[y/N]: "
    )
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def build_time_bucket_name(file_path: Path, *, group_by: GroupByMode) -> str:
    modified_dt = datetime.fromtimestamp(file_path.stat().st_mtime)
    # 分目录策略只依赖 mtime，便于保持性能和行为可预期。
    if group_by == "year":
        return f"{modified_dt.year:04d}"
    if group_by == "month":
        return f"{modified_dt.year:04d}_{modified_dt.month:02d}"
    return f"{modified_dt.year:04d}_{modified_dt.month:02d}_{modified_dt.day:02d}"


def iter_all_files(root_dir: Path) -> List[Path]:
    return [p for p in root_dir.rglob("*") if p.is_file()]


def parse_ext_whitelist(raw_text: str) -> set[str]:
    if not raw_text.strip():
        return set()

    items = re.split(r"[,\s;]+", raw_text.strip())
    normalized: set[str] = set()
    for item in items:
        token = item.strip().lower()
        if not token:
            continue
        if not token.startswith("."):
            token = f".{token}"
        normalized.add(token)
    return normalized


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


def build_run_log_file_name(prefix: str, run_stamp: str, operation_count: int) -> str:
    return f"{prefix}_{run_stamp}_ops{operation_count}.log"


def save_run_logs(
    *,
    root_dir: Path,
    backup_root: Path,
    run_stamp: str,
    total_ops: int,
    all_log_lines: List[str],
    bucket_move_logs: Dict[str, List[str]],
) -> tuple[Path, List[Path]]:
    root_log_file = root_dir / build_run_log_file_name("ym_organize", run_stamp, total_ops)
    root_log_file.write_text("\n".join(all_log_lines) + "\n", encoding="utf-8-sig")

    bucket_log_files: List[Path] = []
    for bucket_name, lines in bucket_move_logs.items():
        if not lines:
            continue
        bucket_dir = backup_root / bucket_name
        month_log_file = bucket_dir / build_run_log_file_name(
            "ym_moves", run_stamp, len(lines)
        )
        month_log_file.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        bucket_log_files.append(month_log_file)
    return root_log_file, bucket_log_files


def build_command_line_text(argv: List[str]) -> str:
    if sys.platform.startswith("win"):
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def organize_files(
    root_dir: Path,
    *,
    group_by: GroupByMode,
    output_dir: Path | None,
    remove_empty_dirs: bool,
    ext_whitelist: set[str],
    command_line_text: str,
    parsed_args_text: str,
    json_events: bool = False,
) -> RunStats:
    stats = RunStats()
    backup_root = resolve_backup_root(root_dir, group_by=group_by, output_dir=output_dir)
    backup_root.mkdir(parents=True, exist_ok=True)
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_log_lines: List[str] = []
    bucket_move_logs: Dict[str, List[str]] = defaultdict(list)
    bucket_moved_counts: Dict[str, int] = defaultdict(int)

    def log_line(message: str) -> None:
        all_log_lines.append(message)
        emit_log(message, json_events=json_events)

    log_line(f"CommandLine: {command_line_text}")
    log_line(f"Arguments: {parsed_args_text}")
    log_line(f"GroupBy: {group_by} ({GROUP_BY_LABELS[group_by]})")
    log_line(f"BackupRoot: {backup_root}")

    files = iter_all_files(root_dir)
    stats.total_files = len(files)
    log_line(f"待处理文件总数：{stats.total_files}")
    if ext_whitelist:
        log_line(
            "文件类型白名单已启用："
            + ", ".join(sorted(ext_whitelist))
            + "（仅移动命中后缀）"
        )
    else:
        log_line("文件类型白名单为空：将整理全部文件。")
    emit_event(
        "start",
        {
            "total": stats.total_files,
            "backup_root": str(backup_root),
            "ext_whitelist": sorted(ext_whitelist),
            "group_by": group_by,
        },
        json_events=json_events,
    )

    for file_path in files:
        stats.processed_files += 1
        file_ext = file_path.suffix.lower()
        if ext_whitelist and file_ext not in ext_whitelist:
            stats.filtered_by_whitelist += 1
            emit_event(
                "progress",
                {"processed": stats.processed_files, "total": stats.total_files},
                json_events=json_events,
            )
            continue
        try:
            bucket_dir_name = build_time_bucket_name(file_path, group_by=group_by)
            bucket_dir = backup_root / bucket_dir_name
            bucket_dir.mkdir(parents=True, exist_ok=True)
            relative_parent = file_path.parent.relative_to(root_dir)
            from_dir = "." if str(relative_parent) == "." else str(relative_parent)

            base_target_path = bucket_dir / file_path.name
            if base_target_path.exists():
                source_size = file_path.stat().st_size
                target_size = base_target_path.stat().st_size
                if source_size == target_size:
                    skip_message = (
                        f"Skip: [{file_path.name}] -> [{bucket_dir_name}]  :   "
                        f"From:{from_dir}\\  跳过相同文件(size={source_size})"
                    )
                    log_line(skip_message)
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

            target_path, renamed = unique_target_path(bucket_dir, file_path.name)
            if renamed:
                stats.renamed_files += 1
                log_line(
                    f"[警告] 发现重名，自动重命名：{file_path.name} -> {target_path.name}",
                )
                emit_event(
                    "name_conflict",
                    {
                        "original_name": file_path.name,
                        "renamed_to": target_path.name,
                        "bucket": bucket_dir_name,
                    },
                    json_events=json_events,
                )

            move_file_safely(file_path, target_path)
            stats.moved_files += 1
            bucket_moved_counts[bucket_dir_name] += 1

            status_info = (
                f"renamed_to={target_path.name}" if renamed else "status=ok"
            )
            message = (
                f"Move: [{file_path.name}] -> [{bucket_dir_name}]  :   "
                f"From:{from_dir}\\ {status_info}"
            )
            log_line(message)
            bucket_move_logs[bucket_dir_name].append(message)
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
            bucket_dir_name = build_time_bucket_name(file_path, group_by=group_by)
            relative_parent = file_path.parent.relative_to(root_dir)
            from_dir = "." if str(relative_parent) == "." else str(relative_parent)
            error_message = (
                f"Fail: [{file_path.name}] -> [{bucket_dir_name}]  :   "
                f"From:{from_dir}\\  error={exc}"
            )
            log_line(error_message)
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
            root_dir,
            logger=log_line,
            json_events=json_events,
        )

    stats.bucket_moved_counts = dict(sorted(bucket_moved_counts.items(), key=lambda x: x[0]))
    summary_line = (
        "Summary: "
        f"success={stats.moved_files}, "
        f"failed={stats.failed_files}, "
        f"skipped={stats.skipped_same_files}, "
        f"filtered_by_whitelist={stats.filtered_by_whitelist}, "
        f"renamed={stats.renamed_files}, "
        f"removed_empty_dirs={stats.removed_empty_dirs}, "
        f"total={stats.total_files}"
    )
    log_line(summary_line)

    for bucket_name, count in stats.bucket_moved_counts.items():
        log_line(f"SummaryByBucket: [{bucket_name}] moved_in={count}")

    root_log_file, bucket_log_files = save_run_logs(
        root_dir=root_dir,
        backup_root=backup_root,
        run_stamp=run_stamp,
        total_ops=stats.total_files,
        all_log_lines=all_log_lines,
        bucket_move_logs=dict(bucket_move_logs),
    )
    log_line(f"LogSaved: root={root_log_file.name}")
    for month_log_file in sorted(bucket_log_files):
        log_line(f"LogSaved: bucket={month_log_file.parent.name}/{month_log_file.name}")

    emit_event(
        "done",
        {
            "total": stats.total_files,
            "moved": stats.moved_files,
            "skipped_same_files": stats.skipped_same_files,
            "filtered_by_whitelist": stats.filtered_by_whitelist,
            "renamed": stats.renamed_files,
            "failed": stats.failed_files,
            "removed_empty_dirs": stats.removed_empty_dirs,
            "group_by": group_by,
            "bucket_moved_counts": stats.bucket_moved_counts,
            "month_moved_counts": stats.bucket_moved_counts,
        },
        json_events=json_events,
    )
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将目录中的文件按修改时间（年/月/日）整理到同级备份目录。")
    parser.add_argument("--root", required=True, help="待整理的根目录路径")
    parser.add_argument(
        "--output",
        default="",
        help=(
            "输出目录（可选）。指定有效目录时直接使用该路径；"
            "未指定时默认使用 {根目录名}_{Y|YM|YMD} 同级目录"
        ),
    )
    parser.add_argument(
        "--group-by",
        default="month",
        choices=["year", "month", "day"],
        help="分目录模式：year=按年，month=按月（默认），day=按日",
    )
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
    parser.add_argument(
        "--ext-whitelist",
        default="",
        help=(
            "仅移动白名单后缀（为空则整理全部）。"
            "支持空格/逗号/分号分隔，如 '.jpg .png,.mp4'"
        ),
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

    output_dir: Path | None = None
    output_text = args.output.strip()
    if output_text:
        output_dir = Path(output_text)

    try:
        backup_root = resolve_backup_root(
            root_dir,
            group_by=args.group_by,
            output_dir=output_dir,
        )
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if not args.yes:
        if not confirm_operation(
            root_dir,
            group_by=args.group_by,
            backup_root=backup_root,
        ):
            print("已取消操作。")
            return 0

    command_line_text = build_command_line_text([sys.executable, *sys.argv])
    parsed_args_text = json.dumps(vars(args), ensure_ascii=False)
    ext_whitelist = parse_ext_whitelist(args.ext_whitelist)

    try:
        organize_files(
            root_dir,
            remove_empty_dirs=args.remove_empty_dirs,
            group_by=args.group_by,
            output_dir=output_dir,
            ext_whitelist=ext_whitelist,
            command_line_text=command_line_text,
            parsed_args_text=parsed_args_text,
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
