"""ADB 推送辅助（自包含，不依赖仓库其他包）。"""

from __future__ import annotations

import subprocess
from typing import Sequence


def run_cmd(command: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def ensure_adb_ready() -> None:
    run_cmd(["adb", "version"])
    state_result = run_cmd(["adb", "get-state"], check=False)
    state_text = (state_result.stdout or "").strip().lower()
    if state_result.returncode == 0 and "device" in state_text:
        return

    devices_result = run_cmd(["adb", "devices"], check=False)
    devices_text = (devices_result.stdout or "").strip()
    err_text = (state_result.stderr or "").strip()
    raise RuntimeError(
        "ADB 设备未就绪，请检查 USB 调试授权。\n"
        f"adb get-state stderr: {err_text or '(empty)'}\n"
        f"adb devices output:\n{devices_text or '(empty)'}"
    )


def normalize_phone_dir(phone_dir: str) -> str:
    normalized = phone_dir.replace("\\", "/").rstrip("/")
    return normalized if normalized else "/"


def get_remote_path_kind(phone_path: str) -> str:
    normalized = normalize_phone_dir(phone_path)
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
    normalized = normalize_phone_dir(phone_dir)
    kind = get_remote_path_kind(normalized)
    if kind == "file":
        raise RuntimeError(
            "手机端目标已存在且为文件（非目录），请先删除或重命名："
            f"{normalized}"
        )
    escaped = normalized.replace("'", "'\"'\"'")
    proc = run_cmd(["adb", "shell", f"mkdir -p '{escaped}'"], check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"无法创建手机目录：{normalized}。{err}")


def ensure_remote_parent_for_file(phone_file_path: str) -> None:
    """确保手机端文件路径的父目录存在。"""
    normalized = phone_file_path.replace("\\", "/")
    if "/" not in normalized:
        return
    parent = normalized.rsplit("/", 1)[0]
    if parent:
        ensure_remote_directory(parent)


def is_sync_option_unsupported(text: str) -> bool:
    lowered = text.lower()
    return "unknown option" in lowered or "invalid option" in lowered


def adb_push_with_optional_sync(
    local_src: str, phone_dest: str
) -> tuple[bool, subprocess.CompletedProcess[str], str]:
    """
    优先 adb push --sync，不支持则降级普通 push。

    返回 (used_sync, proc, message)。
    """
    first = run_cmd(["adb", "push", "--sync", local_src, phone_dest], check=False)
    if first.returncode == 0:
        return True, first, "OK"

    merged = f"{first.stdout}\n{first.stderr}"
    if is_sync_option_unsupported(merged):
        fallback = run_cmd(["adb", "push", local_src, phone_dest], check=False)
        if fallback.returncode == 0:
            return False, fallback, "OK（adb 不支持 --sync，已降级为普通 push）"
        err = (fallback.stderr or fallback.stdout or "").strip()
        return False, fallback, err or "push 失败"

    err = (first.stderr or first.stdout or "").strip()
    return True, first, err or "push 失败"
