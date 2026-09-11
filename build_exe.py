"""
把啟動視窗打包成 exe，給沒裝 Python 的人用。

用法：
    py build_exe.py

需要先裝好：
    py -m pip install pyinstaller pyaudiowpatch numpy scipy faster-whisper
    py -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"

產出：
    dist/遊戲即時字幕/          ← 可以直接在這台電腦上雙擊裡面的 exe 試用
    dist/遊戲即時字幕.zip       ← 要給朋友的就是這個壓縮檔

⚠️ 每次打包都會把 dist/遊戲即時字幕/ 整個清掉重做，不要把自己的東西放在裡面。
⚠️ 壓縮檔會自動排除金鑰（.env）和個人設定，就算你在 dist 裡試用過也不會外流。
"""

import sys
import zipfile
import subprocess
from pathlib import Path

APP_NAME = "遊戲即時字幕"
HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
APP_DIR = DIST / APP_NAME
ZIP_FILE = DIST / f"{APP_NAME}.zip"

# 程式執行時才會產生、屬於個人的檔案，絕對不能打包進給別人的壓縮檔
PERSONAL_FILES = {".env", "gemini_key.txt", "launcher_settings.json",
                  "overlay_pos.json", "transcript.txt"}


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
           *nvidia_binaries(),
           str(HERE / "launcher.py")]
    subprocess.run(cmd, cwd=HERE, check=True)


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
    (APP_DIR / "使用說明.txt").write_bytes((HERE / "使用說明.txt").read_bytes())
    print("\n壓縮中（檔案很大，要等一下）⋯")
    make_zip()
    print(f"\n完成！")
    print(f"  資料夾：{APP_DIR}（{folder_size_mb(APP_DIR):,.0f} MB）")
    print(f"  壓縮檔：{ZIP_FILE}（{ZIP_FILE.stat().st_size / 1024 ** 2:,.0f} MB）")


if __name__ == "__main__":
    main()
