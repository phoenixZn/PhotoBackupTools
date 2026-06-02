"""视频缩略图抽帧（OpenCV 优先，ffmpeg 降级）与内存 LRU 缓存。"""

from __future__ import annotations

import shutil
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image

try:
    import cv2
except ModuleNotFoundError:
    cv2 = None  # type: ignore[assignment]

_CACHE_MAX = 64
_thumbnail_cache: OrderedDict[tuple[str, int, int, int], Image.Image] = OrderedDict()


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration_sec: float | None
    fps: float | None


def _cache_get(key: tuple[str, int, int, int]) -> Image.Image | None:
    if key not in _thumbnail_cache:
        return None
    _thumbnail_cache.move_to_end(key)
    return _thumbnail_cache[key].copy()


def _cache_put(key: tuple[str, int, int, int], image: Image.Image) -> None:
    _thumbnail_cache[key] = image.copy()
    _thumbnail_cache.move_to_end(key)
    while len(_thumbnail_cache) > _CACHE_MAX:
        _thumbnail_cache.popitem(last=False)


def _frame_is_blank(frame) -> bool:
    if frame is None or frame.size == 0:
        return True
    return float(frame.mean()) < 1.0


def _opencv_read_frame_at(cap, pos_sec: float) -> object | None:
    if pos_sec > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, pos_sec * 1000.0)
    ok, frame = cap.read()
    if ok and not _frame_is_blank(frame):
        return frame
    return None


def _frame_to_pil(frame) -> Image.Image:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


def _extract_frame_opencv(path: Path) -> Image.Image | None:
    if cv2 is None:
        return None
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        return None
    try:
        info = _read_video_info_from_capture(cap)
        frame = _opencv_read_frame_at(cap, 0.0)
        if frame is None and info and info.duration_sec:
            seek = min(1.0, info.duration_sec / 2.0)
            frame = _opencv_read_frame_at(cap, seek)
        if frame is None:
            return None
        return _frame_to_pil(frame)
    finally:
        cap.release()


def _extract_frame_ffmpeg(path: Path) -> Image.Image | None:
    if not shutil.which("ffmpeg"):
        return None
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-ss",
                "1",
                "-i",
                str(path),
                "-vframes",
                "1",
                "-f",
                "image2pipe",
                "-vcodec",
                "png",
                "pipe:1",
            ],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        with Image.open(BytesIO(proc.stdout)) as img:
            return img.convert("RGB")
    except OSError:
        return None


def _read_video_info_from_capture(cap) -> VideoInfo | None:
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    frame_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if width <= 0 or height <= 0:
        return None
    duration: float | None = None
    if fps > 0 and frame_count > 0:
        duration = frame_count / fps
    return VideoInfo(
        width=width,
        height=height,
        duration_sec=duration,
        fps=fps if fps > 0 else None,
    )


def get_video_info(path: Path) -> VideoInfo | None:
    if cv2 is None:
        return None
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        return None
    try:
        return _read_video_info_from_capture(cap)
    finally:
        cap.release()


def get_video_thumbnail(path: Path, max_size: tuple[int, int]) -> Image.Image | None:
    """返回已缩放的 PIL 缩略图；失败返回 None。"""
    resolved = path.resolve()
    try:
        mtime_ns = resolved.stat().st_mtime_ns
    except OSError:
        return None

    max_w, max_h = max_size
    cache_key = (str(resolved), mtime_ns, max_w, max_h)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    raw = _extract_frame_opencv(resolved)
    if raw is None:
        raw = _extract_frame_ffmpeg(resolved)
    if raw is None:
        return None

    display = raw.copy()
    display.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    _cache_put(cache_key, display)
    return display.copy()


def format_duration(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "未知"
    total = int(sec)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"
