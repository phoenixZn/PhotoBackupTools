"""
ADB 按日期范围备份脚本（命令行版）。

功能概览：
1) 通过 `adb shell find + stat` 扫描手机目录下的文件元数据（路径、大小、mtime）。
2) 依据日期范围与扩展名规则筛选出待备份文件。
3) 逐个执行 `adb pull` 拉取到本地目录，并记录详细日志。
4) 统计传输结果（成功/覆盖/失败），并输出平均传输速率。

设计要点：
- 尽量复用 adb 能力，不引入复杂依赖。
- 元数据与传输分离，便于定位问题。
- 日志兼顾“人可读”与“机器可解析”。
"""

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, List, Optional, Sequence, Tuple


@dataclass
class RemoteFile:
    """描述单个待备份文件的核心信息。"""

    # 手机上的绝对路径（adb pull 的源路径）。
    remote_path: str
    # 相对 phone_dir 的路径，用于在 PC 侧保持目录结构。
    rel_path: str
    # 文件大小（字节），来自远端 stat。
    size: int
    # 修改时间（Unix 时间戳，秒）。
    mtime_epoch: int
    # 修改时间（本地时区 datetime，便于日志阅读）。
    mtime_local: dt.datetime


@dataclass
class SyncStats:
    """保存单次同步的关键统计结果。"""

    copied: int
    overwritten: int
    failed: int
    successful_mtimes: List[dt.datetime]


DEFAULT_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".heic",
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".3gp",
}


def parse_date(raw: str) -> dt.date:
    """解析日期参数，兼容三种常见格式。"""

    # 同时兼容 `2026.04.30` / `2026-04-30` / `2026/04/30`。
    for fmt in ("%Y.%m.%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Invalid date '{raw}'. Use YYYY.MM.DD or YYYY-MM-DD or YYYY/MM/DD."
    )


