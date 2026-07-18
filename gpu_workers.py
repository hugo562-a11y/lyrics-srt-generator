"""在獨立行程執行需要 torch 的 GPU 工作（人聲分離／強制對齊）。

torch 內建的 cuDNN（CUDA 13）跟 nvidia-cudnn-cu12（ctranslate2／faster-whisper
需要，CUDA 12）檔名相同但版本不同，同一行程內混用會讓其中一邊載入錯版本的
DLL 而崩潰（CUDNN_STATUS_SUBLIBRARY_VERSION_MISMATCH）。把會 import torch 的
工作丟到這支獨立行程執行，主行程就不會再被污染。
"""
from __future__ import annotations

import json
import sys


def _run_demucs(args: list[str]) -> None:
    from demucs.separate import main as demucs_main
    demucs_main(args)


def _run_whisperx(input_path: str, output_path: str) -> None:
    import whisperx

    with open(input_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    source_path = payload["source_path"]
    model_name = payload["model_name"]
    lang_code = payload["language"]
    device = payload["device"]
    temperature = payload["temperature"]
    compute_type = "float16" if device == "cuda" else "int8"

    model = whisperx.load_model(model_name, device=device, compute_type=compute_type, language=lang_code, asr_options={"temperatures": [temperature]})
    result = model.transcribe(source_path, batch_size=16, language=lang_code)
    align_lang = result.get("language", lang_code) or "zh"
    align_model, metadata = whisperx.load_align_model(language_code=align_lang, device=device)
    aligned = whisperx.align(result["segments"], align_model, metadata, source_path, device=device)

    words = []
    for seg in aligned.get("segments", []):
        seg_words = seg.get("words", [])
        for w in seg_words:
            ws, we = w.get("start"), w.get("end")
            wt = str(w.get("word", "")).strip()
            if wt and ws is not None and we is not None and we > ws:
                words.append({"start": float(ws), "end": float(we), "text": wt})
        if not seg_words:
            text = str(seg.get("text", "")).strip()
            s, e = seg.get("start", 0), seg.get("end", 0)
            if text and e > s:
                words.append({"start": float(s), "end": float(e), "text": text})

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"words": words}, f)


def main() -> None:
    mode = sys.argv[1]
    if mode == "demucs":
        _run_demucs(sys.argv[2:])
    elif mode == "whisperx":
        _run_whisperx(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(f"unknown worker mode: {mode}")


if __name__ == "__main__":
    main()
