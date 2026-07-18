"""本機歌詞辨識、音樂段標記與 SRT／動態透明字幕匯出工具。"""
from __future__ import annotations

import copy
import difflib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Iterable

from bootstrap import add_nvidia_dll_paths, block_conflicting_torch, check_ffmpeg, ensure_optional_package, ensure_required_packages, gpu_runtime_ready, install_gpu_runtime

block_conflicting_torch()
from subtitle_png_renderer import ANIMATION_STYLES
from prompts import PROMPT_STYLE_MAP as PROMPT_STYLES


APP_TITLE = "歌詞 SRT 產生器"
MUSIC_KIND = "音樂"
LYRIC_KIND = "歌詞"
SUPPORTED_AUDIO = [("音檔", "*.mp3 *.wav *.m4a *.flac *.aac *.ogg"), ("所有檔案", "*.*")]
SUPPORTED_LYRICS = [("歌詞文字檔", "*.txt *.lrc"), ("所有檔案", "*.*")]
PNG_ASPECTS = {
    "16:9（1920×1080）": (1920, 1080),
    "9:16（1080×1920）": (1080, 1920),
    "1:1（1080×1080）": (1080, 1080),
    "4:3（1440×1080）": (1440, 1080),
}
PNG_ANIMATION_STYLES = ANIMATION_STYLES

# 深色介面色票，比照主流影音剪輯工具（Premiere／DaVinci）的暗色風格。
DARK_BG = "#1e1f22"
DARK_PANEL = "#26282c"
DARK_FIELD = "#303338"
DARK_BORDER = "#3f4248"
DARK_FG = "#e6e6e6"
DARK_MUTED_FG = "#9aa0a6"
DARK_ACCENT = "#4c8bf5"
WAVE_CANVAS_BG = "#141518"
WAVE_RULER_BG = "#202226"
WAVE_RULER_TICK = "#3a3d42"
WAVE_RULER_TEXT = "#aeb4bd"
WAVE_EMPTY_LINE = "#43474e"
WAVE_FILL = "#4f7fb8"
WAVE_MID_LINE = "#345d8c"
WAVE_LABEL_FG = "#f5f6f8"
LYRIC_COLOR = "#5b9bd9"
LYRIC_OUTLINE = "#3d6fa0"
MUSIC_COLOR = "#3fa06a"
MUSIC_OUTLINE = "#2c7a4f"
DELETED_COLOR = "#6b6f76"
DELETED_OUTLINE = "#55585e"
SELECTED_OUTLINE = "#ff8a4c"
START_HANDLE_COLOR = "#37d67a"
END_HANDLE_COLOR = "#ff5c5c"
PLAYHEAD_COLOR = "#ff4d4f"


@dataclass
class Segment:
    start: float
    end: float
    kind: str
    text: str
    deleted: bool = False


def format_timecode(seconds: float) -> str:
    """顯示為 HH:MM:SS:CC（百分之一秒，方便人工編修）。"""
    seconds = max(0.0, float(seconds))
    centiseconds = int(round(seconds * 100))
    hours, rem = divmod(centiseconds, 360000)
    minutes, rem = divmod(rem, 6000)
    secs, cs = divmod(rem, 100)
    return f"{hours:02}:{minutes:02}:{secs:02}:{cs:02}"


def parse_timecode(value: str) -> float:
    """接受秒數、MM:SS 或 HH:MM:SS:CC。"""
    value = value.strip().replace(",", ".")
    if not value:
        raise ValueError("時間不可空白")
    if ":" not in value:
        return max(0.0, float(value))
    parts = [float(item) for item in value.split(":")]
    if len(parts) == 2:
        return max(0.0, parts[0] * 60 + parts[1])
    if len(parts) == 3:
        return max(0.0, parts[0] * 3600 + parts[1] * 60 + parts[2])
    if len(parts) == 4:
        return max(0.0, parts[0] * 3600 + parts[1] * 60 + parts[2] + parts[3] / 100)
    raise ValueError("請使用 秒數、MM:SS 或 HH:MM:SS:CC")


def srt_timecode(seconds: float) -> str:
    milliseconds = int(round(max(0.0, seconds) * 1000))
    hours, rem = divmod(milliseconds, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def probe_duration(path: Path) -> float:
    """優先用 soundfile，其他格式用 ffprobe。"""
    try:
        import soundfile as sf
        return float(sf.info(path).duration)
    except Exception:
        pass
    try:
        output = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            text=True, stderr=subprocess.STDOUT,
        )
        return float(output.strip())
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as exc:
        raise RuntimeError("無法取得音檔長度。請安裝 FFmpeg，或使用 WAV/FLAC 音檔。") from exc


def decode_waveform(path: Path, sample_rate: int = 4000) -> "np.ndarray":
    """用 ffmpeg 解碼成單聲道 PCM，回傳 -1~1 浮點數陣列，供聲波顯示使用。"""
    import numpy as np
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("找不到 FFmpeg 的 ffmpeg.exe，無法產生聲波顯示。請安裝 FFmpeg 並加入 PATH。")
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not result.stdout:
        message = result.stderr.decode("utf-8", "ignore").strip() or "未知錯誤"
        raise RuntimeError(f"聲波解碼失敗：{message}")
    return np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768.0


def normalize_lyrics(raw_segments: Iterable[object]) -> list[Segment]:
    result: list[Segment] = []
    for item in raw_segments:
        text = str(item.text).strip()
        if not text or item.end <= item.start:
            continue
        # Whisper 有時將一個很長的唱句合併；保留模型的時間範圍，交由使用者在表格拆分。
        result.append(Segment(float(item.start), float(item.end), LYRIC_KIND, text))
    return result


def word_timing_anchors(raw_segments: Iterable[object]) -> list[Segment]:
    """將 Whisper 的逐字時間轉為更細的對齊錨點；無逐字資料則回退整句。"""
    anchors: list[Segment] = []
    for segment in raw_segments:
        words = getattr(segment, "words", None) or []
        for word in words:
            start = getattr(word, "start", None)
            end = getattr(word, "end", None)
            text = str(getattr(word, "word", "")).strip()
            if text and start is not None and end is not None and end > start:
                anchors.append(Segment(float(start), float(end), LYRIC_KIND, text))
        if not words:
            text = str(getattr(segment, "text", "")).strip()
            if text and segment.end > segment.start:
                anchors.append(Segment(float(segment.start), float(segment.end), LYRIC_KIND, text))
    return _remove_hallucination_repeats(anchors)


def _remove_hallucination_repeats(anchors: list[Segment], min_repeat: int = 3) -> list[Segment]:
    """偵測連續重複文字，若出現 ≥ min_repeat 次則只保留第一次，其餘刪除。"""
    if not anchors:
        return anchors
    result: list[Segment] = []
    repeat_count = 0
    last_norm = ""
    for anchor in anchors:
        norm = _comparison_text(anchor.text)
        if norm == last_norm:
            repeat_count += 1
            if repeat_count < min_repeat:
                result.append(anchor)
        else:
            repeat_count = 0
            last_norm = norm
            result.append(anchor)
    return result


def read_lyric_lines(path: Path) -> list[str]:
    """讀取一般文字或 LRC 歌詞；LRC 的既有時間標籤會被忽略。"""
    last_error: UnicodeError | None = None
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp950"):
        try:
            lines = []
            for raw_line in path.read_text(encoding=encoding).splitlines():
                line = re.sub(r"^(?:\[\d{1,2}:\d{2}(?:\.\d{1,3})?\])+", "", raw_line).strip()
                if line and not re.fullmatch(r"\[[A-Za-z]+:.*\]", line):
                    lines.append(line)
            return lines
        except UnicodeError as exc:
            last_error = exc
    raise ValueError("無法讀取歌詞檔，請另存為 UTF-8 純文字檔。") from last_error


def _comparison_text(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value.lower())


def align_reference_lyrics(reference_lines: list[str], recognized: list[Segment], duration: float, max_span: int = 32) -> list[Segment]:
    """用 AI 聽到的時間錨點，依序套用使用者提供的原始歌詞文字。"""
    if not reference_lines:
        return recognized
    if not recognized:
        step = duration / len(reference_lines) if reference_lines and duration else 1.0
        return [Segment(i * step, min(duration, (i + 1) * step), LYRIC_KIND, text) for i, text in enumerate(reference_lines)]

    # 唱腔常使辨識文字與正確歌詞不完全一致。以平均每個時間錨點涵蓋的字數
    # 推算合理句長，避免文字相似度失準時一次吃掉太多（或太少）後續錨點。
    anchor_lengths = [max(1, len(_comparison_text(item.text))) for item in recognized]
    average_chars_per_anchor = max(1.0, sum(anchor_lengths) / len(anchor_lengths))
    result: list[Segment] = []
    anchor = 0
    for line_index, line in enumerate(reference_lines):
        remaining_lines = len(reference_lines) - line_index
        remaining_anchors = len(recognized) - anchor
        if remaining_anchors <= 0:
            previous_end = result[-1].end if result else 0.0
            step = max(0.05, (duration - previous_end) / remaining_lines)
            result.append(Segment(previous_end, min(duration, previous_end + step), LYRIC_KIND, line))
            continue
        # 逐字模式可合併較多錨點；仍保留足夠項目給後面的歌詞行。
        max_end = min(len(recognized) - (remaining_lines - 1), anchor + max_span)
        wanted = _comparison_text(line)
        expected_span = max(1, round(max(1, len(wanted)) / average_chars_per_anchor))
        best_end, best_score = anchor + 1, -1.0
        for end in range(anchor + 1, max_end + 1):
            heard = _comparison_text("".join(item.text for item in recognized[anchor:end]))
            text_score = difflib.SequenceMatcher(None, wanted, heard).ratio()
            length_penalty = 0.018 * abs((end - anchor) - expected_span)
            score = text_score - length_penalty
            if score > best_score:
                best_end, best_score = end, score
        result.append(Segment(recognized[anchor].start, recognized[best_end - 1].end, LYRIC_KIND, line))
        anchor = best_end
    # 尚未配到的 ASR 尾段仍屬於最後一句，避免尾字被截短。
    if anchor < len(recognized) and result:
        result[-1].end = max(result[-1].end, recognized[-1].end)
    return result


def add_music_markers(lyrics: list[Segment], duration: float, min_gap: float) -> list[Segment]:
    """以每句歌詞的前後空檔建立前奏、間奏、尾奏。"""
    if not lyrics:
        return [Segment(0.0, duration, MUSIC_KIND, "[純音樂]")] if duration else []
    output: list[Segment] = []
    cursor = 0.0
    for index, lyric in enumerate(lyrics):
        gap_end = max(cursor, lyric.start)
        if gap_end - cursor >= min_gap:
            label = "[前奏]" if index == 0 else "[間奏]"
            output.append(Segment(cursor, gap_end, MUSIC_KIND, label))
        output.append(lyric)
        cursor = max(cursor, lyric.end)
    if duration - cursor >= min_gap:
        output.append(Segment(cursor, duration, MUSIC_KIND, "[尾奏]"))
    return output


def _fix_overlapping_segments(segments: list[Segment]) -> list[Segment]:
    """修正相鄰段落的時間重疊：以中點切分，確保無重疊。"""
    for i in range(len(segments) - 1):
        if segments[i].end > segments[i + 1].start:
            mid = (segments[i].end + segments[i + 1].start) / 2
            segments[i].end = mid
            segments[i + 1].start = mid
    return segments


