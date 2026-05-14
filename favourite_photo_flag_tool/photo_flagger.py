from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from PIL import Image, ImageTk


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif"}
FAVOURITE_FILE = "favourite_list.txt"
PREVIEW_POS_FILE = "preview_pos.txt"


class PhotoFlaggerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Favourite Photo Flagger")
        self.root.geometry("1200x800")
        self.root.minsize(900, 640)

        self.current_dir: Optional[Path] = None
        self.image_paths: list[Path] = []
        self.current_index = 0
        self.favourites: set[str] = set()
        self.initial_favourites: set[str] = set()

        self.current_photo_image: Optional[ImageTk.PhotoImage] = None
        self.current_image_size: Optional[tuple[int, int]] = None
        self.current_shoot_time: Optional[str] = None
        self._render_after_id: Optional[str] = None

        self._build_ui()
        self._bind_shortcuts()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._update_controls_state()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        top = ttk.Frame(self.root, padding=(10, 10, 10, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Button(top, text="选择目录", command=self.choose_directory).grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.dir_var = tk.StringVar(value="未选择目录")
        ttk.Label(top, textvariable=self.dir_var).grid(row=0, column=1, sticky="ew")

        middle = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        middle.grid(row=1, column=0, sticky="nsew")
        middle.columnconfigure(0, weight=1)
        middle.rowconfigure(0, weight=1)

        self.image_label = ttk.Label(
            middle, anchor="center", text="请选择目录并开始浏览照片。"
        )
        self.image_label.grid(row=0, column=0, sticky="nsew")
        self.image_label.bind("<Configure>", self._on_image_area_resize)

        bottom = ttk.Frame(self.root, padding=(10, 6, 10, 10))
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)

        self.info_var = tk.StringVar(value="文件信息：-")
        ttk.Label(bottom, textvariable=self.info_var).grid(row=0, column=0, sticky="w")

        self.status_var = tk.StringVar(value="状态：等待选择目录")
        ttk.Label(bottom, textvariable=self.status_var).grid(row=1, column=0, sticky="w")

        self.favourite_state_var = tk.StringVar(value="未标记喜爱")
        self.favourite_state_label = tk.Label(
            bottom,
            textvariable=self.favourite_state_var,
            font=("Microsoft YaHei UI", 16, "bold"),
            fg="#808080",
            anchor="w",
        )
        self.favourite_state_label.grid(row=2, column=0, sticky="w", pady=(8, 0))

        self.write_log_on_close_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            bottom,
            text="关闭窗口时记录本次标记变化日志",
            variable=self.write_log_on_close_var,
        ).grid(row=3, column=0, sticky="w", pady=(8, 2))

        changes_frame = ttk.LabelFrame(bottom, text="本次运行标记变化（相对本次打开目录时）")
        changes_frame.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        changes_frame.columnconfigure(0, weight=1)
        changes_frame.columnconfigure(1, weight=1)
        changes_frame.rowconfigure(1, weight=1)

        ttk.Label(changes_frame, text="新增标记").grid(row=0, column=0, sticky="w", padx=(8, 0))
        ttk.Label(changes_frame, text="取消标记").grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.added_listbox = tk.Listbox(changes_frame, height=6)
        self.added_listbox.grid(row=1, column=0, sticky="nsew", padx=(8, 4), pady=(4, 8))
        self.removed_listbox = tk.Listbox(changes_frame, height=6)
        self.removed_listbox.grid(row=1, column=1, sticky="nsew", padx=(4, 8), pady=(4, 8))

        actions = ttk.Frame(bottom)
        actions.grid(row=5, column=0, sticky="w", pady=(8, 0))

        self.prev_btn = ttk.Button(actions, text="上一张 (← / A)", command=self.show_prev)
        self.prev_btn.grid(row=0, column=0, padx=(0, 6))

        self.next_btn = ttk.Button(actions, text="下一张 (→ / D)", command=self.show_next)
        self.next_btn.grid(row=0, column=1, padx=(0, 6))

        self.toggle_btn = ttk.Button(
            actions, text="切换喜爱 (F)", command=self.toggle_favourite
        )
        self.toggle_btn.grid(row=0, column=2, padx=(0, 6))

        self.add_btn = ttk.Button(
            actions, text="标记喜爱 (W / ↑)", command=self.add_favourite
        )
        self.add_btn.grid(row=0, column=3, padx=(0, 6))

        self.remove_btn = ttk.Button(
            actions, text="取消喜爱 (S / ↓)", command=self.remove_favourite
        )
        self.remove_btn.grid(row=0, column=4)

        self.open_btn = ttk.Button(
            actions, text="打开图片", command=self.open_current_image
        )
        self.open_btn.grid(row=0, column=5, padx=(6, 0))

        self.locate_btn = ttk.Button(
            actions, text="定位", command=self.locate_current_image
        )
        self.locate_btn.grid(row=0, column=6, padx=(6, 0))

        self.backup_btn = ttk.Button(
            actions, text="备份list", command=self.backup_favourite_list
        )
        self.backup_btn.grid(row=0, column=7, padx=(6, 0))

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

    def choose_directory(self) -> None:
        selected = filedialog.askdirectory(title="选择照片目录")
        if not selected:
            return
        self.load_directory(Path(selected))

    def load_directory(self, directory: Path) -> None:
        self.current_dir = directory
        self.dir_var.set(str(directory))
        self.image_paths = self._scan_images(directory)
        self.favourites = self._read_favourites()
        self.initial_favourites = set(self.favourites)
        self.current_index = self._restore_index()

        if not self.image_paths:
            self.current_photo_image = None
            self.current_image_size = None
            self.current_shoot_time = None
            self.image_label.configure(image="", text="当前目录未发现可预览的图片。")
            self.info_var.set("文件信息：-")
            self.status_var.set("状态：目录中无图片")
            self._refresh_favourite_state_label(False)
            self._refresh_session_change_view()
            self._update_controls_state()
            return

        self.status_var.set(f"状态：已加载 {len(self.image_paths)} 张图片")
        self._refresh_session_change_view()
        self._show_current_image()

    def _scan_images(self, directory: Path) -> list[Path]:
        image_files = [
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ]
        image_files.sort(key=lambda p: str(p.relative_to(directory)).lower())
        return image_files

    def _read_favourites(self) -> set[str]:
        if not self.current_dir:
            return set()
        fav_path = self.current_dir / FAVOURITE_FILE
        if not fav_path.exists():
            return set()
        try:
            lines = fav_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            messagebox.showwarning("读取失败", f"无法读取 {FAVOURITE_FILE}：{exc}")
            return set()
        return {line.strip() for line in lines if line.strip()}

    def _restore_index(self) -> int:
        if not self.current_dir or not self.image_paths:
            return 0
        pos_path = self.current_dir / PREVIEW_POS_FILE
        if not pos_path.exists():
            return 0
        try:
            saved_rel = pos_path.read_text(encoding="utf-8").strip()
        except OSError:
            return 0
        if not saved_rel:
            return 0
        for idx, path in enumerate(self.image_paths):
            if self._relative_path(path) == saved_rel:
                return idx
        return 0

    def _show_current_image(self) -> None:
        if not self.image_paths:
            return
        current_path = self.image_paths[self.current_index]
        try:
            with Image.open(current_path) as img:
                self.current_image_size = img.size
                self.current_shoot_time = self._extract_shoot_time(img)
                display = img.copy()
                max_w, max_h = self._get_preview_size()
                display.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
        except OSError as exc:
            self.current_photo_image = None
            self.current_image_size = None
            self.current_shoot_time = None
            self.image_label.configure(
                image="", text=f"无法预览图片：{current_path.name}\n{exc}"
            )
            self.info_var.set(f"文件信息：{current_path.name}（加载失败）")
            self.status_var.set("状态：加载图片失败")
            self._update_controls_state()
            return

        self.current_photo_image = ImageTk.PhotoImage(display)
        self.image_label.configure(image=self.current_photo_image, text="")

        self._save_preview_position()
        self._refresh_photo_info()
        self._update_controls_state()

    def _refresh_photo_info(self) -> None:
        if not self.image_paths:
            self.info_var.set("文件信息：-")
            self._refresh_favourite_state_label(False)
            return
        path = self.image_paths[self.current_index]
        index_text = f"{self.current_index + 1}/{len(self.image_paths)}"
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
        self.info_var.set(
            f"文件：{path.name} | 序号：{index_text} | 分辨率：{resolution} | "
            f"大小：{size_text} | 修改时间：{modified_time} | 拍摄时间：{shoot_time} | 喜爱：{starred}"
        )
        self._refresh_favourite_state_label(starred == "是")

    def _refresh_favourite_state_label(self, is_favourite: bool) -> None:
        if is_favourite:
            self.favourite_state_var.set("已标记喜爱")
            self.favourite_state_label.configure(fg="#C62828")
            return
        self.favourite_state_var.set("未标记喜爱")
        self.favourite_state_label.configure(fg="#808080")

    def _session_added(self) -> list[str]:
        return sorted(self.favourites - self.initial_favourites)

    def _session_removed(self) -> list[str]:
        return sorted(self.initial_favourites - self.favourites)

    def _refresh_session_change_view(self) -> None:
        added = self._session_added()
        removed = self._session_removed()

        self.added_listbox.delete(0, tk.END)
        if added:
            for rel in added:
                self.added_listbox.insert(tk.END, rel)
        else:
            self.added_listbox.insert(tk.END, "（无）")

        self.removed_listbox.delete(0, tk.END)
        if removed:
            for rel in removed:
                self.removed_listbox.insert(tk.END, rel)
        else:
            self.removed_listbox.insert(tk.END, "（无）")

    def _update_controls_state(self) -> None:
        has_image = bool(self.image_paths)
        prev_state = "normal" if has_image and self.current_index > 0 else "disabled"
        next_state = (
            "normal"
            if has_image and self.current_index < len(self.image_paths) - 1
            else "disabled"
        )
        common_state = "normal" if has_image else "disabled"

        self.prev_btn.configure(state=prev_state)
        self.next_btn.configure(state=next_state)
        self.toggle_btn.configure(state=common_state)
        self.add_btn.configure(state=common_state)
        self.remove_btn.configure(state=common_state)
        self.open_btn.configure(state=common_state)
        self.locate_btn.configure(state=common_state)
        backup_state = "normal" if self.current_dir else "disabled"
        self.backup_btn.configure(state=backup_state)

    def _on_image_area_resize(self, _event: tk.Event) -> None:
        if not self.image_paths:
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
        if self.current_index >= len(self.image_paths) - 1:
            return
        self.current_index += 1
        self._show_current_image()

    def show_first(self) -> None:
        if not self.image_paths:
            return
        self.current_index = 0
        self._show_current_image()

    def show_last(self) -> None:
        if not self.image_paths:
            return
        self.current_index = len(self.image_paths) - 1
        self._show_current_image()

    def toggle_favourite(self) -> None:
        if not self.image_paths:
            return
        rel = self._relative_path(self.image_paths[self.current_index])
        if rel in self.favourites:
            self.favourites.remove(rel)
            self.status_var.set(f"状态：已取消喜爱 -> {rel}")
        else:
            self.favourites.add(rel)
            self.status_var.set(f"状态：已标记喜爱 -> {rel}")
        self._save_favourites()
        self._refresh_photo_info()
        self._refresh_session_change_view()

    def add_favourite(self) -> None:
        if not self.image_paths:
            return
        rel = self._relative_path(self.image_paths[self.current_index])
        if rel in self.favourites:
            self.status_var.set(f"状态：已是喜爱 -> {rel}")
            return
        self.favourites.add(rel)
        self._save_favourites()
        self._refresh_photo_info()
        self._refresh_session_change_view()
        self.status_var.set(f"状态：已标记喜爱 -> {rel}")

    def remove_favourite(self) -> None:
        if not self.image_paths:
            return
        rel = self._relative_path(self.image_paths[self.current_index])
        if rel not in self.favourites:
            self.status_var.set(f"状态：当前未标记喜爱 -> {rel}")
            return
        self.favourites.remove(rel)
        self._save_favourites()
        self._refresh_photo_info()
        self._refresh_session_change_view()
        self.status_var.set(f"状态：已取消喜爱 -> {rel}")

    def open_current_image(self) -> None:
        if not self.image_paths:
            return
        current_path = self.image_paths[self.current_index]
        try:
            os.startfile(str(current_path))
        except AttributeError:
            # Fallback for non-Windows environments.
            if sys.platform == "darwin":
                subprocess.run(["open", str(current_path)], check=True)
            else:
                subprocess.run(["xdg-open", str(current_path)], check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            messagebox.showwarning("打开失败", f"无法用系统默认方式打开图片：\n{exc}")

    def locate_current_image(self) -> None:
        if not self.image_paths:
            return
        current_path = self.image_paths[self.current_index]
        try:
            if sys.platform.startswith("win"):
                # explorer.exe 在成功定位时也可能返回非 0，不能用返回码判失败。
                subprocess.run(
                    ["explorer", "/select,", str(current_path)],
                    check=False,
                )
            elif sys.platform == "darwin":
                subprocess.run(["open", "-R", str(current_path)], check=True)
            else:
                subprocess.run(["xdg-open", str(current_path.parent)], check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            messagebox.showwarning("定位失败", f"无法定位当前图片：\n{exc}")

    def backup_favourite_list(self) -> None:
        if not self.current_dir:
            messagebox.showwarning("备份失败", "请先选择目录。")
            return
        src = self.current_dir / FAVOURITE_FILE
        if not src.exists():
            messagebox.showwarning(
                "备份失败",
                f"当前目录下不存在 {FAVOURITE_FILE}，请先标记至少一张喜爱图片。",
            )
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = self.current_dir / f"favourite_list_{timestamp}.txt"
        try:
            shutil.copy2(src, dst)
            self.status_var.set(f"状态：已备份 -> {dst.name}")
        except OSError as exc:
            messagebox.showwarning("备份失败", f"无法创建备份文件：\n{exc}")

    def _save_favourites(self) -> None:
        if not self.current_dir:
            return
        fav_path = self.current_dir / FAVOURITE_FILE
        ordered = self._ordered_favourites()
        content = "\n".join(ordered)
        if content:
            content += "\n"
        try:
            fav_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            messagebox.showwarning("保存失败", f"无法写入 {FAVOURITE_FILE}：{exc}")

    def _save_preview_position(self) -> None:
        if not self.current_dir or not self.image_paths:
            return
        rel = self._relative_path(self.image_paths[self.current_index])
        pos_path = self.current_dir / PREVIEW_POS_FILE
        try:
            pos_path.write_text(rel + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showwarning("保存失败", f"无法写入 {PREVIEW_POS_FILE}：{exc}")

    def _ordered_favourites(self) -> list[str]:
        if not self.image_paths:
            return sorted(self.favourites)
        order_map = {self._relative_path(path): idx for idx, path in enumerate(self.image_paths)}
        return sorted(self.favourites, key=lambda rel: order_map.get(rel, 10**9))

    def _is_current_favourite(self) -> bool:
        if not self.image_paths:
            return False
        rel = self._relative_path(self.image_paths[self.current_index])
        return rel in self.favourites

    def _relative_path(self, path: Path) -> str:
        if not self.current_dir:
            return path.name
        return str(path.relative_to(self.current_dir)).replace("\\", "/")

    def _write_session_change_log(self) -> None:
        if not self.current_dir:
            return
        if not self.write_log_on_close_var.get():
            return

        logs_dir = self.current_dir / ".logs"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = logs_dir / f"favourite_changes_{timestamp}.txt"
        added = self._session_added()
        removed = self._session_removed()

        lines = [
            f"timestamp: {datetime.now().isoformat(timespec='seconds')}",
            f"directory: {self.current_dir}",
            f"added_count: {len(added)}",
            f"removed_count: {len(removed)}",
            "",
            "[added]",
        ]
        lines.extend(added if added else ["(none)"])
        lines.append("")
        lines.append("[removed]")
        lines.extend(removed if removed else ["(none)"])
        lines.append("")

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
        for tag_id in (36867, 36868, 306):  # DateTimeOriginal, DateTimeDigitized, DateTime
            value = exif.get(tag_id)
            if isinstance(value, str) and value.strip():
                return value.strip().replace(":", "-", 2)
        return None


def main() -> None:
    root = tk.Tk()
    app = PhotoFlaggerApp(root)
    _ = app

    if len(sys.argv) > 1:
        startup_dir = Path(sys.argv[1]).expanduser()
        if startup_dir.is_dir():
            app.load_directory(startup_dir)
        else:
            messagebox.showwarning(
                "启动参数无效",
                f"传入的目录不存在或不可访问：\n{startup_dir}",
            )

    root.mainloop()


if __name__ == "__main__":
    main()
