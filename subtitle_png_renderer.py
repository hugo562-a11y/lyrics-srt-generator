"""Transparent animated PNG subtitle sequence renderer used by the GUI."""
from __future__ import annotations

import math
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont


Progress = Callable[[str], None]
ANIMATION_STYLES = (
    "逐字點亮", "彈跳聚焦", "滑入淡出", "電影柔和",
    "暴風雨", "脈衝擴散", "水波震盪", "雷射掃過",
    "氣泡彈出", "殘影拖曳", "閃爍霓虹", "粒子爆破",
)


@dataclass
class SubtitleStyle:
    font_size: int = 64
    text_color: tuple[int, int, int] = (246, 247, 224)
    outline_color: tuple[int, int, int] = (16, 12, 9)
    valign: str = "bottom"
    halign: str = "center"
    offset_x: float = 0.0
    offset_y: float = 0.0
    anim_intensity: float = 1.0
    anim_speed: float = 1.0
    font_path: str = ""


DEFAULT_STYLE = SubtitleStyle()

_SEED_CACHE: dict[str, int] = {}


def _stable_seed(key: str) -> int:
    if key not in _SEED_CACHE:
        _SEED_CACHE[key] = random.Random(hash(key)).randint(0, 999999)
    return _SEED_CACHE[key]


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


def _fit_lines(text: str, font_path: Path, max_width: int, size: int, max_lines: int = 4) -> tuple[list[str], "ImageFont.FreeTypeFont"]:
    from PIL import ImageFont

    def _line_width(f: "ImageFont.FreeTypeFont", t: str) -> float:
        return sum(f.getlength(ch) for ch in t)

    best_lines, best_font = [text], ImageFont.truetype(str(font_path), max(24, size), index=0)
    while size >= 20:
        font = ImageFont.truetype(str(font_path), size, index=0)
        if _line_width(font, text) <= max_width:
            return [text], font
        line, lines = "", []
        for character in text:
            candidate = line + character
            if line and _line_width(font, candidate) > max_width:
                lines.append(line); line = character
            else: line = candidate
        if line: lines.append(line)
        if len(lines) <= max_lines:
            return lines, font
        best_lines, best_font = lines, font
        size -= 4
    return best_lines, best_font


