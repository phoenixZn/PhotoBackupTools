"""
子目录文件类型统计 CLI 工具。

使用说明（示例）：
1) 交互确认后执行：
   python stat_subdir_by_type.py --root "D:/demo/source_dir"
2) 跳过确认：
   python stat_subdir_by_type.py --root "D:/demo/source_dir" --yes
3) 给 GUI 使用的结构化事件输出：
   python stat_subdir_by_type.py --root "D:/demo/source_dir" --yes --json-events
4) 执行前清理旧的统计文件：
   python stat_subdir_by_type.py --root "D:/demo/source_dir" --yes --remove-old-stats
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List


EVENT_PREFIX = "__EVENT__ "
STAT_FILE_PREFIX = "统计_"
NO_EXT_LABEL = "无后缀"
# Windows 文件名非法字符
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')


@dataclass
class SubdirStats:
    subdir_name: str
    subdir_path: Path
    total_files: int = 0
    ext_counts: Dict[str, int] = field(default_factory=dict)
    oldest_mtime: str | None = None
    newest_mtime: str | None = None
    stat_file_path: Path | None = None
    failed: bool = False
    error_message: str = ""


@dataclass
class RunStats:
    total_subdirs: int = 0
    processed_subdirs: int = 0
    generated_files: int = 0
    skipped_empty_subdirs: int = 0
    failed_subdirs: int = 0
    removed_old_stats: int = 0


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


def sanitize_path_segment(name: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub("_", name.strip())
    return cleaned or "_"


def ext_token(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if not suffix:
        return NO_EXT_LABEL
    return suffix.lstrip(".")


def format_mtime(file_path: Path) -> str:
    modified_dt = datetime.fromtimestamp(file_path.stat().st_mtime)
    return f"{modified_dt.year:04d}_{modified_dt.month:02d}_{modified_dt.day:02d}"


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


def iter_subdir_files(subdir: Path) -> List[Path]:
    return [p for p in subdir.rglob("*") if p.is_file()]


def iter_immediate_subdirs(root_dir: Path) -> List[Path]:
    return sorted(
        [p for p in root_dir.iterdir() if p.is_dir()],
        key=lambda p: p.name.lower(),
    )


def build_stat_filename(subdir_name: str, ext_counts: Dict[str, int]) -> str:
    safe_name = sanitize_path_segment(subdir_name)
    parts = [f"{STAT_FILE_PREFIX}{safe_name}"]
    for ext, count in sorted(ext_counts.items(), key=lambda x: (x[0].lower(), x[0])):
        parts.append(f"{sanitize_path_segment(ext)}({count})")
    filename = "_".join(parts) + ".txt"
    if len(filename) > 240:
        # 过长时仅保留子目录名与总数，避免 Windows 路径限制。
        total = sum(ext_counts.values())
        filename = f"{STAT_FILE_PREFIX}{safe_name}_共{total}项.txt"
    return filename


def build_stat_content(
    *,
    root_dir: Path,
    subdir: Path,
    files: List[Path],
    ext_counts: Dict[str, int],
    run_stamp: str,
    command_line_text: str,
    parsed_args_text: str,
) -> str:
    lines: List[str] = []
    lines.append("子目录文件统计报告")
    lines.append("=" * 40)
    lines.append(f"根目录：{root_dir}")
    lines.append(f"子目录：{subdir}")
    lines.append(f"生成时间：{run_stamp}")
    lines.append(f"命令行：{command_line_text}")
    lines.append(f"参数：{parsed_args_text}")
    lines.append("")
    lines.append(f"文件总数：{len(files)}")
    lines.append("")
    lines.append("扩展名数量统计：")
    if ext_counts:
        for ext, count in sorted(ext_counts.items(), key=lambda x: (-x[1], x[0].lower())):
            lines.append(f"  - {ext}: {count}")
    else:
        lines.append("  （无文件）")
    lines.append("")

    if files:
        mtimes = [format_mtime(p) for p in files]
        oldest = min(mtimes)
        newest = max(mtimes)
        lines.append("修改日期范围（按文件 mtime，格式 YYYY_MM_DD）：")
        lines.append(f"  最旧：{oldest}")
        lines.append(f"  最新：{newest}")
        lines.append("")
        lines.append("各扩展名修改日期范围：")
        by_ext: Dict[str, List[str]] = defaultdict(list)
        for file_path in files:
            by_ext[ext_token(file_path)].append(format_mtime(file_path))
        for ext, dates in sorted(by_ext.items(), key=lambda x: x[0].lower()):
            lines.append(f"  - {ext}: 最旧 {min(dates)}，最新 {max(dates)}（{len(dates)} 个文件）")
    else:
        lines.append("修改日期范围：无文件，无法计算。")

    lines.append("")
    lines.append("说明：统计包含该子目录下所有层级的文件；修改时间取自文件系统 mtime。")
    return "\n".join(lines) + "\n"


def unique_target_path(target_dir: Path, file_name: str) -> Path:
    candidate = target_dir / file_name
    if not candidate.exists():
        return candidate

    stem = Path(file_name).stem
    suffix = Path(file_name).suffix
    index = 1
    while True:
        candidate = target_dir / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def remove_old_stat_files(root_dir: Path) -> int:
    removed = 0
    for path in root_dir.iterdir():
        if path.is_file() and path.name.startswith(STAT_FILE_PREFIX) and path.suffix.lower() == ".txt":
            path.unlink()
            removed += 1
    return removed


def confirm_operation(root_dir: Path, subdir_count: int) -> bool:
    prompt = (
        f"将在目录中生成统计文件：{root_dir}\n"
        f"待统计子目录数量：{subdir_count}\n"
        "每个子目录对应一个 统计_*.txt 文件，是否继续？[y/N]: "
    )
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def build_command_line_text(argv: List[str]) -> str:
    if sys.platform.startswith("win"):
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def analyze_subdir(
    subdir: Path,
    *,
    ext_whitelist: set[str],
) -> tuple[List[Path], Dict[str, int]]:
    all_files = iter_subdir_files(subdir)
    if not ext_whitelist:
        files = all_files
    else:
        files = [p for p in all_files if p.suffix.lower() in ext_whitelist]

    ext_counts: Dict[str, int] = defaultdict(int)
    for file_path in files:
        ext_counts[ext_token(file_path)] += 1
    return files, dict(ext_counts)


def generate_subdir_stat(
    *,
    root_dir: Path,
    subdir: Path,
    ext_whitelist: set[str],
    run_stamp: str,
    command_line_text: str,
    parsed_args_text: str,
) -> SubdirStats:
    result = SubdirStats(subdir_name=subdir.name, subdir_path=subdir)
    try:
        files, ext_counts = analyze_subdir(subdir, ext_whitelist=ext_whitelist)
        result.total_files = len(files)
        result.ext_counts = ext_counts

        if files:
            mtimes = [format_mtime(p) for p in files]
            result.oldest_mtime = min(mtimes)
            result.newest_mtime = max(mtimes)

        filename = build_stat_filename(subdir.name, ext_counts)
        target_path = unique_target_path(root_dir, filename)
        content = build_stat_content(
            root_dir=root_dir,
            subdir=subdir,
            files=files,
            ext_counts=ext_counts,
            run_stamp=run_stamp,
            command_line_text=command_line_text,
            parsed_args_text=parsed_args_text,
        )
        target_path.write_text(content, encoding="utf-8-sig")
        result.stat_file_path = target_path
    except Exception as exc:
        result.failed = True
        result.error_message = str(exc)
    return result


def run_statistics(
    root_dir: Path,
    *,
    ext_whitelist: set[str],
    remove_old_stats: bool,
    command_line_text: str,
    parsed_args_text: str,
    json_events: bool = False,
) -> RunStats:
    stats = RunStats()
    run_stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def log_line(message: str) -> None:
        emit_log(message, json_events=json_events)

    log_line(f"CommandLine: {command_line_text}")
    log_line(f"Arguments: {parsed_args_text}")
    log_line(f"RootDir: {root_dir}")

    if remove_old_stats:
        stats.removed_old_stats = remove_old_stat_files(root_dir)
        log_line(f"已清理旧统计文件：{stats.removed_old_stats} 个")

    subdirs = iter_immediate_subdirs(root_dir)
    stats.total_subdirs = len(subdirs)
    log_line(f"待统计子目录数量：{stats.total_subdirs}")

    if ext_whitelist:
        log_line(
            "文件类型白名单已启用："
            + ", ".join(sorted(ext_whitelist))
            + "（仅统计命中后缀）"
        )
    else:
        log_line("文件类型白名单为空：统计子目录内全部文件。")

    emit_event(
        "start",
        {
            "total": stats.total_subdirs,
            "ext_whitelist": sorted(ext_whitelist),
        },
        json_events=json_events,
    )

    for subdir in subdirs:
        stats.processed_subdirs += 1
        sub_result = generate_subdir_stat(
            root_dir=root_dir,
            subdir=subdir,
            ext_whitelist=ext_whitelist,
            run_stamp=run_stamp,
            command_line_text=command_line_text,
            parsed_args_text=parsed_args_text,
        )

        if sub_result.failed:
            stats.failed_subdirs += 1
            log_line(f"[错误] {subdir.name}：{sub_result.error_message}")
            emit_event(
                "error",
                {
                    "path": str(subdir),
                    "message": sub_result.error_message,
                    "stage": "generate_stat",
                },
                json_events=json_events,
            )
        elif sub_result.total_files == 0:
            stats.skipped_empty_subdirs += 1
            stats.generated_files += 1
            log_line(
                f"已生成（空目录）：{sub_result.stat_file_path.name} <- {subdir.name}"
            )
        else:
            stats.generated_files += 1
            range_text = f"{sub_result.oldest_mtime} ~ {sub_result.newest_mtime}"
            log_line(
                f"已生成：{sub_result.stat_file_path.name} <- {subdir.name} "
                f"（{sub_result.total_files} 个文件，{range_text}）"
            )

        emit_event(
            "progress",
            {"processed": stats.processed_subdirs, "total": stats.total_subdirs},
            json_events=json_events,
        )

    summary = (
        "Summary: "
        f"subdirs={stats.total_subdirs}, "
        f"generated={stats.generated_files}, "
        f"empty={stats.skipped_empty_subdirs}, "
        f"failed={stats.failed_subdirs}, "
        f"removed_old_stats={stats.removed_old_stats}"
    )
    log_line(summary)

    emit_event(
        "done",
        {
            "total": stats.total_subdirs,
            "generated": stats.generated_files,
            "empty_subdirs": stats.skipped_empty_subdirs,
            "failed": stats.failed_subdirs,
            "removed_old_stats": stats.removed_old_stats,
        },
        json_events=json_events,
    )
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="遍历根目录下每个子目录，在根目录生成按文件类型汇总的统计 txt。"
    )
    parser.add_argument("--root", required=True, help="待统计的根目录路径")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认，直接执行")
    parser.add_argument(
        "--json-events",
        action="store_true",
        help="仅输出结构化事件（供 GUI 解析）",
    )
    parser.add_argument(
        "--remove-old-stats",
        action="store_true",
        help="执行前删除根目录下已有的 统计_*.txt 文件",
    )
    parser.add_argument(
        "--ext-whitelist",
        default="",
        help=(
            "仅统计白名单后缀（为空则统计全部）。"
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

    subdirs = iter_immediate_subdirs(root_dir)
    if not args.yes:
        if not confirm_operation(root_dir, len(subdirs)):
            print("已取消操作。")
            return 0

    command_line_text = build_command_line_text([sys.executable, *sys.argv])
    parsed_args_text = json.dumps(vars(args), ensure_ascii=False)
    ext_whitelist = parse_ext_whitelist(args.ext_whitelist)

    try:
        run_statistics(
            root_dir,
            ext_whitelist=ext_whitelist,
            remove_old_stats=args.remove_old_stats,
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
