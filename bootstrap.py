"""首次執行時自動補齊 Python 套件與 Windows CUDA DLL 搜尋路徑。"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Callable


REQUIRED_PACKAGES = {
    "faster_whisper": "faster-whisper>=1.1.0",
    "soundfile": "soundfile>=0.12.1",
    "numpy": "numpy>=1.26.0",
    "PIL": "Pillow>=10.0.0",
}
GPU_PACKAGES = ("nvidia-cublas-cu12", "nvidia-cudnn-cu12")
Status = Callable[[str], None]


def _pip_install(packages: list[str], status: Status) -> None:
    status("正在下載必要套件：" + ", ".join(packages))
    command = [sys.executable, "-m", "pip", "install", "--upgrade", *packages]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        tail = result.stdout[-1600:]
        raise RuntimeError(f"套件安裝失敗。請確認網路連線後重試。\n\n{tail}")


def ensure_required_packages(status: Status) -> None:
    """只安裝目前環境缺少的套件，不重複下載已安裝內容。"""
    missing = [spec for module, spec in REQUIRED_PACKAGES.items() if importlib.util.find_spec(module) is None]
    if missing:
        _pip_install(missing, status)
    status("必要套件已就緒。")


def ensure_optional_package(module: str, package: str, status: Status) -> None:
    """僅在使用者啟用選用功能時才下載對應套件。"""
    if importlib.util.find_spec(module) is None:
        _pip_install([package], status)


def add_nvidia_dll_paths() -> bool:
    """讓 pip 安裝的 NVIDIA DLL 可被 CTranslate2 找到。"""
    candidates: list[Path] = []
    for root in map(Path, sys.path):
        nvidia = root / "nvidia"
        if nvidia.is_dir():
            candidates.extend((nvidia / "cublas" / "bin", nvidia / "cudnn" / "bin"))
    # CUDA Toolkit 預設位置也會被收錄，方便已有安裝的使用者。
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
    """下載 CTranslate2 在 CUDA 12 下所需的 cublas/cudnn wheel。"""
    _pip_install(list(GPU_PACKAGES), status)
    add_nvidia_dll_paths()


def gpu_runtime_ready() -> bool:
    """確認 CTranslate2 的 CUDA 12 執行期 DLL 都可被目前行程找到。"""
    add_nvidia_dll_paths()
    return bool(shutil.which("cublas64_12.dll") and shutil.which("cudnn64_9.dll"))