def _draw(frame: "Image.Image", item: object, now: float, energy: float, width: int, height: int, style: str, subtitle_style: SubtitleStyle) -> None:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    text, start, end = item.text, item.start, item.end
    font_path = Path(subtitle_style.font_path) if subtitle_style.font_path and Path(subtitle_style.font_path).exists() else _font_path()
    lines, font = _fit_lines(text, font_path, max(200, int(width * 0.80) - 80), subtitle_style.font_size)
    local, duration = now - start, end - start
    intensity = subtitle_style.anim_intensity
    speed = subtitle_style.anim_speed

    enter_duration = (.45 if style == "電影柔和" else .20) / speed
    leave_duration = (.32 if style == "電影柔和" else .16) / speed
    enter, leave = _smooth(local / enter_duration), _smooth((end - now) / leave_duration)
    base_alpha = int(255 * min(enter, leave))

    margin_y = height * 0.08
    available_h = height - margin_y * 2
    line_h_ratio = 1.34
    max_lines = max(1, int(available_h / (font.size * line_h_ratio)))

    if len(lines) > max_lines:
        safe_size = max(16, int(font.size * max_lines / len(lines)))
        lines, font = _fit_lines(text, font_path, max(200, int(width * 0.80) - 80), safe_size, max_lines=max_lines)

    line_h = int(font.size * line_h_ratio)
    block_h = line_h * len(lines)

    chars = max(1, len(text.replace(" ", "")))
    karaoke = min(duration * .70, max(.55, duration - .28))

    if subtitle_style.valign == "top":
        base_top = margin_y
    elif subtitle_style.valign == "middle":
        base_top = (height - block_h) / 2
    else:
        base_top = height - margin_y - block_h
    base_top = max(0, min(base_top, height - block_h))
    margin_x = width * 0.10

    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    cursor = 0

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
        y = base_top + line_no * line_h

        for char_i, (char, char_w) in enumerate(zip(line, widths)):
            if char == " ":
                x += char_w; continue

            char_progress = cursor / chars
            progress = (local - karaoke * char_progress) / (.12 / speed)
            rng = random.Random(_stable_seed(f"{text}_{cursor}"))
            char_phase = rng.random() * 0.4
            karaoke_reveal = _smooth(max(0.0, min(1.0, (local - karaoke * char_progress) / (.15 / speed))))

            alpha = base_alpha
            glow_color = (255, 194, 65)
            extra_scale = 1.0
            extra_x, extra_y = 0.0, 0.0
            extra_rotation = 0.0
            ghost_trail = 0
            glow_radius = font.size // (11 if style != "電影柔和" else 14)
            glow_alpha_mult = 0.27
            stroke_w = max(3, font.size // 18)

            if style == "逐字點亮":
                alpha = int(base_alpha * karaoke_reveal)
                lit = _smooth(progress)
                active = max(0., 1 - abs(progress - .5) * 1.6)
                if lit > .55:
                    glow_color = (255, 194, 65)
                else:
                    glow_color = subtitle_style.text_color
                extra_scale = 1 + .15 * active * intensity
                glow_alpha_mult = 0.27 + 0.25 * active * intensity

            elif style == "彈跳聚焦":
                alpha = int(base_alpha * karaoke_reveal)
                char_bounce_t = max(0, min(1, progress))
                bounce = math.sin(char_bounce_t * math.pi)
                drop = max(0, 1 - char_bounce_t * 3) * 40
                extra_scale = 1 + .3 * bounce * intensity + energy * .05 * intensity
                extra_y = -int(drop * intensity) + int(bounce * 14 * intensity)
                glow_alpha_mult = 0.2 + 0.4 * bounce * intensity

            elif style == "滑入淡出":
                motion = height * .070
                slide = (1 - enter) * motion - (1 - leave) * height * .018
                extra_y = int(slide)
                extra_scale = 1 + energy * .04 * intensity

            elif style == "電影柔和":
                extra_scale = 1 + energy * .03 * intensity
                glow_alpha_mult = 0.14

            elif style == "暴風雨":
                alpha = int(base_alpha * karaoke_reveal)
                active = max(0., 1 - abs(progress - .5) * 1.6)
                shake_t = local * 12 * speed
                shake_amp = 8 * active * intensity + energy * 3 * intensity
                shake_x = math.sin(shake_t + char_phase * 20) * shake_amp
                shake_y = math.cos(shake_t * 1.3 + char_phase * 15) * shake_amp * 0.7
                extra_x, extra_y = int(shake_x), int(shake_y)
                flash = max(0, math.sin(shake_t * 0.5 + char_phase * 10))
                extra_scale = 1 + 0.25 * flash * active * intensity
                glow_color = (255, int(100 + 94 * flash), int(65 + 120 * flash))
                glow_alpha_mult = 0.3 + 0.6 * flash * active * intensity
                stroke_w = max(4, font.size // 14)

            elif style == "脈衝擴散":
                alpha = int(base_alpha * karaoke_reveal)
                active = max(0., 1 - abs(progress - .5) * 1.6)
                pulse = active * math.sin(max(0, progress) * math.pi)
                ring_r = int(pulse * font.size * 2.0 * intensity)
                extra_scale = 1 + 0.25 * pulse * intensity
                glow_alpha_mult = 0.2 + 0.5 * pulse * intensity
                glow_color = (100, 180, 255)
                if ring_r > 5:
                    ring_layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
                    cx = int(x + char_w / 2)
                    cy = int(y + font.size / 2)
                    rd = ImageDraw.Draw(ring_layer)
                    ring_alpha = int(100 * pulse * intensity)
                    for r_off in range(-3, 4):
                        rd.ellipse([cx - ring_r + r_off, cy - ring_r + r_off, cx + ring_r - r_off, cy + ring_r - r_off],
                                   outline=(100, 200, 255, max(0, ring_alpha - abs(r_off) * 12)), width=2)
                    layer.alpha_composite(ring_layer)

            elif style == "水波震盪":
                alpha = int(base_alpha * karaoke_reveal)
                active = max(0., 1 - abs(progress - .5) * 1.6)
                wave = math.sin(progress * math.pi * 2 + char_phase * 12) * active * intensity
                wave2 = math.cos(progress * math.pi * 1.5 + char_phase * 8) * active * intensity
                extra_y = int(wave * 16)
                extra_x = int(wave2 * 8)
                extra_scale = 1 + 0.12 * abs(wave) * intensity
                glow_alpha_mult = 0.2 + 0.25 * abs(wave)
                glow_color = (80, 220, 255)

            elif style == "雷射掃過":
                alpha = int(base_alpha * karaoke_reveal)
                sweep_pos = (local * 2.5 * speed) % 1.5 - 0.25
                char_pos = char_progress
                dist = abs(sweep_pos - char_pos)
                hit = max(0, 1 - dist * 4)
                extra_scale = 1 + 0.35 * hit * intensity
                extra_y = -int(10 * hit * intensity)
                alpha = min(255, int(base_alpha * karaoke_reveal * (0.4 + 0.6 * (0.3 + 0.7 * hit))))
                glow_color = (255, 50, 50) if hit > 0.3 else (255, 200, 100)
                glow_alpha_mult = 0.2 + 0.7 * hit * intensity
                stroke_w = max(4, font.size // 12) if hit > 0.3 else stroke_w

            elif style == "氣泡彈出":
                alpha = int(base_alpha * karaoke_reveal)
                pop_t = max(0, min(1, progress))
                pop = math.sin(pop_t * math.pi * 1.2)
                squeeze_x = 1 + 0.2 * pop * intensity
                squeeze_y = 1 - 0.15 * pop * intensity
                extra_scale = 1 + 0.3 * pop * intensity
                rise = max(0, 1 - pop_t * 3) * 30
                extra_y = -int(rise * pop * intensity)
                glow_alpha_mult = 0.2 + 0.4 * pop * intensity
                glow_color = (255, 180, 255)

            elif style == "殘影拖曳":
                alpha = int(base_alpha * karaoke_reveal)
                active = max(0., 1 - abs(progress - .5) * 1.6)
                ghost_trail = int(4 * active * intensity)
                extra_x = int(active * 12 * intensity)
                extra_y = -int(6 * active * intensity)
                extra_scale = 1 + 0.15 * active * intensity
                glow_alpha_mult = 0.25 + 0.35 * active * intensity
                glow_color = (150, 100, 255)

            elif style == "閃爍霓虹":
                alpha = int(base_alpha * karaoke_reveal)
                active = max(0., 1 - abs(progress - .5) * 1.6)
                flicker_on = math.sin(local * 15 * speed + char_phase * 20) > -0.2
                neon_bright = 1.0 if flicker_on else 0.25
                neon_bright *= active
                extra_scale = 1 + 0.1 * neon_bright * intensity
                glow_alpha_mult = 0.15 + 0.7 * neon_bright * intensity
                hue_shift = (char_phase * 360) % 360
                if hue_shift < 120:
                    glow_color = (255, 80, 80)
                elif hue_shift < 240:
                    glow_color = (80, 255, 80)
                else:
                    glow_color = (80, 80, 255)
                stroke_w = max(4, font.size // 13)

            elif style == "粒子爆破":
                alpha = int(base_alpha * karaoke_reveal)
                active = max(0., 1 - abs(progress - .5) * 1.6)
                scatter = active
                gather = max(0, min(1, progress * 2))
                if scatter > 0.1:
                    extra_x = int((rng.random() - 0.5) * 50 * scatter * intensity)
                    extra_y = int((rng.random() - 0.5) * 50 * scatter * intensity)
                    extra_scale = 0.3 + 0.7 * gather
                else:
                    extra_scale = 1 + 0.08 * math.sin(progress * 10) * intensity
                glow_alpha_mult = 0.2 + 0.4 * gather * intensity
                glow_color = (255, 140, 50)

            pop = 1 + extra_scale - 1
            total_scale = pop * (1 + energy * .03 * intensity)
            motion = height * (.070 if style == "滑入淡出" else .035)
            top_y = base_top + int((1 - enter) * motion - (1 - leave) * height * .018) + int(subtitle_style.offset_y * height)
            draw_y = top_y + line_no * line_h + extra_y

            glyph = Image.new("RGBA", (int(char_w + 80), font.size + 100), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glyph)
            gd.text((40, 30), char, font=font, fill=(*subtitle_style.text_color, alpha), stroke_width=stroke_w, stroke_fill=(*subtitle_style.outline_color, int(alpha * .9)))

            glow = glyph.filter(ImageFilter.GaussianBlur(max(3, glow_radius)))
            glow.putalpha(glow.getchannel("A").point(lambda value: int(value * min(1.0, glow_alpha_mult * intensity))))
            glow_color_a = (*glow_color, alpha)

            if ghost_trail > 0:
                trail_layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
                for ti in range(1, ghost_trail + 1):
                    trail_offset = ti * 4 * intensity
                    trail_alpha = int(alpha * 0.25 / ti)
                    trail_glyph = Image.new("RGBA", (int(char_w + 80), font.size + 100), (0, 0, 0, 0))
                    ImageDraw.Draw(trail_glyph).text((40, 30), char, font=font, fill=(150, 100, 255, trail_alpha), stroke_width=stroke_w, stroke_fill=(16, 12, 9, int(trail_alpha * 0.5)))
                    trail_glyph = trail_glyph.filter(ImageFilter.GaussianBlur(max(2, font.size // 16)))
                    tx = int(x - trail_offset + extra_x)
                    ty = int(draw_y + 30)
                    trail_layer.alpha_composite(trail_glyph, (tx, ty))
                layer.alpha_composite(trail_layer)

            glow_pos = (int(x - 40 + extra_x), int(draw_y + 10))
            layer.alpha_composite(glow, glow_pos)

            if total_scale != 1:
                glyph = glyph.resize((int(glyph.width * total_scale), int(glyph.height * total_scale)), Image.Resampling.LANCZOS)
                position = (int(x + char_w / 2 - glyph.width / 2 + extra_x), int(draw_y + font.size / 2 - glyph.height / 2))
            else:
                position = (int(x - 40 + extra_x), int(draw_y + 10))

            layer.alpha_composite(glyph, position)
            x += char_w; cursor += 1

    frame.alpha_composite(layer)


def render_preview_frame(segments: Iterable[object], now: float, width: int, height: int, style: str, subtitle_style: SubtitleStyle | None = None) -> "Image.Image":
    from PIL import Image
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for item in segments:
        if item.start <= now < item.end:
            _draw(image, item, now, .35, width, height, style, subtitle_style or DEFAULT_STYLE)
            break
    return image


def render_sequence(segments: Iterable[object], audio_path: Path | None, duration: float, output: Path, width: int, height: int, fps: int, status: Progress, style: str = "逐字點亮", subtitle_style: SubtitleStyle | None = None) -> int:
    from PIL import Image
    subtitle_style = subtitle_style or DEFAULT_STYLE
    items = sorted((item for item in segments if item.end > item.start), key=lambda item: item.start)
    if not items: raise RuntimeError("沒有可輸出的歌詞。")
    font = Path(subtitle_style.font_path) if subtitle_style.font_path and Path(subtitle_style.font_path).exists() else _font_path()
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
