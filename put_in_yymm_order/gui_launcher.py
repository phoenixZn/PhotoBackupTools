"""
按月目录整理 GUI 启动器（tkinter）。

使用说明（示例）：
1) 启动界面：
   python gui_launcher.py
2) 在界面中选择/拖入根目录后，点击“按月整理”。
3) GUI 只负责参数收集与结果展示，实际整理由 organize_by_month.py 执行。
"""

from __future__ import annotations

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

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore

    DND_ENABLED = True
except Exception:
    DND_ENABLED = False
    DND_FILES = None
    TkinterDnD = None


class MonthOrganizerGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("按月目录整理工具")
        self.root.geometry("860x560")

        self.root_path_var = tk.StringVar()
        self.progress_var = tk.StringVar(value="进度：0 / 0")
        self.remove_empty_var = tk.BooleanVar(value=False)
        self.warn_error_only_var = tk.BooleanVar(value=False)

        self._event_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._process: subprocess.Popen[str] | None = None
        self._subprocess_encoding = locale.getpreferredencoding(False) or "utf-8"

        self._build_ui()
        self.root.after(100, self._poll_queue)

    def _build_ui(self) -> None:
        top_frame = ttk.Frame(self.root, padding=12)
        top_frame.pack(fill="x")

        ttk.Label(top_frame, text="根目录：").pack(side="left")
        self.path_entry = ttk.Entry(top_frame, textvariable=self.root_path_var)
        self.path_entry.pack(side="left", fill="x", expand=True, padx=(6, 8))
        ttk.Button(top_frame, text="浏览", command=self._select_root_dir).pack(side="left")

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

        progress_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        progress_frame.pack(fill="x")
        self.progressbar = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progressbar.pack(side="left", fill="x", expand=True)
        ttk.Label(progress_frame, textvariable=self.progress_var).pack(side="left", padx=(8, 0))

        action_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        action_frame.pack(fill="x")
        self.run_button = ttk.Button(action_frame, text="按月整理", command=self._run_organize)
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
        prefixes = ("[警告]", "[错误]", "[stderr]", "[fatal]")
        if text.lower().startswith("fatal"):
            return True
        return text.startswith(prefixes)

    def _set_running(self, running: bool) -> None:
        self.run_button.configure(state=("disabled" if running else "normal"))

    def _build_command(self, root_dir: Path) -> list[str]:
        script_path = Path(__file__).resolve().parent / "organize_by_month.py"
        cmd = [
            sys.executable,
            str(script_path),
            "--root",
            str(root_dir),
            "--yes",
            "--json-events",
        ]
        if self.remove_empty_var.get():
            cmd.append("--remove-empty-dirs")
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
            "此操作将移动所有文件到月份文件夹，是否继续？",
        )
        if not confirmed:
            self._append_log("用户取消了本次整理。")
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
            failed = int(payload.get("failed", 0))
            removed = int(payload.get("removed_empty_dirs", 0))
            self._append_log(
                f"[结果] 总计 {total}，成功 {moved}，跳过同文件 {skipped}，失败 {failed}，删除空目录 {removed}"
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


def main() -> None:
    root = create_root()
    MonthOrganizerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
