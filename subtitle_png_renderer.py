"""Transparent animated PNG subtitle sequence renderer used by the GUI."""
from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont


Progress = Callable[[str], None]
ANIMATION_STYLES = ("逐字點亮", "彈跳聚焦", "滑入淡出", "電影柔和")


@dataclass
class SubtitleStyle:
    """使用者可調整的字幕外觀與位置設定。"""
    font_size: int = 64
    text_color: tuple[int, int, int] = (246, 247, 244)
    outline_color: tuple[int, int, int] = (16, 12, 9)
    valign: str = "bottom"  # "top" | "middle" | "bottom"
    halign: str = "center"  # "left" | "center" | "right"
    offset_x: float = 0.0   # 畫面寬度的比例（-0.5～0.5）
    offset_y: float = 0.0   # 畫面高度的比例（-0.5～0.5）


DEFAULT_STYLE = SubtitleStyle()


def _font_path() -> Path:
    candidates = (Path(r"C:\Windows\Fonts\msjhbd.ttc"), Path(r"C:\Windows\Fonts\NotoSansTC-VF.ttf"))
    return next((path for path in candidates if path.exists()), candidates[0])


def _smooth(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def _energy(audio_path: Path | None, fps: int, frames: int) -> list[float]:
    if not audio_path or not shutil.which("ffmpeg"):
        return [0.0] * frames
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(audio_path), "-ac", "1", "-ar", str(fps), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    values = [abs(int.from_bytes(result.stdout[i:i + 2], "little", signed=True)) / 32768 for i in range(0, len(result.stdout) - 1, 2)]
    peak = max(values, default=1.0) or 1.0
    return [min(1.0, values[min(i, len(values) - 1)] / peak) if values else 0.0 for i in range(frames)]


def _fit_lines(text: str, font_path: Path, max_width: int, size: int) -> tuple[list[str], ImageFont.FreeTypeFont]:
    while size >= 24:
        font = ImageFont.truetype(str(font_path), size, index=0)
        if font.getlength(text) <= max_width:
            return [text], font
        # Chinese does not require spaces between words; wrap one glyph at a time
        # so portrait and square exports remain inside their safe area.
        line, lines = "", []
        for character in text:
            candidate = line + character
            if line and font.getlength(candidate) > max_width:
                lines.append(line); line = character
            else: line = candidate
        if line: lines.append(line)
        if len(lines) <= 2: return lines, font
        size -= 4
    return [text], ImageFont.truetype(str(font_path), 24, index=0)


def _draw(frame: Image.Image, item: object, now: float, energy: float, width: int, height: int, style: str, subtitle_style: SubtitleStyle) -> None:
    text, start, end = item.text, item.start, item.end
    font_path = _font_path()
    lines, font = _fit_lines(text, font_path, int(width * 0.80), subtitle_style.font_size)
    local, duration = now - start, end - start
    enter_duration = .45 if style == "電影柔和" else .20
    leave_duration = .32 if style == "電影柔和" else .16
    enter, leave = _smooth(local / enter_duration), _smooth((end - now) / leave_duration)
    alpha = int(255 * min(enter, leave))
    pop = 1 + .12 * math.sin(min(local / .28, 1) * math.pi) if style == "彈跳聚焦" else 1
    scale = (1 + energy * .03) * pop
    line_h = int(font.size * 1.34)
    block_h = line_h * len(lines)
    motion = height * (.070 if style == "滑入淡出" else .035)

    margin_y = height * 0.08
    if subtitle_style.valign == "top":
        base_top = margin_y
    elif subtitle_style.valign == "middle":
        base_top = (height - block_h) / 2
    else:
        base_top = height - margin_y - block_h
    top = base_top + int((1 - enter) * motion - (1 - leave) * height * .018) + int(subtitle_style.offset_y * height)

    margin_x = width * 0.10
    chars = max(1, len(text.replace(" ", ""))); karaoke = min(duration * .70, max(.55, duration - .28)); cursor = 0
    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    for line_no, line in enumerate(lines):
        widths = [font.getlength(ch) for ch in line]
        line_w = sum(widths)
        if subtitle_style.halign == "left":
            x = margin_x
        elif subtitle_style.halign == "right":
            x = width - margin_x - line_w
        else:
            x = (width - line_w) / 2
        x += subtitle_style.offset_x * width
        y = top + line_no * line_h
        for char, char_w in zip(line, widths):
            if char == " ": x += char_w; continue
            progress = (local - karaoke * cursor / chars) / .12
            karaoke_on = style == "逐字點亮"
            lit, active = (_smooth(progress), max(0., 1 - abs(progress - .5) * 1.6)) if karaoke_on else (0., 0.)
            color = (255, 194, 65, alpha) if karaoke_on and lit > .55 else (*subtitle_style.text_color, alpha)
            glyph = Image.new("RGBA", (int(char_w + 70), font.size + 90), (0, 0, 0, 0))
            ImageDraw.Draw(glyph).text((35, 20), char, font=font, fill=color, stroke_width=max(3, font.size // 18), stroke_fill=(*subtitle_style.outline_color, int(alpha * .9)))
            glow = glyph.filter(ImageFilter.GaussianBlur(max(3, font.size // (14 if style == "電影柔和" else 11))))
            glow.putalpha(glow.getchannel("A").point(lambda value: int(value * (.14 if style == "電影柔和" else .27 + active * .2))))
            layer.alpha_composite(glow, (int(x - 35), int(y - 20)))
            char_scale = scale * (1 + .075 * active)
            if char_scale != 1:
                glyph = glyph.resize((int(glyph.width * char_scale), int(glyph.height * char_scale)), Image.Resampling.LANCZOS)
                position = (int(x + char_w / 2 - glyph.width / 2), int(y + font.size / 2 - glyph.height / 2))
            else: position = (int(x - 35), int(y - 20))
            layer.alpha_composite(glyph, position); x += char_w; cursor += 1
    frame.alpha_composite(layer)


def render_preview_frame(segments: Iterable[object], now: float, width: int, height: int, style: str, subtitle_style: SubtitleStyle | None = None) -> Image.Image:
    """Render one transparent frame for the Tk preview; it never writes to disk."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for item in segments:
        if item.start <= now < item.end:
            _draw(image, item, now, .35, width, height, style, subtitle_style or DEFAULT_STYLE)
            break
    return image


def render_sequence(segments: Iterable[object], audio_path: Path | None, duration: float, output: Path, width: int, height: int, fps: int, status: Progress, style: str = "逐字點亮", subtitle_style: SubtitleStyle | None = None) -> int:
    """Render active lyric segments.  The caller owns threading and UI updates."""
    subtitle_style = subtitle_style or DEFAULT_STYLE
    items = sorted((item for item in segments if item.end > item.start), key=lambda item: item.start)
    if not items: raise RuntimeError("沒有可輸出的歌詞。")
    font = _font_path()
    if not font.exists(): raise RuntimeError("找不到可顯示中文的字型（Microsoft JhengHei／Noto Sans TC）。")
    total = math.ceil(duration * fps)
    output.mkdir(parents=True, exist_ok=True)
    status("正在分析音樂能量，字幕將依節奏微幅律動…")
    energy = _energy(audio_path, fps, total)
    active = 0
    for number in range(total):
        now = number / fps
        while active + 1 < len(items) and now >= items[active].end: active += 1
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        segment = items[active]
        if segment.start <= now < segment.end: _draw(image, segment, now, energy[number], width, height, style, subtitle_style)
        image.save(output / f"lyrics_{number + 1:06d}.png", compress_level=4)
        if number % max(1, fps * 3) == 0 or number + 1 == total:
            status(f"正在輸出透明 PNG 序列：{number + 1:,}/{total:,} 張")
    return total
