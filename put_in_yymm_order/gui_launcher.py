"""
目录整理 GUI 启动器（tkinter，支持按年/按月/按日）。

使用说明（示例）：
1) 启动界面：
   python gui_launcher.py
2) 在界面中选择/拖入根目录后，选择整理模式并点击“开始整理”。
3) GUI 只负责参数收集与结果展示，实际整理由 organize_by_month.py 执行。
"""

from __future__ import annotations

import argparse
import json
import locale
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


EVENT_PREFIX = "__EVENT__ "
SETTINGS_FILE_NAME = "gui_settings.json"

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore

    DND_ENABLED = True
except Exception:
    DND_ENABLED = False
    DND_FILES = None
    TkinterDnD = None


GROUP_BY_LABEL_TO_VALUE = {
    "按年": "year",
    "按月": "month",
    "按日": "day",
}
# 配置文件存英文值，界面显示中文标签。
GROUP_BY_VALUE_TO_LABEL = {value: key for key, value in GROUP_BY_LABEL_TO_VALUE.items()}


class MonthOrganizerGUI:
    def __init__(self, root: tk.Tk, *, settings_file: str = SETTINGS_FILE_NAME) -> None:
        self.root = root
        self.root.title("目录整理工具")
        self.root.geometry("860x560")
        self._settings_file = settings_file

        self.root_path_var = tk.StringVar()
        self.ext_whitelist_var = tk.StringVar()
        self.group_by_var = tk.StringVar(value="month")
        self.progress_var = tk.StringVar(value="进度：0 / 0")
        self.remove_empty_var = tk.BooleanVar(value=False)
        self.warn_error_only_var = tk.BooleanVar(value=False)
        self.auto_save_var = tk.BooleanVar(value=True)

        self._event_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._process: subprocess.Popen[str] | None = None
        self._subprocess_encoding = locale.getpreferredencoding(False) or "utf-8"

        self._build_ui()
        self._load_settings()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_queue)

    def _build_ui(self) -> None:
        top_frame = ttk.Frame(self.root, padding=12)
        top_frame.pack(fill="x")

        ttk.Label(top_frame, text="根目录：").pack(side="left")
        self.path_entry = ttk.Entry(top_frame, textvariable=self.root_path_var)
        self.path_entry.pack(side="left", fill="x", expand=True, padx=(6, 8))
        ttk.Button(top_frame, text="浏览", command=self._select_root_dir).pack(side="left")

        whitelist_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        whitelist_frame.pack(fill="x")
        ttk.Label(whitelist_frame, text="文件类型白名单：").pack(side="left")
        whitelist_entry = ttk.Entry(whitelist_frame, textvariable=self.ext_whitelist_var)
        whitelist_entry.pack(side="left", fill="x", expand=True, padx=(6, 8))
        ttk.Label(whitelist_frame, text="留空=全部；示例：.jpg .png,.mp4").pack(side="left")

        group_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        group_frame.pack(fill="x")
        ttk.Label(group_frame, text="分目录模式：").pack(side="left")
        self.group_by_combo = ttk.Combobox(
            group_frame,
            state="readonly",
            values=list(GROUP_BY_LABEL_TO_VALUE.keys()),
            width=14,
        )
        self.group_by_combo.pack(side="left", padx=(6, 8))
        self.group_by_combo.set("按月")
        self.group_by_combo.bind("<<ComboboxSelected>>", self._on_group_mode_changed)
        ttk.Label(
            group_frame,
            text="按年=YYYY，按月=YYYY_MM，按日=YYYY_MM_DD",
        ).pack(side="left")

        option_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        option_frame.pack(fill="x")
        ttk.Checkbutton(
            option_frame,
            text="整理完成后删除原目录下空子目录",
            variable=self.remove_empty_var,
        ).pack(side="left")
        ttk.Checkbutton(
            option_frame,
            text="仅显示警告/错误",
            variable=self.warn_error_only_var,
        ).pack(side="left", padx=(16, 0))
        ttk.Checkbutton(
            option_frame,
            text=f"自动保存配置: {self._settings_path().name}",
            variable=self.auto_save_var,
        ).pack(side="left", padx=(16, 0))

        progress_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        progress_frame.pack(fill="x")
        self.progressbar = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progressbar.pack(side="left", fill="x", expand=True)
        ttk.Label(progress_frame, textvariable=self.progress_var).pack(side="left", padx=(8, 0))

        action_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        action_frame.pack(fill="x")
        self.run_button = ttk.Button(action_frame, text="开始整理", command=self._run_organize)
        self.run_button.pack(side="left")

        log_frame = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=20, wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set, state="disabled")

        if DND_ENABLED and hasattr(self.path_entry, "drop_target_register"):
            self.path_entry.drop_target_register(DND_FILES)
            self.path_entry.dnd_bind("<<Drop>>", self._on_drop_path)
            self._append_log("已启用目录拖拽，请将目录拖入路径输入框。")
        else:
            self._append_log("未检测到 tkinterdnd2，当前仅支持“浏览”选择目录。")

    def _on_drop_path(self, event: tk.Event) -> None:
        raw_data = str(getattr(event, "data", "")).strip()
        if not raw_data:
            return
        raw_path = raw_data.strip("{}").strip('"')
        path = Path(raw_path).expanduser()
        if path.exists() and path.is_dir():
            self.root_path_var.set(str(path))
            self._append_log(f"已拖入目录：{path}")
        else:
            messagebox.showwarning("无效目录", "拖入内容不是有效目录，请重新选择。")

    def _select_root_dir(self) -> None:
        selected = filedialog.askdirectory(title="选择待整理根目录")
        if selected:
            self.root_path_var.set(selected)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _is_warn_or_error_message(self, message: str) -> bool:
        text = message.strip()
        if not text:
            return False
        prefixes = ("[警告]", "[错误]", "[stderr]", "[fatal]", "Fail:")
        if text.lower().startswith("fatal"):
            return True
        return text.startswith(prefixes)

    def _on_group_mode_changed(self, _event: tk.Event | None = None) -> None:
        selected = self.group_by_combo.get().strip()
        self.group_by_var.set(GROUP_BY_LABEL_TO_VALUE.get(selected, "month"))

    def _set_running(self, running: bool) -> None:
        self.run_button.configure(state=("disabled" if running else "normal"))

    def _settings_path(self) -> Path:
        # 若传入相对路径，则基于 GUI 脚本目录解析，便于双击脚本时定位稳定。
        configured_path = Path(self._settings_file).expanduser()
        if configured_path.is_absolute():
            return configured_path
        return Path(__file__).resolve().parent / configured_path

    def _default_settings(self) -> dict[str, object]:
        return {
            "root_dir": "",
            "ext_whitelist": "",
            "group_by": "month",
            "remove_empty_dirs": False,
            "warn_error_only": False,
            "auto_save": True,
        }

    def _collect_current_settings(self) -> dict[str, object]:
        return {
            "root_dir": self.root_path_var.get().strip(),
            "ext_whitelist": self.ext_whitelist_var.get().strip(),
            "group_by": self.group_by_var.get().strip() or "month",
            "remove_empty_dirs": self.remove_empty_var.get(),
            "warn_error_only": self.warn_error_only_var.get(),
            "auto_save": self.auto_save_var.get(),
        }

    def _apply_settings(self, settings: dict[str, object]) -> None:
        self.root_path_var.set(str(settings.get("root_dir", "") or ""))
        self.ext_whitelist_var.set(str(settings.get("ext_whitelist", "") or ""))
        group_by = str(settings.get("group_by", "month") or "month").lower()
        if group_by not in GROUP_BY_VALUE_TO_LABEL:
            group_by = "month"
        self.group_by_var.set(group_by)
        self.group_by_combo.set(GROUP_BY_VALUE_TO_LABEL[group_by])
        self.remove_empty_var.set(bool(settings.get("remove_empty_dirs", False)))
        self.warn_error_only_var.set(bool(settings.get("warn_error_only", False)))
        self.auto_save_var.set(bool(settings.get("auto_save", True)))

    def _save_settings(self) -> None:
        settings_path = self._settings_path()
        payload = self._collect_current_settings()
        # 使用 UTF-8 JSON，兼顾中文可读性与跨平台解析稳定性。
        settings_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_settings(self) -> None:
        settings_path = self._settings_path()
        defaults = self._default_settings()
        if not settings_path.exists():
            self._apply_settings(defaults)
            # 首次启动自动生成模板，避免用户手工创建配置文件。
            self._save_settings()
            self._append_log(f"已创建默认配置：{settings_path.name}")
            return

        try:
            raw = settings_path.read_text(encoding="utf-8").strip()
            loaded = json.loads(raw) if raw else {}
            if not isinstance(loaded, dict):
                raise ValueError("配置根节点必须是 JSON 对象。")
            merged = {**defaults, **loaded}
            self._apply_settings(merged)
            self._append_log(f"已加载配置：{settings_path.name}")
        except Exception as exc:
            self._apply_settings(defaults)
            self._append_log(f"[警告] 配置读取失败，已使用默认设置：{exc}")

    def _on_close(self) -> None:
        # 统一在窗口关闭时自动保存，避免运行中频繁写盘。
        if self.auto_save_var.get():
            try:
                self._save_settings()
                self._append_log(f"配置已保存：{self._settings_path().name}")
            except Exception as exc:
                messagebox.showwarning("保存配置失败", f"自动保存配置失败：{exc}")
        self.root.destroy()

    def _build_command(self, root_dir: Path) -> list[str]:
        script_path = Path(__file__).resolve().parent / "organize_by_month.py"
        cmd = [
            sys.executable,
            str(script_path),
            "--root",
            str(root_dir),
            "--group-by",
            self.group_by_var.get(),
            "--yes",
            "--json-events",
        ]
        if self.remove_empty_var.get():
            cmd.append("--remove-empty-dirs")
        whitelist_text = self.ext_whitelist_var.get().strip()
        if whitelist_text:
            cmd.extend(["--ext-whitelist", whitelist_text])
        return cmd

    def _run_organize(self) -> None:
        root_input = self.root_path_var.get().strip()
        if not root_input:
            messagebox.showwarning("参数缺失", "请先选择根目录。")
            return

        root_dir = Path(root_input).expanduser()
        if not root_dir.exists() or not root_dir.is_dir():
            messagebox.showwarning("目录无效", "请选择一个存在的目录。")
            return

        confirmed = messagebox.askyesno(
            "安全确认",
            "此操作将移动所有文件到目标时间目录，是否继续？",
        )
        if not confirmed:
            self._append_log("用户取消了本次整理。")
            return

        self.progressbar.configure(value=0, maximum=100)
        self.progress_var.set("进度：0 / 0")
        self._set_running(True)
        self._append_log(f"开始执行：{root_dir}（模式：{self.group_by_var.get()}）")

        cmd = self._build_command(root_dir)
        thread = threading.Thread(target=self._run_subprocess_worker, args=(cmd,), daemon=True)
        thread.start()

    def _run_subprocess_worker(self, cmd: list[str]) -> None:
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding=self._subprocess_encoding,
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            self._event_queue.put(("log", f"启动失败：{exc}"))
            self._event_queue.put(("done", "failed"))
            return

        assert self._process.stdout is not None
        assert self._process.stderr is not None

        for line in self._process.stdout:
            self._event_queue.put(("stdout", line.rstrip("\n")))

        stderr_text = self._process.stderr.read()
        return_code = self._process.wait()

        if stderr_text.strip():
            for row in stderr_text.splitlines():
                self._event_queue.put(("log", f"[stderr] {row}"))

        self._event_queue.put(("done", str(return_code)))

    def _handle_event_line(self, line: str) -> None:
        if line.startswith(EVENT_PREFIX):
            payload_text = line[len(EVENT_PREFIX) :].strip()
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                self._append_log(line)
                return
            self._handle_json_event(payload)
        else:
            self._append_log(line)

    def _handle_json_event(self, payload: dict) -> None:
        event_type = payload.get("event")
        if event_type == "log":
            message = str(payload.get("message", ""))
            if self.warn_error_only_var.get() and not self._is_warn_or_error_message(message):
                return
            self._append_log(message)
            return

        if event_type == "progress":
            processed = int(payload.get("processed", 0))
            total = int(payload.get("total", 0))
            self._update_progress(processed, total)
            return

        if event_type == "done":
            total = int(payload.get("total", 0))
            moved = int(payload.get("moved", 0))
            skipped = int(payload.get("skipped_same_files", 0))
            filtered = int(payload.get("filtered_by_whitelist", 0))
            renamed = int(payload.get("renamed", 0))
            failed = int(payload.get("failed", 0))
            removed = int(payload.get("removed_empty_dirs", 0))
            self._append_log(
                f"[结果] 总计 {total}，成功 {moved}，跳过同文件 {skipped}，"
                f"白名单过滤 {filtered}，重命名 {renamed}，失败 {failed}，删除空目录 {removed}"
            )
            return

        if event_type == "error":
            message = str(payload.get("message", "未知错误"))
            path = str(payload.get("path", ""))
            self._append_log(f"[错误] {path} {message}".strip())
            return

        if event_type == "fatal":
            message = str(payload.get("message", "未知致命错误"))
            self._append_log(f"[fatal] {message}")
            return

    def _update_progress(self, processed: int, total: int) -> None:
        if total <= 0:
            self.progressbar.configure(maximum=100, value=0)
            self.progress_var.set("进度：0 / 0")
            return
        self.progressbar.configure(maximum=total, value=processed)
        self.progress_var.set(f"进度：{processed} / {total}")

    def _poll_queue(self) -> None:
        try:
            while True:
                event_type, content = self._event_queue.get_nowait()
                if event_type == "stdout":
                    self._handle_event_line(content)
                elif event_type == "log":
                    if self.warn_error_only_var.get() and not self._is_warn_or_error_message(content):
                        continue
                    self._append_log(content)
                elif event_type == "done":
                    self._set_running(False)
                    if content == "0":
                        self._append_log("执行结束。")
                    else:
                        self._append_log(f"执行结束，退出码：{content}")
        except queue.Empty:
            pass
        finally:
            self.root.after(120, self._poll_queue)


def create_root() -> tk.Tk:
    if DND_ENABLED and TkinterDnD is not None:
        return TkinterDnD.Tk()
    return tk.Tk()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="目录整理 GUI 启动器（按年/按月/按日）。")
    parser.add_argument(
        "--settings-file",
        default=SETTINGS_FILE_NAME,
        help=(
            "指定 GUI 配置文件路径（默认: gui_settings.json）。"
            "可传文件名或绝对路径。"
        ),
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    root = create_root()
    MonthOrganizerGUI(root, settings_file=args.settings_file)
    root.mainloop()


if __name__ == "__main__":
    main()
