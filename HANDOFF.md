# 歌詞 SRT 產生器：交接說明

最後更新：2026-07-12  
GitHub：<https://github.com/hugo562-a11y/lyrics-srt-generator>  
目前分支：`main`

## 專案目的

Windows Python 桌面程式：匯入歌曲音檔，以本機 Whisper 取得演唱時間，搭配使用者提供的正確歌詞產生含 `[前奏]`、`[間奏]`、`[尾奏]` 的 SRT。匯出前可在程式內播放並檢查文字跟隨位置。

## 目前檔案結構

```text
app.py          Tkinter 主程式、Whisper 分析、歌詞對齊、SRT、播放控制
bootstrap.py    自動檢查／安裝 Python 套件、CUDA DLL 路徑與 runtime
requirements.txt 基本 Python 依賴
run.bat         建立 .venv 後啟動程式
build_exe.bat   PyInstaller Windows 打包腳本
README.md       使用說明
HANDOFF.md      本文件
```

## 已完成功能

- 匯入 `mp3/wav/m4a/flac/aac/ogg`，讀取音檔長度。
- 本機 `faster-whisper`：模型可選 `tiny` 至 `large-v3`，語言與 CPU/GPU 可選。
- 缺少基本 Python 套件時自動安裝；啟動期間只檢查一次。
- GPU 模式：檢查 `cublas64_12.dll`、`cudnn64_9.dll`；缺少時自動透過 PyPI 補 NVIDIA runtime。GPU 無法使用時可退回 CPU。
- 匯入 `.txt` / `.lrc` 歌詞（每句一行；LRC 舊時間碼忽略）。
- 「精準逐字對齊」：Whisper `word_timestamps=True`，輸出採歌詞檔原文，不採 AI 聽錯文字。
- 對齊保護：同時考慮文字相似度與每句字數，避免後段辨識錯誤後字幕提早跑完。
- 使用參考歌詞時，完整分析不啟用 VAD，避免拖音被切掉；另執行一次保守 VAD 來找第一句人聲，保留前奏空檔。
- 依歌詞間空檔產生 `[前奏]`、`[間奏]`、`[尾奏]`；門檻由「最短音樂段」控制。
- 表格可雙擊編輯開始、結束、類型、文字；具新增、刪除／還原、Undo/Redo、SRT 匯出。
- 可選 Demucs 人聲分離（首次按需安裝及下載模型）；若其安裝失敗，關閉勾選仍可用。
- 進度條：下載套件、模型、CUDA runtime、人聲分離、AI 分析時顯示活動動畫。
- 預聽：使用系統 FFmpeg 的 `ffplay`，避免 Python 3.14 無 pygame wheel 的編譯失敗。
  - 點選表格列：從該列開始連續播放。
  - `只播選取句`：到該列結束自動停止。
  - 停止會保留目前位置。
  - 播放時自動捲動並藍色標示目前區段。

## 重要設計與環境資訊

- 開發端 Python 為 `C:\Python314\python.exe`。這造成 `pygame` 無法取得 wheel；不要改回 pygame。
- 本機已有 `C:\Program Files\ffmpeg\bin\ffplay.exe`，且 `ffprobe` / `ffplay` 都在 PATH。
- GPU：RTX 3070，NVIDIA Driver 581.57（顯示 CUDA 13）。系統沒有 CUDA Toolkit / `nvcc` / `CUDA_PATH`。
- `nvidia-cublas-cu12` 已在使用者 site-packages，但 `nvidia-cudnn-cu12` 初次尚未安裝；`bootstrap.py` 會處理 DLL 目錄與安裝。
- 工作區目前有使用者私有、未追蹤檔案：歌詞 TXT、測試 MP3、輸出的 SRT。**不要 add、commit 或刪除它們。**

## 最近修改與已知限制

### 前奏與後段對齊

最後一次修改（commit `4623d81`）以第二次 VAD 分析偵測第一句人聲，並從完整分析的逐字錨點中移除該人聲開始點之前的錨點。這是為了同時避免：

1. 關閉 VAD 後，前奏伴奏被幻聽為第一句歌詞，導致 `[前奏]` 消失。
2. 開啟 VAD 後，後段拖音／伴奏中的演唱被過早切掉，導致歌詞提早跑完。

此方法已通過純邏輯測試，但**尚未以實際完整歌曲反覆人工驗聽**；這是最高優先驗證項目。若仍不準，最值得做的是把「第一句人聲起點」與「逐句對齊」視覺化，或導入真正的強制對齊模型；目前是基於 Whisper 時間錨點的近似對齊。

### 播放器

`ffplay` 由 subprocess 啟動，程式用 `time.monotonic()` 推算播放游標。這對預聽足夠，但不是 sample-accurate。`ffplay -ss` 對某些格式的 seek 精度可能有限。

### 人聲分離

`demucs>=4.0.1` 是選用依賴。Python 3.14 是否能順利安裝 Demucs/PyTorch 尚未在這台電腦完整驗證。若失敗，應顯示簡短錯誤並保留不分離的流程；不要讓它阻斷基本功能。

### 打包

`build_exe.bat` 目前使用 PyInstaller 收集 `faster_whisper` / `ctranslate2`。尚未實做真正 EXE 打包與乾淨電腦測試。發佈前需測試模型下載、FFmpeg 隨附或 PATH、GPU/CPU fallback。

## 驗證指令

```powershell
python -m py_compile app.py bootstrap.py
python app.py
```

目前已有幾個以 `python -c` 執行過的邏輯驗證：

- 時間碼轉換、前奏／間奏／尾奏建立。
- 參考歌詞保留原文、逐字錨點對齊。
- 後段 AI 文字皆錯時，字數約束仍將歌詞分佈至最後錨點。
- 進度列接線。
- `ffplay -version` 可執行。

## 建議下一步

1. 先以使用者的 `等了九個月 -60sc.mp3` 與其 TXT，在 GUI 實際跑一次，播放檢查 `[前奏]`、後段與單句播放。
2. 若前奏仍消失，檢查兩次 Whisper 分析的第一個 segment 時間；可能需要提供手動「前奏結束」時間或更可靠人聲檢測。
3. 若後段仍漂移，考慮加入波形、拖曳句首／句尾與鍵盤 50ms 微調；或評估可在 Python 3.14 使用的強制對齊方案。
4. 以乾淨 Windows 使用者環境測試 `run.bat` / `build_exe.bat`。

