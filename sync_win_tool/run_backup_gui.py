import datetime as dt
import json
import os
import queue
import re
import shlex
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

"""
Android 按日期范围备份的 GUI 启动器。

设计说明：
1) 本文件只负责界面层；真正备份逻辑仍在
   `cmd_adb_date_range_backup.py`，避免重复实现业务逻辑。
2) GUI 负责收集参数、做输入校验、组装命令并展示子进程输出。
3) 子进程在工作线程执行；主线程通过轮询队列安全更新 Tk 组件。
"""


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
# 后端脚本路径（按设计放在当前目录之外）。
BACKUP_SCRIPT = SCRIPT_DIR / "cmd_adb_date_range_backup.py"
# GUI 配置文件与启动器放在同目录，便于迁移和管理。
CONFIG_PATH = SCRIPT_DIR / "gui_config.json"
# 静态配置文件：仅手工维护，程序运行中不会写回。
STATIC_CONFIG_PATH = SCRIPT_DIR / "static_config.json"
GUI_LOG_DIR = SCRIPT_DIR / "logs"
# 简单全量同步（外部 cmd）临时启动脚本目录。
GUI_TEMP_DIR = GUI_LOG_DIR / "temp"
# 日期输入支持的格式。
DATE_FORMATS = ("%Y.%m.%d", "%Y-%m-%d", "%Y/%m/%d")
PROGRESS_PATTERN = re.compile(r"^\[(\d+)/(\d+)\]")
RATE_PATTERN = re.compile(r"^RATE\tavg_bps=([0-9.]+)\thuman=(.+)$")


def parse_date(raw: str) -> dt.date:
    """按允许的格式解析日期字符串；失败时抛出 ValueError。"""
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError("Invalid date format")


