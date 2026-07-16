# 歌詞 SRT 產生器 — 專案結構報告

> 分支：`feature/merged-v2`｜GitHub：`https://github.com/hugo562-a11y/lyrics-srt-generator`
> 生成日期：2026-07-16

---

## 1. 專案概覽

Windows 桌面應用程式，用 Whisper 識別歌唱時間軸，對齊使用者提供的歌詞，匯出 SRT 字幕、透明動畫 PNG 序列、卡拉 OK 人聲/伴奏分離。

| 項目 | 值 |
|---|---|
| 語言 | Python 3.14.6（Windows） |
| GUI | tkinter + ttk（clam 主題 + Windows Dark Titlebar） |
| GPU | RTX 3070 / CUDA 581.57（CPU fallback） |
| 總 Python 行數 | ~2,720 行 |

---

## 2. 檔案結構

```
lyrics-srt-generator-merged/
├── app.py                    (1973行)  主程式：GUI + 轉錄管線 + 匯出
├── subtitle_png_renderer.py  (382行)   透明 PNG 字幕渲染器（12種動畫）
├── bootstrap.py              (148行)   自動安裝依賴 + GPU 設定
├── image_generator.py        (148行)   AI 圖片生成（DALL-E 3 / Gemini）
├── prompts.py                (69行)    歌詞→英文圖片 prompt 轉換
├── requirements.txt          (11行)    套件清單
├── run.bat                   (28行)    啟動腳本（系統 Python，無 venv）
├── build_exe.bat             (14行)    PyInstaller 打包腳本
├── README.md                 (69行)    使用說明
├── HANDOFF.md                (147行)   開發交接文件
├── CHANGELOG.md              (43行)    變更紀錄
└── 超人爸爸.lrproj            (109行)   範例專案檔（JSON）
```

---

## 3. 核心模組分析

### 3.1 `app.py` — 主程式（1973行）

#### 資料結構
- **`Segment`** dataclass：`start`, `end`, `kind`（音樂/歌詞）, `text`, `deleted`

#### 重要常數
| 常數 | 值 | 說明 |
|---|---|---|
| `PNG_ASPECTS` | 4種解析度 | 16:9 (1920×1080)、9:16、1:1、4:3 |
| `DARK_BG` 等 | 20+ 色碼 | 深色主題配色 |
| 預設字級 | 64 | spinbox 預設值（range 24-160） |
| 預設 no_speech | 0.4 | VAD 非語音門檻 |
| 預設溫度 | 0 | Whisper 採樣溫度 |
| 最大 Undo | 80 層 | 全 Segment 列表 deep copy |
| 事件輪詢 | 120ms | 背景執行緒結果輪詢間隔 |
| 播放更新 | 75ms | 音訊播放位置更新間隔 |

#### 功能模組一覽

| 功能 | 關鍵方法 | 說明 |
|---|---|---|
| **音訊匯入** | `import_audio()` | 檔案對話框 → probe duration → 載入波形 |
| **波形顯示** | `decode_waveform()` | ffmpeg 解碼 PCM 4kHz → numpy float32 |
| **WaveformView** | 類別 (284-572行) | Canvas 波形 widget：時間尺規 + 色塊 + 拖拽手柄 |
| **音訊播放** | `toggle_playback()` 等 | ffplay 子程序 + monotonic() 時間追蹤 |
| **Whisper 轉錄** | `_run_transcription()` | GPU 偵測 → Demucs → whisperx → 對齊 → 音樂標記 |
| **歌詞對齊** | `align_reference_lyrics()` | SequenceMatcher + 長度懲罰評分 |
| **幻覺過濾** | `_remove_hallucination_repeats()` | 偵測 Whisper 重複文字（≥3次） |
| **音樂標記** | `add_music_markers()` | 歌詞間隙插入 [前奏][間奏][尾奏] |
| **SRT 匯出** | `export_srt()` | UTF-8-BOM SRT 格式 |
| **PNG 匯出** | `export_dynamic_png()` | 30fps 透明 PNG 序列（背景執行緒） |
| **卡拉OK 匯出** | `export_karaoke_stems()` | Demucs 人聲分離 → WAV |
| **AI 圖片** | `_start_image_generation()` | DALL-E 3 / Gemini 圖片生成 |
| **影片匯出** | `_export_lyric_video()` | ffmpeg 複合濾鏡拼接圖片+音訊 |
| **專案存檔** | `save_project()` / `load_project()` | JSON `.lrproj` 格式 |
| **Undo/Redo** | `push_undo()` / `undo()` / `redo()` | 最深 80 層 |
| **字幕預覽** | `_refresh_preview()` | 即時渲染到 Canvas，75ms 刷新 |
| **字幕樣式** | `_current_subtitle_style()` | 從 UI 讀取所有樣式設定 |

