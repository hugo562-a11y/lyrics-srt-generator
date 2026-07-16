"""Lyric-to-prompt conversion for AI image generation."""
from __future__ import annotations


def lyric_to_prompt(lyric_text: str, style_preset: str = "", extra_tags: str = "") -> str:
    """Convert a lyric line into an English image generation prompt.

    Strategy:
    1. Clean the lyric text (remove punctuation, markers like [前奏]).
    2. Build a descriptive prompt combining the lyric imagery + style preset.
    3. Append quality tags for better API results.
    """
    text = lyric_text.strip()
    for marker in ("[前奏]", "[間奏]", "[尾奏]", "[純音樂]"):
        text = text.replace(marker, "")
    text = text.strip(" \t\n,，。.、！!？?～~…·")

    if not text:
        return _build_prompt("abstract musical visualization, sound waves, ambient atmosphere", style_preset, extra_tags)

    prompt = _build_prompt(text, style_preset, extra_tags)
    return prompt


def _build_prompt(description: str, style_preset: str, extra_tags: str) -> str:
    parts = [description]
    if style_preset:
        parts.append(style_preset)
    parts.append("high quality, detailed, beautiful composition")
    if extra_tags:
        parts.append(extra_tags)
    return ", ".join(parts)


def build_batch_prompts(
    segments: list[object],
    style_preset: str = "",
    extra_tags: str = "",
) -> list[dict]:
    """Build prompts for a list of Segment-like objects.

    Returns a list of dicts with keys: index, text, prompt, start, end.
    """
    items = []
    for i, seg in enumerate(segments):
        text = getattr(seg, "text", str(seg))
        start = getattr(seg, "start", 0.0)
        end = getattr(seg, "end", 0.0)
        prompt = lyric_to_prompt(text, style_preset, extra_tags)
        items.append({
            "index": i + 1,
            "text": text,
            "prompt": prompt,
            "start": start,
            "end": end,
        })
    return items


PROMPT_STYLE_MAP = {
    "電影風": "cinematic film still, dramatic lighting, shallow depth of field, 4K, photorealistic",
    "動漫風": "anime style illustration, vibrant colors, detailed background, studio ghibli quality",
    "水彩風": "watercolor painting, soft brush strokes, gentle pastel colors, artistic",
    "油畫風": "oil painting, rich textures, classical composition, masterpiece quality",
    "賽博龐克": "cyberpunk neon cityscape, glowing lights, futuristic dystopia, high detail",
    "寫實攝影": "photorealistic, professional DSLR photography, sharp focus, natural lighting",
    "極簡風": "minimalist design, clean lines, simple geometric shapes, elegant composition",
    "夢幻風": "dreamy ethereal atmosphere, soft magical glow, fantasy landscape, particles",
}
