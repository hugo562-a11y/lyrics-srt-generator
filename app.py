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
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Iterable

from bootstrap import add_nvidia_dll_paths, ensure_optional_package, ensure_required_packages, gpu_runtime_ready, install_gpu_runtime
from subtitle_png_renderer import ANIMATION_STYLES


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
        self.rowconfigure(3, weight=1)

        top = ttk.Frame(self, padding=(14, 12, 14, 6))
        top.grid(row=0, column=0, sticky="ew")
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

        controls = ttk.LabelFrame(self, text="本機 AI 分析", padding=10)
        controls.grid(row=1, column=0, sticky="ew", padx=14, pady=6)
        controls.columnconfigure(9, weight=1)
        ttk.Label(controls, text="模型").grid(row=0, column=0, padx=(0, 5))
        self.model_var = tk.StringVar(value="large-v3")
        ttk.Combobox(controls, textvariable=self.model_var, width=11, state="readonly", values=("tiny", "base", "small", "medium", "large-v3")).grid(row=0, column=1)
        ttk.Label(controls, text="語言").grid(row=0, column=2, padx=(14, 5))
        self.language_var = tk.StringVar(value="zh")
        ttk.Combobox(controls, textvariable=self.language_var, width=8, state="readonly", values=("auto", "zh", "ja", "en", "ko")).grid(row=0, column=3)
        ttk.Label(controls, text="最短音樂段（秒）").grid(row=0, column=4, padx=(14, 5))
        self.min_gap_var = tk.StringVar(value="1.2")
        ttk.Entry(controls, textvariable=self.min_gap_var, width=7).grid(row=0, column=5)
        ttk.Label(controls, text="運算").grid(row=0, column=6, padx=(14, 5))
        self.device_var = tk.StringVar(value="自動（GPU 優先）")
        ttk.Combobox(controls, textvariable=self.device_var, width=16, state="readonly", values=("自動（GPU 優先）", "GPU", "CPU")).grid(row=0, column=7)
        self.analyze_btn = ttk.Button(controls, text="開始 AI 分析", command=self.analyze)
        self.analyze_btn.grid(row=0, column=8, padx=(14, 0))
        self.precise_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="精準逐字對齊（建議）", variable=self.precise_var).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.vocals_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="先分離人聲（較慢）", variable=self.vocals_var).grid(row=1, column=3, columnspan=3, sticky="w", pady=(8, 0))
        self.force_align_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="強制對齊（最精準，需額外套件）", variable=self.force_align_var).grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Label(controls, text="隨機性").grid(row=1, column=6, padx=(14, 5), sticky="e")
        self.temperature_var = tk.StringVar(value="0")
        ttk.Entry(controls, textvariable=self.temperature_var, width=5).grid(row=1, column=7, sticky="w")
        ttk.Label(controls, text="非語音門檻").grid(row=1, column=8, padx=(14, 5), sticky="e")
        self.no_speech_var = tk.StringVar(value="0.4")
        ttk.Entry(controls, textvariable=self.no_speech_var, width=5).grid(row=1, column=9, sticky="w")
        self.progress_var = tk.StringVar(value="等待匯入音檔")
        status_row = ttk.Frame(controls)
        status_row.grid(row=3, column=0, columnspan=10, sticky="ew", pady=(8, 0))
        status_row.columnconfigure(0, weight=1)
        ttk.Label(status_row, textvariable=self.progress_var, foreground=DARK_ACCENT).grid(row=0, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(status_row, mode="indeterminate", length=220)
        self.progress_bar.grid(row=0, column=1, sticky="e", padx=(12, 0))

        wave_frame = ttk.LabelFrame(self, text="聲波與時間軸", padding=(8, 4))
        wave_frame.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 6))
        self.waveform = WaveformView(wave_frame, on_seek=self._waveform_seek, on_select=self._activate_segment, on_edit=self._waveform_edit)
        self.waveform.pack(fill="both", expand=True)

        body = ttk.Frame(self, padding=(14, 6))
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        body_pane = ttk.PanedWindow(body, orient="horizontal")
        body_pane.grid(row=0, column=0, sticky="nsew")

        tree_frame = ttk.Frame(body_pane)
        columns = ("start", "end", "kind", "text")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        for key, title, width in (("start", "開始", 140), ("end", "結束", 140), ("kind", "類型", 90), ("text", "文字／標記", 460)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="center" if key != "text" else "w", stretch=key == "text")
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<ButtonRelease-1>", self.play_clicked_row)
        self.tree.bind("<Double-1>", self._begin_edit)
        body_pane.add(tree_frame, weight=3)

        preview_panel = ttk.LabelFrame(body_pane, text="字幕預覽（跟隨主播放器）", padding=8)
        self.preview_image_label = tk.Label(preview_panel, background="#08090b", bd=1, relief="solid")
        self.preview_image_label.pack()
        body_pane.add(preview_panel, weight=2)

        bottom = ttk.Frame(self, padding=(14, 6, 14, 14))
        bottom.grid(row=4, column=0, sticky="ew")
        self.play_btn = ttk.Button(bottom, text="▶ 播放", command=self.toggle_playback)
        self.play_btn.pack(side="left")
        ttk.Button(bottom, text="■ 停止", command=self.stop_playback).pack(side="left", padx=(6, 14))
        self.play_time_var = tk.StringVar(value="00:00:00:00")
        ttk.Label(bottom, textvariable=self.play_time_var, width=13).pack(side="left")
        self.play_slider = ttk.Scale(bottom, from_=0, to=1, command=lambda _value: None)
        self.play_slider.pack(side="left", fill="x", expand=True, padx=(5, 14))
        self.play_slider.bind("<ButtonRelease-1>", self.seek_playback)
        ttk.Button(bottom, text="▶ 只播選取句", command=lambda: self.play_selected_segment(only_segment=True)).pack(side="left", padx=(0, 14))
        ttk.Button(bottom, text="＋ 新增列", command=self.add_segment).pack(side="left")
        ttk.Button(bottom, text="刪除／還原", command=self.toggle_deleted).pack(side="left", padx=6)
        ttk.Button(bottom, text="✂ 在此斷句", command=self.split_at_playhead).pack(side="left", padx=(6, 0))
        ttk.Button(bottom, text="復原", command=self.undo).pack(side="left", padx=(18, 0))
        ttk.Button(bottom, text="重做", command=self.redo).pack(side="left", padx=6)
        ttk.Label(bottom, text="雙擊欄位可修改；時間格式：00:00:00:00", foreground=DARK_MUTED_FG).pack(side="left", padx=18)

        caption_export = ttk.LabelFrame(self, text="字幕樣式與匯出", padding=(10, 8))
        caption_export.grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 12))
        style_row = ttk.Frame(caption_export)
        style_row.pack(fill="x")
        self.png_aspect_var = tk.StringVar(value="16:9（1920×1080）")
        ttk.Label(style_row, text="比例").pack(side="left", padx=(0, 4))
        aspect_combo = ttk.Combobox(style_row, textvariable=self.png_aspect_var, state="readonly", width=16, values=tuple(PNG_ASPECTS))
        aspect_combo.pack(side="left", padx=(0, 12))
        aspect_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_preview())
        self.png_animation_var = tk.StringVar(value="逐字點亮")
        ttk.Label(style_row, text="動畫").pack(side="left", padx=(0, 4))
        animation_combo = ttk.Combobox(style_row, textvariable=self.png_animation_var, state="readonly", width=10, values=PNG_ANIMATION_STYLES)
        animation_combo.pack(side="left", padx=(0, 12))
        animation_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_preview())
        ttk.Label(style_row, text="字級").pack(side="left", padx=(0, 4))
        size_spin = ttk.Spinbox(style_row, from_=24, to=160, increment=2, textvariable=self.subtitle_font_size_var, width=5, command=self._refresh_preview)
        size_spin.pack(side="left", padx=(0, 12))
        size_spin.bind("<KeyRelease>", lambda _event: self._refresh_preview())
        self.text_color_btn = tk.Button(style_row, text="文字顏色", width=8, command=self._pick_text_color,
                                         background=self.subtitle_text_color, activebackground=self.subtitle_text_color)
        self.text_color_btn.pack(side="left", padx=(0, 8))
        self.outline_color_btn = tk.Button(style_row, text="外框顏色", width=8, command=self._pick_outline_color,
                                            background=self.subtitle_outline_color, activebackground=self.subtitle_outline_color,
                                            foreground="#ffffff", activeforeground="#ffffff")
        self.outline_color_btn.pack(side="left")

        position_row = ttk.Frame(caption_export)
        position_row.pack(fill="x", pady=(8, 0))
        ttk.Label(position_row, text="垂直位置").pack(side="left", padx=(0, 4))
        valign_combo = ttk.Combobox(position_row, textvariable=self.subtitle_valign_var, state="readonly", width=6, values=("上方", "中間", "下方"))
        valign_combo.pack(side="left", padx=(0, 12))
        valign_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_preview())
        ttk.Label(position_row, text="水平位置").pack(side="left", padx=(0, 4))
        halign_combo = ttk.Combobox(position_row, textvariable=self.subtitle_halign_var, state="readonly", width=6, values=("靠左", "置中", "靠右"))
        halign_combo.pack(side="left", padx=(0, 12))
        halign_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_preview())
        ttk.Label(position_row, text="左右偏移").pack(side="left", padx=(0, 4))
        ttk.Scale(position_row, from_=-0.4, to=0.4, variable=self.subtitle_offset_x_var, length=110, command=lambda _v: self._refresh_preview()).pack(side="left", padx=(0, 12))
        ttk.Label(position_row, text="上下偏移").pack(side="left", padx=(0, 4))
        ttk.Scale(position_row, from_=-0.4, to=0.4, variable=self.subtitle_offset_y_var, length=110, command=lambda _v: self._refresh_preview()).pack(side="left")

        anim_row = ttk.Frame(caption_export)
        anim_row.pack(fill="x", pady=(6, 0))
        ttk.Label(anim_row, text="動畫強度").pack(side="left", padx=(0, 4))
        ttk.Scale(anim_row, from_=0.0, to=3.0, variable=self.anim_intensity_var, length=140, command=lambda _v: self._refresh_preview()).pack(side="left", padx=(0, 4))
        ttk.Label(anim_row, textvariable=self.anim_intensity_var, width=4).pack(side="left", padx=(0, 16))
        ttk.Label(anim_row, text="動畫速度").pack(side="left", padx=(0, 4))
        ttk.Scale(anim_row, from_=0.2, to=3.0, variable=self.anim_speed_var, length=140, command=lambda _v: self._refresh_preview()).pack(side="left", padx=(0, 4))
        ttk.Label(anim_row, textvariable=self.anim_speed_var, width=4).pack(side="left")

        action_row = ttk.Frame(caption_export)
        action_row.pack(fill="x", pady=(10, 0))
        self.karaoke_btn = ttk.Button(action_row, text="匯出人聲／伴奏（卡拉OK）", command=self.export_karaoke_stems)
        self.karaoke_btn.pack(side="left")
        self.png_export_btn = ttk.Button(action_row, text="匯出動態 PNG（透明）", command=self.export_dynamic_png)
        self.png_export_btn.pack(side="right", padx=(8, 0))
        ttk.Button(action_row, text="匯出 SRT", command=self.export_srt).pack(side="right")

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
            # 啟動與分析可能同時觸發；整個應用程式生命週期只檢查／安裝一次。
            with self._dependency_lock:
                if self.dependencies_ready.is_set():
                    return
                ensure_required_packages(lambda text: self.events.put(("status", text)))
                self.dependencies_ready.set()
            self.events.put(("ready", None))
        except Exception as exc:
            self.events.put(("dependency_error", str(exc)))

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
        threading.Thread(target=self._run_transcription, args=(self.audio_path, self.model_var.get(), self.language_var.get(), self.device_var.get(), min_gap, reference, self.precise_var.get(), self.vocals_var.get(), temperature, no_speech, self.force_align_var.get()), daemon=True).start()

    def _separate_vocals(self, path: Path) -> tuple[Path, Path | None]:
        """用 Demucs 建立人聲軌；回傳暫存目錄供呼叫端清理。"""
        ensure_optional_package("demucs", "demucs>=4.0.1", lambda text: self.events.put(("status", text)))
        from demucs.separate import main as demucs_main
        output_dir = Path(tempfile.mkdtemp(prefix="lyrics_srt_demucs_"))
        self.events.put(("status", "正在分離人聲與伴奏，首次使用會下載 Demucs 模型…"))
        demucs_main(["--two-stems", "vocals", "-n", "htdemucs", "-o", str(output_dir), str(path)])
        vocal_path = output_dir / "htdemucs" / path.stem / "vocals.wav"
        if not vocal_path.exists():
            shutil.rmtree(output_dir, ignore_errors=True)
            raise RuntimeError("人聲分離沒有產生 vocals.wav。")
        return vocal_path, output_dir

    def _run_transcription(self, path: Path, model_name: str, language: str, device_choice: str, min_gap: float, reference: list[str], precise: bool, separate_vocals: bool, temperature: float = 0.0, no_speech: float = 0.4, force_align: bool = False) -> None:
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
                    ensure_optional_package("whisperx", "whisperx>=3.3.0", lambda text: self.events.put(("status", text)))
                    import whisperx
                    lang_code = None if language == "auto" else language
                    wx_device = "cuda" if use_gpu else "cpu"
                    self.events.put(("status", "正在以 whisperx 轉錄音檔…"))
                    wx_model = whisperx.load_model(model_name, device=wx_device, compute_type="float16" if wx_device == "cuda" else "int8", language=lang_code)
                    wx_result = wx_model.transcribe(str(source_path), batch_size=16, language=lang_code, temperature=temperature)
                    self.events.put(("status", "正在載入 CTC 對齊模型…"))
                    align_lang = wx_result.get("language", lang_code) or "zh"
                    wx_align_model, wx_metadata = whisperx.load_align_model(language_code=align_lang, device=wx_device)
                    self.events.put(("status", "正在以 CTC 強制對齊取得精確時間點…"))
                    wx_aligned = whisperx.align(wx_result["segments"], wx_align_model, wx_metadata, str(source_path), device=wx_device)
                    recognized = []
                    for seg in wx_aligned.get("segments", []):
                        words = seg.get("words", [])
                        for w in words:
                            ws, we = w.get("start"), w.get("end")
                            wt = str(w.get("word", "")).strip()
                            if wt and ws is not None and we is not None and we > ws:
                                recognized.append(Segment(float(ws), float(we), LYRIC_KIND, wt))
                        if not words:
                            text = str(seg.get("text", "")).strip()
                            s, e = seg.get("start", 0), seg.get("end", 0)
                            if text and e > s:
                                recognized.append(Segment(float(s), float(e), LYRIC_KIND, text))
                    if vocal_onset >= min_gap:
                        recognized = [item for item in recognized if item.end > vocal_onset - 0.08]
                    lyrics = align_reference_lyrics(reference, recognized, self.duration)
                    self.events.put(("status", f"已以 whisperx CTC 強制對齊完成 {len(reference)} 句歌詞。"))
                except Exception as wx_exc:
                    self.events.put(("status", f"whisperx 安裝失敗（可能不支援此 Python 版本），改用標準對齊：{wx_exc}"))
                    wx_failed = True
            if not lyrics:
                if reference:
                    self.events.put(("status", "正在辨識前奏結束與第一句人聲位置…"))
                    onset_raw, _ = model.transcribe(str(source_path), language=None if language == "auto" else language, vad_filter=True, vad_parameters=vad_params, condition_on_previous_text=False, beam_size=5, initial_prompt=prompt_text, temperature=temperature, no_speech_threshold=no_speech)
                    onset_segments = normalize_lyrics(list(onset_raw))
                    if onset_segments:
                        vocal_onset = onset_segments[0].start
                self.events.put(("status", "正在分析音訊與逐字時間點，請稍候…"))
                raw, _ = model.transcribe(str(source_path), language=None if language == "auto" else language, vad_filter=not bool(reference), vad_parameters=vad_params if reference else None, condition_on_previous_text=False, beam_size=5, word_timestamps=precise, initial_prompt=prompt_text, temperature=temperature, no_speech_threshold=no_speech)
                raw_segments = list(raw)
                recognized = word_timing_anchors(raw_segments) if precise else normalize_lyrics(raw_segments)
                if vocal_onset >= min_gap:
                    recognized = [item for item in recognized if item.end > vocal_onset - 0.08]
                lyrics = align_reference_lyrics(reference, recognized, self.duration) if reference else normalize_lyrics(raw_segments)
                if reference:
                    level = "逐字" if precise and any(getattr(item, "words", None) for item in raw_segments) else "逐句"
                    self.events.put(("status", f"已以 {len(reference)} 句參考歌詞進行 {level} 節奏對齊。"))
            self.events.put(("done", _fix_overlapping_segments(add_music_markers(lyrics, self.duration, min_gap))))
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
                elif event == "dependency_error":
                    self._set_progress_status("必要套件安裝失敗", busy=False)
                    messagebox.showerror(APP_TITLE, f"無法完成自動安裝：\n{payload}")
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
        subtitle_style = self._current_subtitle_style()
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

    def _current_subtitle_style(self):
        from subtitle_png_renderer import SubtitleStyle
        valign_map = {"上方": "top", "中間": "middle", "下方": "bottom"}
        halign_map = {"靠左": "left", "置中": "center", "靠右": "right"}
        return SubtitleStyle(
            font_size=int(self.subtitle_font_size_var.get()),
            text_color=self._hex_to_rgb(self.subtitle_text_color),
            outline_color=self._hex_to_rgb(self.subtitle_outline_color),
            valign=valign_map.get(self.subtitle_valign_var.get(), "bottom"),
            halign=halign_map.get(self.subtitle_halign_var.get(), "center"),
            offset_x=self.subtitle_offset_x_var.get(),
            offset_y=self.subtitle_offset_y_var.get(),
            anim_intensity=self.anim_intensity_var.get(),
            anim_speed=self.anim_speed_var.get(),
        )

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

    def _preview_dimensions(self) -> tuple[int, int]:
        width, height = PNG_ASPECTS[self.png_aspect_var.get()]
        scale = min(520 / width, 380 / height)
        return max(2, int(width * scale)), max(2, int(height * scale))

    def _refresh_preview(self, now: float | None = None) -> None:
        """依目前主播放位置重繪內嵌字幕預覽；不寫檔、不影響時間軸。

        `now` 由 `_update_playback` 在播放中傳入即時內插時間；其餘呼叫（拖曳、選取、
        改樣式）不傳時間，直接使用 `self.playback_offset`（暫停/拖曳後的固定位置）。
        """
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
            width, height = self._preview_dimensions()
            image = render_preview_frame(active, preview_time, width, height, self.png_animation_var.get(), self._current_subtitle_style())
            self.preview_photo = ImageTk.PhotoImage(image)
            self.preview_image_label.configure(image=self.preview_photo, width=width, height=height)
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


if __name__ == "__main__":
    LyricsSrtApp().mainloop()