#### 轉錄管線流程（`_run_transcription`）

```
1. GPU 偵測（CUDA float16 → CPU int8 fallback）
2. [可選] Demucs 人聲分離（--two-stems vocals, htdemucs）
3. [可選] whisperx CTC 強制對齊
4. faster-whisper 轉錄（含 word timestamps）
5. [可選] 前奏偵測（保守 VAD 找第一個語音 onset）
6. 參考歌詞對齊到 Whisper 錨點
7. 插入音樂標記（[前奏][間奏][尾奏]）
```

#### 架構模式

- **背景執行緒 + 事件佇列**：所有長時間操作在 daemon 執行緒執行，透過 `queue.Queue` 回傳結果，主執行緒 120ms 輪詢
- **延遲匯入**：PIL/demucs/whisperx 在函式內部 import，確保 bootstrap 先完成安裝
- **Splash 啟動**：`_install_splash()` 顯示啟動畫面，背景安裝依賴

---

### 3.2 `subtitle_png_renderer.py` — PNG 渲染器（382行）

#### 資料結構
- **`SubtitleStyle`** dataclass：`font_size`, `text_color`, `outline_color`, `valign`, `halign`, `offset_x`, `offset_y`, `anim_intensity`, `anim_speed`, `font_path`

#### 核心函式

| 函式 | 說明 |
|---|---|
| `_fit_lines()` | 自動換行 + 字級縮放（下限8px） |
| `_draw()` | 核心渲染（252行）：逐字元繪製 glow + glyph + stroke |
| `render_preview_frame()` | 預覽單幀 |
| `render_sequence()` | 匯出整個 PNG 序列（30fps） |

#### 12 種動畫效果

| 效果 | 行為 |
|---|---|
| 逐字點亮 | 依序亮起 + 金色光暈 + 微縮放 |
| 彈跳聚焦 | 從上方掉落 + sine 彈跳物理 |
| 滑入淡出 | 整行滑入/滑出（非逐字） |
| 電影柔和 | 柔和淡入/淡出 + 低光暈（非逐字） |
| 暴風雨 | 逐字震動 + 閃爍光暈 |
| 脈衝擴散 | 逐字擴散環 |
| 水波震盪 | 逐字 sine 波位移 |
| 雷射掃過 | 雷射線掃過，命中字發紅光 |
| 氣泡彈出 | 從下方彈跳 + 擠壓變形 |
| 殘影拖曳 | 滑入 + 殘影拖尾 |
| 閃爍霓虹 | 隨機開關 + 紅綠藍霓虹色 |
| 粒子爆破 | 隨機散開 → 重組 |

#### 渲染流程

```
_fit_lines() → 計算行數/字級/行高
→ 計算垂直對齊 (top/middle/bottom) + 偏移
→ 逐行 → 逐字元:
    1. 計算 karaoke_reveal（時間比例 → smoothstep）
    2. 依動畫樣式計算 extra_scale/extra_x/extra_y/extra_rotation
    3. 繪製 glyph 畫布 (char_w+80 × font_size+100)，字在 (40,30)
    4. 加 GaussianBlur 做光暈
    5. [可選] 殘影/環/粒子效果
    6. alpha_composite 到 layer
→ layer 合成到 frame
```

