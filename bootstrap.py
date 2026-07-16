"""首次執行時自動補齊 Python 套件與 Windows CUDA DLL 搜尋路徑。"""
from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Callable


# ── 套件清單 ──────────────────────────────────────────────────────
# (import_name, pip_spec, is_optional)
#   is_optional=True  → 使用者啟用特定功能時才安裝
#   is_optional=False → 啟動時必須安裝完成
ALL_PACKAGES: list[tuple[str, str, bool]] = [
    ("numpy",          "numpy>=1.26.0",          False),
    ("PIL",            "Pillow>=10.0.0",          False),
    ("soundfile",      "soundfile>=0.12.1",       False),
    ("requests",       "requests>=2.31.0",        False),
    ("faster_whisper", "faster-whisper>=1.1.0",   False),
    ("demucs",         "demucs>=4.0.1",           False),
    ("torch",          "torch>=2.1.0",            True),
    ("whisperx",       "whisperx>=3.3.0",         True),
]

GPU_PACKAGES = ("nvidia-cublas-cu12", "nvidia-cudnn-cu12")

Status = Callable[[str], None]


# ── 工具函式 ──────────────────────────────────────────────────────
def _pip_install(packages: list[str], status: Status, extra_args: list[str] | None = None) -> bool:
    """安裝套件，回傳 True 表示成功。"""
    status(f"正在安裝：{', '.join(packages)}")
    command = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade"]
    if extra_args:
        command.extend(extra_args)
    command.extend(packages)
    result = subprocess.run(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode == 0


def _verify_import(module: str) -> bool:
    """驗證模組可正常 import（DLL 缺失也能偵測）。"""
    try:
        importlib.import_module(module)
        return True
    except Exception:
        return False


# ── 主要安裝流程 ──────────────────────────────────────────────────
def ensure_required_packages(status: Status) -> None:
    """啟動前完整檢查所有必要套件，缺什麼裝什麼，裝完驗證。"""
    all_ok = all(
        importlib.util.find_spec(mod) is not None and _verify_import(mod)
        for mod, _spec, opt in ALL_PACKAGES if not opt
    )
    if all_ok:
        status("所有必要套件已就緒。")
        check_ffmpeg(status)
        return

    status("正在升級 pip 與 setuptools…")
    _pip_install(["pip", "setuptools", "wheel"], status)

    for module, spec, optional in ALL_PACKAGES:
        if optional:
            continue
        installed = importlib.util.find_spec(module) is not None
        works = installed and _verify_import(module)

        if works:
            continue

        action = "重新安裝" if installed else "安裝"
        status(f"正在{action} {spec}…")

        if installed:
            _pip_install([spec], status, extra_args=["--force-reinstall"])

        if not _verify_import(module):
            ok = _pip_install([spec], status)
            if not ok:
                _pip_install([spec], status, extra_args=["--force-reinstall"])

        if not _verify_import(module):
            status(f"[FAIL] {spec} 安裝失敗，部分功能可能受限")
        else:
            tag = "（選用）" if optional else ""
            status(f"[OK] {spec} 已就緒{tag}")

    check_ffmpeg(status)
    status("所有必要套件已就緒。")


def ensure_optional_package(module: str, package: str, status: Status) -> bool:
    """僅在使用者啟用選用功能時才下載；回傳是否成功。"""
    if importlib.util.find_spec(module) is not None and _verify_import(module):
        return True
    ok = _pip_install([package], status)
    if not ok:
        ok = _pip_install([package], status, extra_args=["--force-reinstall"])
    return ok and _verify_import(module)


def check_ffmpeg(status: Status) -> bool:
    """檢查 ffmpeg 是否可用，不可用時提示。"""
    if shutil.which("ffmpeg"):
        return True
    status("[WARN] 找不到 ffmpeg，聲波顯示與部分匯出功能將受限")
    return False


# ── CUDA ──────────────────────────────────────────────────────────
def add_nvidia_dll_paths() -> bool:
    candidates: list[Path] = []
    for root in map(Path, sys.path):
        nvidia = root / "nvidia"
        if nvidia.is_dir():
            candidates.extend((nvidia / "cublas" / "bin", nvidia / "cudnn" / "bin"))
    cuda_root = os.environ.get("CUDA_PATH")
    if cuda_root:
        candidates.append(Path(cuda_root) / "bin")
    found = False
    for directory in candidates:
        if directory.is_dir():
            found = True
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(directory))
    return found


def install_gpu_runtime(status: Status) -> None:
    _pip_install(list(GPU_PACKAGES), status)
    add_nvidia_dll_paths()


def gpu_runtime_ready() -> bool:
    add_nvidia_dll_paths()
    return bool(shutil.which("cublas64_12.dll") and shutil.which("cudnn64_9.dll"))
