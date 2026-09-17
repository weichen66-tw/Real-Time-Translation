"""
語音辨識模型放在哪裡、有沒有備妥、怎麼下載。

模型一律放在「exe 旁邊（開發時是這個專案資料夾）的 models 資料夾」，一個模型一個子資料夾：
    models/small/  ← model.bin、config.json、tokenizer.json、vocabulary.txt

這樣做的原因：打包時可以把預設的 small 模型一起放進去（build_exe.py 的 BUNDLED_MODELS），
朋友解壓縮就能直接用，不用第一次按「開始」時從國外網站下載幾百 MB
（實測網路慢或被擋的時候會卡在那裡不動，看起來像當掉）。
models 資料夾裡沒有的模型，才需要在啟動視窗按「下載」。

用到這裡的檔案：
    launcher.py          顯示「已備妥／尚未下載」、一鍵下載
    overlay_subtitle.py  載入模型前先問要用哪個來源
    live_transcribe.py   同上（除錯用的簡化版，行為要跟上面一致）
    build_exe.py         打包時把內建的模型複製到 exe 旁邊
"""

import os
import sys
from pathlib import Path

# 從 Hugging Face 下載模型時，它新版的下載方式（xet）實測會卡在 0% 不動，
# 改用傳統的 HTTP 下載比較穩。一定要在 import faster_whisper 之前設定才有效，
# 所以放在這裡最上面（download() 裡才會真的去 import）。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

# 打包成 exe 時模型放在 exe 旁邊，而不是解壓縮用的暫存資料夾
HERE = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent)
MODELS_DIR = HERE / "models"

# 每個模型下載完大約有多少位元組。只拿來算下載進度條的百分比，
# 跟實際差一點點不影響使用（進度最多顯示到 99%，下載完成才跳 100%）。
TOTAL_BYTES = {"base": 147_882_941,
               "small": 486_212_372,
               "large-v3-turbo": 1_621_665_983}

# 這幾個檔案都在才算下載完整。vocabulary 有的模型是 .txt、有的是 .json，另外檢查。
REQUIRED_FILES = ("config.json", "model.bin", "tokenizer.json")

# Hugging Face 上的模型倉庫名稱，抄自 faster_whisper.utils._MODELS。
# 只拿來檢查「以前是不是已經下載過了」，所以只要涵蓋啟動視窗提供的這三個。
HF_REPOS = {"base": "Systran/faster-whisper-base",
            "small": "Systran/faster-whisper-small",
            "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"}


def model_dir(name):
    """這個模型在 models 資料夾裡的位置"""
    return MODELS_DIR / name


def total_bytes(name):
    """下載完大約多少位元組；不認得的模型回 0"""
    return TOTAL_BYTES.get(name, 0)


def has_local(name):
    """models 資料夾裡已經有這個模型（完整的）"""
    folder = model_dir(name)
    return (all((folder / f).is_file() for f in REQUIRED_FILES)
            and any(folder.glob("vocabulary.*")))


def hf_cache_dir():
    """Hugging Face 把下載過的模型放在哪裡（跟它自己的預設規則一致）"""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface")
    return Path(home) / "hub"


def in_hf_cache(name):
    """
    舊版本（模型還沒放進 models 資料夾的時候）下載過的模型，會留在 Hugging Face 的快取裡。
    這種情況也算備妥，不能再叫使用者下載一次。
    """
    repo = HF_REPOS.get(name)
    if not repo:
        return False
    folder = hf_cache_dir() / ("models--" + repo.replace("/", "--"))
    return any(folder.glob("snapshots/*/model.bin"))


def is_ready(name):
    """不必再下載就能直接開始用"""
    return has_local(name) or in_hf_cache(name)


def resolve(name):
    """
    要交給 WhisperModel 的東西：models 資料夾裡有就給資料夾路徑（免下載），
    沒有就給原本的名字（faster-whisper 會自己找快取，找不到才上網下載）。
    """
    return str(model_dir(name)) if has_local(name) else name


def downloaded_bytes(name):
    """models/<名稱> 資料夾目前有多大，拿來算下載進度（含下載到一半的暫存檔）"""
    folder = model_dir(name)
    if not folder.is_dir():
        return 0
    total = 0
    for f in folder.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass        # 下載中的檔案隨時可能被改名或搬走，算不到就跳過
    return total


def download(name):
    """
    把模型下載到 models/<名稱>。已經有了就什麼都不做，中途失敗會直接丟出例外。
    斷線重來會從中斷的地方接續（huggingface_hub 自己處理的）。
    """
    if has_local(name):
        return
    import warnings
    from faster_whisper.utils import download_model
    with warnings.catch_warnings():
        # faster-whisper 1.2 會傳一個新版 huggingface_hub 已經不用的參數過去，
        # 它只是印個提醒、不影響下載，但會讓使用者看到一段看不懂的英文警告
        warnings.simplefilter("ignore", UserWarning)
        download_model(name, output_dir=str(model_dir(name)))