class WaveformView(ttk.Frame):
    """聲波、時間軸與逐句範圍；可直接拖曳句子的起訖點來校正時間。"""

    RULER_H = 18
    WAVE_H = 96
    MIN_PPS = 8.0
    MAX_PPS = 400.0
    EDGE_GRAB_PX = 6

    def __init__(self, master: tk.Widget, *, on_seek, on_select, on_edit) -> None:
        super().__init__(master)
        self.on_seek = on_seek
        self.on_select = on_select
        self.on_edit = on_edit
        self.duration = 0.0
        self.samples = None  # np.ndarray | None，讀取聲波前先顯示空白時間軸
        self.segments: list[Segment] = []
        self.selected_index: int | None = None
        self.pixels_per_second = 40.0
        self.playhead = 0.0
        self._drag: dict | None = None

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="拖曳邊界調整起訖時間；點聲波句子播放；點時間尺跳轉；滾輪縮放；按住中鍵拖曳平移", foreground=DARK_MUTED_FG).pack(side="left")
        ttk.Button(toolbar, text="－", width=3, command=lambda: self.zoom(1 / 1.5)).pack(side="right")
        ttk.Button(toolbar, text="符合視窗", command=self.fit_to_window).pack(side="right", padx=4)
        ttk.Button(toolbar, text="＋", width=3, command=lambda: self.zoom(1.5)).pack(side="right")

        canvas_area = ttk.Frame(self)
        canvas_area.pack(fill="both", expand=True)
        canvas_area.columnconfigure(0, weight=1)
        canvas_area.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(canvas_area, height=self.RULER_H + self.WAVE_H, background=WAVE_CANVAS_BG, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="ew")
        hscroll = ttk.Scrollbar(canvas_area, orient="horizontal", command=self.canvas.xview)
        hscroll.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(xscrollcommand=hscroll.set)

        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-2>", self._on_pan_start)
        self.canvas.bind("<B2-Motion>", self._on_pan_move)

    def set_audio(self, duration: float, samples=None) -> None:
        self.duration = max(0.0, duration)
        if samples is not None:
            self.samples = samples
        else:
            self.samples = None
        self.after(80, self.fit_to_window)

    def set_segments(self, segments: list[Segment], selected_index: int | None) -> None:
        self.segments = segments
        self.selected_index = selected_index
        self._redraw()

    def set_selected(self, index: int | None) -> None:
        self.selected_index = index
        self._redraw()

    def zoom(self, factor: float) -> None:
        if self.duration <= 0:
            return
        center_time = self._x_to_time(self.canvas.canvasx(self.canvas.winfo_width() / 2))
        self.pixels_per_second = min(self.MAX_PPS, max(self.MIN_PPS, self.pixels_per_second * factor))
        self._redraw()
        self.reveal_time(center_time, center=True)

    def _zoom_at(self, screen_x: float, factor: float) -> None:
        """以滑鼠所在時間點為中心縮放，縮放後同一時間點仍停留在游標下方。"""
        if self.duration <= 0:
            return
        anchor_time = self._x_to_time(self.canvas.canvasx(screen_x))
        self.pixels_per_second = min(self.MAX_PPS, max(self.MIN_PPS, self.pixels_per_second * factor))
        self._redraw()
        width = self._canvas_width()
        if width <= 0:
            return
        target = max(0.0, self._time_to_x(anchor_time) - screen_x)
        self.canvas.xview_moveto(min(1.0, target / width))

    def fit_to_window(self) -> None:
        width = self.canvas.winfo_width() or 900
        if self.duration > 0:
            self.pixels_per_second = max(self.MIN_PPS, min(self.MAX_PPS, width / self.duration))
        self._redraw()

    def set_playhead(self, t: float, follow: bool = False) -> None:
        self.playhead = max(0.0, min(self.duration, t))
        x = self._time_to_x(self.playhead)
        total_h = self.RULER_H + self.WAVE_H
        if self.canvas.find_withtag("playhead"):
            self.canvas.coords("playhead", x, 0, x, total_h)
        else:
            self.canvas.create_line(x, 0, x, total_h, fill=PLAYHEAD_COLOR, width=2, tags=("playhead",))
        if follow:
            self.reveal_time(self.playhead)

    def reveal_time(self, t: float, center: bool = False) -> None:
        width = self._canvas_width()
        view_w = self.canvas.winfo_width()
        if width <= 0 or view_w <= 0:
            return
        x = self._time_to_x(t)
        if center:
            self.canvas.xview_moveto(max(0.0, x - view_w / 2) / width)
            return
        left = self.canvas.canvasx(0)
        right = left + view_w
        if x < left + 20 or x > right - 20:
            self.canvas.xview_moveto(max(0.0, x - view_w / 2) / width)

    def _canvas_width(self) -> int:
        return max(1, int(self.duration * self.pixels_per_second))

    def _time_to_x(self, t: float) -> float:
        return t * self.pixels_per_second

    def _x_to_time(self, x: float) -> float:
        if not self.pixels_per_second:
            return 0.0
        return max(0.0, min(self.duration, x / self.pixels_per_second))

    def _nice_interval(self) -> float:
        target_px = 90
        raw = target_px / max(self.pixels_per_second, 0.01)
        for step in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600):
            if step >= raw:
                return step
        return 600.0

    def _redraw(self) -> None:
        self.canvas.delete("all")
        width = self._canvas_width()
        height = self.RULER_H + self.WAVE_H
        self.canvas.configure(scrollregion=(0, 0, width, height))
        self._draw_ruler(width, height)
        self._draw_waveform(width)
        self._draw_segments()
        self.canvas.create_line(self._time_to_x(self.playhead), 0, self._time_to_x(self.playhead), height, fill=PLAYHEAD_COLOR, width=2, tags=("playhead",))

    def _draw_ruler(self, width: int, height: int) -> None:
        self.canvas.create_rectangle(0, 0, width, self.RULER_H, fill=WAVE_RULER_BG, width=0)
        interval = self._nice_interval()
        steps = int(self.duration / interval) + 2 if interval else 0
        for i in range(steps):
            t = i * interval
            x = self._time_to_x(t)
            self.canvas.create_line(x, 0, x, height, fill=WAVE_RULER_TICK)
            self.canvas.create_text(x + 3, self.RULER_H / 2, text=format_timecode(t)[:8], anchor="w", font=("Consolas", 8), fill=WAVE_RULER_TEXT)

    def _draw_waveform(self, width: int) -> None:
        top = self.RULER_H
        mid = top + self.WAVE_H / 2
        half = self.WAVE_H / 2 - 4
        columns = max(1, min(width, 4000))
        if self.samples is None or len(self.samples) == 0 or width <= 0:
            self.canvas.create_line(0, mid, width, mid, fill=WAVE_EMPTY_LINE)
            return
        import numpy as np
        bucket = max(1, len(self.samples) // columns)
        usable = (len(self.samples) // bucket) * bucket
        if usable == 0:
            self.canvas.create_line(0, mid, width, mid, fill=WAVE_EMPTY_LINE)
            return
        peaks = np.abs(self.samples[:usable].reshape(-1, bucket)).max(axis=1)
        xs = np.linspace(0, width, num=len(peaks), endpoint=False)
        top_points = [(float(x), mid - float(p) * half) for x, p in zip(xs, peaks)]
        bottom_points = [(float(x), mid + float(p) * half) for x, p in zip(xs, peaks)]
        polygon: list[float] = []
        for x, y in top_points:
            polygon.extend((x, y))
        for x, y in reversed(bottom_points):
            polygon.extend((x, y))
        self.canvas.create_polygon(*polygon, fill=WAVE_FILL, outline="", width=0)
        self.canvas.create_line(0, mid, width, mid, fill=WAVE_MID_LINE)

    GAP_PX = 2

    def _draw_segments(self) -> None:
        top = self.RULER_H
        bottom = top + self.WAVE_H
        for index, segment in enumerate(self.segments):
            x0 = self._time_to_x(segment.start)
            x1 = self._time_to_x(segment.end)
            if segment.deleted:
                fill, outline = DELETED_COLOR, DELETED_OUTLINE
            elif segment.kind == MUSIC_KIND:
                fill, outline = MUSIC_COLOR, MUSIC_OUTLINE
            else:
                fill, outline = LYRIC_COLOR, LYRIC_OUTLINE
            width_px = 2 if index == self.selected_index else 1
            outline_color = SELECTED_OUTLINE if index == self.selected_index else outline
            # 兩句緊鄰時保留一點視覺間隙，避免色塊黏在一起、難以分辨與拖曳。
            mid = (x0 + x1) / 2
            fill_left = min(x0 + self.GAP_PX, mid)
            fill_right = max(x1 - self.GAP_PX, mid)
            self.canvas.create_rectangle(fill_left, top, fill_right, bottom, fill=fill, stipple="gray50", outline=outline_color, width=width_px, tags=("segment", f"seg:{index}"))
            label = segment.text if len(segment.text) <= 40 else segment.text[:39] + "…"
            self.canvas.create_text(fill_left + 4, top + 4, text=label, anchor="nw", font=("Microsoft JhengHei UI", 8), fill=WAVE_LABEL_FG, tags=("segment_label",))
            self.canvas.create_line(x0, top, x0, bottom, fill=START_HANDLE_COLOR, width=2, tags=("handle", f"handle:{index}:start"))
            self.canvas.create_line(x1, top, x1, bottom, fill=END_HANDLE_COLOR, width=2, tags=("handle", f"handle:{index}:end"))

    def _find_handle(self, x: float, y: float) -> tuple[int, str] | None:
        best = None
        best_dist = self.EDGE_GRAB_PX
        for item in self.canvas.find_withtag("handle"):
            coords = self.canvas.coords(item)
            if not coords:
                continue
            dist = abs(coords[0] - x)
            if dist <= best_dist:
                best_dist = dist
                tag = next((t for t in self.canvas.gettags(item) if t.startswith("handle:")), None)
                if tag:
                    _, idx_str, edge = tag.split(":")
                    best = (int(idx_str), edge)
        return best

    def _find_segment(self, x: float, y: float) -> int | None:
        for item in self.canvas.find_withtag("segment"):
            x0, y0, x1, y1 = self.canvas.coords(item)
            if x0 <= x <= x1 and y0 <= y <= y1:
                tag = next((t for t in self.canvas.gettags(item) if t.startswith("seg:")), None)
                if tag:
                    return int(tag.split(":")[1])
        return None

    def _on_press(self, event: tk.Event) -> None:
        x = self.canvas.canvasx(event.x)
        y = event.y
        self._drag = None
        if y <= self.RULER_H:
            self.on_seek(self._x_to_time(x))
            return
        handle = self._find_handle(x, y)
        if handle is not None:
            self._drag = {"index": handle[0], "edge": handle[1]}
            return
        index = self._find_segment(x, y)
        if index is not None:
            self.on_select(index)
        else:
            self.on_seek(self._x_to_time(x))

    def _on_drag(self, event: tk.Event) -> None:
        if not self._drag:
            return
        x = self.canvas.canvasx(event.x)
        t = self._x_to_time(x)
        index, edge = self._drag["index"], self._drag["edge"]
        if not (0 <= index < len(self.segments)):
            self._drag = None
            return
        segment = self.segments[index]
        if edge == "start":
            t = min(t, segment.end - 0.02)
        else:
            t = max(t, segment.start + 0.02)
        t = max(0.0, min(self.duration, t))
        self._drag["preview"] = t
        x = self._time_to_x(t)
        top, bottom = self.RULER_H, self.RULER_H + self.WAVE_H
        self.canvas.coords(f"handle:{index}:{edge}", x, top, x, bottom)
        rect = next(iter(self.canvas.find_withtag(f"seg:{index}")), None)
        if rect:
            x0, y0, x1, y1 = self.canvas.coords(rect)
            if edge == "start":
                self.canvas.coords(rect, x, y0, x1, y1)
            else:
                self.canvas.coords(rect, x0, y0, x, y1)

    def _on_release(self, _event: tk.Event) -> None:
        if self._drag and "preview" in self._drag:
            self.on_edit(self._drag["index"], self._drag["edge"], self._drag["preview"])
        self._drag = None

    def _on_wheel(self, event: tk.Event) -> None:
        self._zoom_at(event.x, 1.15 if event.delta > 0 else 1 / 1.15)

    def _on_pan_start(self, event: tk.Event) -> None:
        self.canvas.scan_mark(event.x, event.y)

    def _on_pan_move(self, event: tk.Event) -> None:
        self.canvas.scan_dragto(event.x, event.y, gain=1)


class LyricsSrtApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1360x960")
        self.minsize(1150, 760)
        self.audio_path: Path | None = None
        self.duration = 0.0
        self.reference_lyrics: list[str] = []
        self.dependencies_ready = threading.Event()
        self._dependency_lock = threading.Lock()
        self.segments: list[Segment] = []
        self.undo_stack: list[list[Segment]] = []
        self.redo_stack: list[list[Segment]] = []
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._editing: tuple[str, str] | None = None
        self.playback_offset = 0.0
        self.playback_started_at = 0.0
        self.playing = False
        self.playing_row: int | None = None
        self.play_stop_at: float | None = None
        self._ffplay: str | None = None
        self._audio_process: subprocess.Popen[str] | None = None
        self.preview_image_label: tk.Label | None = None
        self.preview_photo: object | None = None
        self.subtitle_font_size_var = tk.IntVar(value=64)
        self.subtitle_text_color = "#f6f7f4"
        self.subtitle_outline_color = "#100c09"
        self.subtitle_valign_var = tk.StringVar(value="下方")
        self.subtitle_halign_var = tk.StringVar(value="置中")
        self.subtitle_offset_x_var = tk.DoubleVar(value=0.0)
        self.subtitle_offset_y_var = tk.DoubleVar(value=0.0)
        self.anim_intensity_var = tk.DoubleVar(value=1.0)
        self.anim_speed_var = tk.DoubleVar(value=1.0)
        self.preview_zoom_var = tk.DoubleVar(value=1.0)
        
        # 初始化可用的中英文字型映射（從 Windows 註冊表掃描所有已安裝的字型）
        self.font_paths = {}
        try:
            import winreg
            # 1. 讀取系統全域字型 (HKLM)
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
                    info = winreg.QueryInfoKey(key)
                    for i in range(info[1]):
                        name, value, _ = winreg.EnumValue(key, i)
                        font_name = re.sub(r"\s*\((TrueType|OpenType|PostScript|Vertical|All Res)\)", "", name, flags=re.IGNORECASE)
                        if not os.path.isabs(value):
                            value = os.path.join(r"C:\Windows\Fonts", value)
                        ext = os.path.splitext(value)[1].lower()
                        if ext in (".ttf", ".ttc", ".otf") and os.path.exists(value):
                            self.font_paths[font_name] = value
            except Exception:
                pass
            
            # 2. 讀取目前使用者字型 (HKCU)
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
                    info = winreg.QueryInfoKey(key)
                    for i in range(info[1]):
                        name, value, _ = winreg.EnumValue(key, i)
                        font_name = re.sub(r"\s*\((TrueType|OpenType|PostScript|Vertical|All Res)\)", "", name, flags=re.IGNORECASE)
                        if not os.path.isabs(value):
                            local_appdata = os.environ.get("LOCALAPPDATA", "")
                            if local_appdata:
                                value = os.path.join(local_appdata, r"Microsoft\Windows\Fonts", value)
                        ext = os.path.splitext(value)[1].lower()
                        if ext in (".ttf", ".ttc", ".otf") and os.path.exists(value):
                            self.font_paths[font_name] = value
            except Exception:
                pass
        except Exception:
            pass

        # 字母排序
        self.font_paths = dict(sorted(self.font_paths.items(), key=lambda x: x[0].lower()))
        if not self.font_paths:
            self.font_paths["系統預設字型"] = ""
            
        # 尋找微軟正黑體或常用字型作為預設值
        default_font_name = "系統預設字型"
        candidates = ["Microsoft JhengHei UI Bold", "Microsoft JhengHei Bold", "微軟正黑體 粗體", "Microsoft JhengHei", "微軟正黑體"]
        for cand in candidates:
            match = next((name for name in self.font_paths if cand.lower() in name.lower()), None)
            if match:
                default_font_name = match
                break
        if default_font_name == "系統預設字型" and self.font_paths:
            default_font_name = next(iter(self.font_paths.keys()))
            
        self.subtitle_font_name_var = tk.StringVar()
        self.subtitle_font_name_var.set(default_font_name)
        
        self.img_provider_var = tk.StringVar(value="openai")
        self.img_api_key_var = tk.StringVar(value="")
        self.img_style_var = tk.StringVar(value="電影風")
        self._build_ui()
        self._apply_dark_titlebar()
        self.bind_all("<Control-z>", self.undo)
        self.bind_all("<Control-y>", self.redo)
        self.bind_all("<Control-s>", lambda _e: self.save_project())
        self.bind_all("<Control-o>", lambda _e: self.load_project())
        # 空白鍵＝播放／暫停；取代按鈕與勾選框原本「space＝按下」的預設行為，
        # 但不可回傳 "break"，否則會連 bindtags 後面的 all（全域）都一併擋掉，
        # 導致焦點停在任何按鈕上時（點擊按鈕後 ttk 會自動把焦點留在該按鈕）空白鍵完全沒反應。
        self.bind_class("TButton", "<space>", lambda _e: None)
        self.bind_class("TCheckbutton", "<space>", lambda _e: None)
        self.bind_all("<space>", self._on_space_key)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.after(120, self._poll_events)
        self.after(250, self._check_dependencies_async)
        self.after(75, self._update_playback)

    def _apply_dark_titlebar(self) -> None:
        """Windows 10/11 可讓標題列也套用深色；不支援的系統會靜默略過。"""
        try:
            import ctypes
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            value = ctypes.c_int(1)
            for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE：新舊 Windows 版本編號不同
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass

    def on_closing(self) -> None:
        self._stop_audio_process()
        self.destroy()

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        # "clam" 是唯一能讓 ttk 元件完全套用自訂顏色的內建主題；
        # 預設的 "vista" 主題會畫原生 Windows 控件，大多顏色設定會被忽略。
        style.theme_use("clam")
        self.configure(background=DARK_BG)
        style.configure(".", background=DARK_BG, foreground=DARK_FG, fieldbackground=DARK_FIELD,
                         bordercolor=DARK_BORDER, darkcolor=DARK_BG, lightcolor=DARK_BG,
                         troughcolor=DARK_FIELD, insertcolor=DARK_FG, font=("Microsoft JhengHei UI", 10))
        style.configure("TFrame", background=DARK_BG)
        style.configure("TLabelframe", background=DARK_BG, foreground=DARK_FG, bordercolor=DARK_BORDER)
        style.configure("TLabelframe.Label", background=DARK_BG, foreground=DARK_FG)
        style.configure("TLabel", background=DARK_BG, foreground=DARK_FG)
        style.configure("TButton", background=DARK_PANEL, foreground=DARK_FG, bordercolor=DARK_BORDER, focuscolor=DARK_ACCENT, padding=4)
        style.map("TButton", background=[("active", "#35383e"), ("pressed", "#2a2c30")], foreground=[("disabled", DARK_MUTED_FG)])
        style.configure("TCheckbutton", background=DARK_BG, foreground=DARK_FG)
        style.map("TCheckbutton", background=[("active", DARK_BG)])
        style.configure("TEntry", fieldbackground=DARK_FIELD, foreground=DARK_FG, insertcolor=DARK_FG, bordercolor=DARK_BORDER)
        style.configure("TCombobox", fieldbackground=DARK_FIELD, foreground=DARK_FG, background=DARK_FIELD, arrowcolor=DARK_FG, bordercolor=DARK_BORDER)
        style.map("TCombobox", fieldbackground=[("readonly", DARK_FIELD)], foreground=[("readonly", DARK_FG)])
        style.configure("Vertical.TScrollbar", background=DARK_PANEL, troughcolor=DARK_BG, bordercolor=DARK_BORDER, arrowcolor=DARK_FG)
        style.configure("Horizontal.TScrollbar", background=DARK_PANEL, troughcolor=DARK_BG, bordercolor=DARK_BORDER, arrowcolor=DARK_FG)
        style.configure("Horizontal.TScale", background=DARK_BG, troughcolor=DARK_FIELD)
        style.configure("TProgressbar", background=DARK_ACCENT, troughcolor=DARK_FIELD, bordercolor=DARK_BORDER)
        style.configure("Treeview", background=DARK_FIELD, fieldbackground=DARK_FIELD, foreground=DARK_FG,
                         rowheight=28, font=("Microsoft JhengHei UI", 10), bordercolor=DARK_BORDER)
        style.configure("Treeview.Heading", background=DARK_PANEL, foreground=DARK_FG, font=("Microsoft JhengHei UI", 10, "bold"))
        style.map("Treeview.Heading", background=[("active", "#35383e")])
        style.map("Treeview", background=[("selected", DARK_ACCENT)], foreground=[("selected", "#ffffff")])
        # ttk Combobox 的下拉清單是原生 tk Listbox，要另外用 option_add 上色。
        self.option_add("*TCombobox*Listbox.background", DARK_FIELD)
        self.option_add("*TCombobox*Listbox.foreground", DARK_FG)
        self.option_add("*TCombobox*Listbox.selectBackground", DARK_ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=0)
        self.rowconfigure(2, weight=1)

        # ── 頂部列：檔案操作 ──────────────────────────────────────────
        top = ttk.Frame(self, padding=(14, 10, 14, 6))
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Button(top, text="匯入音檔", command=self.import_audio).grid(row=0, column=0, padx=(0, 10))
        self.file_var = tk.StringVar(value="尚未選擇音檔")
        ttk.Label(top, textvariable=self.file_var, anchor="w").grid(row=0, column=1, sticky="ew")
        self.duration_var = tk.StringVar(value="長度：--")
        ttk.Label(top, textvariable=self.duration_var).grid(row=0, column=2, padx=(10, 0))
        ttk.Button(top, text="存檔", command=self.save_project).grid(row=0, column=3, padx=(16, 4))
        ttk.Button(top, text="載入", command=self.load_project).grid(row=0, column=4, padx=(4, 8))
        ttk.Button(top, text="匯入歌詞檔", command=self.import_lyrics).grid(row=0, column=5, padx=(16, 8))
        self.lyrics_file_var = tk.StringVar(value="未使用參考歌詞")
        ttk.Label(top, textvariable=self.lyrics_file_var, foreground=MUSIC_COLOR).grid(row=0, column=6, sticky="w")

        # ── AI 分析區 ───────────────────────────────────────────────
        ai_frame = ttk.LabelFrame(self, text=" 本機 AI 分析 ", padding=(10, 8))
        ai_frame.grid(row=1, column=0, sticky="ew", padx=14, pady=(6, 4))
        ai_frame.columnconfigure(9, weight=1)
        ttk.Label(ai_frame, text="模型").grid(row=0, column=0, padx=(0, 4))
        self.model_var = tk.StringVar(value="large-v3")
        ttk.Combobox(ai_frame, textvariable=self.model_var, width=9, state="readonly", values=("tiny", "base", "small", "medium", "large-v3")).grid(row=0, column=1, padx=(0, 8))
        ttk.Label(ai_frame, text="語言").grid(row=0, column=2, padx=(0, 4))
        self.language_var = tk.StringVar(value="zh")
        ttk.Combobox(ai_frame, textvariable=self.language_var, width=6, state="readonly", values=("auto", "zh", "ja", "en", "ko")).grid(row=0, column=3, padx=(0, 8))
        ttk.Label(ai_frame, text="運算").grid(row=0, column=4, padx=(0, 4))
        self.device_var = tk.StringVar(value="自動（GPU 優先）")
        ttk.Combobox(ai_frame, textvariable=self.device_var, width=14, state="readonly", values=("自動（GPU 優先）", "GPU", "CPU")).grid(row=0, column=5, padx=(0, 8))
        self.analyze_btn = ttk.Button(ai_frame, text="開始 AI 分析", command=self.analyze)
        self.analyze_btn.grid(row=0, column=6, padx=(14, 0))
        opts_row = ttk.Frame(ai_frame)
        opts_row.grid(row=1, column=0, columnspan=7, sticky="w", pady=(6, 0))
        self.precise_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_row, text="精準逐字對齊", variable=self.precise_var).pack(side="left", padx=(0, 10))
        self.vocals_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts_row, text="分離人聲", variable=self.vocals_var).pack(side="left", padx=(0, 10))
        self.force_align_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts_row, text="強制對齊", variable=self.force_align_var).pack(side="left", padx=(0, 10))
        self.intro_filter_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_row, text="前奏保護", variable=self.intro_filter_var).pack(side="left", padx=(0, 16))
        ttk.Label(opts_row, text="最短音樂段").pack(side="left", padx=(0, 2))
        self.min_gap_var = tk.StringVar(value="1.2")
        ttk.Entry(opts_row, textvariable=self.min_gap_var, width=5).pack(side="left", padx=(0, 8))
        ttk.Label(opts_row, text="隨機性").pack(side="left", padx=(0, 2))
        self.temperature_var = tk.StringVar(value="0")
        ttk.Entry(opts_row, textvariable=self.temperature_var, width=4).pack(side="left", padx=(0, 8))
        ttk.Label(opts_row, text="非語音").pack(side="left", padx=(0, 2))
        self.no_speech_var = tk.StringVar(value="0.6")
        ttk.Entry(opts_row, textvariable=self.no_speech_var, width=4).pack(side="left")
        status_row = ttk.Frame(ai_frame)
        status_row.grid(row=2, column=0, columnspan=7, sticky="ew", pady=(6, 0))
        status_row.columnconfigure(0, weight=1)
        self.progress_var = tk.StringVar(value="等待匯入音檔")
        ttk.Label(status_row, textvariable=self.progress_var, foreground=DARK_ACCENT).grid(row=0, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(status_row, mode="indeterminate", length=180)
        self.progress_bar.grid(row=0, column=1, sticky="e", padx=(8, 0))

        # ── 中央：聲波 + 歌詞列表 ───────────────────────────────────
        center = ttk.Frame(self)
        center.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 4))
        center.columnconfigure(0, weight=1)
        center.rowconfigure(1, weight=1)

        wave_frame = ttk.LabelFrame(center, text=" 聲波與時間軸 ", padding=(8, 4))
        wave_frame.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.waveform = WaveformView(wave_frame, on_seek=self._waveform_seek, on_select=self._activate_segment, on_edit=self._waveform_edit)
        self.waveform.pack(fill="both", expand=True)

        body = ttk.LabelFrame(center, text=" 歌詞時間軸 ", padding=(8, 4))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        columns = ("start", "end", "kind", "text")
        self.tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="browse")
        for key, title, width in (("start", "開始", 130), ("end", "結束", 130), ("kind", "類型", 80), ("text", "文字／標記", 420)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="center" if key != "text" else "w", stretch=key == "text")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<ButtonRelease-1>", self.play_clicked_row)
        self.tree.bind("<Double-1>", self._begin_edit)

        # ── 右側：字幕預覽 + 字幕樣式 ──────────────────────────────
        right = ttk.Frame(self)
        right.grid(row=1, column=1, rowspan=3, sticky="nsew", padx=(0, 14), pady=(6, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        preview_panel = ttk.LabelFrame(right, text=" 字幕預覽 ", padding=8)
        preview_panel.grid(row=0, column=0, sticky="nsew", pady=(0, 4))
        preview_panel.rowconfigure(0, weight=1)
        preview_panel.columnconfigure(0, weight=1)
        self.preview_image_label = tk.Canvas(preview_panel, background="#08090b", highlightthickness=0)
        self.preview_image_label.grid(row=0, column=0, sticky="nsew")
        self.preview_image_label.bind("<MouseWheel>", self._on_preview_scroll)
        self.preview_image_label.bind("<Button-4>", self._on_preview_scroll)
        self.preview_image_label.bind("<Button-5>", self._on_preview_scroll)

        style_frame = ttk.LabelFrame(right, text=" 字幕樣式 ", padding=(10, 8))
        style_frame.grid(row=1, column=0, sticky="ew")
        style_frame.columnconfigure(5, weight=1)

        r = 0
        ttk.Label(style_frame, text="比例").grid(row=r, column=0, sticky="w", padx=(0, 4))
        self.png_aspect_var = tk.StringVar(value="16:9（1920×1080）")
        aspect_combo = ttk.Combobox(style_frame, textvariable=self.png_aspect_var, state="readonly", width=14, values=tuple(PNG_ASPECTS))
        aspect_combo.grid(row=r, column=1, sticky="w", pady=(0, 4))
        aspect_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_preview())
        ttk.Label(style_frame, text="動畫").grid(row=r, column=2, sticky="w", padx=(12, 4))
        self.png_animation_var = tk.StringVar(value="逐字點亮")
        animation_combo = ttk.Combobox(style_frame, textvariable=self.png_animation_var, state="readonly", width=9, values=PNG_ANIMATION_STYLES)
        animation_combo.grid(row=r, column=3, sticky="w", pady=(0, 4))
        animation_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_preview())
        ttk.Label(style_frame, text="字級").grid(row=r, column=4, sticky="w", padx=(12, 4))
        size_spin = ttk.Spinbox(style_frame, from_=24, to=160, increment=2, textvariable=self.subtitle_font_size_var, width=5, command=self._refresh_preview)
        size_spin.grid(row=r, column=5, sticky="w", pady=(0, 4))
        size_spin.bind("<KeyRelease>", lambda _event: self._refresh_preview())

        r = 1
        self.text_color_btn = tk.Button(style_frame, text="文字色", width=6, command=self._pick_text_color,
                                         background=self.subtitle_text_color, activebackground=self.subtitle_text_color)
        self.text_color_btn.grid(row=r, column=0, sticky="w", pady=(0, 4))
        self.outline_color_btn = tk.Button(style_frame, text="外框色", width=6, command=self._pick_outline_color,
                                            background=self.subtitle_outline_color, activebackground=self.subtitle_outline_color,
                                            foreground="#ffffff", activeforeground="#ffffff")
        self.outline_color_btn.grid(row=r, column=1, sticky="w", padx=(4, 0), pady=(0, 4))
        ttk.Label(style_frame, text="垂直").grid(row=r, column=2, sticky="w", padx=(12, 4))
        valign_combo = ttk.Combobox(style_frame, textvariable=self.subtitle_valign_var, state="readonly", width=5, values=("上方", "中間", "下方"))
        valign_combo.grid(row=r, column=3, sticky="w", pady=(0, 4))
        valign_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_preview())
        ttk.Label(style_frame, text="水平").grid(row=r, column=4, sticky="w", padx=(12, 4))
        halign_combo = ttk.Combobox(style_frame, textvariable=self.subtitle_halign_var, state="readonly", width=5, values=("靠左", "置中", "靠右"))
        halign_combo.grid(row=r, column=5, sticky="w", pady=(0, 4))
        halign_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_preview())

        r = 2
        ttk.Label(style_frame, text="左右").grid(row=r, column=0, sticky="w", padx=(0, 4))
        ttk.Scale(style_frame, from_=-0.4, to=0.4, variable=self.subtitle_offset_x_var, length=80, command=lambda _v: self._refresh_preview()).grid(row=r, column=1, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Label(style_frame, text="上下").grid(row=r, column=3, sticky="w", padx=(12, 4))
        ttk.Scale(style_frame, from_=-0.4, to=0.4, variable=self.subtitle_offset_y_var, length=80, command=lambda _v: self._refresh_preview()).grid(row=r, column=4, columnspan=2, sticky="ew", pady=(0, 4))

        r = 3
        ttk.Label(style_frame, text="強度").grid(row=r, column=0, sticky="w", padx=(0, 4))
        ttk.Scale(style_frame, from_=0.0, to=3.0, variable=self.anim_intensity_var, length=80, command=lambda _v: self._refresh_preview()).grid(row=r, column=1, columnspan=2, sticky="ew")
        ttk.Label(style_frame, text="速度").grid(row=r, column=3, sticky="w", padx=(12, 4))
        ttk.Scale(style_frame, from_=0.2, to=3.0, variable=self.anim_speed_var, length=80, command=lambda _v: self._refresh_preview()).grid(row=r, column=4, columnspan=2, sticky="ew")
 
        r = 4
        ttk.Label(style_frame, text="字型").grid(row=r, column=0, sticky="w", padx=(0, 4), pady=(4, 0))
        self.font_combo = ttk.Combobox(style_frame, textvariable=self.subtitle_font_name_var, state="readonly", width=14)
        self.font_combo.grid(row=r, column=1, columnspan=3, sticky="ew", pady=(4, 0))
        self.font_combo["values"] = tuple(self.font_paths.keys())
        self.font_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_preview())
        ttk.Button(style_frame, text="瀏覽...", command=self._browse_font, width=6).grid(row=r, column=4, columnspan=2, sticky="w", padx=(12, 0), pady=(4, 0))

        # — AI 影像生成區 —
        img_frame = ttk.LabelFrame(right, text=" AI 影像生成 ", padding=(10, 8))
        img_frame.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        img_frame.columnconfigure(4, weight=1)

        r2 = 0
        ttk.Label(img_frame, text="服務").grid(row=r2, column=0, sticky="w", padx=(0, 4))
        provider_combo = ttk.Combobox(img_frame, textvariable=self.img_provider_var, state="readonly", width=8, values=("openai", "gemini"))
        provider_combo.grid(row=r2, column=1, sticky="w", pady=(0, 4))
        ttk.Label(img_frame, text="風格").grid(row=r2, column=2, sticky="w", padx=(8, 4))
        img_style_combo = ttk.Combobox(img_frame, textvariable=self.img_style_var, state="readonly", width=8, values=tuple(PROMPT_STYLES))
        img_style_combo.grid(row=r2, column=3, sticky="w", pady=(0, 4))

        r2 = 1
        ttk.Label(img_frame, text="API Key").grid(row=r2, column=0, sticky="w", padx=(0, 4))
        api_key_entry = ttk.Entry(img_frame, textvariable=self.img_api_key_var, show="*", width=28)
        api_key_entry.grid(row=r2, column=1, columnspan=3, sticky="ew", pady=(0, 4))
        ttk.Button(img_frame, text="測試", command=self._test_img_api, width=5).grid(row=r2, column=4, padx=(4, 0), pady=(0, 4))

        r2 = 2
        self.img_gen_btn = ttk.Button(img_frame, text="為每句歌詞生成影像", command=self._start_image_generation)
        self.img_gen_btn.grid(row=r2, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.img_export_btn = ttk.Button(img_frame, text="匯出歌詞影片（需先生成影像）", command=self._export_lyric_video)
        self.img_export_btn.grid(row=r2, column=3, columnspan=2, sticky="ew", pady=(4, 0), padx=(4, 0))

        # ── 底部列：播放控制 + 匯出 ──────────────────────────────────
        bottom = ttk.Frame(self, padding=(14, 6, 14, 12))
        bottom.grid(row=4, column=0, columnspan=2, sticky="ew")
        play_row = ttk.Frame(bottom)
        play_row.pack(fill="x")
        self.play_btn = ttk.Button(play_row, text="▶ 播放", command=self.toggle_playback)
        self.play_btn.pack(side="left")
        ttk.Button(play_row, text="■ 停止", command=self.stop_playback).pack(side="left", padx=(4, 10))
        self.play_time_var = tk.StringVar(value="00:00:00:00")
        ttk.Label(play_row, textvariable=self.play_time_var, width=13).pack(side="left")
        self.play_slider = ttk.Scale(play_row, from_=0, to=1, command=lambda _value: None)
        self.play_slider.pack(side="left", fill="x", expand=True, padx=(5, 10))
        self.play_slider.bind("<ButtonRelease-1>", self.seek_playback)
        ttk.Button(play_row, text="播選取句", command=lambda: self.play_selected_segment(only_segment=True)).pack(side="left", padx=(0, 10))
        ttk.Separator(play_row, orient="vertical").pack(side="left", fill="y", padx=4)
        ttk.Button(play_row, text="＋新增", command=self.add_segment).pack(side="left", padx=(0, 4))
        ttk.Button(play_row, text="刪除", command=self.toggle_deleted).pack(side="left", padx=(0, 4))
        ttk.Button(play_row, text="✂斷句", command=self.split_at_playhead).pack(side="left", padx=(0, 4))
        ttk.Button(play_row, text="復原", command=self.undo).pack(side="left", padx=(8, 0))
        ttk.Button(play_row, text="重做", command=self.redo).pack(side="left", padx=(4, 0))
        ttk.Separator(play_row, orient="vertical").pack(side="left", fill="y", padx=8)
        self.karaoke_btn = ttk.Button(play_row, text="匯出卡拉OK", command=self.export_karaoke_stems)
        self.karaoke_btn.pack(side="left", padx=(0, 4))
        ttk.Button(play_row, text="匯出 SRT", command=self.export_srt).pack(side="left", padx=(0, 4))
        self.png_export_btn = ttk.Button(play_row, text="匯出 PNG", command=self.export_dynamic_png)
        self.png_export_btn.pack(side="left")
        ttk.Label(play_row, text="雙擊欄位可編輯", foreground=DARK_MUTED_FG).pack(side="left", padx=(16, 0))

    def _check_dependencies_async(self) -> None:
        self._set_progress_status("正在確認必要套件（已安裝時不會下載）…", busy=True)
        threading.Thread(target=self._ensure_dependencies, daemon=True).start()

    def _set_progress_status(self, text: str, busy: bool | None = None) -> None:
        """以活動進度條顯示無法預先估算的下載與 AI 工作。"""
        self.progress_var.set(text)
        if busy is None:
            busy = text.startswith(("正在", "GPU DLL", "偵測到"))
        if busy:
            self.progress_bar.start(12)
        else:
            self.progress_bar.stop()

    def _ensure_dependencies(self) -> None:
        try:
            with self._dependency_lock:
                if self.dependencies_ready.is_set():
                    return
                ensure_required_packages(lambda text: self.events.put(("status", text)))
                self.dependencies_ready.set()
            self.events.put(("ready", None))
        except Exception as exc:
            self.events.put(("status", f"套件安裝異常（不影響核心功能）：{exc}"))
            self.dependencies_ready.set()

    def import_audio(self) -> None:
        selected = filedialog.askopenfilename(title="選擇音檔", filetypes=SUPPORTED_AUDIO)
        if not selected:
            return
        try:
            self.audio_path = Path(selected)
            self.duration = probe_duration(self.audio_path)
        except RuntimeError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.segments.clear(); self.undo_stack.clear(); self.redo_stack.clear()
        self.stop_playback(reset_position=True)
        self.play_slider.configure(to=max(0.01, self.duration))
        self.file_var.set(str(self.audio_path))
        self.duration_var.set(f"長度：{format_timecode(self.duration)}")
        self._set_progress_status("已匯入，請選擇模型後開始分析。", busy=False)
        self.refresh_tree()
        self.waveform.set_audio(self.duration, None)
        self._load_waveform_async()

    def _load_waveform_async(self) -> None:
        path = self.audio_path
        if not path:
            return
        threading.Thread(target=self._decode_waveform, args=(path,), daemon=True).start()

    def _decode_waveform(self, path: Path) -> None:
        try:
            self._ensure_dependencies()
            samples = decode_waveform(path)
            self.events.put(("waveform", (path, samples)))
        except Exception as exc:
            self.events.put(("waveform_error", str(exc)))

    def _load_audio_backend(self) -> None:
        ffplay = shutil.which("ffplay")
        if ffplay:
            self.events.put(("audio_ready", ffplay))
        else:
            self.events.put(("audio_error", "找不到 FFmpeg 的 ffplay.exe。請安裝 FFmpeg 並加入 PATH 後重新啟動程式。"))

    def _on_space_key(self, event: tk.Event) -> str | None:
        widget = event.widget
        if isinstance(widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
            return None
        self.toggle_playback()
        return "break"

    def toggle_playback(self) -> None:
        if not self.audio_path:
            messagebox.showinfo(APP_TITLE, "請先匯入音檔。")
            return
        if self._ffplay is None:
            self.play_btn.configure(state="disabled")
            self._set_progress_status("正在確認本機 FFmpeg 播放器…", busy=True)
            threading.Thread(target=self._load_audio_backend, daemon=True).start()
            return
        if self.playing:
            self._stop_audio_process()
            self.playback_offset += time.monotonic() - self.playback_started_at
            self.playing = False
            self.play_btn.configure(text="▶ 繼續")
        else:
            self._start_playback(float(self.play_slider.get()))

    def _start_playback(self, offset: float) -> None:
        if not self._ffplay or not self.audio_path:
            return
        offset = min(max(0.0, offset), max(0.0, self.duration - 0.01))
        try:
            self._stop_audio_process()
            self._audio_process = subprocess.Popen(
                [self._ffplay, "-nodisp", "-autoexit", "-loglevel", "error", "-ss", str(offset), str(self.audio_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"無法播放此音檔：\n{exc}")
            return
        self.playback_offset = offset
        self.playback_started_at = time.monotonic()
        self.playing = True
        self.play_btn.configure(text="❚❚ 暫停")

    def _stop_audio_process(self) -> None:
        if self._audio_process and self._audio_process.poll() is None:
            self._audio_process.terminate()
        self._audio_process = None

    def stop_playback(self, reset_position: bool = False) -> None:
        if self.playing:
            self.playback_offset += time.monotonic() - self.playback_started_at
        self._stop_audio_process()
        self.playing = False
        self.play_stop_at = None
        if reset_position:
            self.playback_offset = 0.0
        self.playing_row = None
        if hasattr(self, "play_slider"):
            self.play_slider.set(self.playback_offset)
        if hasattr(self, "play_time_var"):
            self.play_time_var.set(format_timecode(self.playback_offset))
        if hasattr(self, "play_btn"):
            self.play_btn.configure(text="▶ 播放")
        if hasattr(self, "waveform"):
            self.waveform.set_playhead(self.playback_offset)
        if hasattr(self, "tree"):
            self.refresh_tree()

    def seek_playback(self, _event: tk.Event) -> None:
        self.playback_offset = float(self.play_slider.get())
        self.waveform.set_playhead(self.playback_offset)
        if self.playing:
            self._start_playback(self.playback_offset)

    def _waveform_seek(self, t: float) -> None:
        self.playback_offset = t
        self.play_slider.set(t)
        self.play_time_var.set(format_timecode(t))
        self.waveform.set_playhead(t)
        if self.playing:
            self._start_playback(t)

    def _waveform_edit(self, index: int, edge: str, value: float) -> None:
        if not (0 <= index < len(self.segments)):
            return
        self.push_undo("拖曳調整時間")
        segment = self.segments[index]
        if edge == "start":
            segment.start = max(0.0, value)
        else:
            segment.end = min(self.duration, value)
        if segment.end <= segment.start:
            self.undo_stack.pop()
            return
        self.refresh_tree()

    def play_clicked_row(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self._activate_segment(int(row))

    def _activate_segment(self, index: int) -> None:
        """選取該句並移動播放頭；只有原本就在播放時才接續播放，不會自己開始播放。"""
        if not (0 <= index < len(self.segments)):
            return
        self.tree.selection_set(str(index))
        self.tree.see(str(index))
        segment = self.segments[index]
        self.playback_offset = segment.start
        self.play_slider.set(segment.start)
        self.play_time_var.set(format_timecode(segment.start))
        self.playing_row = index
        self.refresh_tree()
        self.waveform.set_selected(index)
        self.waveform.set_playhead(segment.start)
        if self.playing:
            self._start_playback(segment.start)

    def play_selected_segment(self, only_segment: bool = False) -> None:
        row = self.tree.selection()
        if not row:
            messagebox.showinfo(APP_TITLE, "請先點選要播放的列。")
            return
        segment = self.segments[int(row[0])]
        self.play_stop_at = segment.end if only_segment else None
        self.play_slider.set(segment.start)
        self.playback_offset = segment.start
        self.playing_row = int(row[0])
        self.refresh_tree()
        self.waveform.set_selected(int(row[0]))
        self.waveform.set_playhead(segment.start, follow=True)
        if self._ffplay is None:
            self.toggle_playback()
        else:
            self._start_playback(segment.start)

    def _update_playback(self) -> None:
        if self.playing:
            current = self.playback_offset + time.monotonic() - self.playback_started_at
            if current >= self.duration or (self.play_stop_at is not None and current >= self.play_stop_at) or (self._audio_process is not None and self._audio_process.poll() is not None):
                self.stop_playback(reset_position=current >= self.duration)
            else:
                self.play_slider.set(current)
                self.play_time_var.set(format_timecode(current))
                self.waveform.set_playhead(current, follow=True)
                self._refresh_preview(current)
                active = next((i for i, item in enumerate(self.segments) if not item.deleted and item.start <= current <= item.end), None)
                if active != self.playing_row:
                    self.playing_row = active
                    self.refresh_tree()
                    if active is not None and self.tree.exists(str(active)):
                        self.tree.selection_set(str(active))
                        self.tree.focus(str(active))
                        self.tree.see(str(active))
        else:
            self._refresh_preview()
        self.after(75, self._update_playback)

    def import_lyrics(self) -> None:
        selected = filedialog.askopenfilename(title="選擇歌詞文字檔", filetypes=SUPPORTED_LYRICS)
        if not selected:
            return
        try:
            self.reference_lyrics = read_lyric_lines(Path(selected))
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        if not self.reference_lyrics:
            messagebox.showerror(APP_TITLE, "歌詞檔沒有可用的文字行。請每句歌詞各佔一行。")
            return
        self.lyrics_file_var.set(f"參考歌詞：{Path(selected).name}（{len(self.reference_lyrics)} 句）")
        self._set_progress_status("已載入參考歌詞。AI 會只取得時間，SRT 將使用此歌詞原文。", busy=False)

    def analyze(self) -> None:
        if not self.audio_path:
            messagebox.showinfo(APP_TITLE, "請先匯入音檔。")
            return
        try:
            min_gap = max(0.0, float(self.min_gap_var.get()))
        except ValueError:
            messagebox.showerror(APP_TITLE, "最短音樂段必須是數字。")
            return
        try:
            temperature = max(0.0, float(self.temperature_var.get()))
            no_speech = float(self.no_speech_var.get())
        except ValueError:
            messagebox.showerror(APP_TITLE, "隨機性與非語音門檻必須是數字。")
            return
        lang = self.language_var.get()
        if lang in ("zh", "ja", "ko") and not self.vocals_var.get() and not self.force_align_var.get():
            if not messagebox.askyesno(APP_TITLE,
                f"偵測到語言為「{lang}」，建議同時開啟：\n\n"
                "  ☑ 先分離人聲（降低伴奏干擾）\n"
                "  ☑ 強制對齊（取得精確時間）\n\n"
                "目前設定仍可執行，但辨識度可能較低。\n是否繼續？"):
                return
        self.analyze_btn.configure(state="disabled")
        self._set_progress_status("正在載入本機模型並轉錄，首次使用會下載模型…", busy=True)
        reference = list(self.reference_lyrics)
        threading.Thread(target=self._run_transcription, args=(self.audio_path, self.model_var.get(), self.language_var.get(), self.device_var.get(), min_gap, reference, self.precise_var.get(), self.vocals_var.get(), temperature, no_speech, self.force_align_var.get(), self.intro_filter_var.get()), daemon=True).start()

    def _separate_vocals(self, path: Path) -> tuple[Path, Path | None]:
        """用 Demucs 建立人聲軌；回傳暫存目錄供呼叫端清理。"""
        ok = ensure_optional_package("demucs", "demucs>=4.0.1", lambda text: self.events.put(("status", text)))
        if not ok:
            raise RuntimeError("Demucs 安裝失敗，無法分離人聲。")
        output_dir = Path(tempfile.mkdtemp(prefix="lyrics_srt_demucs_"))
        self.events.put(("status", "正在分離人聲與伴奏，首次使用會下載 Demucs 模型…"))
        worker = Path(__file__).with_name("gpu_workers.py")
        args = [sys.executable, str(worker), "demucs", "--two-stems", "vocals", "-n", "htdemucs", "-o", str(output_dir), str(path)]
        result = subprocess.run(args, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise RuntimeError(f"人聲分離失敗：\n{result.stdout[-1200:]}")
        vocal_path = output_dir / "htdemucs" / path.stem / "vocals.wav"
        if not vocal_path.exists():
            shutil.rmtree(output_dir, ignore_errors=True)
            raise RuntimeError("人聲分離沒有產生 vocals.wav。")
        return vocal_path, output_dir

    def _run_transcription(self, path: Path, model_name: str, language: str, device_choice: str, min_gap: float, reference: list[str], precise: bool, separate_vocals: bool, temperature: float = 0.0, no_speech: float = 0.6, force_align: bool = False, intro_filter: bool = True) -> None:
        temporary_dir: Path | None = None
        try:
            self._ensure_dependencies()
            from faster_whisper import WhisperModel
            self.events.put(("status", "正在以本機 Whisper 分析音檔…"))
            use_gpu = device_choice != "CPU"
            try:
                if not use_gpu:
                    raise RuntimeError("使用者選擇 CPU")
                add_nvidia_dll_paths()
                if not gpu_runtime_ready():
                    self.events.put(("status", "偵測到 CUDA 執行庫不完整，正在自動下載所需 DLL…"))
                    install_gpu_runtime(lambda text: self.events.put(("status", text)))
                model = WhisperModel(model_name, device="cuda", compute_type="float16")
                self.events.put(("status", "已使用 NVIDIA GPU 加速。"))
            except Exception as gpu_error:
                dll_missing = any(word in str(gpu_error).lower() for word in ("cublas", "cudnn", ".dll", "library"))
                if (device_choice == "GPU" or dll_missing) and "使用者選擇" not in str(gpu_error):
                    self.events.put(("status", "GPU DLL 不完整，正在自動下載 NVIDIA 執行庫…"))
                    install_gpu_runtime(lambda text: self.events.put(("status", text)))
                    model = WhisperModel(model_name, device="cuda", compute_type="float16")
                else:
                    self.events.put(("status", f"GPU 暫時不可用，已改用 CPU：{gpu_error}"))
                    model = WhisperModel(model_name, device="cpu", compute_type="int8")
            source_path = path
            if separate_vocals:
                source_path, temporary_dir = self._separate_vocals(path)
            vocal_onset = 0.0
            prompt_text = "\n".join(reference[:6]) if reference else ""
            vad_params = dict(min_silence_duration_ms=400, speech_pad_ms=250)
            lyrics: list[Segment] = []
            wx_failed = False
            if reference and force_align:
                try:
                    self.events.put(("status", "正在安裝 whisperx 強制對齊套件（首次較久）…"))
                    ok = ensure_optional_package("whisperx", "whisperx>=3.3.0", lambda text: self.events.put(("status", text)))
                    if not ok:
                        raise RuntimeError("whisperx 安裝失敗")
                    lang_code = None if language == "auto" else language
                    wx_device = "cuda" if use_gpu else "cpu"
                    self.events.put(("status", "正在以 whisperx 轉錄並強制對齊（獨立行程執行，避免 GPU 函式庫衝突）…"))
                    wx_input = Path(tempfile.mkstemp(prefix="lyrics_srt_wx_in_", suffix=".json")[1])
                    wx_output = Path(tempfile.mkstemp(prefix="lyrics_srt_wx_out_", suffix=".json")[1])
                    try:
                        wx_input.write_text(json.dumps({
                            "source_path": str(source_path),
                            "model_name": model_name,
                            "language": lang_code,
                            "device": wx_device,
                            "temperature": temperature,
                        }), encoding="utf-8")
                        worker = Path(__file__).with_name("gpu_workers.py")
                        wx_proc = subprocess.run(
                            [sys.executable, str(worker), "whisperx", str(wx_input), str(wx_output)],
                            text=True, encoding="utf-8", errors="replace",
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        if wx_proc.returncode:
                            raise RuntimeError(f"whisperx 強制對齊失敗：\n{wx_proc.stdout[-1200:]}")
                        wx_words = json.loads(wx_output.read_text(encoding="utf-8"))["words"]
                    finally:
                        wx_input.unlink(missing_ok=True)
                        wx_output.unlink(missing_ok=True)
                    recognized = [Segment(float(w["start"]), float(w["end"]), LYRIC_KIND, str(w["text"])) for w in wx_words]
                    if intro_filter and vocal_onset >= min_gap:
                        recognized = [item for item in recognized if item.end > vocal_onset - 0.08]
                    lyrics = align_reference_lyrics(reference, recognized, self.duration)
                    self.events.put(("status", f"已以 whisperx CTC 強制對齊完成 {len(reference)} 句歌詞。"))
                except Exception as wx_exc:
                    self.events.put(("status", f"whisperx 安裝失敗（可能不支援此 Python 版本），改用標準對齊：{wx_exc}"))
                    wx_failed = True
            if not lyrics:
                if reference and intro_filter:
                    self.events.put(("status", "正在辨識前奏結束與第一句人聲位置…"))
                    onset_raw, _ = model.transcribe(str(source_path), language=None if language == "auto" else language, vad_filter=True, condition_on_previous_text=False, beam_size=5, initial_prompt=prompt_text or None)
                    onset_segments = normalize_lyrics(list(onset_raw))
                    if onset_segments:
                        vocal_onset = onset_segments[0].start
                self.events.put(("status", "正在分析音訊與逐字時間點，請稍候…"))
                raw, _ = model.transcribe(str(source_path), language=None if language == "auto" else language, vad_filter=not bool(reference), condition_on_previous_text=False, beam_size=5, word_timestamps=precise, initial_prompt=prompt_text or None)
                raw_segments = list(raw)
                recognized = word_timing_anchors(raw_segments) if precise else normalize_lyrics(raw_segments)
                if intro_filter and vocal_onset >= min_gap:
                    recognized = [item for item in recognized if item.end > vocal_onset - 0.08]
                lyrics = align_reference_lyrics(reference, recognized, self.duration) if reference else normalize_lyrics(raw_segments)
                if reference:
                    level = "逐字" if precise and any(getattr(item, "words", None) for item in raw_segments) else "逐句"
                    self.events.put(("status", f"已以 {len(reference)} 句參考歌詞進行 {level} 節奏對齊。"))
            self.events.put(("done", add_music_markers(lyrics, self.duration, min_gap)))
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            if temporary_dir:
                shutil.rmtree(temporary_dir, ignore_errors=True)

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "status": self._set_progress_status(str(payload))
                elif event == "ready": self._set_progress_status("必要套件已安裝，這次啟動不會再檢查或下載。", busy=False)
                elif event == "audio_ready":
                    self._ffplay = str(payload)
                    self.play_btn.configure(state="normal")
                    self._set_progress_status("播放器已就緒。按播放即可同步檢查每一句。", busy=False)
                    self._start_playback(float(self.play_slider.get()))
                elif event == "audio_error":
                    self.play_btn.configure(state="normal")
                    self._set_progress_status("播放器準備失敗", busy=False)
                    messagebox.showerror(APP_TITLE, f"無法準備播放器：\n{payload}")
                elif event == "done":
                    self.push_undo("AI 分析")
                    self.segments = payload  # type: ignore[assignment]
                    active = [s for s in self.segments if not s.deleted]
                    lyric_count = sum(1 for s in active if s.kind == LYRIC_KIND)
                    music_count = sum(1 for s in active if s.kind == MUSIC_KIND)
                    summary = f"分析完成：{lyric_count} 句歌詞、{music_count} 個音樂段，可雙擊表格校正後匯出。"
                    self.refresh_tree(); self._set_progress_status(summary, busy=False)
                    self.analyze_btn.configure(state="normal")
                elif event == "error":
                    self.analyze_btn.configure(state="normal")
                    self._set_progress_status("分析失敗", busy=False)
                    messagebox.showerror(APP_TITLE, f"AI 分析失敗：\n{payload}")
                elif event == "waveform":
                    src_path, samples = payload
                    if self.audio_path == src_path:
                        self.waveform.set_audio(self.duration, samples)
                elif event == "waveform_error":
                    self._set_progress_status(f"聲波顯示無法產生（不影響其他功能）：{payload}", busy=False)
                elif event == "png_done":
                    output, frames = payload
                    self.png_export_btn.configure(state="normal")
                    self._set_progress_status(f"已輸出 {frames:,} 張透明 PNG：{output}", busy=False)
                    messagebox.showinfo(APP_TITLE, f"動態字幕 PNG 序列已完成。\n\n{output}\n\n規格：透明 RGBA、30 fps、可直接以影像序列匯入剪輯軟體。")
                elif event == "png_error":
                    self.png_export_btn.configure(state="normal")
                    self._set_progress_status("動態 PNG 匯出失敗", busy=False)
                    messagebox.showerror(APP_TITLE, f"無法輸出動態字幕 PNG：\n{payload}")
                elif event == "karaoke_done":
                    self.karaoke_btn.configure(state="normal")
                    self._set_progress_status(f"已輸出人聲／伴奏：{payload}", busy=False)
                    messagebox.showinfo(APP_TITLE, f"卡拉OK人聲／伴奏分軌已完成。\n\n{payload}")
                elif event == "karaoke_error":
                    self.karaoke_btn.configure(state="normal")
                    self._set_progress_status("人聲／伴奏分軌失敗", busy=False)
                    messagebox.showerror(APP_TITLE, f"無法輸出人聲／伴奏：\n{payload}")
                elif event == "img_test_done":
                    ok, msg = payload
                    self._set_progress_status(f"API 測試：{msg}", busy=False)
                    if ok:
                        messagebox.showinfo(APP_TITLE, f"API 連線成功！\n{msg}")
                    else:
                        messagebox.showerror(APP_TITLE, f"API 連線失敗：\n{msg}")
                elif event == "img_gen_done":
                    output_dir, success, fail = payload
                    self._set_progress_status(f"影像生成完成：成功 {success} 張，失敗 {fail} 張", busy=False)
                    detail = f"已將 {success} 張歌詞影像儲存至：\n{output_dir}"
                    if fail:
                        detail += f"\n\n{fail} 張生成失敗，可重新執行補生成。"
                    messagebox.showinfo(APP_TITLE, detail)
                elif event == "img_error":
                    self._set_progress_status("影像生成失敗", busy=False)
                    messagebox.showerror(APP_TITLE, f"影像生成失敗：\n{payload}")
                elif event == "video_done":
                    self._set_progress_status(f"歌詞影片已匯出：{payload}", busy=False)
                    messagebox.showinfo(APP_TITLE, f"歌詞影片已完成！\n\n{payload}")
        except queue.Empty:
            pass
        self.after(120, self._poll_events)

    def refresh_tree(self) -> None:
        selected = self.tree.selection()
        for item in self.tree.get_children(): self.tree.delete(item)
        for i, segment in enumerate(self.segments):
            tag = "deleted" if segment.deleted else ("music" if segment.kind == MUSIC_KIND else "lyric")
            tags = (tag, "playing") if i == self.playing_row else (tag,)
            self.tree.insert("", "end", iid=str(i), values=(format_timecode(segment.start), format_timecode(segment.end), segment.kind, segment.text), tags=tags)
        self.tree.tag_configure("music", foreground=MUSIC_COLOR)
        self.tree.tag_configure("deleted", foreground=DELETED_COLOR)
        self.tree.tag_configure("playing", background="#2b4a72", foreground="#eaf4ff")
        if selected and self.tree.exists(selected[0]): self.tree.selection_set(selected[0])
        if hasattr(self, "waveform"):
            current_selection = self.tree.selection()
            self.waveform.set_segments(self.segments, int(current_selection[0]) if current_selection else None)

    def push_undo(self, _label: str) -> None:
        self.undo_stack.append(copy.deepcopy(self.segments))
        if len(self.undo_stack) > 80: self.undo_stack.pop(0)
        self.redo_stack.clear()

    def undo(self, _event: object = None) -> str:
        if self.undo_stack:
            self.redo_stack.append(copy.deepcopy(self.segments)); self.segments = self.undo_stack.pop(); self.refresh_tree()
        return "break"

    def redo(self, _event: object = None) -> str:
        if self.redo_stack:
            self.undo_stack.append(copy.deepcopy(self.segments)); self.segments = self.redo_stack.pop(); self.refresh_tree()
        return "break"

    def selected_index(self) -> int | None:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def add_segment(self) -> None:
        self.push_undo("新增列")
        start = self.segments[-1].end if self.segments else 0.0
        self.segments.append(Segment(start, min(self.duration, start + 1.0), LYRIC_KIND, "新歌詞"))
        self.refresh_tree(); self.tree.selection_set(str(len(self.segments) - 1))

    def toggle_deleted(self) -> None:
        idx = self.selected_index()
        if idx is None: return
        self.push_undo("刪除／還原")
        self.segments[idx].deleted = not self.segments[idx].deleted
        self.refresh_tree(); self.tree.selection_set(str(idx))

    def _begin_edit(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y); column = self.tree.identify_column(event.x)
        if not row or column == "#0": return
        col = ("start", "end", "kind", "text")[int(column[1:]) - 1]
        bbox = self.tree.bbox(row, column)
        if not bbox: return
        x, y, width, height = bbox
        old = self.tree.set(row, col)
        if col == "kind":
            widget: tk.Widget = ttk.Combobox(self.tree, values=(LYRIC_KIND, MUSIC_KIND), state="readonly")
            widget.set(old)
        else:
            widget = ttk.Entry(self.tree); widget.insert(0, old); widget.select_range(0, tk.END)
        widget.place(x=x, y=y, width=width, height=height)
        widget.focus_set(); self._editing = (row, col)
        widget.bind("<Return>", lambda _e: self._commit_edit(widget))
        widget.bind("<FocusOut>", lambda _e: self._commit_edit(widget))
        widget.bind("<Escape>", lambda _e: (widget.destroy(), None))

    def _commit_edit(self, widget: tk.Widget) -> None:
        if not self._editing or not widget.winfo_exists(): return
        row, col = self._editing; value = widget.get().strip()  # type: ignore[attr-defined]
        widget.destroy(); self._editing = None
        idx = int(row); segment = self.segments[idx]
        try:
            self.push_undo("編輯列")
            if col == "start": segment.start = parse_timecode(value)
            elif col == "end": segment.end = parse_timecode(value)
            elif col == "kind": segment.kind = value
            else: segment.text = value
            if segment.end <= segment.start: raise ValueError("結束時間必須晚於開始時間")
        except ValueError as exc:
            self.undo_stack.pop()  # 此次不合法操作不保留快照
            messagebox.showerror(APP_TITLE, str(exc))
        self.refresh_tree(); self.tree.selection_set(row)

    def export_srt(self) -> None:
        active = sorted((item for item in self.segments if not item.deleted and item.end > item.start), key=lambda item: item.start)
        if not active:
            messagebox.showinfo(APP_TITLE, "沒有可匯出的標記。")
            return
        initial = (self.audio_path.stem + ".srt") if self.audio_path else "lyrics.srt"
        output = filedialog.asksaveasfilename(title="匯出 SRT", defaultextension=".srt", initialfile=initial, filetypes=[("SRT 字幕", "*.srt")])
        if not output: return
        with open(output, "w", encoding="utf-8-sig", newline="\n") as handle:
            for number, item in enumerate(active, 1):
                handle.write(f"{number}\n{srt_timecode(item.start)} --> {srt_timecode(item.end)}\n{item.text}\n\n")
        self._set_progress_status(f"已匯出：{output}", busy=False)
        messagebox.showinfo(APP_TITLE, "SRT 匯出完成。")

    def save_project(self) -> None:
        initial = (self.audio_path.stem + ".lrproj") if self.audio_path else "project.lrproj"
        output = filedialog.asksaveasfilename(title="儲存專案", defaultextension=".lrproj", initialfile=initial, filetypes=[("歌詞專案", "*.lrproj")])
        if not output:
            return
        data = {
            "version": 1,
            "audio_path": str(self.audio_path) if self.audio_path else None,
            "duration": self.duration,
            "reference_lyrics": self.reference_lyrics,
            "segments": [
                {"start": s.start, "end": s.end, "kind": s.kind, "text": s.text, "deleted": s.deleted}
                for s in self.segments
            ],
            "settings": {
                "png_aspect": self.png_aspect_var.get(),
                "png_animation": self.png_animation_var.get(),
                "font_size": self.subtitle_font_size_var.get(),
                "font_name": self.subtitle_font_name_var.get(),
                "font_path": self.font_paths.get(self.subtitle_font_name_var.get(), ""),
                "text_color": self.subtitle_text_color,
                "outline_color": self.subtitle_outline_color,
                "valign": self.subtitle_valign_var.get(),
                "halign": self.subtitle_halign_var.get(),
                "offset_x": self.subtitle_offset_x_var.get(),
                "offset_y": self.subtitle_offset_y_var.get(),
                "anim_intensity": self.anim_intensity_var.get(),
                "anim_speed": self.anim_speed_var.get(),
                "model": self.model_var.get(),
                "language": self.language_var.get(),
                "device": self.device_var.get(),
                "precise": self.precise_var.get(),
                "vocals": self.vocals_var.get(),
                "force_align": self.force_align_var.get(),
            },
        }
        with open(output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self._set_progress_status(f"已儲存專案：{output}", busy=False)

    def load_project(self) -> None:
        selected = filedialog.askopenfilename(title="載入專案", filetypes=[("歌詞專案", "*.lrproj")])
        if not selected:
            return
        try:
            with open(selected, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"無法讀取專案檔：{exc}")
            return
        try:
            self.stop_playback(reset_position=True)
            audio_str = data.get("audio_path")
            if audio_str and Path(audio_str).exists():
                self.audio_path = Path(audio_str)
                self.duration = float(data.get("duration", 0))
                self.file_var.set(str(self.audio_path))
                self.duration_var.set(f"長度：{format_timecode(self.duration)}")
                self.play_slider.configure(to=max(0.01, self.duration))
            self.reference_lyrics = list(data.get("reference_lyrics", []))
            if self.reference_lyrics:
                self.lyrics_file_var.set(f"參考歌詞已載入（{len(self.reference_lyrics)} 句）")
            self.segments = [
                Segment(float(s["start"]), float(s["end"]), s["kind"], s["text"], s.get("deleted", False))
                for s in data.get("segments", [])
            ]
            self.undo_stack.clear()
            self.redo_stack.clear()
            s = data.get("settings", {})
            if "png_aspect" in s: self.png_aspect_var.set(s["png_aspect"])
            if "png_animation" in s: self.png_animation_var.set(s["png_animation"])
            if "font_size" in s: self.subtitle_font_size_var.set(int(s["font_size"]))
            if "font_name" in s:
                font_name = s["font_name"]
                font_path = s.get("font_path", "")
                if font_path:
                    self.font_paths[font_name] = font_path
                self.font_combo["values"] = tuple(self.font_paths.keys())
                self.subtitle_font_name_var.set(font_name)
            if "text_color" in s: self.subtitle_text_color = s["text_color"]
            if "outline_color" in s: self.subtitle_outline_color = s["outline_color"]
            if "valign" in s: self.subtitle_valign_var.set(s["valign"])
            if "halign" in s: self.subtitle_halign_var.set(s["halign"])
            if "offset_x" in s: self.subtitle_offset_x_var.set(float(s["offset_x"]))
            if "offset_y" in s: self.subtitle_offset_y_var.set(float(s["offset_y"]))
            if "anim_intensity" in s: self.anim_intensity_var.set(float(s["anim_intensity"]))
            if "anim_speed" in s: self.anim_speed_var.set(float(s["anim_speed"]))
            if "model" in s: self.model_var.set(s["model"])
            if "language" in s: self.language_var.set(s["language"])
            if "device" in s: self.device_var.set(s["device"])
            if "precise" in s: self.precise_var.set(bool(s["precise"]))
            if "vocals" in s: self.vocals_var.set(bool(s["vocals"]))
            if "force_align" in s: self.force_align_var.set(bool(s["force_align"]))
            self.refresh_tree()
            self._refresh_preview()
            self._set_progress_status(f"已載入專案：{selected}", busy=False)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"載入專案失敗：{exc}")

    def export_dynamic_png(self) -> None:
        """以目前表格（含使用者手動校正後的時間）輸出透明動態字幕序列。"""
        active = [item for item in self.segments if not item.deleted and item.kind == LYRIC_KIND and item.text.strip()]
        if not active:
            messagebox.showinfo(APP_TITLE, "沒有可輸出的歌詞。請先完成 AI 分析，或新增歌詞列。")
            return
        if not self.duration:
            messagebox.showinfo(APP_TITLE, "請先匯入音檔，才能取得完整序列的時間長度。")
            return
        parent = filedialog.askdirectory(title="選擇動態字幕 PNG 序列的儲存位置")
        if not parent:
            return
        width, height = PNG_ASPECTS[self.png_aspect_var.get()]
        stem = self.audio_path.stem if self.audio_path else "lyrics"
        output = Path(parent) / f"{stem}_動態字幕PNG_{width}x{height}_30fps"
        if output.exists() and any(output.iterdir()):
            messagebox.showerror(APP_TITLE, f"輸出資料夾已存在且不是空的：\n{output}\n\n請選擇其他位置，避免覆蓋既有影格。")
            return
        self.png_export_btn.configure(state="disabled")
        self._set_progress_status("正在準備動態字幕 PNG 匯出…", busy=True)
        # 複製時間軸資料，讓輸出期間仍可安全操作或繼續校正 UI。
        snapshot = copy.deepcopy(active)
        style = self.png_animation_var.get()
        subtitle_style = self._current_subtitle_style(target_height=height)
        threading.Thread(target=self._run_dynamic_png_export, args=(snapshot, output, width, height, style, subtitle_style), daemon=True).start()

    def _run_dynamic_png_export(self, segments: list[Segment], output: Path, width: int, height: int, style: str, subtitle_style: object) -> None:
        try:
            # 延後載入，首次啟動時讓 bootstrap 有機會自動安裝 Pillow。
            from subtitle_png_renderer import render_sequence
            frames = render_sequence(segments, self.audio_path, self.duration, output, width, height, 30,
                                     lambda text: self.events.put(("status", text)), style, subtitle_style)
            self.events.put(("png_done", (output, frames)))
        except Exception as exc:
            self.events.put(("png_error", str(exc)))

    @staticmethod
    def _hex_to_rgb(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))

    def _current_subtitle_style(self, target_height: float | None = None):
        from subtitle_png_renderer import SubtitleStyle
        valign_map = {"上方": "top", "中間": "middle", "下方": "bottom"}
        halign_map = {"靠左": "left", "置中": "center", "靠右": "right"}
        if target_height is None:
            _, target_height = PNG_ASPECTS[self.png_aspect_var.get()]
        base_font = int(self.subtitle_font_size_var.get())
        scaled_font = max(8, int(base_font * (target_height / 720.0)))
        return SubtitleStyle(
            font_size=scaled_font,
            text_color=self._hex_to_rgb(self.subtitle_text_color),
            outline_color=self._hex_to_rgb(self.subtitle_outline_color),
            valign=valign_map.get(self.subtitle_valign_var.get(), "bottom"),
            halign=halign_map.get(self.subtitle_halign_var.get(), "center"),
            offset_x=self.subtitle_offset_x_var.get(),
            offset_y=self.subtitle_offset_y_var.get(),
            anim_intensity=self.anim_intensity_var.get(),
            anim_speed=self.anim_speed_var.get(),
            font_path=self.font_paths.get(self.subtitle_font_name_var.get(), ""),
        )

    def _browse_font(self) -> None:
        path = filedialog.askopenfilename(
            title="選擇字型檔案",
            filetypes=[("字型檔案", "*.ttc *.ttf *.otf"), ("所有檔案", "*.*")]
        )
        if path:
            font_path = Path(path)
            name = f"自訂: {font_path.name}"
            self.font_paths[name] = str(font_path)
            self.font_combo["values"] = tuple(self.font_paths.keys())
            self.subtitle_font_name_var.set(name)
            self._refresh_preview()

    def _pick_text_color(self) -> None:
        color = colorchooser.askcolor(color=self.subtitle_text_color, title="選擇文字顏色")[1]
        if color:
            self.subtitle_text_color = color
            self.text_color_btn.configure(background=color, activebackground=color)
            self._refresh_preview()

    def _pick_outline_color(self) -> None:
        color = colorchooser.askcolor(color=self.subtitle_outline_color, title="選擇外框顏色")[1]
        if color:
            self.subtitle_outline_color = color
            self.outline_color_btn.configure(background=color, activebackground=color)
            self._refresh_preview()

    def _get_preview_dimensions(self) -> tuple[int, int, int, int]:
        """計算 Canvas 尺寸與貼合目標比例並套用 Viewport 縮放的影片容器大小。
        
        返回: (canvas_w, canvas_h, video_w, video_h)
        """
        cw = 480
        ch = 270
        if self.preview_image_label and self.preview_image_label.winfo_exists():
            w = self.preview_image_label.winfo_width()
            h = self.preview_image_label.winfo_height()
            if w > 20 and h > 20:
                cw, ch = w, h
        
        aspect_w, aspect_h = PNG_ASPECTS[self.png_aspect_var.get()]
        pad = 20
        avail_w = max(50, cw - pad)
        avail_h = max(50, ch - pad)
        
        # 計算等比例縮放適配
        scale = min(avail_w / aspect_w, avail_h / aspect_h)
        zoom = self.preview_zoom_var.get()
        sw = max(2, int(aspect_w * scale * zoom))
        sh = max(2, int(aspect_h * scale * zoom))
        return cw, ch, sw, sh

    def _on_preview_scroll(self, event) -> None:
        delta = 0
        if hasattr(event, "delta") and event.delta:
            delta = 1 if event.delta > 0 else -1
        elif event.num == 4:
            delta = 1
        elif event.num == 5:
            delta = -1
        if delta:
            current = self.preview_zoom_var.get()
            new_zoom = max(0.3, min(3.0, current + delta * 0.1))
            self.preview_zoom_var.set(round(new_zoom, 2))
            self._refresh_preview()

    def _refresh_preview(self, now: float | None = None) -> None:
        """依目前主播放位置重繪內嵌字幕預覽；不寫檔、不影響時間軸。"""
        if not self.preview_image_label or not self.preview_image_label.winfo_exists():
            return
        try:
            from PIL import ImageTk  # Pillow 由 bootstrap 於首次啟動安裝，尚未就緒時直接跳過。
            from subtitle_png_renderer import render_preview_frame
        except ImportError:
            return
        try:
            preview_time = self.playback_offset if now is None else now
            active = [item for item in self.segments if not item.deleted and item.kind == LYRIC_KIND and item.text.strip()]
            
            cw, ch, sw, sh = self._get_preview_dimensions()
            style = self._current_subtitle_style(target_height=sh)
            
            image = render_preview_frame(active, preview_time, sw, sh, self.png_animation_var.get(), style)
            if not self.playing and not any(s.start <= preview_time < s.end for s in active) and active:
                image = render_preview_frame(active, active[0].start + 0.01, sw, sh, self.png_animation_var.get(), style)
            
            self.preview_photo = ImageTk.PhotoImage(image)
            canvas = self.preview_image_label
            canvas.delete("all")
            
            # 填滿 Canvas 深色背景
            canvas.create_rectangle(0, 0, cw, ch, fill="#08090b", width=0)
            
            # 繪製置中的影片比例容器（ Letterbox / Pillarbox ）
            x0 = (cw - sw) // 2
            y0 = (ch - sh) // 2
            x1 = x0 + sw
            y1 = y0 + sh
            canvas.create_rectangle(x0, y0, x1, y1, fill="#141518", outline="#4c8bf5", width=1)
            
            # 將透明字幕圖像覆蓋至該影片比例容器上
            canvas.create_image(cw // 2, ch // 2, image=self.preview_photo, anchor="center")
        except Exception:
            pass  # 預覽是輔助功能，繪製失敗不應打斷主要編輯流程。

    def split_at_playhead(self) -> None:
        """把選取的歌詞句在目前播放頭位置切成兩句，文字依時間比例自動分配字數。"""
        idx = self.selected_index()
        if idx is None:
            messagebox.showinfo(APP_TITLE, "請先選取一句歌詞。")
            return
        segment = self.segments[idx]
        t = self.playback_offset
        if segment.kind != LYRIC_KIND:
            messagebox.showinfo(APP_TITLE, "只能斷開歌詞句，音樂標記無法斷句。")
            return
        if not (segment.start < t < segment.end):
            messagebox.showinfo(APP_TITLE, "請先把播放頭（點聲波時間尺或拖曳滑桿）移到選取句子中間要斷開的位置。")
            return
        self.push_undo("斷句")
        text = segment.text
        fraction = (t - segment.start) / (segment.end - segment.start)
        split_index = max(1, min(len(text) - 1, round(len(text) * fraction))) if len(text) > 1 else 1
        first_text, second_text = text[:split_index] or text, text[split_index:] or text
        first = Segment(segment.start, t, LYRIC_KIND, first_text)
        second = Segment(t, segment.end, LYRIC_KIND, second_text)
        self.segments[idx:idx + 1] = [first, second]
        self.refresh_tree()
        self.tree.selection_set(str(idx))
        self.tree.see(str(idx))

    def export_karaoke_stems(self) -> None:
        """用 Demucs 把音檔分成人聲／伴奏兩個 WAV 檔，供卡拉OK版本使用。"""
        if not self.audio_path:
            messagebox.showinfo(APP_TITLE, "請先匯入音檔。")
            return
        parent = filedialog.askdirectory(title="選擇人聲／伴奏 WAV 的儲存位置")
        if not parent:
            return
        stem = self.audio_path.stem
        output = Path(parent) / f"{stem}_卡拉OK分軌"
        if output.exists() and any(output.iterdir()):
            messagebox.showerror(APP_TITLE, f"輸出資料夾已存在且不是空的：\n{output}\n\n請選擇其他位置。")
            return
        self.karaoke_btn.configure(state="disabled")
        self._set_progress_status("正在分離人聲與伴奏，首次使用會下載 Demucs 模型…", busy=True)
        threading.Thread(target=self._run_karaoke_export, args=(self.audio_path, output), daemon=True).start()

    def _run_karaoke_export(self, path: Path, output: Path) -> None:
        temporary_dir: Path | None = None
        try:
            self._ensure_dependencies()
            vocal_path, temporary_dir = self._separate_vocals(path)
            accompaniment_path = vocal_path.parent / "no_vocals.wav"
            output.mkdir(parents=True, exist_ok=True)
            shutil.copy(vocal_path, output / f"{path.stem}_人聲.wav")
            if accompaniment_path.exists():
                shutil.copy(accompaniment_path, output / f"{path.stem}_伴奏.wav")
            self.events.put(("karaoke_done", output))
        except Exception as exc:
            self.events.put(("karaoke_error", str(exc)))
        finally:
            if temporary_dir:
                shutil.rmtree(temporary_dir, ignore_errors=True)

    # ── AI 影像生成 ───────────────────────────────────────────────────

    def _test_img_api(self) -> None:
        from image_generator import ImageGenerator
        provider = self.img_provider_var.get()
        key = self.img_api_key_var.get().strip()
        if not key:
            messagebox.showwarning(APP_TITLE, "請先輸入 API Key。")
            return
        self._set_progress_status("正在測試 API 連線…", busy=True)
        def _do():
            try:
                gen = ImageGenerator(provider, key)
                ok, msg = gen.test_connection()
                self.events.put(("img_test_done", (ok, msg)))
            except Exception as exc:
                self.events.put(("img_test_done", (False, str(exc))))
        threading.Thread(target=_do, daemon=True).start()

    def _start_image_generation(self) -> None:
        from image_generator import ImageGenerator
        from prompts import build_batch_prompts
        provider = self.img_provider_var.get()
        key = self.img_api_key_var.get().strip()
        if not key:
            messagebox.showwarning(APP_TITLE, "請先輸入 API Key。")
            return
        active = [s for s in self.segments if not s.deleted and s.kind == LYRIC_KIND and s.text.strip()]
        if not active:
            messagebox.showinfo(APP_TITLE, "沒有可生成影像的歌詞句。")
            return
        style_name = self.img_style_var.get()
        style_preset = PROMPT_STYLES.get(style_name, "")
        prompts = build_batch_prompts(active, style_preset=style_preset)
        stem = self.audio_path.stem if self.audio_path else "lyrics"
        output_dir = Path(str(self.audio_path.parent / f"{stem}_歌詞影像")) if self.audio_path else Path(f"{stem}_歌詞影像")
        if output_dir.exists() and any(output_dir.iterdir()):
            if not messagebox.askyesno(APP_TITLE, f"資料夾已存在：\n{output_dir}\n是否覆蓋？"):
                return
        self._set_progress_status("正在生成歌詞影像…", busy=True)
        def _do():
            try:
                gen = ImageGenerator(provider, key, style=style_name)
                def _prog(text):
                    self.events.put(("status", text))
                results = gen.generate_batch(prompts, output_dir, on_progress=_prog, delay=1.5)
                success = sum(1 for r in results if r.success)
                fail = len(results) - success
                self.events.put(("img_gen_done", (output_dir, success, fail)))
            except Exception as exc:
                self.events.put(("img_error", str(exc)))
        threading.Thread(target=_do, daemon=True).start()

    def _export_lyric_video(self) -> None:
        active = [s for s in self.segments if not s.deleted and s.kind == LYRIC_KIND and s.text.strip()]
        if not active:
            messagebox.showinfo(APP_TITLE, "沒有歌詞句可匯出影片。")
            return
        if not self.audio_path or not self.duration:
            messagebox.showinfo(APP_TITLE, "請先匯入音檔。")
            return
        stem = self.audio_path.stem
        img_dir = self.audio_path.parent / f"{stem}_歌詞影像"
        if not img_dir.exists() or not any(img_dir.glob("*.png")):
            messagebox.showinfo(APP_TITLE, f"找不到影像資料夾：\n{img_dir}\n請先點「為每句歌詞生成影像」。")
            return
        output = filedialog.asksaveasfilename(
            title="匯出歌詞影片", defaultextension=".mp4",
            initialfile=f"{stem}_歌詞影片.mp4",
            filetypes=[("MP4 影片", "*.mp4")],
        )
        if not output:
            return
        self._set_progress_status("正在合成歌詞影片…", busy=True)
        def _do():
            try:
                import subprocess as sp
                filter_parts = []
                inputs = ["-i", str(self.audio_path)]
                idx = 0
                for i, seg in enumerate(active):
                    img_file = img_dir / f"lyrics_{i + 1:03d}.png"
                    if not img_file.exists():
                        continue
                    dur = max(0.1, seg.end - seg.start)
                    inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(img_file)]
                    filter_parts.append(f"[{idx + 1}:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[v{idx}]")
                    idx += 1
                if idx == 0:
                    self.events.put(("img_error", "找不到對應的歌詞影像檔。"))
                    return
                concatInputs = "".join(f"[v{i}]" for i in range(idx))
                filter_parts.append(f"{concatInputs}concat=n={idx}:v=1:a=0[outv]")
                filter_str = ";".join(filter_parts)
                cmd = ["ffmpeg", "-y"] + inputs + [
                    "-filter_complex", filter_str,
                    "-map", "0:a", "-map", "[outv]",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "23",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest", "-movflags", "+faststart",
                    output,
                ]
                result = sp.run(cmd, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode != 0:
                    self.events.put(("img_error", f"ffmpeg 錯誤：\n{result.stderr[-500:]}"))
                    return
                self.events.put(("video_done", output))
            except Exception as exc:
                self.events.put(("img_error", str(exc)))
        threading.Thread(target=_do, daemon=True).start()


if __name__ == "__main__":
    import threading as _th

    def _install_splash() -> None:
        """顯示安裝等待視窗，所有套件裝完+驗證通過才關閉。"""
        splash = tk.Tk()
        splash.title("正在準備環境")
        splash.geometry("460x160")
        splash.resizable(False, False)
        splash.configure(bg="#1e1e2e")
        splash.protocol("WM_DELETE_WINDOW", lambda: None)
        ttk.Label(splash, text="正在檢查並安裝必要套件…", background="#1e1e2e", foreground="#cdd6f4",
                  font=("Microsoft JhengHei UI", 12)).pack(pady=(20, 8))
        status_var = tk.StringVar(value="正在掃描環境…")
        status_lbl = ttk.Label(splash, textvariable=status_var, background="#1e1e2e", foreground="#a6adc8",
                               font=("Microsoft JhengHei UI", 9), wraplength=420)
        status_lbl.pack()
        bar = ttk.Progressbar(splash, mode="indeterminate", length=360)
        bar.pack(pady=10)
        bar.start(10)

        def _do_install() -> None:
            try:
                ensure_required_packages(lambda t: splash.after(0, status_var.set, t))
                check_ffmpeg(lambda t: splash.after(0, status_var.set, t))
            except Exception as exc:
                splash.after(0, status_var.set, f"安裝過程異常：{exc}")
                import time as _t; _t.sleep(2)
            splash.after(0, splash.destroy)

        _th.Thread(target=_do_install, daemon=True).start()
        splash.mainloop()

    _install_splash()
    LyricsSrtApp().mainloop()
