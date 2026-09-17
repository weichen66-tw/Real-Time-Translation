"""
把啟動視窗打包成 exe，給沒裝 Python 的人用。

用法：
    py build_exe.py

需要先裝好：
    py -m pip install pyinstaller pyaudiowpatch numpy scipy faster-whisper proc-tap pycaw
    py -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"

產出：
    dist/遊戲即時字幕/          ← 可以直接在這台電腦上雙擊裡面的 exe 試用
    dist/遊戲即時字幕.zip       ← 要給朋友的就是這個壓縮檔

BUNDLED_MODELS 裡的語音辨識模型會一起包進去，朋友解壓縮就能直接用，不用自己下載。
這台電腦的 models/ 資料夾裡還沒有的話，打包時會先幫你下載一次（之後就不用再等）。
其他沒內建的模型，使用者在啟動視窗按「下載」就會拿到。

⚠️ 每次打包都會把 dist/遊戲即時字幕/ 整個清掉重做，不要把自己的東西放在裡面。
⚠️ 壓縮檔會自動排除金鑰（.env）和個人設定，就算你在 dist 裡試用過也不會外流。
"""

import sys
import shutil
import zipfile
import subprocess
from pathlib import Path

import whisper_models

APP_NAME = "遊戲即時字幕"
HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
APP_DIR = DIST / APP_NAME
ZIP_FILE = DIST / f"{APP_NAME}.zip"

# 程式執行時才會產生、屬於個人的檔案，絕對不能打包進給別人的壓縮檔
PERSONAL_FILES = {".env", "launcher_settings.json", "overlay_pos.json", "transcript.txt"}

# 給使用者看的說明（中文、英文），打包時複製到 exe 旁邊
GUIDES = ("使用說明.txt", "User Guide (English).txt")

# 要一起包進去的語音辨識模型（啟動視窗「辨識準確度」的預設值是 small）。
# 多包一個就多好幾百 MB，所以只內建最常用的那一個，其他讓使用者自己按「下載」。
BUNDLED_MODELS = ("small",)


def nvidia_binaries():
    """
    pip 裝的 NVIDIA 元件放在 site-packages/nvidia/<名稱>/bin，PyInstaller 不會自動收，
    這裡手動加進去，並保持同樣的資料夾結構，
    讓 overlay_subtitle.add_nvidia_dll_dirs() 在 exe 裡也找得到。
    """
    args = []
    for p in sys.path:
        for bin_dir in (Path(p) / "nvidia").glob("*/bin"):
            dest = f"nvidia/{bin_dir.parent.name}/bin"
            args += ["--add-binary", f"{bin_dir}\\*.dll;{dest}"]
    if not args:
        print("⚠️ 沒找到 NVIDIA 元件，打包出來的 exe 只能用 CPU 跑")
    return args


def run_pyinstaller():
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--windowed", "--name", APP_NAME,
           "--hidden-import", "overlay_subtitle",
           "--collect-all", "faster_whisper",
           "--collect-all", "ctranslate2",
           "--collect-all", "onnxruntime",
           "--collect-all", "proctap",      # 只錄單一程式的聲音（含它自己的 DLL）
           "--collect-all", "pycaw",        # 列出正在發出聲音的程式
           "--collect-all", "comtypes",
           "--hidden-import", "psutil",
           *nvidia_binaries(),
           str(HERE / "launcher.py")]
    subprocess.run(cmd, cwd=HERE, check=True)


def bundle_models():
    """把要內建的模型放到 exe 旁邊的 models/ 底下，這台電腦還沒有就先下載一次"""
    for name in BUNDLED_MODELS:
        if not whisper_models.has_local(name):
            size = whisper_models.total_bytes(name) / 1024 ** 2
            print(f"\n下載 {name} 模型（約 {size:,.0f} MB，只有第一次打包要等）⋯")
            whisper_models.download(name)
        dest = APP_DIR / "models" / name
        shutil.rmtree(dest, ignore_errors=True)
        # 跳過 .cache：那是下載套件自己的暫存資料夾，給朋友的版本用不到
        shutil.copytree(whisper_models.model_dir(name), dest,
                        ignore=shutil.ignore_patterns(".cache"))
        print(f"已內建模型：{name}")


def make_zip():
    """壓縮成一個檔案方便傳給朋友，同時排除個人檔案"""
    ZIP_FILE.unlink(missing_ok=True)
    with zipfile.ZipFile(ZIP_FILE, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(APP_DIR.rglob("*")):
            if f.is_file() and f.name not in PERSONAL_FILES:
                z.write(f, f.relative_to(DIST))


def folder_size_mb(path):
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1024 ** 2


def main():
    run_pyinstaller()
    for guide in GUIDES:
        (APP_DIR / guide).write_bytes((HERE / guide).read_bytes())
    bundle_models()
    print("\n壓縮中（檔案很大，要等一下）⋯")
    make_zip()
    print(f"\n完成！")
    print(f"  資料夾：{APP_DIR}（{folder_size_mb(APP_DIR):,.0f} MB）")
    print(f"  壓縮檔：{ZIP_FILE}（{ZIP_FILE.stat().st_size / 1024 ** 2:,.0f} MB）")


if __name__ == "__main__":
    main()