def run_cmd(command: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """
    统一执行外部命令并收集输出。

    - 强制 UTF-8 + replace，避免设备端输出异常编码导致脚本中断。
    - 默认 check=True；调用方可按需关闭并自行处理 returncode。
    """
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def ensure_adb_ready() -> None:
    """
    检查 adb 与设备状态。

    失败时抛出带上下文的异常，方便 GUI 或命令行直接提示排障信息。
    """
    # 先确保 adb 命令本身可用。
    run_cmd(["adb", "version"])
    # 再检查是否至少有一台已授权设备处于 device 状态。
    state_result = run_cmd(["adb", "get-state"], check=False)
    state_text = (state_result.stdout or "").strip().lower()
    if state_result.returncode == 0 and "device" in state_text:
        return

    # 失败时额外补充 `adb devices` 输出，便于判断未授权/离线等状态。
    devices_result = run_cmd(["adb", "devices"], check=False)
    devices_text = (devices_result.stdout or "").strip()
    err_text = (state_result.stderr or "").strip()
    raise RuntimeError(
        "ADB device not ready. Check USB cable, enable USB debugging, and allow RSA authorization on phone.\n"
        f"adb get-state stderr: {err_text or '(empty)'}\n"
        f"adb devices output:\n{devices_text or '(empty)'}"
    )


def build_find_with_stat(phone_dir: str, stat_bin: str) -> str:
    """构造在设备端执行的 find+stat shell 命令。"""
    # 单引号转义，避免路径中含 `'` 破坏 shell 语法。
    escaped = phone_dir.replace("'", "'\"'\"'")
    return (
        f"find '{escaped}' -type f -exec {stat_bin} -c '%Y|%s|%n' {{}} \\;"
    )


def fetch_remote_metadata(phone_dir: str) -> List[Tuple[str, int, int]]:
    """
    扫描手机目录并返回元数据列表。

    返回值元素格式：`(remote_path, size, mtime_epoch)`。
    """
    # 不同 Android ROM 的可用工具不一致，按兼容顺序尝试。
    candidates = ("stat", "toybox stat", "busybox stat")
    last_err: Optional[Exception] = None

    for stat_cmd in candidates:
        command = build_find_with_stat(phone_dir, stat_cmd)
        try:
            completed = run_cmd(["adb", "shell", command], check=True)
            items: List[Tuple[str, int, int]] = []
            for line in completed.stdout.splitlines():
                row = line.strip()
                # 只处理符合 `%Y|%s|%n` 格式的行。
                if not row or "|" not in row:
                    continue
                parts = row.split("|", 2)
                if len(parts) != 3:
                    continue
                mtime_raw, size_raw, remote_path = parts
                try:
                    items.append((remote_path.strip(), int(size_raw), int(mtime_raw)))
                except ValueError:
                    # 遇到脏数据时跳过该行，不影响整体任务。
                    continue
            if items:
                return items
        except Exception as exc:  # noqa: BLE001
            # 保留最后一次异常，所有候选都失败后统一抛出。
            last_err = exc
            continue

    raise RuntimeError(
        "Failed to read file metadata from device via adb shell stat/toybox/busybox."
    ) from last_err


def normalize_root(path: str) -> str:
    """标准化目录路径：统一斜杠并去掉末尾 `/`。"""
    p = path.replace("\\", "/").rstrip("/")
    return p if p else "/"


def relative_remote_path(remote_path: str, root: str) -> str:
    """
    将绝对 remote_path 转成相对 root 的路径。

    若不在 root 下，则退化为文件名，防止意外路径导致报错中断。
    """
    rp = normalize_root(remote_path)
    root_n = normalize_root(root)
    if rp == root_n:
        return ""
    prefix = root_n + "/"
    if rp.startswith(prefix):
        return rp[len(prefix) :]
    return PurePosixPath(rp).name


def has_allowed_extension(remote_path: str, extensions: Optional[set[str]]) -> bool:
    """检查扩展名是否在允许集合内；`extensions=None` 表示全放行。"""
    if not extensions:
        return True
    suffix = PurePosixPath(remote_path).suffix.lower()
    return suffix in extensions


def filter_files(
    rows: Sequence[Tuple[str, int, int]],
    phone_dir: str,
    start_date: dt.date,
    end_date: dt.date,
    extensions: Optional[set[str]],
) -> List[RemoteFile]:
    """
    按扩展名与日期范围筛选文件，并构造 `RemoteFile` 列表。

    注意：日期比较使用文件 mtime 的本地日期部分（闭区间）。
    """
    selected: List[RemoteFile] = []
    for remote_path, size, mtime_epoch in rows:
        if not has_allowed_extension(remote_path, extensions):
            continue
        mtime_local = dt.datetime.fromtimestamp(mtime_epoch)
        mdate = mtime_local.date()
        if not (start_date <= mdate <= end_date):
            continue
        rel_path = relative_remote_path(remote_path, phone_dir)
        if not rel_path:
            continue
        selected.append(
            RemoteFile(
                remote_path=remote_path,
                rel_path=rel_path,
                size=size,
                mtime_epoch=mtime_epoch,
                mtime_local=mtime_local,
            )
        )
    return selected


def make_log_file(base_dir: Path) -> Path:
    """创建并返回日志文件路径（按时间戳命名）。"""
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return logs_dir / f"backup_{now}.log"


def format_rate(bps: float) -> str:
    """将字节每秒转换成易读文本（B/s, KB/s, MB/s, GB/s）。"""
    if bps <= 0:
        return "0 B/s"
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    value = bps
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B/s":
                return f"{value:.0f} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return "0 B/s"


def build_sync_record_path(pc_dir: Path) -> Path:
    """生成目标目录同级的同步记录文件路径。"""
    return pc_dir.parent / f"{pc_dir.name}_SyncRecord.txt"


def parse_sync_record_json(record_path: Path) -> dict[str, Any]:
    """读取旧的同步记录；损坏时备份原文件并返回空结构。"""
    if not record_path.exists():
        return {}
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
        raise ValueError("record json root must be object")
    except Exception as exc:  # noqa: BLE001
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = record_path.with_name(f"{record_path.stem}_{stamp}.bad.bak")
        try:
            record_path.replace(backup_path)
            print(
                f"Warning: sync record file is invalid and moved to backup: {backup_path} ({exc})"
            )
        except Exception as backup_exc:  # noqa: BLE001
            print(
                "Warning: sync record file is invalid and could not be moved to backup: "
                f"{backup_exc} ({exc})"
            )
        return {}


def build_sync_time_range(successful_mtimes: Sequence[dt.datetime]) -> dict[str, str] | None:
    """计算本次成功同步文件的时间范围。"""
    if not successful_mtimes:
        return None
    start_time = min(successful_mtimes)
    end_time = max(successful_mtimes)
    return {
        "start": start_time.isoformat(),
        "end": end_time.isoformat(),
    }


def write_sync_record_file(
    *,
    pc_dir: Path,
    command_date: dt.datetime,
    params_summary: dict[str, Any],
    successful_mtimes: Sequence[dt.datetime],
) -> Path:
    """合并写入同步记录文件。"""
    record_path = build_sync_record_path(pc_dir)
    payload = parse_sync_record_json(record_path)
    history = payload.get("history")
    if not isinstance(history, list):
        history = []

    time_range = build_sync_time_range(successful_mtimes)
    latest_file_time = max(successful_mtimes).isoformat() if successful_mtimes else None
    now_iso = dt.datetime.now().isoformat()

    history.append(
        {
            "commandDate": command_date.isoformat(),
            "paramsSummary": params_summary,
            "syncTimeRange": time_range,
            "syncFileCount": len(successful_mtimes),
        }
    )

    payload["targetFolderName"] = pc_dir.name
    payload["targetFolderPath"] = str(pc_dir)
    payload["latestFileTimeInFolder"] = latest_file_time
    payload["lastSyncDate"] = now_iso
    payload["history"] = history

    record_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return record_path


def sync_files(files: Sequence[RemoteFile], pc_dir: Path, log_path: Path) -> SyncStats:
    """
    执行实际文件同步，并写入详细日志与统计信息。

    速率策略：
    - `bytes_ok_total` 仅统计成功传输的文件大小，避免失败文件“虚增吞吐”。
    - 实时速率采用平均值（累计字节 / 累计耗时），并按 1s 节流输出。
    """
    # 结果计数器。
    copied = 0
    overwritten = 0
    failed = 0
    successful_mtimes: List[dt.datetime] = []
    # 成功传输的总字节数（用于平均速率计算）。
    bytes_ok_total = 0
    # `adb pull -a` 相关状态：优先保留 mtime，失败后可自动降级。
    preserve_mode = "auto"
    preserve_supported = True
    preserve_warned = False
    # 传输计时使用单调时钟，避免系统时间被修改带来的影响。
    transfer_started_ts = time.monotonic()
    last_rate_print_ts = transfer_started_ts

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"Start: {dt.datetime.now().isoformat()}\n")
        log.write(f"Target PC dir: {pc_dir}\n")
        log.write(f"Total selected files: {len(files)}\n\n")
        log.write("Timestamp strategy: prefer 'adb pull -a' (preserve file mtime), fallback to normal pull.\n")
        log.write("Note: EXIF capture time in photo content is unchanged by copying.\n\n")

        # 逐文件传输；保持相对目录结构到本地。
        for idx, rf in enumerate(files, start=1):
            local_path = pc_dir.joinpath(*PurePosixPath(rf.rel_path).parts)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            existed = local_path.exists()
            action = "OVERWRITE" if existed else "COPY"

            # 首选 `adb pull -a` 以保留文件 mtime。
            cmd = ["adb", "pull", "-a", rf.remote_path, str(local_path)] if preserve_supported else [
                "adb",
                "pull",
                rf.remote_path,
                str(local_path),
            ]
            proc = run_cmd(cmd, check=False)
            if preserve_supported and proc.returncode != 0:
                err_lower = proc.stderr.lower()
                # 某些 adb 版本不支持 -a；识别后仅降级一次并继续任务。
                if "unknown option" in err_lower or "invalid option" in err_lower:
                    preserve_supported = False
                    preserve_mode = "fallback_no_a"
                    if not preserve_warned:
                        print("Warning: your adb does not support 'pull -a'; fallback to normal pull.")
                        log.write("WARN\tadb pull -a not supported, fallback to normal pull.\n")
                        preserve_warned = True
                    proc = run_cmd(["adb", "pull", rf.remote_path, str(local_path)], check=False)

            if proc.returncode == 0:
                copied += 1
                successful_mtimes.append(rf.mtime_local)
                # rf.size 来自扫描元数据，不做额外 I/O 查询，开销低。
                bytes_ok_total += max(0, rf.size)
                if existed:
                    overwritten += 1
                print(f"[{idx}/{len(files)}] {action}: {rf.rel_path}")
                log.write(
                    f"OK\t{action}\t{rf.rel_path}\tmtime={rf.mtime_local.isoformat()}\tsize={rf.size}\tmode={preserve_mode}\n"
                )
            else:
                failed += 1
                print(f"[{idx}/{len(files)}] FAIL: {rf.rel_path}")
                log.write(
                    f"FAIL\t{rf.rel_path}\tstderr={proc.stderr.strip().replace(chr(10), ' | ')}\n"
                )

            # 实时速率走“低频节流”，避免 stdout 高频打印影响吞吐。
            now_ts = time.monotonic()
            should_emit_rate = (now_ts - last_rate_print_ts) >= 1.0 or idx == len(files)
            if should_emit_rate:
                # 防止极端情况下除零。
                elapsed_sec = max(0.001, now_ts - transfer_started_ts)
                avg_bps = bytes_ok_total / elapsed_sec
                # 固定前缀 `RATE\t` 供 GUI 低成本解析。
                print(f"RATE\tavg_bps={avg_bps:.2f}\thuman={format_rate(avg_bps)}")
                last_rate_print_ts = now_ts

        # 写入汇总统计，供后续审计/排障使用。
        transfer_elapsed_sec = max(0.001, time.monotonic() - transfer_started_ts)
        average_bps = bytes_ok_total / transfer_elapsed_sec
        log.write("\n")
        log.write(f"Copied/Updated: {copied}\n")
        log.write(f"Overwritten: {overwritten}\n")
        log.write(f"Failed: {failed}\n")
        log.write(f"Total bytes transferred: {bytes_ok_total}\n")
        log.write(f"Transfer elapsed seconds: {transfer_elapsed_sec:.3f}\n")
        log.write(f"Average transfer rate: {format_rate(average_bps)} ({average_bps:.2f} B/s)\n")
        log.write(f"Timestamp mode result: {preserve_mode}\n")
        log.write(f"End: {dt.datetime.now().isoformat()}\n")

    print("")
    print(f"Done. copied_or_updated={copied}, overwritten={overwritten}, failed={failed}")
    print(f"Average transfer rate: {format_rate(average_bps)} ({average_bps:.2f} B/s)")
    print(f"Log file: {log_path}")
    return SyncStats(
        copied=copied,
        overwritten=overwritten,
        failed=failed,
        successful_mtimes=successful_mtimes,
    )


