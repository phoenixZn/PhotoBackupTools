import json
import os
import queue
import shlex
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


# 当前脚本所在目录。
SCRIPT_DIR = Path(__file__).resolve().parent
# 命令行后端脚本：GUI 只负责调用，不在界面层重复实现业务逻辑。
BACKEND_SCRIPT = SCRIPT_DIR / "cmd_adb_favourite_sync.py"
# GUI 表单持久化配置文件（记录最近一次输入）。
CONFIG_PATH = SCRIPT_DIR / "gui_config.json"


class FavouriteSyncGuiApp:
    """ADB Favourite 同步工具的 Tkinter 主控制器。"""

    def __init__(self, root: tk.Tk) -> None:
        # 主窗口初始化。
        self.root = root
        self.root.title("ADB Favourite Sync")
        self.root.geometry("860x560")

        # 工作线程输出通过队列回传到主线程，避免 Tk 跨线程调用崩溃。
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.running = False
        # 当前后台任务是同步还是预对比，用于完成时的弹窗文案。
        self._run_is_preview = False

        # 先加载配置，再绑定 UI 变量，保证打开界面即可回填。
        defaults = self.default_config()
        config = self.load_config(defaults)

        self.python_var = tk.StringVar(value=str(config["python"]))
        self.pc_dir_var = tk.StringVar(value=str(config["pc_dir"]))
        self.phone_dir_var = tk.StringVar(value=str(config["phone_dir"]))
        self.list_file_var = tk.StringVar(value=str(config["list_file"]))
        self.push_all_var = tk.BooleanVar(value=bool(config["push_all"]))

        self.build_ui()
        self.update_list_file_state()
        self.root.after(80, self.flush_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    @staticmethod
    def default_config() -> dict[str, str | bool]:
        """无配置文件时的默认表单值。"""
        return {
            "python": "python",
            "pc_dir": "E:\\AdbBak\\TestData\\IconLib\\GuoQi",
            "phone_dir": "/sdcard/Download/FavouriteSync",
            "list_file": "favourite_list.txt",
            "push_all": False,
        }

    def load_config(self, defaults: dict[str, str | bool]) -> dict[str, str | bool]:
        """读取 GUI 配置并与默认值合并，兼容缺字段场景。"""
        if not CONFIG_PATH.exists():
            return defaults
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return defaults
        merged = dict(defaults)
        if isinstance(data, dict):
            merged["python"] = str(data.get("python", defaults["python"]))
            merged["pc_dir"] = str(data.get("pc_dir", defaults["pc_dir"]))
            merged["phone_dir"] = str(data.get("phone_dir", defaults["phone_dir"]))
            merged["list_file"] = str(data.get("list_file", defaults["list_file"]))
            merged["push_all"] = bool(data.get("push_all", defaults["push_all"]))
        return merged

    def save_config(self) -> None:
        """保存当前表单值，供下次启动自动回填。"""
        payload = {
            "python": self.python_var.get().strip(),
            "pc_dir": self.pc_dir_var.get().strip(),
            "phone_dir": self.phone_dir_var.get().strip(),
            "list_file": self.list_file_var.get().strip(),
            "push_all": self.push_all_var.get(),
        }
        CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def build_ui(self) -> None:
        """构建界面布局：参数区、控制区、日志区。"""
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(5, weight=1)

        ttk.Label(container, text="Python Command").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(container, textvariable=self.python_var).grid(row=0, column=1, sticky=tk.EW, pady=4)

        ttk.Label(container, text="PC Directory").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(container, textvariable=self.pc_dir_var).grid(row=1, column=1, sticky=tk.EW, pady=4)
        ttk.Button(container, text="Browse...", command=self.choose_pc_dir).grid(
            row=1, column=2, sticky=tk.W, padx=(8, 0), pady=4
        )

        ttk.Label(container, text="Phone Directory").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(container, textvariable=self.phone_dir_var).grid(row=2, column=1, sticky=tk.EW, pady=4)

        ttk.Label(container, text="List File Name").grid(row=3, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        self.list_file_entry = ttk.Entry(container, textvariable=self.list_file_var)
        self.list_file_entry.grid(row=3, column=1, sticky=tk.EW, pady=4)
        self.push_all_check = ttk.Checkbutton(
            container,
            text="Push all files (ignore list file)",
            variable=self.push_all_var,
            command=self.update_list_file_state,
        )
        self.push_all_check.grid(row=3, column=2, sticky=tk.W, padx=(8, 0), pady=4)

        controls = ttk.Frame(container)
        controls.grid(row=4, column=0, columnspan=3, sticky=tk.W, pady=(6, 8))
        self.run_button = ttk.Button(controls, text="Run Favourite Sync", command=self.on_run_click)
        self.run_button.pack(side=tk.LEFT, padx=(0, 8))
        self.preview_button = ttk.Button(controls, text="预对比", command=self.on_preview_click)
        self.preview_button.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(controls, text="Clear Log", command=self.clear_log).pack(side=tk.LEFT)

        ttk.Label(container, text="Execution Log").grid(row=5, column=0, sticky=tk.NW, padx=(0, 8), pady=(6, 0))
        self.log_text = tk.Text(container, height=20, wrap="word")
        self.log_text.grid(row=5, column=1, columnspan=2, sticky=tk.NSEW, pady=(6, 0))
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=self.log_text.yview)
        scrollbar.grid(row=5, column=3, sticky=tk.NS, pady=(6, 0))
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def update_list_file_state(self) -> None:
        """根据“全量推送”勾选状态切换清单文件输入框可编辑性。"""
        if self.push_all_var.get():
            self.list_file_entry.configure(state=tk.DISABLED)
        else:
            self.list_file_entry.configure(state=tk.NORMAL)

    def choose_pc_dir(self) -> None:
        """选择 PC 源目录并回填输入框。"""
        selected = filedialog.askdirectory(title="Select PC source directory")
        if selected:
            self.pc_dir_var.set(selected)

    def collect_and_validate(self) -> dict[str, str | bool]:
        """采集并校验输入参数。"""
        python_cmd = self.python_var.get().strip()
        pc_dir = self.pc_dir_var.get().strip()
        phone_dir = self.phone_dir_var.get().strip()
        list_file = self.list_file_var.get().strip()
        push_all = self.push_all_var.get()

        if not python_cmd:
            raise ValueError("Python command is required.")
        if not pc_dir:
            raise ValueError("PC directory is required.")
        if not phone_dir:
            raise ValueError("Phone directory is required.")
        if not push_all:
            if not list_file:
                raise ValueError("List file name is required when push-all is unchecked.")
            if "/" in list_file or "\\" in list_file:
                raise ValueError("List file name should not contain path separator.")
        elif not list_file:
            # 全量模式下该值不会生效，但为配置持久化保留默认值。
            list_file = "favourite_list.txt"

        return {
            "python": python_cmd,
            "pc_dir": pc_dir,
            "phone_dir": phone_dir,
            "list_file": list_file,
            "push_all": push_all,
        }

    def build_command(self, values: dict[str, str | bool], *, preview: bool = False) -> list[str]:
        """
        组装后端命令。

        说明：
        - 支持 `py -3` 之类的复合 Python 启动命令；
        - 自动补 `-u`，尽量确保后端输出实时刷新到 GUI。
        """
        python_raw = values["python"]
        try:
            python_items = shlex.split(python_raw, posix=False)
        except ValueError:
            python_items = [python_raw]
        if not python_items:
            python_items = [python_raw]

        if "-u" not in python_items:
            python_items.insert(1 if len(python_items) >= 1 else 0, "-u")

        command = [
            *python_items,
            str(BACKEND_SCRIPT),
            "--pc-dir",
            values["pc_dir"],
            "--phone-dir",
            str(values["phone_dir"]),
            "--list-file",
            str(values["list_file"]),
        ]
        if bool(values["push_all"]):
            command.append("--push-all")
        if preview:
            command.append("--preview")
        return command

    def _set_running(self, running: bool) -> None:
        """统一切换运行状态与按钮可用性。"""
        self.running = running
        state = tk.DISABLED if running else tk.NORMAL
        self.run_button.configure(state=state)
        self.preview_button.configure(state=state)

    def _start_backend(self, values: dict[str, str | bool], *, preview: bool) -> None:
        """
        校验通过后启动后端：确认框 -> 保存配置 -> 后台线程执行。

        preview=True 时附加 --preview，不执行 adb push。
        """
        if preview:
            confirm_title = "Confirm Preview"
            confirm_text = (
                "Preview sync plan (no files will be pushed):\n\n"
                f"Python: {values['python']}\n"
                f"PC Dir: {values['pc_dir']}\n"
                f"Phone Dir: {values['phone_dir']}\n"
                f"List File: {values['list_file']}\n"
                f"Push All: {values['push_all']}\n"
            )
            log_title = "Starting preview (no push)..."
        else:
            confirm_title = "Confirm"
            confirm_text = (
                "Please confirm sync settings:\n\n"
                f"Python: {values['python']}\n"
                f"PC Dir: {values['pc_dir']}\n"
                f"Phone Dir: {values['phone_dir']}\n"
                f"List File: {values['list_file']}\n"
                f"Push All: {values['push_all']}\n"
            )
            log_title = "Starting favourite sync..."

        if not messagebox.askyesno(confirm_title, confirm_text):
            return

        try:
            self.save_config()
        except Exception as exc:
            messagebox.showwarning("Warning", f"Could not save GUI config: {exc}")

        command = self.build_command(values, preview=preview)
        self._run_is_preview = preview
        self._set_running(True)
        self.log_line("")
        self.log_line("=" * 64)
        self.log_line(log_title)
        self.log_line("Command: " + " ".join(command))
        self.log_line("=" * 64)

        self.worker_thread = threading.Thread(target=self.execute_command, args=(command,), daemon=True)
        self.worker_thread.start()

    def on_run_click(self) -> None:
        """处理“Run Favourite Sync”点击事件。"""
        if self.running:
            return
        try:
            values = self.collect_and_validate()
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return
        self._start_backend(values, preview=False)

    def on_preview_click(self) -> None:
        """处理「预对比」点击：只列出待同步文件，不执行 push。"""
        if self.running:
            return
        try:
            values = self.collect_and_validate()
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return
        self._start_backend(values, preview=True)

    def execute_command(self, command: list[str]) -> None:
        """
        在后台线程启动后端脚本并转发输出。

        注意：该方法不直接操作 Tk 控件，只把消息写入队列。
        """
        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=str(SCRIPT_DIR),
                env=env,
            )
        except Exception as exc:
            self.log_queue.put(("line", f"Failed to start process: {exc}"))
            self.log_queue.put(("done", "launch_error"))
            return

        assert proc.stdout is not None
        for line in proc.stdout:
            self.log_queue.put(("line", line.rstrip("\n")))

        self.log_queue.put(("done", str(proc.wait())))

    def flush_log_queue(self) -> None:
        """主线程轮询队列并更新日志/弹窗。"""
        while True:
            try:
                item_type, payload = self.log_queue.get_nowait()
            except queue.Empty:
                break

            if item_type == "line":
                self.log_line(payload)
            elif item_type == "done":
                is_preview = self._run_is_preview
                self._set_running(False)
                if payload == "launch_error":
                    title = "Preview failed" if is_preview else "Sync failed"
                    messagebox.showerror(title, "Could not launch backend command.")
                else:
                    exit_code = int(payload)
                    self.log_line("")
                    self.log_line(f"Process finished with exit code {exit_code}.")
                    if exit_code == 0:
                        if is_preview:
                            messagebox.showinfo(
                                "Preview completed",
                                "Preview finished. See log for files to sync.",
                            )
                        else:
                            messagebox.showinfo(
                                "Sync completed",
                                "Favourite sync finished successfully.",
                            )
                    else:
                        title = "Preview failed" if is_preview else "Sync failed"
                        action = "Preview" if is_preview else "Favourite sync"
                        messagebox.showerror(title, f"{action} exited with code {exit_code}.")

        self.root.after(80, self.flush_log_queue)

    def log_line(self, message: str) -> None:
        """向日志区追加单行文本。"""
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)

    def clear_log(self) -> None:
        """清空日志区。"""
        self.log_text.delete("1.0", tk.END)

    def on_close(self) -> None:
        """窗口关闭处理：运行中先二次确认，退出前尽量保存配置。"""
        if self.running:
            if not messagebox.askyesno("Exit", "Sync is still running. Exit anyway?"):
                return
        try:
            self.save_config()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    """程序入口：检查后端脚本并启动 GUI。"""
    if not BACKEND_SCRIPT.exists():
        messagebox.showerror("Missing script", f"Cannot find backend script:\n{BACKEND_SCRIPT}")
        return

    root = tk.Tk()
    app = FavouriteSyncGuiApp(root)
    root.minsize(760, 500)
    root.mainloop()


if __name__ == "__main__":
    main()
