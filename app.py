"""本機歌詞辨識、音樂段標記與 SRT 匯出工具。"""
from __future__ import annotations

import copy
import difflib
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
from tkinter import filedialog, messagebox, ttk
from typing import Iterable

from bootstrap import add_nvidia_dll_paths, ensure_optional_package, ensure_required_packages, gpu_runtime_ready, install_gpu_runtime


APP_TITLE = "歌詞 SRT 產生器"
MUSIC_KIND = "音樂"
LYRIC_KIND = "歌詞"
SUPPORTED_AUDIO = [("音檔", "*.mp3 *.wav *.m4a *.flac *.aac *.ogg"), ("所有檔案", "*.*")]
SUPPORTED_LYRICS = [("歌詞文字檔", "*.txt *.lrc"), ("所有檔案", "*.*")]


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
    return anchors


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


class LyricsSrtApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1080x700")
        self.minsize(860, 540)
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
        self._build_ui()
        self.bind_all("<Control-z>", self.undo)
        self.bind_all("<Control-y>", self.redo)
        self.after(120, self._poll_events)
        self.after(250, self._check_dependencies_async)
        self.after(75, self._update_playback)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.configure("Treeview", rowheight=28, font=("Microsoft JhengHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft JhengHei UI", 10, "bold"))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        top = ttk.Frame(self, padding=(14, 12, 14, 6))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Button(top, text="匯入音檔", command=self.import_audio).grid(row=0, column=0, padx=(0, 10))
        self.file_var = tk.StringVar(value="尚未選擇音檔")
        ttk.Label(top, textvariable=self.file_var, anchor="w").grid(row=0, column=1, sticky="ew")
        self.duration_var = tk.StringVar(value="長度：--")
        ttk.Label(top, textvariable=self.duration_var).grid(row=0, column=2, padx=(10, 0))
        ttk.Button(top, text="匯入歌詞檔", command=self.import_lyrics).grid(row=0, column=3, padx=(16, 8))
        self.lyrics_file_var = tk.StringVar(value="未使用參考歌詞")
        ttk.Label(top, textvariable=self.lyrics_file_var, foreground="#346b39").grid(row=0, column=4, sticky="w")

        controls = ttk.LabelFrame(self, text="本機 AI 分析", padding=10)
        controls.grid(row=1, column=0, sticky="ew", padx=14, pady=6)
        controls.columnconfigure(9, weight=1)
        ttk.Label(controls, text="模型").grid(row=0, column=0, padx=(0, 5))
        self.model_var = tk.StringVar(value="small")
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
        self.progress_var = tk.StringVar(value="等待匯入音檔")
        status_row = ttk.Frame(controls)
        status_row.grid(row=2, column=0, columnspan=10, sticky="ew", pady=(8, 0))
        status_row.columnconfigure(0, weight=1)
        ttk.Label(status_row, textvariable=self.progress_var, foreground="#245a9c").grid(row=0, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(status_row, mode="indeterminate", length=220)
        self.progress_bar.grid(row=0, column=1, sticky="e", padx=(12, 0))

        body = ttk.Frame(self, padding=(14, 6))
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        columns = ("start", "end", "kind", "text")
        self.tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="browse")
        for key, title, width in (("start", "開始", 140), ("end", "結束", 140), ("kind", "類型", 90), ("text", "文字／標記", 580)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="center" if key != "text" else "w", stretch=key == "text")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<ButtonRelease-1>", self.play_clicked_row)
        self.tree.bind("<Double-1>", self._begin_edit)

        bottom = ttk.Frame(self, padding=(14, 6, 14, 14))
        bottom.grid(row=3, column=0, sticky="ew")
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
        ttk.Button(bottom, text="復原", command=self.undo).pack(side="left", padx=(18, 0))
        ttk.Button(bottom, text="重做", command=self.redo).pack(side="left", padx=6)
        ttk.Label(bottom, text="雙擊欄位可修改；時間格式：00:00:00:00", foreground="#555").pack(side="left", padx=18)
        ttk.Button(bottom, text="匯出 SRT", command=self.export_srt).pack(side="right")

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

    def _load_audio_backend(self) -> None:
        ffplay = shutil.which("ffplay")
        if ffplay:
            self.events.put(("audio_ready", ffplay))
        else:
            self.events.put(("audio_error", "找不到 FFmpeg 的 ffplay.exe。請安裝 FFmpeg 並加入 PATH 後重新啟動程式。"))

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
        if hasattr(self, "tree"):
            self.refresh_tree()

    def seek_playback(self, _event: tk.Event) -> None:
        self.playback_offset = float(self.play_slider.get())
        if self.playing:
            self._start_playback(self.playback_offset)

    def play_clicked_row(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.play_selected_segment(only_segment=False)

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
                active = next((i for i, item in enumerate(self.segments) if not item.deleted and item.start <= current <= item.end), None)
                if active != self.playing_row:
                    self.playing_row = active
                    self.refresh_tree()
                    if active is not None and self.tree.exists(str(active)):
                        self.tree.selection_set(str(active))
                        self.tree.focus(str(active))
                        self.tree.see(str(active))
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
        self.analyze_btn.configure(state="disabled")
        self._set_progress_status("正在載入本機模型並轉錄，首次使用會下載模型…", busy=True)
        reference = list(self.reference_lyrics)
        threading.Thread(target=self._run_transcription, args=(self.audio_path, self.model_var.get(), self.language_var.get(), self.device_var.get(), min_gap, reference, self.precise_var.get(), self.vocals_var.get()), daemon=True).start()

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

    def _run_transcription(self, path: Path, model_name: str, language: str, device_choice: str, min_gap: float, reference: list[str], precise: bool, separate_vocals: bool) -> None:
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
                    # CUDA DLL 缺少時，先自動補齊 PyPI 可提供的執行庫，再重試一次。
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
            if reference:
                # 另跑一次保守的 VAD，只用來找第一句真正人聲的起點。
                # 完整逐字對齊仍使用不過濾的分析，避免後段拖音被切掉。
                self.events.put(("status", "正在辨識前奏結束與第一句人聲位置…"))
                onset_raw, _ = model.transcribe(str(source_path), language=None if language == "auto" else language, vad_filter=True, condition_on_previous_text=False, beam_size=5)
                onset_segments = normalize_lyrics(list(onset_raw))
                if onset_segments:
                    vocal_onset = onset_segments[0].start
            self.events.put(("status", "正在分析音訊與逐字時間點，請稍候…"))
            # 歌曲拖音與伴奏很容易被一般語音 VAD 當成靜音切掉，尤其是後半段。
            # 提供正確歌詞時以完整音檔對齊，避免錨點在歌曲尚未結束前耗盡。
            raw, _ = model.transcribe(str(source_path), language=None if language == "auto" else language, vad_filter=not bool(reference), condition_on_previous_text=False, beam_size=5, word_timestamps=precise)
            raw_segments = list(raw)
            recognized = word_timing_anchors(raw_segments) if precise else normalize_lyrics(raw_segments)
            if vocal_onset >= min_gap:
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
                    self.refresh_tree(); self._set_progress_status(f"分析完成：{len(self.segments)} 個標記，可直接校正後匯出。", busy=False)
                    self.analyze_btn.configure(state="normal")
                elif event == "error":
                    self.analyze_btn.configure(state="normal")
                    self._set_progress_status("分析失敗", busy=False)
                    messagebox.showerror(APP_TITLE, f"AI 分析失敗：\n{payload}")
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
        self.tree.tag_configure("music", foreground="#346b39")
        self.tree.tag_configure("deleted", foreground="#999999")
        self.tree.tag_configure("playing", background="#dbeafe", foreground="#0f3d75")
        if selected and self.tree.exists(selected[0]): self.tree.selection_set(selected[0])

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


if __name__ == "__main__":
    LyricsSrtApp().mainloop()
