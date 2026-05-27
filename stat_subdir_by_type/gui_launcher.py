"""
子目录文件类型统计 GUI 启动器（tkinter）。

使用说明（示例）：
1) 启动界面：
   python gui_launcher.py
2) 在界面中选择/拖入根目录后，点击“开始统计”。
3) GUI 只负责参数收集与结果展示，实际统计由 stat_subdir_by_type.py 执行。
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

from stat_subdir_by_type import remove_old_stat_files


EVENT_PREFIX = "__EVENT__ "
SETTINGS_FILE_NAME = "gui_settings.json"

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore

    DND_ENABLED = True
except Exception:
    DND_ENABLED = False
    DND_FILES = None
    TkinterDnD = None


class SubdirStatsGUI:
    def __init__(self, root: tk.Tk, *, settings_file: str = SETTINGS_FILE_NAME) -> None:
        self.root = root
        self.root.title("子目录文件类型统计工具")
        self.root.geometry("860x520")
        self._settings_file = settings_file

        self.root_path_var = tk.StringVar()
        self.ext_whitelist_var = tk.StringVar()
        self.progress_var = tk.StringVar(value="进度：0 / 0")
        self.remove_old_stats_var = tk.BooleanVar(value=False)
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

        hint_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        hint_frame.pack(fill="x")
        ttk.Label(
            hint_frame,
            text="将在根目录为每个一级子目录生成 统计_{子目录名}_扩展名(数量).txt",
        ).pack(side="left")

        whitelist_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        whitelist_frame.pack(fill="x")
        ttk.Label(whitelist_frame, text="文件类型白名单：").pack(side="left")
        whitelist_entry = ttk.Entry(whitelist_frame, textvariable=self.ext_whitelist_var)
        whitelist_entry.pack(side="left", fill="x", expand=True, padx=(6, 8))
        ttk.Label(whitelist_frame, text="留空=全部；示例：.jpg .png,.mp4").pack(side="left")

        option_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        option_frame.pack(fill="x")
        ttk.Checkbutton(
            option_frame,
            text="执行前删除根目录下已有的 统计_*.txt",
            variable=self.remove_old_stats_var,
        ).pack(side="left")
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
        self.run_button = ttk.Button(action_frame, text="开始统计", command=self._run_stats)
        self.run_button.pack(side="left")
        ttk.Button(
            action_frame,
            text="清理统计 txt",
            command=self._cleanup_stat_files,
        ).pack(side="left", padx=(12, 0))

        log_frame = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=18, wrap="word")
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
        selected = filedialog.askdirectory(title="选择待统计根目录")
        if selected:
            self.root_path_var.set(selected)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        self.run_button.configure(state=("disabled" if running else "normal"))

    def _settings_path(self) -> Path:
        configured_path = Path(self._settings_file).expanduser()
        if configured_path.is_absolute():
            return configured_path
        return Path(__file__).resolve().parent / configured_path

    def _default_settings(self) -> dict[str, object]:
        return {
            "root_dir": "",
            "ext_whitelist": "",
            "remove_old_stats": False,
            "auto_save": True,
        }

    def _collect_current_settings(self) -> dict[str, object]:
        return {
            "root_dir": self.root_path_var.get().strip(),
            "ext_whitelist": self.ext_whitelist_var.get().strip(),
            "remove_old_stats": self.remove_old_stats_var.get(),
            "auto_save": self.auto_save_var.get(),
        }

    def _apply_settings(self, settings: dict[str, object]) -> None:
        self.root_path_var.set(str(settings.get("root_dir", "") or ""))
        self.ext_whitelist_var.set(str(settings.get("ext_whitelist", "") or ""))
        self.remove_old_stats_var.set(bool(settings.get("remove_old_stats", False)))
        self.auto_save_var.set(bool(settings.get("auto_save", True)))

    def _save_settings(self) -> None:
        settings_path = self._settings_path()
        payload = self._collect_current_settings()
        settings_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_settings(self) -> None:
        settings_path = self._settings_path()
        defaults = self._default_settings()
        if not settings_path.exists():
            self._apply_settings(defaults)
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
        if self.auto_save_var.get():
            try:
                self._save_settings()
                self._append_log(f"配置已保存：{self._settings_path().name}")
            except Exception as exc:
                messagebox.showwarning("保存配置失败", f"自动保存配置失败：{exc}")
        self.root.destroy()

    def _build_command(self, root_dir: Path) -> list[str]:
        script_path = Path(__file__).resolve().parent / "stat_subdir_by_type.py"
        cmd = [
            sys.executable,
            str(script_path),
            "--root",
            str(root_dir),
            "--yes",
            "--json-events",
        ]
        if self.remove_old_stats_var.get():
            cmd.append("--remove-old-stats")
        whitelist_text = self.ext_whitelist_var.get().strip()
        if whitelist_text:
            cmd.extend(["--ext-whitelist", whitelist_text])
        return cmd

    def _cleanup_stat_files(self) -> None:
        root_input = self.root_path_var.get().strip()
        if not root_input:
            messagebox.showwarning("参数缺失", "请先选择根目录。")
            return

        root_dir = Path(root_input).expanduser().resolve()
        if not root_dir.exists() or not root_dir.is_dir():
            messagebox.showwarning("目录无效", "请选择一个存在的目录。")
            return

        confirmed = messagebox.askyesno(
            "确认清理",
            f"将删除该目录下所有 统计_*.txt 文件：\n{root_dir}\n是否继续？",
        )
        if not confirmed:
            self._append_log("用户取消了清理统计文件。")
            return

        try:
            removed = remove_old_stat_files(root_dir)
            self._append_log(f"已清理 {removed} 个统计 txt 文件：{root_dir}")
        except Exception as exc:
            messagebox.showerror("清理失败", str(exc))
            self._append_log(f"[错误] 清理失败：{exc}")

    def _run_stats(self) -> None:
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
            "将在根目录为每个一级子目录生成统计 txt 文件，是否继续？",
        )
        if not confirmed:
            self._append_log("用户取消了本次统计。")
            return

        self.progressbar.configure(value=0, maximum=100)
        self.progress_var.set("进度：0 / 0")
        self._set_running(True)
        self._append_log(f"开始执行：{root_dir}")

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
            self._append_log(message)
            return

        if event_type == "progress":
            processed = int(payload.get("processed", 0))
            total = int(payload.get("total", 0))
            self._update_progress(processed, total)
            return

        if event_type == "done":
            total = int(payload.get("total", 0))
            generated = int(payload.get("generated", 0))
            empty_subdirs = int(payload.get("empty_subdirs", 0))
            failed = int(payload.get("failed", 0))
            removed = int(payload.get("removed_old_stats", 0))
            self._append_log(
                f"[结果] 子目录 {total} 个，生成 {generated} 个统计文件，"
                f"空子目录 {empty_subdirs}，失败 {failed}，清理旧文件 {removed}"
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
    parser = argparse.ArgumentParser(description="子目录文件类型统计 GUI 启动器。")
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
    SubdirStatsGUI(root, settings_file=args.settings_file)
    root.mainloop()


if __name__ == "__main__":
    main()