---

### 3.3 `bootstrap.py` — 依賴管理（148行）

#### 套件分類

| 類別 | 套件 |
|---|---|
| **必要** | numpy, Pillow, soundfile, requests, faster-whisper |
| **可選** | demucs, whisperx |
| **GPU** | nvidia-cublas-cu12, nvidia-cudnn-cu12 |

#### 啟動流程

```
 splash 畫面 → 後台執行緒:
   1. 升級 pip/setuptools/wheel
   2. 逐一檢查必要套件（跳過可選）
   3. 缺少 → pip install（force-reinstall fallback）
   4. 檢查 ffmpeg
 → 銷毀 splash → 啟動主程式
```

**重要設計**：`torch` 不在安裝清單中 — 由 demucs/whisperx 透過 pip 依賴間接安裝。

---

### 3.4 `image_generator.py` — AI 圖片生成（148行）

| 供應商 | 模型 | API |
|---|---|---|
| OpenAI | `dall-e-3` | `api.openai.com/v1/images/generations` |
| Google | `imagen-3.0-generate-002` | `generativelanguage.googleapis.com` |

支援連線測試、單張生成、批次生成（含延遲控制）。

---

### 3.5 `prompts.py` — Prompt 轉換（69行）

8 種風格映射：電影風、動漫風、水彩風、油畫風、賽博龐克、寫實攝影、極簡風、夢幻風。

---

## 4. 非程式檔案

| 檔案 | 用途 |
|---|---|
| `run.bat` | 系統 Python 啟動（無 venv），嘗試 `py -3` → `python` → 錯誤提示 |
| `build_exe.bat` | PyInstaller 打包 → `dist/LyricsSrtGenerator/` |
| `requirements.txt` | 離線安裝參考（必要 + 可選分兩段） |
| `超人爸爸.lrproj` | 範例專案：9:16、逐字點亮、字級38、large-v3 |
| `.gitignore` | 忽略 .venv, \_\_pycache\_\_, build/, dist/ |

---

## 5. 支援格式

| 類型 | 格式 |
|---|---|
| 音訊輸入 | mp3, wav, m4a, flac, aac, ogg |
| 歌詞輸入 | txt, lrc（每行一句） |
| 字幕輸出 | SRT（UTF-8-BOM） |
| 圖片輸出 | 透明 RGBA PNG 序列（6位數編號） |
| 影片輸出 | MP4（H.264 + AAC，ffmpeg） |
| 卡拉OK | WAV（人聲 + 伴奏分軌） |
| 專案檔 | JSON `.lrproj` |

---

## 6. 近期重要修復

| 日期 | 修復 | 說明 |
|---|---|---|
| 2026-07-16 | 字級地板限制 | `max(16/24/20)` → `max(8)`，預覽字級變化不再被 clamp |
| 2026-07-16 | Glyph padding 裁切 | `_fit_lines` max_width 減 80px，補償每字 ±40px 畫布 padding |
| 2026-07-15 | 預覽無限寬度 | `tk.Label` → `tk.Canvas` 防止圖片自動放大 |
| 2026-07-15 | 12種動畫重寫 | 全部改為逐字元獨立效果 |
| 2026-07-15 | no_speech 預設 0.6→0.4 | 更好的語音偵測結果 |
| 2026-07-15 | 轉錄參數修正 | no_speech_threshold / initial_prompt / vad_parameters 均正確傳入 |
| 2026-07-15 | Bootstrap 修正 | 可選套件不再每次啟動重裝；torch 移出安裝清單 |

---

## 7. 已知限制

- **whisperx 不相容** Python 3.14.6，自動 fallback 到標準對齊
- **`torch` 不在安裝清單**，由 demucs/whisperx 間接安裝（~2GB）
- PNG 渲染為 **逐幀 Pillow 繪製**，大量字幕匯出較慢
- **AI 圖片生成**需外部 API key（OpenAI / Google）
- **Demucs 分離**需 ffmpeg 在 PATH 中
