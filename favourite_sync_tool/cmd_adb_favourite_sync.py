"""
ADB favourite 文件推送脚本（命令行版）。

用途：
- 将 PC 目录中的“已收藏文件”同步到 Android 指定目录。
- 收藏来源有两种模式：
  1) 传入 `--push-all`：忽略清单文件，直接推送整个目录内容。
  2) 否则读取 `--list-file` 指定的清单文件：按行逐条推送文件。
- 若清单文件不存在或为空：提示后结束（退出码 0，不算错误）。

实现重点：
- 优先使用 `adb push --sync`，不支持时自动降级为 `adb push`。
- 全量模式使用“目录内容推送”写法，避免目标目录多套一层同名文件夹。
- 同步前会检查手机目标路径类型；若目标已存在且是文件，会明确报错提示处理方式。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Sequence


DEFAULT_LIST_FILE = "favourite_list.txt"


def run_cmd(command: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """
    统一执行外部命令并收集输出。

    说明：
    - 强制 UTF-8 解码并使用 replace，避免设备端乱码导致脚本中断。
    - 调用方可通过 `check=False` 自行处理返回码和错误信息。
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
    检查 adb 与设备是否就绪。

    校验顺序：
    1) `adb version`：确认 adb 命令可执行；
    2) `adb get-state`：确认至少有一个已授权设备处于 device 状态；
    3) 失败时附加 `adb devices` 输出，便于排障。
    """
    run_cmd(["adb", "version"])
    state_result = run_cmd(["adb", "get-state"], check=False)
    state_text = (state_result.stdout or "").strip().lower()
    if state_result.returncode == 0 and "device" in state_text:
        return

    devices_result = run_cmd(["adb", "devices"], check=False)
    devices_text = (devices_result.stdout or "").strip()
    err_text = (state_result.stderr or "").strip()
    raise RuntimeError(
        "ADB device not ready. Check USB debugging authorization.\n"
        f"adb get-state stderr: {err_text or '(empty)'}\n"
        f"adb devices output:\n{devices_text or '(empty)'}"
    )


def normalize_phone_dir(phone_dir: str) -> str:
    """标准化手机目录（统一分隔符，去掉末尾 /）。"""
    normalized = phone_dir.replace("\\", "/").rstrip("/")
    return normalized if normalized else "/"


def build_phone_dir_for_content_push(phone_dir: str) -> str:
    """构造目录内容推送路径，避免目标目录多出同名层。"""
    return f"{normalize_phone_dir(phone_dir)}/."


def get_remote_path_kind(phone_dir: str) -> str:
    """
    判断手机端目标路径类型。

    返回值：
    - "dir"：路径存在且是目录
    - "file"：路径存在但不是目录（通常是普通文件）
    - "missing"：路径不存在
    - "unknown"：无法可靠判断（例如 shell 执行异常）
    """
    normalized = normalize_phone_dir(phone_dir)
    escaped = normalized.replace("'", "'\"'\"'")
    probe = (
        f"if [ -d '{escaped}' ]; then echo __TYPE_DIR__;"
        f" elif [ -e '{escaped}' ]; then echo __TYPE_FILE__;"
        " else echo __TYPE_MISSING__; fi"
    )
    proc = run_cmd(["adb", "shell", probe], check=False)
    if proc.returncode != 0:
        return "unknown"
    out = (proc.stdout or "").strip()
    if "__TYPE_DIR__" in out:
        return "dir"
    if "__TYPE_FILE__" in out:
        return "file"
    if "__TYPE_MISSING__" in out:
        return "missing"
    return "unknown"


def ensure_remote_directory(phone_dir: str) -> None:
    """
    确保手机端目标目录存在，并处理“同名文件占位”的异常场景。

    关键保护：
    - 若目标路径已存在但类型是文件（非目录），直接报错并给出修复建议；
      否则继续执行 `mkdir -p`，保证后续 push 一定指向目录。
    """
    normalized = normalize_phone_dir(phone_dir)
    kind = get_remote_path_kind(normalized)
    if kind == "file":
        raise RuntimeError(
            "Remote target exists as a file, not a directory. "
            f"Please delete or rename it first: {normalized}"
        )

    escaped = normalized.replace("'", "'\"'\"'")
    proc = run_cmd(["adb", "shell", f"mkdir -p '{escaped}'"], check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Failed to create remote directory: {normalized}. {err}")


def is_sync_option_unsupported(text: str) -> bool:
    """判断 adb 是否不支持 --sync 选项。"""
    lowered = text.lower()
    return "unknown option" in lowered or "invalid option" in lowered


def adb_push_with_optional_sync(local_src: str, phone_dest: str) -> tuple[bool, subprocess.CompletedProcess[str]]:
    """
    优先使用 `adb push --sync`，若不支持则自动降级为普通 `adb push`。

    返回值：
    - used_sync: 本次最终是否使用 --sync
    - proc: 最终命令执行结果
    """
    # 优先尝试 --sync：只传增量，速度更快。
    first = run_cmd(["adb", "push", "--sync", local_src, phone_dest], check=False)
    if first.returncode == 0:
        return True, first

    merged_output = f"{first.stdout}\n{first.stderr}"
    if is_sync_option_unsupported(merged_output):
        # 老版本 adb 不支持 --sync 时自动降级。
        fallback = run_cmd(["adb", "push", local_src, phone_dest], check=False)
        return False, fallback
    return True, first


def read_favourite_entries(list_path: Path) -> list[str]:
    """读取清单文件，返回去空行后的条目（保留原顺序）。"""
    lines = list_path.read_text(encoding="utf-8").splitlines()
    result: list[str] = []
    for line in lines:
        item = line.strip()
        if item:
            result.append(item)
    return result


def resolve_list_item(pc_dir: Path, raw_item: str) -> Path:
    """
    把清单条目解析为 `pc_dir` 下的绝对文件路径。

    安全约束：
    - 解析后必须仍位于 `pc_dir` 之下，防止 `../` 路径逃逸。
    """
    posix_item = raw_item.replace("\\", "/").strip()
    item_path = PurePosixPath(posix_item)
    candidate = (pc_dir / Path(*item_path.parts)).resolve()
    pc_root = pc_dir.resolve()
    candidate.relative_to(pc_root)
    return candidate


def sync_all_mode(pc_dir: Path, phone_dir: str) -> int:
    """处理 `--push-all` 模式：推送整个目录内容。"""
    phone_dest = normalize_phone_dir(phone_dir)
    local_src = str(pc_dir.resolve()) + "\\."
    print("Mode: push_all (push all directory contents)")
    print(f"Command source: {local_src}")
    print(f"Command target: {phone_dest}")
    # local_src 使用 `pc_dir\.`，表示“推目录内容而非目录本身”。
    used_sync, proc = adb_push_with_optional_sync(local_src, phone_dest)
    if not used_sync:
        print("Warning: current adb does not support '--sync', fallback to plain push.")
    if proc.returncode == 0:
        print("All files pushed successfully.")
        return 0
    print("Push failed:")
    print((proc.stderr or proc.stdout or "").strip())
    return 1


def sync_list_mode(pc_dir: Path, phone_dir: str, entries: list[str], list_file_name: str) -> int:
    """
    处理清单文件模式：逐条推送。

    目标目录写法固定为 `<phone_dir>/`，避免 adb 把目标误当文件路径。
    """
    phone_dest = normalize_phone_dir(phone_dir) + "/"
    total = len(entries)
    success = 0
    failed = 0
    used_sync_flag: bool | None = None

    print(f"Mode: list_file={list_file_name} (total entries: {total})")
    for idx, entry in enumerate(entries, start=1):
        try:
            source_path = resolve_list_item(pc_dir, entry)
        except Exception:
            failed += 1
            print(f"[{idx}/{total}] FAIL path_invalid: {entry}")
            continue

        if not source_path.exists():
            failed += 1
            print(f"[{idx}/{total}] FAIL missing: {entry}")
            continue
        if not source_path.is_file():
            failed += 1
            print(f"[{idx}/{total}] FAIL not_file: {entry}")
            continue

        # 逐条 push 文件；source_path 必须是实际存在的文件。
        used_sync, proc = adb_push_with_optional_sync(str(source_path), phone_dest)
        if used_sync_flag is None:
            used_sync_flag = used_sync
            if not used_sync:
                print("Warning: current adb does not support '--sync', fallback to plain push.")

        if proc.returncode == 0:
            success += 1
            print(f"[{idx}/{total}] OK: {entry}")
        else:
            failed += 1
            err = (proc.stderr or proc.stdout or "").strip().replace("\n", " | ")
            print(f"[{idx}/{total}] FAIL push_error: {entry} | {err}")

    print("")
    print(f"Done. success={success}, failed={failed}, total={total}")
    return 0 if failed == 0 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync favourite files from PC directory to Android directory."
    )
    parser.add_argument("--pc-dir", required=True, help="PC source directory")
    parser.add_argument("--phone-dir", required=True, help="Android destination directory")
    parser.add_argument(
        "--list-file",
        default=DEFAULT_LIST_FILE,
        help=f"List file name under pc-dir (default: {DEFAULT_LIST_FILE})",
    )
    parser.add_argument(
        "--push-all",
        action="store_true",
        help="If set, push all files under pc-dir and ignore --list-file.",
    )
    return parser.parse_args()


def main() -> int:
    """主流程：参数校验 -> adb 检查 -> 目标目录检查/创建 -> 模式分派。"""
    try:
        args = parse_args()
        pc_dir = Path(args.pc_dir).expanduser().resolve()
        if not pc_dir.exists() or not pc_dir.is_dir():
            print(f"Error: pc-dir does not exist or is not a directory: {pc_dir}", file=sys.stderr)
            return 2

        ensure_adb_ready()
        # 先保证目标是目录，避免 `/sdcard/xxx` 被 adb 误创建为文件。
        ensure_remote_directory(args.phone_dir)

        list_file_name = (args.list_file or DEFAULT_LIST_FILE).strip()
        if not list_file_name:
            list_file_name = DEFAULT_LIST_FILE
        if Path(list_file_name).name != list_file_name:
            print(
                "Error: --list-file should be file name only (no directory path).",
                file=sys.stderr,
            )
            return 2

        if args.push_all:
            return sync_all_mode(pc_dir, args.phone_dir)

        list_file_path = pc_dir / list_file_name
        if not list_file_path.exists():
            print(f"No {list_file_name} found under: {pc_dir}")
            print("No sync action. Exit.")
            return 0

        entries = read_favourite_entries(list_file_path)
        if not entries:
            print(f"{list_file_name} is empty. No sync action. Exit.")
            return 0

        return sync_list_mode(pc_dir, args.phone_dir, entries, list_file_name)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