class BackupGuiApp:
    """Tkinter 主应用控制器。"""

    def __init__(self, root: tk.Tk) -> None:
        # 根窗口初始化。
        self.root = root
        self.root.title("ADB Date Range Backup")
        self.root.geometry("860x600")

        # 队列用于把工作线程输出安全地传回 UI 主线程。
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.running = False
        self.progress_total = 0
        self.progress_current = 0
        self.latest_rate_text = ""
        self.run_started_at: dt.datetime | None = None
        self.current_command: list[str] = []
        self.current_run_logs: list[str] = []

        # 构建界面前先加载持久化配置（或默认值）。
        defaults = self.default_config()
        config = self.load_config(defaults)
        self.static_configs = self.load_static_configs(defaults)
        self.static_config_var = tk.StringVar(value="")

        # Tk 变量用于绑定控件与数据。
        self.python_var = tk.StringVar(value=config["python"])
        self.phone_dir_var = tk.StringVar(value=config["phone_dir"])
        self.pc_dir_var = tk.StringVar(value=config["pc_dir"])
        self.start_date_var = tk.StringVar(value=config["start_date"])
        self.end_date_var = tk.StringVar(value=config["end_date"])
        self.all_files_var = tk.BooleanVar(value=config["all_files"])
        self.write_sync_record_var = tk.BooleanVar(value=config["write_sync_record"])
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text_var = tk.StringVar(value="Progress: idle")

        self.build_ui()
        self.root.after(80, self.flush_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    @staticmethod
    def default_config() -> dict[str, object]:
        """无配置文件时使用的默认表单值。"""
        current_year = dt.date.today().year
        return {
            "python": "python",
            "phone_dir": "/sdcard/DCIM/Camera",
            "pc_dir": "E:\\PhotoBackup\\Camera",
            "start_date": f"{current_year}.01.01",
            "end_date": f"{current_year}.12.31",
            "all_files": False,
            "write_sync_record": False,
        }

    def load_config(self, defaults: dict[str, object]) -> dict[str, object]:
        """
        读取 JSON 配置（若存在）。

        缺失或异常字段会回退到默认值，保证配置不完整或损坏时 GUI 仍可启动。
        """
        if not CONFIG_PATH.exists():
            return defaults
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return defaults

        merged = dict(defaults)
        merged.update(
            {
                "python": str(data.get("python", defaults["python"])),
                "phone_dir": str(data.get("phone_dir", defaults["phone_dir"])),
                "pc_dir": str(data.get("pc_dir", defaults["pc_dir"])),
                "start_date": str(data.get("start_date", defaults["start_date"])),
                "end_date": str(data.get("end_date", defaults["end_date"])),
                "all_files": bool(data.get("all_files", defaults["all_files"])),
                "write_sync_record": bool(data.get("write_sync_record", defaults["write_sync_record"])),
            }
        )
        return merged

    def load_static_configs(self, defaults: dict[str, object]) -> list[dict[str, object]]:
        """
        读取静态配置列表。

        数据源仅供手工维护和界面快速切换，不会被程序运行时自动写回。
        """
        if not STATIC_CONFIG_PATH.exists():
            return []
        try:
            data = json.loads(STATIC_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(data, list):
            return []

        static_configs: list[dict[str, object]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            static_configs.append(
                {
                    "name": name,
                    "python": str(item.get("python", defaults["python"])),
                    "phone_dir": str(item.get("phone_dir", defaults["phone_dir"])),
                    "pc_dir": str(item.get("pc_dir", defaults["pc_dir"])),
                    "start_date": str(item.get("start_date", defaults["start_date"])),
                    "end_date": str(item.get("end_date", defaults["end_date"])),
                    "all_files": bool(item.get("all_files", defaults["all_files"])),
                    "write_sync_record": bool(item.get("write_sync_record", defaults["write_sync_record"])),
                }
            )
        return static_configs

    def save_config(self) -> None:
        """保存当前表单值，供下次启动自动回填。"""
        payload = {
            "python": self.python_var.get().strip(),
            "phone_dir": self.phone_dir_var.get().strip(),
            "pc_dir": self.pc_dir_var.get().strip(),
            "start_date": self.start_date_var.get().strip(),
            "end_date": self.end_date_var.get().strip(),
            "all_files": self.all_files_var.get(),
            "write_sync_record": self.write_sync_record_var.get(),
        }
        CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def save_static_configs(self) -> None:
        """保存静态配置列表。"""
        STATIC_CONFIG_PATH.write_text(json.dumps(self.static_configs, ensure_ascii=True, indent=2), encoding="utf-8")

    def build_ui(self) -> None:
        """
        构建窗口布局：
        - 上部：参数输入区
        - 中部：操作按钮
        - 下部：执行日志区
        """
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(11, weight=1)

        ttk.Label(container, text="Python Command").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(container, textvariable=self.python_var).grid(row=0, column=1, sticky=tk.EW, pady=4)

        ttk.Label(container, text="Phone Directory").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(container, textvariable=self.phone_dir_var).grid(row=1, column=1, sticky=tk.EW, pady=4)

        ttk.Label(container, text="PC Directory").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        pc_dir_entry = ttk.Entry(container, textvariable=self.pc_dir_var)
        pc_dir_entry.grid(row=2, column=1, sticky=tk.EW, pady=4)
        ttk.Button(container, text="Browse...", command=self.choose_pc_dir).grid(row=2, column=2, sticky=tk.W, padx=(8, 0), pady=4)

        ttk.Label(container, text="Start Date").grid(row=3, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(container, textvariable=self.start_date_var).grid(row=3, column=1, sticky=tk.EW, pady=4)

        ttk.Label(container, text="End Date").grid(row=4, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(container, textvariable=self.end_date_var).grid(row=4, column=1, sticky=tk.EW, pady=4)

        ttk.Checkbutton(
            container,
            text="Include all files (--all-files)",
            variable=self.all_files_var,
        ).grid(row=5, column=1, sticky=tk.W, pady=(6, 8))
        ttk.Checkbutton(
            container,
            text="Write sync record file (--write-sync-record)",
            variable=self.write_sync_record_var,
        ).grid(row=6, column=1, sticky=tk.W, pady=(0, 8))

        ttk.Label(container, text="Static Config").grid(row=7, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        static_names = [str(item["name"]) for item in self.static_configs]
        self.static_config_combo = ttk.Combobox(
            container,
            textvariable=self.static_config_var,
            state="readonly",
            values=static_names,
        )
        self.static_config_combo.grid(row=7, column=1, sticky=tk.EW, pady=4)
        ttk.Button(container, text="Save As...", command=self.on_save_static_click).grid(
            row=7, column=2, sticky=tk.W, padx=(8, 0), pady=4
        )
        if static_names:
            self.static_config_combo.bind("<<ComboboxSelected>>", self.on_static_config_selected)
        else:
            self.static_config_combo.configure(state=tk.DISABLED)
            self.static_config_var.set("No static configs")

        ttk.Label(container, textvariable=self.progress_text_var).grid(
            row=8, column=0, columnspan=3, sticky=tk.W, pady=(2, 0)
        )
        self.progress_bar = ttk.Progressbar(
            container,
            mode="determinate",
            maximum=100.0,
            variable=self.progress_var,
        )
        self.progress_bar.grid(row=9, column=0, columnspan=3, sticky=tk.EW, pady=(2, 8))

        controls = ttk.Frame(container)
        controls.grid(row=10, column=0, columnspan=3, sticky=tk.W, pady=(0, 8))
        self.run_button = ttk.Button(controls, text="Run Backup", command=self.on_run_click)
        self.run_button.pack(side=tk.LEFT, padx=(0, 8))
        # ===== 简单全量同步（外部 cmd）UI入口 begin =====
        self.simple_full_sync_button = ttk.Button(
            controls,
            text="简单全量同步",
            command=self.on_simple_full_sync_click,
        )
        self.simple_full_sync_button.pack(side=tk.LEFT, padx=(0, 8))
        # ===== 简单全量同步（外部 cmd）UI入口 end =====
        ttk.Button(controls, text="Clear Log", command=self.clear_log).pack(side=tk.LEFT)

        ttk.Label(container, text="Execution Log").grid(row=11, column=0, sticky=tk.NW, padx=(0, 8), pady=(6, 0))
        self.log_text = tk.Text(container, height=18, wrap="word")
        self.log_text.grid(row=11, column=1, columnspan=2, sticky=tk.NSEW, pady=(6, 0))
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=self.log_text.yview)
        scrollbar.grid(row=11, column=3, sticky=tk.NS, pady=(6, 0))
        self.log_text.configure(yscrollcommand=scrollbar.set)

    @staticmethod
    def sanitize_filename_component(raw: str) -> str:
        """将任意字符串转换为可用于文件名的安全片段。"""
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip())
        return cleaned.strip("._-") or "unknown"

    def extract_source_folder_name(self) -> str:
        """根据 Phone Directory 提取源文件夹名。"""
        phone_dir = self.phone_dir_var.get().strip().replace("\\", "/").rstrip("/")
        if not phone_dir:
            return "unknown"
        tail = phone_dir.split("/")[-1]
        return self.sanitize_filename_component(tail or "root")

    def update_progress_view(self) -> None:
        """刷新进度条和进度文本。"""
        suffix = f" | ~{self.latest_rate_text}" if self.latest_rate_text else ""
        if self.progress_total <= 0:
            self.progress_var.set(0.0)
            if self.running:
                self.progress_text_var.set(f"Progress: preparing...{suffix}")
            else:
                self.progress_text_var.set("Progress: idle")
            return

        progress = min(100.0, (self.progress_current / self.progress_total) * 100.0)
        self.progress_var.set(progress)
        self.progress_text_var.set(
            f"Progress: {self.progress_current}/{self.progress_total} ({progress:.1f}%){suffix}"
        )

    def parse_progress_line(self, line: str) -> None:
        """从后端输出中提取进度信息。"""
        stripped = line.strip()
        rate_match = RATE_PATTERN.match(stripped)
        if rate_match:
            human_rate = rate_match.group(2).strip()
            self.latest_rate_text = human_rate
            self.update_progress_view()
            return

        match = PROGRESS_PATTERN.match(stripped)
        if not match:
            return
        current = int(match.group(1))
        total = int(match.group(2))
        if total <= 0:
            return
        self.progress_total = total
        self.progress_current = min(current, total)
        self.update_progress_view()

    def save_run_log_file(self, exit_code: int) -> Path | None:
        """将本次 GUI 执行日志写入文件。"""
        if self.run_started_at is None:
            return None
        GUI_LOG_DIR.mkdir(parents=True, exist_ok=True)
        started = self.run_started_at.strftime("%Y%m%d_%H%M%S")
        source_name = self.extract_source_folder_name()
        log_path = GUI_LOG_DIR / f"gui_backup_{source_name}_{started}.log"

        header_lines = [
            f"Start: {self.run_started_at.isoformat()}",
            f"End: {dt.datetime.now().isoformat()}",
            f"Exit code: {exit_code}",
            f"Command: {' '.join(self.current_command)}",
            "",
            "Execution output:",
            "",
        ]
        content = "\n".join(header_lines + self.current_run_logs) + "\n"
        log_path.write_text(content, encoding="utf-8")
        return log_path

    def on_static_config_selected(self, _event: object | None = None) -> None:
        """根据下拉框选择，将静态配置填充到表单。"""
        selected_name = self.static_config_var.get().strip()
        if not selected_name:
            return
        selected = next((item for item in self.static_configs if str(item["name"]) == selected_name), None)
        if selected is None:
            return
        self.python_var.set(str(selected["python"]))
        self.phone_dir_var.set(str(selected["phone_dir"]))
        self.pc_dir_var.set(str(selected["pc_dir"]))
        self.start_date_var.set(str(selected["start_date"]))
        self.end_date_var.set(str(selected["end_date"]))
        self.all_files_var.set(bool(selected["all_files"]))
        self.write_sync_record_var.set(bool(selected["write_sync_record"]))

    def refresh_static_config_combo(self) -> None:
        """刷新静态配置下拉框内容。"""
        static_names = [str(item["name"]) for item in self.static_configs]
        self.static_config_combo.configure(values=static_names)
        if static_names:
            self.static_config_combo.configure(state="readonly")
            self.static_config_var.set(static_names[-1])
        else:
            self.static_config_combo.configure(state=tk.DISABLED)
            self.static_config_var.set("No static configs")

    def suggest_static_config_name(self) -> str:
        """生成一个默认的新静态配置名称。"""
        existing = {str(item["name"]).strip() for item in self.static_configs}
        index = 1
        while True:
            candidate = f"Config-{index}"
            if candidate not in existing:
                return candidate
            index += 1

    def on_save_static_click(self) -> None:
        """将当前表单保存为一条新的静态配置。"""
        try:
            values = self.collect_and_validate()
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        suggested_name = self.suggest_static_config_name()
        name = simpledialog.askstring(
            "Save Static Config",
            "Enter static config name:",
            initialvalue=suggested_name,
            parent=self.root,
        )
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showerror("Invalid input", "Static config name is required.")
            return
        if any(str(item["name"]).strip() == name for item in self.static_configs):
            messagebox.showerror("Invalid input", f"Static config name '{name}' already exists.")
            return

        new_item = {
            "name": name,
            "python": str(values["python"]),
            "phone_dir": str(values["phone_dir"]),
            "pc_dir": str(values["pc_dir"]),
            "start_date": str(values["start_date"]),
            "end_date": str(values["end_date"]),
            "all_files": bool(values["all_files"]),
            "write_sync_record": bool(values["write_sync_record"]),
        }
        self.static_configs.append(new_item)
        try:
            self.save_static_configs()
        except Exception as exc:
            self.static_configs.pop()
            messagebox.showerror("Save failed", f"Could not save static config: {exc}")
            return

        self.refresh_static_config_combo()
        self.on_static_config_selected()
        messagebox.showinfo("Saved", f"Static config '{name}' has been added.")

    def choose_pc_dir(self) -> None:
        """打开系统目录选择器，并将结果写入 PC 目录输入框。"""
        selected = filedialog.askdirectory(title="Select target backup directory")
        if selected:
            self.pc_dir_var.set(selected)

    def on_run_click(self) -> None:
        """
        “Run Backup”按钮处理流程：
        1) 校验输入
        2) 弹窗确认参数
        3) 保存配置
        4) 启动工作线程
        """
        if self.running:
            # 任务运行中时忽略重复点击。
            return
        try:
            values = self.collect_and_validate()
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        confirm_text = (
            "Please confirm backup settings:\n\n"
            f"Python: {values['python']}\n"
            f"Phone Dir: {values['phone_dir']}\n"
            f"PC Dir: {values['pc_dir']}\n"
            f"Start Date: {values['start_date']}\n"
            f"End Date: {values['end_date']}\n"
            f"All Files: {values['all_files']}\n"
            f"Write Sync Record: {values['write_sync_record']}\n"
        )
        if not messagebox.askyesno("Confirm", confirm_text):
            return

        try:
            self.save_config()
        except Exception as exc:
            messagebox.showwarning("Warning", f"Could not save GUI config: {exc}")

        command = self.build_command(values)
        self.current_command = command
        self.current_run_logs = []
        self.run_started_at = dt.datetime.now()
        self.progress_total = 0
        self.progress_current = 0
        self.latest_rate_text = ""
        self.progress_var.set(0.0)
        self.progress_text_var.set("Progress: preparing...")
        self.run_button.configure(state=tk.DISABLED)
        # 简单全量同步与日期范围备份互斥，避免同时抢占 adb。
        self.simple_full_sync_button.configure(state=tk.DISABLED)
        self.running = True
        self.log_line("")
        self.log_line("=" * 64)
        self.log_line("Starting backup process...")
        self.log_line("Command: " + " ".join(command))
        self.log_line("=" * 64)

        self.worker_thread = threading.Thread(target=self.execute_command, args=(command,), daemon=True)
        self.worker_thread.start()

    # ===== 简单全量同步（外部 cmd）实现 begin =====
    def collect_fullsync_inputs(self) -> dict[str, str]:
        """读取并校验简单全量同步所需参数。"""
        phone_dir = self.phone_dir_var.get().strip()
        pc_dir = self.pc_dir_var.get().strip()
        if not phone_dir:
            raise ValueError("Phone directory is required.")
        if not pc_dir:
            raise ValueError("PC directory is required.")
        return {"phone_dir": phone_dir, "pc_dir": pc_dir}

    @staticmethod
    def normalize_remote_dir_for_content_pull(phone_dir: str) -> str:
        """
        将远端目录规范化为“目录内容拉取”形式。

        `adb pull <dir>/. <dest>` 可避免多一层同名目录。
        """
        normalized = phone_dir.replace("\\", "/").rstrip("/")
        if not normalized:
            normalized = "/"
        return f"{normalized}/."

    @staticmethod
    def make_safe_batch_token(raw: str) -> str:
        """为批处理变量值做基础转义，减少特殊字符破坏命令解析。"""
        return raw.replace("^", "^^").replace("%", "%%")

    def make_simple_full_sync_log_file(self, phone_dir: str) -> Path:
        """生成简单全量同步日志路径。"""
        GUI_LOG_DIR.mkdir(parents=True, exist_ok=True)
        started = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        normalized = phone_dir.replace("\\", "/").rstrip("/")
        tail = normalized.split("/")[-1] if normalized else "root"
        source_name = self.sanitize_filename_component(tail or "root")
        return GUI_LOG_DIR / f"full_sync_{source_name}_{started}.log"

    def build_external_full_sync_batch(self, phone_dir_content: str, pc_dir: Path, log_path: Path) -> str:
        """构建外部 cmd 执行脚本内容。"""
        now_iso = dt.datetime.now().isoformat()
        phone_value = self.make_safe_batch_token(phone_dir_content)
        pc_value = self.make_safe_batch_token(str(pc_dir))
        log_value = self.make_safe_batch_token(str(log_path))
        return "\n".join(
            [
                "@echo off",
                "setlocal",
                f"set \"PHONE_DIR={phone_value}\"",
                f"set \"PC_DIR={pc_value}\"",
                f"set \"LOG_PATH={log_value}\"",
                "set \"ADB_EXIT=0\"",
                "echo [SimpleFullSync] Running external adb pull...",
                "echo [SimpleFullSync] Command: adb pull -a \"%PHONE_DIR%\" \"%PC_DIR%\"",
                "echo [SimpleFullSync] Log file: \"%LOG_PATH%\"",
                "echo.",
                f"echo [START] {now_iso} > \"%LOG_PATH%\"",
                "echo [MODE] simple_full_sync_external_cmd >> \"%LOG_PATH%\"",
                "echo [COMMAND] adb pull -a \"%PHONE_DIR%\" \"%PC_DIR%\" >> \"%LOG_PATH%\"",
                "echo. >> \"%LOG_PATH%\"",
                "adb pull -a \"%PHONE_DIR%\" \"%PC_DIR%\" >> \"%LOG_PATH%\" 2>&1",
                "set \"ADB_EXIT=%ERRORLEVEL%\"",
                "echo. >> \"%LOG_PATH%\"",
                "echo [EXIT_CODE] %ADB_EXIT% >> \"%LOG_PATH%\"",
                "echo [END] %DATE% %TIME% >> \"%LOG_PATH%\"",
                "type \"%LOG_PATH%\"",
                "echo.",
                "echo [SimpleFullSync] Finished. Exit code: %ADB_EXIT%",
                "echo [SimpleFullSync] Log file: \"%LOG_PATH%\"",
                "start \"\" cmd /c del /f /q \"%~f0\" >nul 2>&1",
                "endlocal & exit /b %ADB_EXIT%",
                "",
            ]
        )

    def launch_external_full_sync(self, phone_dir: str, pc_dir: str) -> Path:
        """通过 cmd 新开窗口执行简单全量同步。"""
        pc_target = Path(pc_dir).expanduser().resolve()
        pc_target.mkdir(parents=True, exist_ok=True)
        remote_content_dir = self.normalize_remote_dir_for_content_pull(phone_dir)
        log_path = self.make_simple_full_sync_log_file(phone_dir)
        GUI_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        runner_name = dt.datetime.now().strftime("full_sync_runner_%Y%m%d_%H%M%S_%f.cmd")
        runner_script = GUI_TEMP_DIR / runner_name
        runner_script.write_text(
            self.build_external_full_sync_batch(remote_content_dir, pc_target, log_path),
            encoding="utf-8",
            newline="\r\n",
        )
        subprocess.Popen(
            ["cmd.exe", "/k", str(runner_script)],
            cwd=str(PROJECT_ROOT),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return log_path

    def on_simple_full_sync_click(self) -> None:
        """点击“简单全量同步”：外部 cmd 执行 adb pull -a。"""
        if self.running:
            messagebox.showwarning("Busy", "Date-range backup is currently running.")
            return
        try:
            values = self.collect_fullsync_inputs()
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            return

        normalized_remote = self.normalize_remote_dir_for_content_pull(values["phone_dir"])
        resolved_pc_dir = Path(values["pc_dir"]).expanduser().resolve()
        confirm_text = (
            "Simple full sync will open a new cmd window.\n\n"
            f"Phone Dir (content): {normalized_remote}\n"
            f"PC Dir: {resolved_pc_dir}\n"
            "Command: adb pull -a <phone_dir/.> <pc_dir>\n"
            "GUI will not show running progress."
        )
        if not messagebox.askyesno("Confirm simple full sync", confirm_text):
            return

        try:
            self.save_config()
        except Exception as exc:
            messagebox.showwarning("Warning", f"Could not save GUI config: {exc}")

        try:
            log_path = self.launch_external_full_sync(
                phone_dir=values["phone_dir"],
                pc_dir=values["pc_dir"],
            )
        except Exception as exc:
            messagebox.showerror("Launch failed", f"Could not launch external cmd: {exc}")
            return

        messagebox.showinfo(
            "External sync started",
            "Simple full sync has been started in a new cmd window.\n\n"
            f"Log file:\n{log_path}\n\n"
            "The cmd window will stay open after command finishes.",
        )
    # ===== 简单全量同步（外部 cmd）实现 end =====

    def collect_and_validate(self) -> dict[str, str | bool]:
        """
        从表单读取参数并校验：
        - 必填项不为空
        - 日期格式合法
        - 开始日期 <= 结束日期
        """
        python_cmd = self.python_var.get().strip()
        phone_dir = self.phone_dir_var.get().strip()
        pc_dir = self.pc_dir_var.get().strip()
        start_date_raw = self.start_date_var.get().strip()
        end_date_raw = self.end_date_var.get().strip()
        all_files = self.all_files_var.get()
        write_sync_record = self.write_sync_record_var.get()

        if not python_cmd:
            raise ValueError("Python command is required.")
        if not phone_dir:
            raise ValueError("Phone directory is required.")
        if not pc_dir:
            raise ValueError("PC directory is required.")
        if not start_date_raw or not end_date_raw:
            raise ValueError("Start date and end date are required.")

        try:
            start_date = parse_date(start_date_raw)
            end_date = parse_date(end_date_raw)
        except ValueError:
            raise ValueError("Date format must be YYYY.MM.DD or YYYY-MM-DD or YYYY/MM/DD.") from None

        if start_date > end_date:
            raise ValueError("Start date cannot be later than end date.")

        return {
            "python": python_cmd,
            "phone_dir": phone_dir,
            "pc_dir": pc_dir,
            "start_date": start_date_raw,
            "end_date": end_date_raw,
            "all_files": all_files,
            "write_sync_record": write_sync_record,
        }

    def build_command(self, values: dict[str, str | bool]) -> list[str]:
        """
        构建子进程参数列表（argv）。

        通过 shlex 拆分支持复合 Python 命令（例如 `py -3`），
        然后拼接后端脚本路径和各项参数。
        """
        python_part = str(values["python"]).strip()
        try:
            python_items = shlex.split(python_part, posix=False)
        except ValueError:
            python_items = [python_part]
        if not python_items:
            python_items = [python_part]

        command = [
            *python_items,
            str(BACKUP_SCRIPT),
            "--phone-dir",
            str(values["phone_dir"]),
            "--pc-dir",
            str(values["pc_dir"]),
            "--start-date",
            str(values["start_date"]),
            "--end-date",
            str(values["end_date"]),
        ]
        # 通过 -u 关闭 Python stdout 缓冲，保证 GUI 实时收到后端日志输出。
        if "-u" not in python_items:
            command.insert(len(python_items), "-u")
        if bool(values["all_files"]):
            command.append("--all-files")
        if bool(values["write_sync_record"]):
            command.append("--write-sync-record")
        return command

    def execute_command(self, command: list[str]) -> None:
        """
        在工作线程执行后端脚本，并逐行转发输出。

        此处不直接操作 UI；输出会写入队列，由主线程
        `flush_log_queue` 统一处理，避免线程安全问题。
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
                cwd=str(PROJECT_ROOT),
                env=env,
            )
        except Exception as exc:
            self.log_queue.put(("line", f"Failed to start process: {exc}"))
            self.log_queue.put(("done", "launch_error"))
            return

        assert proc.stdout is not None
        for line in proc.stdout:
            self.log_queue.put(("line", line.rstrip("\n")))

        code = proc.wait()
        self.log_queue.put(("done", str(code)))

    def flush_log_queue(self) -> None:
        """
        在 Tk 主循环中轮询队列并消费消息。

        消息类型：
        - ("line", text)：追加一行日志
        - ("done", code)：收尾并弹出执行结果
        """
        while True:
            try:
                item_type, payload = self.log_queue.get_nowait()
            except queue.Empty:
                break

            if item_type == "line":
                self.log_line(payload)
                self.current_run_logs.append(payload)
                self.parse_progress_line(payload)
            elif item_type == "done":
                self.running = False
                self.run_button.configure(state=tk.NORMAL)
                # 恢复简单全量同步入口（仅日期范围任务结束时触发）。
                self.simple_full_sync_button.configure(state=tk.NORMAL)
                if payload == "launch_error":
                    self.progress_text_var.set("Progress: failed to launch")
                    log_file: Path | None = None
                    try:
                        log_file = self.save_run_log_file(-1)
                    except Exception as exc:
                        self.log_line(f"Could not save GUI log file: {exc}")
                    launch_text = "Could not launch backup command."
                    if log_file is not None:
                        launch_text += f"\nGUI log saved:\n{log_file}"
                    messagebox.showerror("Backup failed", launch_text)
                else:
                    exit_code = int(payload)
                    if exit_code == 0 and self.progress_total > 0:
                        self.progress_current = self.progress_total
                        self.update_progress_view()
                    log_file: Path | None = None
                    try:
                        log_file = self.save_run_log_file(exit_code)
                    except Exception as exc:
                        self.log_line(f"Could not save GUI log file: {exc}")
                    if exit_code == 0:
                        self.progress_text_var.set("Progress: completed")
                        done_text = "Backup finished successfully."
                        if log_file is not None:
                            done_text += f"\nGUI log saved:\n{log_file}"
                        messagebox.showinfo("Backup completed", done_text)
                    else:
                        self.progress_text_var.set("Progress: failed")
                        fail_text = f"Backup exited with code {exit_code}."
                        if log_file is not None:
                            fail_text += f"\nGUI log saved:\n{log_file}"
                        messagebox.showerror("Backup failed", fail_text)
                    self.log_line("")
                    self.log_line(f"Process finished with exit code {exit_code}.")

        self.root.after(80, self.flush_log_queue)

    def log_line(self, message: str) -> None:
        """向日志区追加一行，并滚动到最新位置。"""
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)

    def clear_log(self) -> None:
        """清空日志区内容。"""
        self.log_text.delete("1.0", tk.END)

    def on_close(self) -> None:
        """
        窗口关闭事件处理。

        若备份仍在运行，先询问用户是否确认退出。
        """
        if self.running:
            if not messagebox.askyesno("Exit", "Backup is still running. Exit GUI anyway?"):
                return
        try:
            self.save_config()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    """程序入口：先检查后端脚本，再启动 Tk 事件循环。"""
    if not BACKUP_SCRIPT.exists():
        messagebox.showerror(
            "Missing script",
            f"Cannot find backup script:\n{BACKUP_SCRIPT}",
        )
        return

    root = tk.Tk()
    app = BackupGuiApp(root)
    root.minsize(760, 520)
    root.mainloop()


if __name__ == "__main__":
    main()