def parse_args() -> argparse.Namespace:
    """定义并解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="Backup files from Android phone to PC by closed date range."
    )
    parser.add_argument("--phone-dir", required=True, help="Phone source directory, e.g. /sdcard/DCIM/Camera")
    parser.add_argument("--pc-dir", required=True, help="PC target backup directory")
    parser.add_argument("--start-date", required=True, type=parse_date, help="Start date inclusive")
    parser.add_argument("--end-date", required=True, type=parse_date, help="End date inclusive")
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="If set, do not limit extensions (default only media/photo extensions).",
    )
    parser.add_argument(
        "--write-sync-record",
        action="store_true",
        help="If set, write/update <target_dir_name>_SyncRecord.txt beside target directory.",
    )
    return parser.parse_args()


def main() -> int:
    """主流程：参数校验 -> 设备检查 -> 元数据扫描 -> 筛选 -> 同步 -> 退出码。"""
    try:
        args = parse_args()
        command_date = dt.datetime.now()
        if args.start_date > args.end_date:
            print("Error: start-date cannot be later than end-date.", file=sys.stderr)
            return 2

        # 目标目录不存在则自动创建，确保后续写入不会因路径缺失失败。
        pc_dir = Path(args.pc_dir).expanduser().resolve()
        pc_dir.mkdir(parents=True, exist_ok=True)

        print("Checking adb/device...")
        ensure_adb_ready()

        print("Reading remote file metadata...")
        rows = fetch_remote_metadata(args.phone_dir)
        print(f"Found {len(rows)} total files in phone directory.")
        params_summary = {
            "phoneDir": args.phone_dir,
            "startDate": args.start_date.isoformat(),
            "endDate": args.end_date.isoformat(),
            "allFiles": bool(args.all_files),
        }

        extensions = None if args.all_files else DEFAULT_EXTENSIONS
        selected = filter_files(
            rows=rows,
            phone_dir=args.phone_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            extensions=extensions,
        )
        print(f"Selected {len(selected)} files in date range [{args.start_date} .. {args.end_date}].")
        if not selected:
            print("No files matched. Exit.")
            if args.write_sync_record:
                try:
                    record_path = write_sync_record_file(
                        pc_dir=pc_dir,
                        command_date=command_date,
                        params_summary=params_summary,
                        successful_mtimes=[],
                    )
                    print(f"Sync record file updated: {record_path}")
                except Exception as exc:  # noqa: BLE001
                    print(f"Warning: failed to write sync record file: {exc}")
            return 0

        # 日志文件放在脚本同级 logs 目录，便于独立运行时定位。
        log_path = make_log_file(Path(__file__).resolve().parent)
        stats = sync_files(selected, pc_dir, log_path)
        if args.write_sync_record:
            try:
                record_path = write_sync_record_file(
                    pc_dir=pc_dir,
                    command_date=command_date,
                    params_summary=params_summary,
                    successful_mtimes=stats.successful_mtimes,
                )
                print(f"Sync record file updated: {record_path}")
            except Exception as exc:  # noqa: BLE001
                print(f"Warning: failed to write sync record file: {exc}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
