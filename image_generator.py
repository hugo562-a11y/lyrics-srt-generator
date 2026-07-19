"""AI image generation module supporting OpenAI DALL-E 3 and Google Gemini Imagen."""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]


Progress = Callable[[str], None]

STYLE_PRESETS = {
    "電影風": "cinematic film still, dramatic lighting, shallow depth of field, 4K",
    "動漫風": "anime style illustration, vibrant colors, detailed background, studio quality",
    "水彩風": "watercolor painting, soft brush strokes, gentle colors, artistic",
    "油畫風": "oil painting, rich textures, classical composition, masterpiece",
    "賽博龐克": "cyberpunk neon cityscape, glowing lights, futuristic, high detail",
    "寫實攝影": "photorealistic, professional photography, sharp focus, natural lighting",
    "極簡風": "minimalist design, clean lines, simple shapes, elegant composition",
    "夢幻風": "dreamy ethereal atmosphere, soft glow, magical particles, fantasy",
}


@dataclass
class ImageGenResult:
    success: bool
    image_path: Path | None = None
    error: str = ""


class ImageGenerator:
    def __init__(self, provider: str, api_key: str, style: str = "電影風", base_url: str | None = None):
        if requests is None:
            raise RuntimeError("缺少 requests 套件，請重新安裝。")
        self.provider = provider
        self.api_key = api_key
        self.style = style
        self.base_url = base_url
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "lyrics-srt-generator/1.0"})

    def test_connection(self) -> tuple[bool, str]:
        try:
            if self.provider == "openai":
                resp = self._session.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=15,
                )
                if resp.status_code == 200:
                    return True, "連線成功"
                return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
            elif self.provider == "gemini":
                url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self.api_key}"
                resp = self._session.get(url, timeout=15)
                if resp.status_code == 200:
                    return True, "連線成功"
                return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
            return False, f"未知服務：{self.provider}"
        except Exception as exc:
            return False, str(exc)

    def generate(self, prompt: str, output_path: Path, size: str = "1024x1024") -> ImageGenResult:
        try:
            if self.provider == "openai":
                return self._generate_openai(prompt, output_path, size)
            elif self.provider == "gemini":
                return self._generate_gemini(prompt, output_path)
            return ImageGenResult(False, error=f"未知服務：{self.provider}")
        except Exception as exc:
            return ImageGenResult(False, error=str(exc))

    def _generate_openai(self, prompt: str, output_path: Path, size: str) -> ImageGenResult:
        url = "https://api.openai.com/v1/images/generations"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        body = {
            "model": "dall-e-3",
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": "standard",
            "response_format": "url",
        }
        resp = self._session.post(url, headers=headers, json=body, timeout=120)
        if resp.status_code != 200:
            return ImageGenResult(False, error=f"API 錯誤 ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        image_url = data["data"][0]["url"]
        img_resp = self._session.get(image_url, timeout=60)
        if img_resp.status_code != 200:
            return ImageGenResult(False, error=f"圖片下載失敗: HTTP {img_resp.status_code}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(img_resp.content)
        return ImageGenResult(True, image_path=output_path)

    def _generate_gemini(self, prompt: str, output_path: Path) -> ImageGenResult:
        model = "gemini-2.0-flash-preview-image-generation"
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            f"?key={self.api_key}"
        )
        headers = {"Content-Type": "application/json"}
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
        }
        resp = self._session.post(url, headers=headers, json=body, timeout=120)
        if resp.status_code != 200:
            return ImageGenResult(False, error=f"API 錯誤 ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline = part.get("inlineData", {})
                if inline.get("data"):
                    img_bytes = base64.b64decode(inline["data"])
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(img_bytes)
                    return ImageGenResult(True, image_path=output_path)
        return ImageGenResult(False, error=f"API 回傳中找不到圖片資料：{str(data)[:200]}")

    def generate_batch(
        self,
        items: list[dict],
        output_dir: Path,
        on_progress: Progress | None = None,
        delay: float = 1.0,
    ) -> list[ImageGenResult]:
        results: list[ImageGenResult] = []
        total = len(items)
        output_dir.mkdir(parents=True, exist_ok=True)
        for i, item in enumerate(items):
            index = item.get("index", i + 1)
            prompt = item.get("prompt", "")
            if on_progress:
                on_progress(f"正在生成第 {i + 1}/{total} 張影像…")
            out_file = output_dir / f"lyrics_{index:03d}.png"
            result = self.generate(prompt, out_file)
            results.append(result)
            if i < total - 1:
                time.sleep(delay)
        return results
