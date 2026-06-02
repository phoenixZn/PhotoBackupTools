"""
喜爱文件挑选复制工具 — GUI 主程序

使用说明（简要）：
  1. 安装依赖: pip install -r requirements.txt
  2. 运行: python favourite_files_app.py  或双击 run.bat
  3. 点击「选择 Base」指定要浏览的照片根目录（DFS 深度优先遍历）
  4. 用方向键 / 按钮翻页，F/W/S 标记或取消喜爱（写入各子目录 favourite_list.txt，仅文件名）
  5. 浏览位置自动写入 Base/preview_pos.txt（相对 Base 的路径）
  6. 设置 Target：PC 模式选本地目录；安卓模式填写如 /sdcard/Backup
  7. 「带路径复制」勾选时保留相对 Base 的目录结构
  8. CopyCurFile 复制当前图；CopyFavouriteFiles 复制 Base 树下全部喜爱文件
  9. PC 复制不覆盖已存在同名文件，冲突信息见底部日志区
 10. 上一目录 / 下一目录（PgUp / PgDn）：按 DFS 顺序跳到相邻子目录的首张图
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Optional

try:
    from PIL import Image, ImageOps, ImageTk
except ModuleNotFoundError:
    import sys
    import tkinter as tk
    from tkinter import messagebox

    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "缺少依赖 Pillow",
        "未安装 Pillow，无法启动本工具。\n\n"
        "请在 favourite_files_copy_tool 目录下执行：\n"
        "  pip install -r requirements.txt\n\n"
        "或双击 run.bat（会自动尝试安装依赖）。",
    )
    _root.destroy()
    sys.exit(1)

from copy_service import copy_all_favourites, copy_current_entry
from favourites import (
    FAVOURITE_FILE,
    filenames_in_container,
    read_favourite_names,
    read_preview_index,
    write_favourite_names,
    write_preview_position,
)
from traversal import ImageEntry, build_dir_first_indices, build_image_entries
from video_preview import VideoInfo, format_duration, get_video_info, get_video_thumbnail


class FavouriteFilesApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("喜爱文件挑选复制工具")
        self.root.geometry("1400x920")
        self.root.minsize(1000, 720)

        self.base_dir: Optional[Path] = None
        self.entries: list[ImageEntry] = []
        self.current_index = 0
        # container_dir -> set of favourite filenames
        self.favourites_by_dir: dict[Path, set[str]] = {}
        self.initial_favourites_by_dir: dict[Path, set[str]] = {}

        self.current_photo_image: Optional[ImageTk.PhotoImage] = None
        self.current_image_size: Optional[tuple[int, int]] = None
        self.current_shoot_time: Optional[str] = None
        self.current_video_info: Optional[VideoInfo] = None
        self._render_after_id: Optional[str] = None
        self._copy_running = False
        self.dir_first_indices: list[int] = []

        self._build_ui()
        self._bind_shortcuts()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._update_controls_state()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        top = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Button(top, text="选择 Base", command=self.choose_base).grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.base_var = tk.StringVar(value="未选择 Base 目录")
        ttk.Label(top, text="Base:").grid(row=0, column=1, sticky="w")
        self.base_label = ttk.Label(top, textvariable=self.base_var)
        self.base_label.grid(row=0, column=2, sticky="ew")
        top.columnconfigure(2, weight=1)

        ttk.Label(top, text="当前子目录:").grid(row=1, column=1, sticky="w", pady=(4, 0))
        self.subdir_var = tk.StringVar(value="-")
        ttk.Label(top, textvariable=self.subdir_var).grid(
            row=1, column=2, sticky="ew", pady=(4, 0)
        )

        target_frame = ttk.LabelFrame(top, text="Target 目录", padding=(8, 4))
        target_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        target_frame.columnconfigure(2, weight=1)

        self.target_mode_var = tk.StringVar(value="pc")
        ttk.Radiobutton(
            target_frame,
            text="PC",
            variable=self.target_mode_var,
            value="pc",
            command=self._on_target_mode_change,
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            target_frame,
            text="安卓",
            variable=self.target_mode_var,
            value="android",
            command=self._on_target_mode_change,
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.target_pc_var = tk.StringVar()
        self.target_pc_entry = ttk.Entry(target_frame, textvariable=self.target_pc_var)
        self.target_pc_entry.grid(row=0, column=2, sticky="ew", padx=(8, 4))
        self.target_pick_btn = ttk.Button(
            target_frame, text="选择 Target", command=self.choose_target_pc
        )
        self.target_pick_btn.grid(row=0, column=3, sticky="w")

        ttk.Label(target_frame, text="安卓路径:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.target_android_var = tk.StringVar(value="/sdcard/")
        self.target_android_entry = ttk.Entry(
            target_frame, textvariable=self.target_android_var, state="disabled"
        )
        self.target_android_entry.grid(
            row=1, column=2, columnspan=2, sticky="ew", padx=(8, 0), pady=(4, 0)
        )

        self.keep_path_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            target_frame, text="带路径复制", variable=self.keep_path_var
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # 主区：左侧大预览 + 右侧标记变化边栏
        main = ttk.Frame(self.root, padding=(10, 0, 10, 4))
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=0, minsize=272)
        main.rowconfigure(0, weight=1)

        preview_frame = ttk.Frame(main)
        preview_frame.grid(row=0, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)

        self.image_label = ttk.Label(
            preview_frame,
            anchor="center",
            text="请选择 Base 目录并开始浏览文件。",
        )
        self.image_label.grid(row=0, column=0, sticky="nsew")
        self.image_label.bind("<Configure>", self._on_image_area_resize)

        sidebar = ttk.Frame(main)
        sidebar.grid(row=0, column=1, sticky="ns", padx=(10, 0))
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(1, weight=1)

        self.favourite_state_var = tk.StringVar(value="未标记喜爱")
        self.favourite_state_label = tk.Label(
            sidebar,
            textvariable=self.favourite_state_var,
            font=("Microsoft YaHei UI", 14, "bold"),
            fg="#808080",
            anchor="w",
            wraplength=260,
        )
        self.favourite_state_label.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        changes_frame = ttk.LabelFrame(sidebar, text="本次运行标记变化", padding=(6, 4))
        changes_frame.grid(row=1, column=0, sticky="nsew")
        changes_frame.columnconfigure(0, weight=1)
        changes_frame.rowconfigure(1, weight=1)
        changes_frame.rowconfigure(3, weight=1)

        ttk.Label(changes_frame, text="新增标记").grid(row=0, column=0, sticky="w")
        self.added_listbox = tk.Listbox(changes_frame, height=10, exportselection=False)
        self.added_listbox.grid(row=1, column=0, sticky="nsew", pady=(2, 6))
        ttk.Label(changes_frame, text="取消标记").grid(row=2, column=0, sticky="w")
        self.removed_listbox = tk.Listbox(changes_frame, height=10, exportselection=False)
        self.removed_listbox.grid(row=3, column=0, sticky="nsew", pady=(2, 0))

        self.write_log_on_close_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            sidebar,
            text="关闭时记录标记变化日志",
            variable=self.write_log_on_close_var,
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

        bottom = ttk.Frame(self.root, padding=(10, 4, 10, 8))
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)

        self.info_var = tk.StringVar(value="文件信息：-")
        ttk.Label(bottom, textvariable=self.info_var).grid(row=0, column=0, sticky="w")

        self.status_var = tk.StringVar(value="状态：等待选择 Base 目录")
        ttk.Label(bottom, textvariable=self.status_var).grid(row=1, column=0, sticky="w")

        actions = ttk.Frame(bottom)
        actions.grid(row=2, column=0, sticky="w", pady=(6, 0))

        self.prev_btn = ttk.Button(actions, text="上一张 (← / A)", command=self.show_prev)
        self.prev_btn.grid(row=0, column=0, padx=(0, 4))
        self.next_btn = ttk.Button(actions, text="下一张 (→ / D)", command=self.show_next)
        self.next_btn.grid(row=0, column=1, padx=(0, 4))
        self.prev_dir_btn = ttk.Button(
            actions, text="上一目录 (PgUp)", command=self.show_prev_directory
        )
        self.prev_dir_btn.grid(row=0, column=2, padx=(0, 4))
        self.next_dir_btn = ttk.Button(
            actions, text="下一目录 (PgDn)", command=self.show_next_directory
        )
        self.next_dir_btn.grid(row=0, column=3, padx=(0, 4))
        self.toggle_btn = ttk.Button(
            actions, text="切换喜爱 (F)", command=self.toggle_favourite
        )
        self.toggle_btn.grid(row=0, column=4, padx=(0, 4))
        self.add_btn = ttk.Button(
            actions, text="标记喜爱 (W / ↑)", command=self.add_favourite
        )
        self.add_btn.grid(row=0, column=5, padx=(0, 4))
        self.remove_btn = ttk.Button(
            actions, text="取消喜爱 (S / ↓)", command=self.remove_favourite
        )
        self.remove_btn.grid(row=0, column=6, padx=(0, 4))
        self.open_btn = ttk.Button(actions, text="打开文件", command=self.open_current_image)
        self.open_btn.grid(row=0, column=7, padx=(4, 0))
        self.locate_btn = ttk.Button(actions, text="定位", command=self.locate_current_image)
        self.locate_btn.grid(row=0, column=8, padx=(4, 0))
        self.backup_btn = ttk.Button(
            actions, text="备份 list", command=self.backup_favourite_list
        )
        self.backup_btn.grid(row=0, column=9, padx=(4, 0))

        copy_actions = ttk.Frame(bottom)
        copy_actions.grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.copy_cur_btn = ttk.Button(
            copy_actions, text="CopyCurFile", command=self.on_copy_current
        )
        self.copy_cur_btn.grid(row=0, column=0, padx=(0, 8))
        self.copy_all_btn = ttk.Button(
            copy_actions, text="CopyFavouriteFiles", command=self.on_copy_all_favourites
        )
        self.copy_all_btn.grid(row=0, column=1)

        log_frame = ttk.LabelFrame(bottom, text="操作日志")
        log_frame.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        log_frame.columnconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=5, state="disabled", wrap=tk.WORD
        )
        self.log_text.grid(row=0, column=0, sticky="ew", padx=4, pady=4)

    def _on_target_mode_change(self) -> None:
        is_pc = self.target_mode_var.get() == "pc"
        if is_pc:
            self.target_pc_entry.configure(state="normal")
            self.target_pick_btn.configure(state="normal")
            self.target_android_entry.configure(state="disabled")
        else:
            self.target_pc_entry.configure(state="disabled")
            self.target_pick_btn.configure(state="disabled")
            self.target_android_entry.configure(state="normal")

    def append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"

        def _append() -> None:
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, line)
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")

        if threading.current_thread() is threading.main_thread():
            _append()
        else:
            self.root.after(0, _append)

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Left>", lambda _e: self.show_prev())
        self.root.bind("<Right>", lambda _e: self.show_next())
        self.root.bind("<a>", lambda _e: self.show_prev())
        self.root.bind("<A>", lambda _e: self.show_prev())
        self.root.bind("<d>", lambda _e: self.show_next())
        self.root.bind("<D>", lambda _e: self.show_next())
        self.root.bind("<f>", lambda _e: self.toggle_favourite())
        self.root.bind("<F>", lambda _e: self.toggle_favourite())
        self.root.bind("<w>", lambda _e: self.add_favourite())
        self.root.bind("<W>", lambda _e: self.add_favourite())
        self.root.bind("<s>", lambda _e: self.remove_favourite())
        self.root.bind("<S>", lambda _e: self.remove_favourite())
        self.root.bind("<Up>", lambda _e: self.add_favourite())
        self.root.bind("<Down>", lambda _e: self.remove_favourite())
        self.root.bind("<Home>", lambda _e: self.show_first())
        self.root.bind("<End>", lambda _e: self.show_last())
        self.root.bind("<Prior>", lambda _e: self.show_prev_directory())
        self.root.bind("<Next>", lambda _e: self.show_next_directory())

    def get_target(self) -> Optional[str]:
        if self.target_mode_var.get() == "pc":
            value = self.target_pc_var.get().strip()
        else:
            value = self.target_android_var.get().strip()
        return value or None

    def is_android_target(self) -> bool:
        return self.target_mode_var.get() == "android"

    def choose_base(self) -> None:
        selected = filedialog.askdirectory(title="选择 Base 目录")
        if not selected:
            return
        self.load_base(Path(selected))

    def choose_target_pc(self) -> None:
        selected = filedialog.askdirectory(title="选择 Target PC 目录")
        if selected:
            self.target_pc_var.set(selected)

    def load_base(self, directory: Path) -> None:
        self.base_dir = directory.resolve()
        self.base_var.set(str(self.base_dir))
        self.entries = build_image_entries(self.base_dir)
        self.dir_first_indices = build_dir_first_indices(self.entries)
        self.favourites_by_dir = {}
        self.initial_favourites_by_dir = {}

        seen_dirs: set[Path] = set()
        for entry in self.entries:
            cdir = entry.container_dir
            if cdir not in seen_dirs:
                seen_dirs.add(cdir)
                fav = read_favourite_names(cdir)
                self.favourites_by_dir[cdir] = set(fav)
                self.initial_favourites_by_dir[cdir] = set(fav)

        self.current_index = read_preview_index(self.entries, self.base_dir)
        image_count = sum(1 for e in self.entries if e.media_kind == "image")
        video_count = sum(1 for e in self.entries if e.media_kind == "video")
        self.append_log(
            f"已加载 Base: {self.base_dir}，共 {len(self.entries)} 个文件"
            f"（图片 {image_count}，视频 {video_count}）"
        )

        if not self.entries:
            self.dir_first_indices = []
            self._clear_preview("当前 Base 下未发现可预览的文件。")
            self.status_var.set("状态：目录中无文件")
            self.subdir_var.set("-")
            self._refresh_session_change_view()
            self._update_controls_state()
            return

        self.status_var.set(
            f"状态：已加载 {len(self.entries)} 个文件（图片 {image_count}，视频 {video_count}）"
        )
        self._refresh_session_change_view()
        self._show_current_image()

    def _clear_preview(self, text: str) -> None:
        self.current_photo_image = None
        self.current_image_size = None
        self.current_shoot_time = None
        self.current_video_info = None
        self.image_label.configure(image="", text=text)
        self.info_var.set("文件信息：-")
        self._refresh_favourite_state_label(False)

    def _current_entry(self) -> Optional[ImageEntry]:
        if not self.entries:
            return None
        return self.entries[self.current_index]

    def _favourites_for(self, container_dir: Path) -> set[str]:
        if container_dir not in self.favourites_by_dir:
            self.favourites_by_dir[container_dir] = read_favourite_names(container_dir)
        return self.favourites_by_dir[container_dir]

    def _save_favourites_for(self, container_dir: Path) -> None:
        names = self._favourites_for(container_dir)
        hint = filenames_in_container(self.entries, container_dir)
        write_favourite_names(container_dir, names, hint)

    def _show_current_image(self) -> None:
        entry = self._current_entry()
        if not entry:
            return

        self.subdir_var.set(str(entry.container_dir))
        current_path = entry.absolute_path

        if entry.media_kind == "video":
            self._show_current_video(entry, current_path)
            return

        try:
            with Image.open(current_path) as img:
                img = ImageOps.exif_transpose(img)
                self.current_image_size = img.size
                self.current_shoot_time = self._extract_shoot_time(img)
                self.current_video_info = None
                display = img.copy()
                max_w, max_h = self._get_preview_size()
                display.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
        except OSError as exc:
            self.current_photo_image = None
            self.current_image_size = None
            self.current_shoot_time = None
            self.current_video_info = None
            self.image_label.configure(
                image="", text=f"无法预览图片：{current_path.name}\n{exc}"
            )
            self.info_var.set(f"文件信息：{current_path.name}（加载失败）")
            self.status_var.set("状态：加载图片失败")
            self._update_controls_state()
            return

        self.current_photo_image = ImageTk.PhotoImage(display)
        self.image_label.configure(image=self.current_photo_image, text="")

        if self.base_dir:
            write_preview_position(self.base_dir, entry.rel_to_base)
        self._refresh_photo_info()
        self._update_controls_state()

    def _show_current_video(self, entry: ImageEntry, current_path: Path) -> None:
        max_w, max_h = self._get_preview_size()
        self.current_video_info = get_video_info(current_path)
        if self.current_video_info:
            self.current_image_size = (
                self.current_video_info.width,
                self.current_video_info.height,
            )
        else:
            self.current_image_size = None
        self.current_shoot_time = None

        display = get_video_thumbnail(current_path, (max_w, max_h))
        if display is None:
            self.current_photo_image = None
            self.image_label.configure(
                image="",
                text=f"无法预览视频：{current_path.name}\n请用「打开文件」在系统中播放。",
            )
            self.info_var.set(f"文件信息：{current_path.name}（预览失败）")
            self.status_var.set("状态：加载视频预览失败")
            if self.base_dir:
                write_preview_position(self.base_dir, entry.rel_to_base)
            self._refresh_photo_info()
            self._update_controls_state()
            return

        self.current_photo_image = ImageTk.PhotoImage(display)
        self.image_label.configure(image=self.current_photo_image, text="")

        if self.base_dir:
            write_preview_position(self.base_dir, entry.rel_to_base)
        self._refresh_photo_info()
        self._update_controls_state()

    def _refresh_photo_info(self) -> None:
        entry = self._current_entry()
        if not entry:
            self.info_var.set("文件信息：-")
            self._refresh_favourite_state_label(False)
            return

        path = entry.absolute_path
        index_text = f"{self.current_index + 1}/{len(self.entries)}"
        resolution = (
            f"{self.current_image_size[0]}x{self.current_image_size[1]}"
            if self.current_image_size
            else "未知"
        )
        size_text = self._human_size(path.stat().st_size)
        modified_time = datetime.fromtimestamp(path.stat().st_mtime).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        shoot_time = self.current_shoot_time or "无"
        starred = "是" if self._is_current_favourite() else "否"
        if entry.media_kind == "video":
            duration = format_duration(
                self.current_video_info.duration_sec if self.current_video_info else None
            )
            fps_text = (
                f"{self.current_video_info.fps:.2f}fps"
                if self.current_video_info and self.current_video_info.fps
                else "未知"
            )
            self.info_var.set(
                f"文件：{path.name} | 类型：视频 | 序号：{index_text} | "
                f"相对路径：{entry.rel_to_base} | 分辨率：{resolution} | "
                f"时长：{duration} | 帧率：{fps_text} | 大小：{size_text} | "
                f"修改时间：{modified_time} | 喜爱：{starred}"
            )
        else:
            self.info_var.set(
                f"文件：{path.name} | 类型：图片 | 序号：{index_text} | "
                f"相对路径：{entry.rel_to_base} | 分辨率：{resolution} | "
                f"大小：{size_text} | 修改时间：{modified_time} | "
                f"拍摄时间：{shoot_time} | 喜爱：{starred}"
            )
        self._refresh_favourite_state_label(starred == "是")

    def _refresh_favourite_state_label(self, is_favourite: bool) -> None:
        if is_favourite:
            self.favourite_state_var.set("已标记喜爱")
            self.favourite_state_label.configure(fg="#C62828")
        else:
            self.favourite_state_var.set("未标记喜爱")
            self.favourite_state_label.configure(fg="#808080")

    def _is_current_favourite(self) -> bool:
        entry = self._current_entry()
        if not entry:
            return False
        return entry.absolute_path.name in self._favourites_for(entry.container_dir)

    def _session_changes(self) -> tuple[list[str], list[str]]:
        added: list[str] = []
        removed: list[str] = []
        all_dirs = set(self.initial_favourites_by_dir) | set(self.favourites_by_dir)
        for cdir in sorted(all_dirs, key=lambda p: str(p).lower()):
            initial = self.initial_favourites_by_dir.get(cdir, set())
            current = self.favourites_by_dir.get(cdir, set())
            rel_dir = (
                str(cdir.relative_to(self.base_dir)).replace("\\", "/")
                if self.base_dir and cdir != self.base_dir
                else "."
            )
            for name in sorted(current - initial):
                added.append(f"{rel_dir}/{name}" if rel_dir != "." else name)
            for name in sorted(initial - current):
                removed.append(f"{rel_dir}/{name}" if rel_dir != "." else name)
        return added, removed

    def _refresh_session_change_view(self) -> None:
        added, removed = self._session_changes()
        self.added_listbox.delete(0, tk.END)
        for item in added or ["（无）"]:
            self.added_listbox.insert(tk.END, item)
        self.removed_listbox.delete(0, tk.END)
        for item in removed or ["（无）"]:
            self.removed_listbox.insert(tk.END, item)

    def _update_controls_state(self) -> None:
        has_image = bool(self.entries)
        prev_state = "normal" if has_image and self.current_index > 0 else "disabled"
        next_state = (
            "normal"
            if has_image and self.current_index < len(self.entries) - 1
            else "disabled"
        )
        common_state = "normal" if has_image else "disabled"
        copy_state = "normal" if has_image and not self._copy_running else "disabled"
        copy_all_state = (
            "normal" if self.base_dir and not self._copy_running else "disabled"
        )

        dir_pos = self._current_directory_position()
        prev_dir_state = (
            "normal"
            if has_image and dir_pos is not None and dir_pos > 0
            else "disabled"
        )
        next_dir_state = (
            "normal"
            if has_image
            and dir_pos is not None
            and dir_pos < len(self.dir_first_indices) - 1
            else "disabled"
        )

        self.prev_btn.configure(state=prev_state)
        self.next_btn.configure(state=next_state)
        self.prev_dir_btn.configure(state=prev_dir_state)
        self.next_dir_btn.configure(state=next_dir_state)
        self.toggle_btn.configure(state=common_state)
        self.add_btn.configure(state=common_state)
        self.remove_btn.configure(state=common_state)
        self.open_btn.configure(state=common_state)
        self.locate_btn.configure(state=common_state)
        self.backup_btn.configure(
            state="normal" if self._current_entry() else "disabled"
        )
        self.copy_cur_btn.configure(state=copy_state)
        self.copy_all_btn.configure(state=copy_all_state)

    def _on_image_area_resize(self, _event: tk.Event) -> None:
        if not self.entries:
            return
        if self._render_after_id:
            self.root.after_cancel(self._render_after_id)
        self._render_after_id = self.root.after(120, self._show_current_image)

    def _get_preview_size(self) -> tuple[int, int]:
        width = max(self.image_label.winfo_width() - 20, 300)
        height = max(self.image_label.winfo_height() - 20, 300)
        return width, height

    def show_prev(self) -> None:
        if self.current_index <= 0:
            return
        self.current_index -= 1
        self._show_current_image()

    def show_next(self) -> None:
        if self.current_index >= len(self.entries) - 1:
            return
        self.current_index += 1
        self._show_current_image()

    def show_first(self) -> None:
        if not self.entries:
            return
        self.current_index = 0
        self._show_current_image()

    def show_last(self) -> None:
        if not self.entries:
            return
        self.current_index = len(self.entries) - 1
        self._show_current_image()

    def _current_directory_position(self) -> Optional[int]:
        """当前图所在子目录在 dir_first_indices 中的序号。"""
        if not self.entries or not self.dir_first_indices:
            return None
        current_dir = self.entries[self.current_index].container_dir.resolve()
        for pos, start_idx in enumerate(self.dir_first_indices):
            if self.entries[start_idx].container_dir.resolve() == current_dir:
                return pos
        return None

    def show_prev_directory(self) -> None:
        pos = self._current_directory_position()
        if pos is None or pos <= 0:
            return
        self.current_index = self.dir_first_indices[pos - 1]
        entry = self._current_entry()
        if entry:
            rel = (
                str(entry.container_dir.relative_to(self.base_dir)).replace("\\", "/")
                if self.base_dir and entry.container_dir != self.base_dir
                else "."
            )
            self.status_var.set(f"状态：切换到上一目录 -> {rel}")
        self._show_current_image()

    def show_next_directory(self) -> None:
        pos = self._current_directory_position()
        if pos is None or pos >= len(self.dir_first_indices) - 1:
            return
        self.current_index = self.dir_first_indices[pos + 1]
        entry = self._current_entry()
        if entry:
            rel = (
                str(entry.container_dir.relative_to(self.base_dir)).replace("\\", "/")
                if self.base_dir and entry.container_dir != self.base_dir
                else "."
            )
            self.status_var.set(f"状态：切换到下一目录 -> {rel}")
        self._show_current_image()

    def toggle_favourite(self) -> None:
        entry = self._current_entry()
        if not entry:
            return
        name = entry.absolute_path.name
        fav = self._favourites_for(entry.container_dir)
        if name in fav:
            fav.remove(name)
            self.status_var.set(f"状态：已取消喜爱 -> {name}")
            self.append_log(f"取消喜爱: {entry.rel_to_base}")
        else:
            fav.add(name)
            self.status_var.set(f"状态：已标记喜爱 -> {name}")
            self.append_log(f"标记喜爱: {entry.rel_to_base}")
        self._save_favourites_for(entry.container_dir)
        self._refresh_photo_info()
        self._refresh_session_change_view()

    def add_favourite(self) -> None:
        entry = self._current_entry()
        if not entry:
            return
        name = entry.absolute_path.name
        fav = self._favourites_for(entry.container_dir)
        if name in fav:
            self.status_var.set(f"状态：已是喜爱 -> {name}")
            return
        fav.add(name)
        self._save_favourites_for(entry.container_dir)
        self._refresh_photo_info()
        self._refresh_session_change_view()
        self.status_var.set(f"状态：已标记喜爱 -> {name}")
        self.append_log(f"标记喜爱: {entry.rel_to_base}")

    def remove_favourite(self) -> None:
        entry = self._current_entry()
        if not entry:
            return
        name = entry.absolute_path.name
        fav = self._favourites_for(entry.container_dir)
        if name not in fav:
            self.status_var.set(f"状态：当前未标记喜爱 -> {name}")
            return
        fav.remove(name)
        self._save_favourites_for(entry.container_dir)
        self._refresh_photo_info()
        self._refresh_session_change_view()
        self.status_var.set(f"状态：已取消喜爱 -> {name}")
        self.append_log(f"取消喜爱: {entry.rel_to_base}")

    def open_current_image(self) -> None:
        entry = self._current_entry()
        if not entry:
            return
        try:
            os.startfile(str(entry.absolute_path))
        except OSError as exc:
            messagebox.showwarning("打开失败", f"无法用系统默认方式打开文件：\n{exc}")

    def locate_current_image(self) -> None:
        entry = self._current_entry()
        if not entry:
            return
        try:
            subprocess.run(
                ["explorer", "/select,", str(entry.absolute_path)],
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            messagebox.showwarning("定位失败", f"无法定位当前文件：\n{exc}")

    def backup_favourite_list(self) -> None:
        entry = self._current_entry()
        if not entry:
            messagebox.showwarning("备份失败", "请先选择 Base 并浏览文件。")
            return
        src = entry.container_dir / FAVOURITE_FILE
        if not src.exists():
            messagebox.showwarning(
                "备份失败",
                f"当前子目录下不存在 {FAVOURITE_FILE}。",
            )
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = entry.container_dir / f"favourite_list_{timestamp}.txt"
        try:
            shutil.copy2(src, dst)
            self.status_var.set(f"状态：已备份 -> {dst.name}")
            self.append_log(f"已备份喜爱清单: {dst}")
        except OSError as exc:
            messagebox.showwarning("备份失败", f"无法创建备份文件：\n{exc}")

    def _validate_copy_preconditions(self) -> bool:
        if not self.base_dir:
            messagebox.showwarning("复制失败", "请先选择 Base 目录。")
            return False
        target = self.get_target()
        if not target:
            messagebox.showwarning("复制失败", "请设置 Target 目录。")
            return False
        if self.is_android_target() and not target.startswith("/"):
            messagebox.showwarning(
                "复制失败", "安卓 Target 路径应以 / 开头，例如 /sdcard/Backup"
            )
            return False
        return True

    def on_copy_current(self) -> None:
        if not self._validate_copy_preconditions():
            return
        entry = self._current_entry()
        if not entry:
            return
        target = self.get_target()
        assert target is not None
        self.append_log(f"开始复制当前文件 -> {target}")
        self._run_copy_task(
            lambda: copy_current_entry(
                entry,
                self.base_dir,  # type: ignore[arg-type]
                target,
                self.keep_path_var.get(),
                self.is_android_target(),
                self.append_log,
            ),
            "当前文件复制完成",
        )

    def on_copy_all_favourites(self) -> None:
        if not self._validate_copy_preconditions():
            return
        target = self.get_target()
        assert target is not None
        self.append_log(f"开始批量复制喜爱文件 -> {target}")
        self._run_copy_task(
            lambda: copy_all_favourites(
                self.base_dir,  # type: ignore[arg-type]
                target,
                self.keep_path_var.get(),
                self.is_android_target(),
                self.append_log,
            ),
            "批量复制任务完成",
        )

    def _run_copy_task(self, task: Callable[[], object], done_msg: str) -> None:
        if self._copy_running:
            return
        self._copy_running = True
        self._update_controls_state()

        def worker() -> None:
            try:
                task()
            except Exception as exc:
                self.append_log(f"复制异常: {exc}")
            finally:

                def finish() -> None:
                    self._copy_running = False
                    self.append_log(done_msg)
                    self.status_var.set(f"状态：{done_msg}")
                    self._update_controls_state()

                self.root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _write_session_change_log(self) -> None:
        if not self.base_dir or not self.write_log_on_close_var.get():
            return
        added, removed = self._session_changes()
        if not added and not removed:
            return

        logs_dir = self.base_dir / ".logs"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = logs_dir / f"favourite_changes_{timestamp}.txt"
        lines = [
            f"timestamp: {datetime.now().isoformat(timespec='seconds')}",
            f"base: {self.base_dir}",
            f"added_count: {len(added)}",
            f"removed_count: {len(removed)}",
            "",
            "[added]",
            *added,
            "",
            "[removed]",
            *removed,
            "",
        ]
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_file.write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            messagebox.showwarning("日志写入失败", f"无法写入日志文件：{exc}")

    def _on_close(self) -> None:
        self._write_session_change_log()
        self.root.destroy()

    @staticmethod
    def _human_size(size: int) -> str:
        units = ["B", "KB", "MB", "GB"]
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
            value /= 1024
        return f"{size}B"

    @staticmethod
    def _extract_shoot_time(img: Image.Image) -> Optional[str]:
        try:
            exif = img.getexif()
        except Exception:
            return None
        if not exif:
            return None
        for tag_id in (36867, 36868, 306):
            value = exif.get(tag_id)
            if isinstance(value, str) and value.strip():
                return value.strip().replace(":", "-", 2)
        return None


def main() -> None:
    root = tk.Tk()
    app = FavouriteFilesApp(root)
    _ = app

    if len(sys.argv) > 1:
        startup_dir = Path(sys.argv[1]).expanduser()
        if startup_dir.is_dir():
            app.load_base(startup_dir)
        else:
            messagebox.showwarning(
                "启动参数无效",
                f"传入的目录不存在或不可访问：\n{startup_dir}",
            )

    root.mainloop()


if __name__ == "__main__":
    main()
