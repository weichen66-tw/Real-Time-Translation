"""
介面文字（中文／英文）

啟動視窗（launcher.py）和字幕程式（overlay_subtitle.py）所有給人看的文字都放這裡，
每一句都要同時寫中文和英文兩份。程式裡用 t("key") 拿目前語言的文字，
有 {name} 這種空格的，用 t("key", name=...) 填進去。

語言由各自的程式決定一次：
    啟動視窗：看設定檔的 ui_lang，沒有就照 Windows 的語言猜
    字幕程式：啟動視窗會用 --ui-lang 傳過去，自己單獨跑時照 Windows 的語言猜

注意：語音辨識、翻譯本身的語言是「翻譯方向」決定的，跟這裡無關。
      給 Gemini 的提示文字也不是介面文字，放在 overlay_subtitle.py。
"""

import ctypes

# 語言代號 → 選單上顯示的名字（用各自的語言寫，外國人才認得出來）
LANGS = {"zh-TW": "繁體中文", "en": "English"}
LANG_INDEX = {"zh-TW": 0, "en": 1}

# key → (繁體中文, English)
TEXTS = {
    # ── 啟動視窗 ──────────────────────────────────────
    "app_title": ("遊戲即時字幕", "Game Live Subtitles"),
    "lang_label": ("介面語言 Language", "Language 介面語言"),
    "tips_title": ("使用步驟", "How to use"),
    "tips": (
        "① 遊戲請設成「無邊框視窗」（全螢幕獨佔會看不到字幕）\n"
        "② 選好下面的設定，按「開始」\n"
        "③ 用滑鼠把字幕拖到喜歡的位置，拖右下角的握把 ◢ 可以調寬度（都會自動記住）\n"
        "④ 遊戲中隨時可用：Ctrl+Alt+方向鍵 移動字幕、Ctrl+Alt+L 鎖定／解鎖\n"
        "　　（鎖定＝滑鼠穿透，不會擋到遊戲操作）",
        '① Set your game to "Borderless window" (subtitles can\'t show over exclusive fullscreen)\n'
        '② Choose the settings below and press "Start"\n'
        "③ Drag the subtitles where you like; drag the ◢ grip at the bottom-right to resize"
        " (both are remembered)\n"
        "④ In game, any time: Ctrl+Alt+Arrow keys move the subtitles, Ctrl+Alt+L locks/unlocks\n"
        "　　(Locked = clicks pass through, so it won't block your game)",
    ),
    "settings_title": ("設定", "Settings"),
    "app_label": ("只翻譯這個程式", "Only this app"),
    "refresh": ("重新整理", "Refresh"),
    "app_hint": (
        "選 Discord 就只翻譯 Discord 的聲音，遊戲角色講話不會被翻。"
        "\n清單裡沒有的話，先把那個程式打開，再按「重新整理」",
        "Pick Discord to translate only Discord voices; in-game characters won't be translated."
        '\nNot in the list? Open that app first, then press "Refresh"',
    ),
    "no_app": ("（不指定，錄整台電腦的聲音）", "(None: capture all PC audio)"),
    "device_label": ("聲音來源", "Audio source"),
    "default_device": ("Windows 預設喇叭", "Windows default speakers"),
    "device_hint": ("別人講話的聲音從哪個喇叭／耳機播出來，就選哪個",
                    "Pick the speakers or headphones that play other people's voices"),
    "device_hint_disabled": ("已指定程式，這項用不到", "Not used when an app is selected"),
    "direction_label": ("翻譯方向", "Translation"),
    "direction_hint": ("對方講什麼語言就選哪個；選錯的話會聽不出字或變亂碼",
                       "Choose the language the other person speaks; the wrong choice gives"
                       " no text or gibberish"),
    "dir_en_zh": ("英文 → 中文（對方講英文，字幕顯示中文翻譯）",
                  "English → Chinese (they speak English, subtitles show Chinese)"),
    "dir_zh_en": ("中文 → 英文（對方講中文，字幕顯示英文翻譯）",
                  "Chinese → English (they speak Chinese, subtitles show English)"),
    "translator_label": ("翻譯方式", "Translator"),
    "tr_google": ("Google 翻譯（快，不用金鑰）", "Google Translate (fast, no key needed)"),
    "tr_gemini": ("Gemini（比較口語，但慢，需要金鑰）", "Gemini (more natural, slower, needs a key)"),
    "key_label": ("Gemini 金鑰", "Gemini API key"),
    "key_hint_google": ("用 Google 翻譯不需要金鑰", "No key needed for Google Translate"),
    "key_hint_saved": ("✔ 已設定（要換新金鑰才需要填）", "✔ Saved (only fill this in to replace it)"),
    "key_hint_missing": ("到 https://aistudio.google.com/apikey 免費申請後貼上",
                         "Get a free key at https://aistudio.google.com/apikey and paste it here"),
    "model_label": ("辨識準確度", "Accuracy"),
    "model_base": ("快速（base，比較不準）", "Fast (base, less accurate)"),
    "model_small": ("標準（small）", "Standard (small)"),
    "model_large": ("精準（large-v3-turbo，需要 NVIDIA 顯示卡）",
                    "Accurate (large-v3-turbo, needs an NVIDIA GPU)"),
    "download": ("下載", "Download"),
    "model_hint_ready": ("✔ 已備妥，不用下載", "✔ Ready to use, no download needed"),
    "model_hint_missing": ("尚未下載（約 {size}）→ 按右邊的「下載」，只要下載一次",
                           'Not downloaded yet (about {size}) → press "Download" on the right;'
                           " it only needs downloading once"),
    "lines_label": ("字幕行數", "Subtitle lines"),
    "seconds_label": ("切段秒數", "Max segment (s)"),
    "seconds_hint": ("講不停時最多幾秒就先送出字幕 → 調小出現得快，句子易被切斷",
                     "How long nonstop speech is held before showing → lower is faster,"
                     " but may cut sentences"),
    "rms_label": ("音量門檻", "Volume threshold"),
    "rms_hint": ("遊戲音效太吵、字幕一直亂跳 → 調高；別人講話沒反應 → 調低",
                 "Random subtitles from game sounds → raise it; voices not picked up → lower it"),
    "click_through": ("一開始就鎖定字幕（滑鼠穿透，不會擋到遊戲操作；隨時可按 Ctrl+Alt+L 解鎖）",
                      "Start with subtitles locked (clicks pass through to the game;"
                      " press Ctrl+Alt+L to unlock any time)"),
    "start": ("▶ 開始", "▶ Start"),
    "stop": ("■ 停止", "■ Stop"),
    "log_title": ("執行紀錄", "Log"),
    "err_key_save": ("金鑰存檔失敗：{error}", "Couldn't save the key: {error}"),
    "warn_no_key": (
        "選了 Gemini 翻譯，但還沒有金鑰。\n\n"
        "請到 https://aistudio.google.com/apikey 免費申請，"
        "貼到「Gemini 金鑰」欄位；或改用 Google 翻譯。",
        "Gemini is selected, but there is no API key yet.\n\n"
        "Get a free key at https://aistudio.google.com/apikey and paste it into"
        ' "Gemini API key", or switch to Google Translate.',
    ),
    "err_start": ("無法啟動字幕程式：{error}", "Couldn't start the subtitle program: {error}"),
    "dl_title": ("下載語音辨識模型", "Downloading the speech model"),
    "dl_connecting": ("連線中⋯", "Connecting…"),
    "dl_progress": ("已下載 {done} ／ 共 {total}", "{done} of {total} downloaded"),
    "dl_cancel": ("取消", "Cancel"),
    "dl_ask": (
        "「{model}」還沒下載（約 {size}），要現在下載嗎？\n\n"
        "需要網路，只要下載一次，之後就不用了。",
        '"{model}" hasn\'t been downloaded yet (about {size}). Download it now?\n\n'
        "It needs an internet connection, and only has to be downloaded once.",
    ),
    "dl_failed": (
        "下載失敗：\n\n{error}\n\n"
        "請確認網路正常後，再按一次「下載」。已經下載的部分會保留，不會從頭來過。",
        "Download failed:\n\n{error}\n\n"
        'Check your internet connection and press "Download" again. What was already'
        " downloaded is kept, so it resumes from there.",
    ),
    "dl_cancelled": ("已取消下載。下次再按「下載」會從中斷的地方接續。",
                     'Download cancelled. Pressing "Download" again resumes where it stopped.'),
    "status_idle": ("尚未開始", "Not started"),
    "status_starting": ("啟動中⋯（第一次開啟可能要等 1～2 分鐘，防毒軟體在檢查檔案）",
                        "Starting… (the first run can take 1–2 minutes while antivirus scans the files)"),
    "status_stopping": ("停止中⋯", "Stopping…"),
    "status_loading": ("載入語音辨識模型中⋯", "Loading the speech model…"),
    "status_model": ("{line}，準備錄音⋯", "{line}, getting ready to record…"),
    "status_running": ("✅ 運作中：有人講話就會出現字幕", "✅ Running: subtitles appear when someone speaks"),
    "status_stopped": ("已停止", "Stopped"),
    "status_crashed": ("⚠️ 字幕程式意外結束（代碼 {code}），請看下方紀錄",
                       "⚠️ The subtitle program stopped unexpectedly (code {code}); see the log below"),

    # ── 字幕程式（會出現在啟動視窗的執行紀錄）────────────
    "ov_devices_title": ("\n=== 可用的 loopback 裝置 ===", "\n=== Available loopback devices ==="),
    "ov_devices_hint": ("\n用 --device 加上名稱的一部分即可指定，例如：--device CABLE\n",
                        "\nUse --device with part of a name to pick one, e.g. --device CABLE\n"),
    "ov_no_device_named": ("找不到名稱包含「{name}」的 loopback 裝置。\n用 --list-devices 看看有哪些可選。",
                           'No loopback device name contains "{name}".\n'
                           "Run with --list-devices to see the options."),
    "ov_no_loopback": ("找不到 loopback 裝置，請先確認 capture_test.py 跑得起來",
                       "No loopback device found. Check that capture_test.py works first."),
    "ov_app_not_running": ("找不到正在執行的「{name}」。請先把它打開，再重新開始字幕。",
                           '"{name}" is not running. Open it first, then start the subtitles again.'),
    "ov_app_capture_failed": ("無法錄「{name}」的聲音（{error}）。"
                              "請確認它還開著，或把「只翻譯這個程式」改回不指定。",
                              'Couldn\'t capture audio from "{name}" ({error}). '
                              'Make sure it\'s still open, or set "Only this app" back to none.'),
    "ov_app_closed": ("「{name}」已經關閉，不會再有字幕了。重新打開它之後，請按「停止」再按「開始」。",
                      '"{name}" was closed, so there will be no more subtitles. '
                      'After reopening it, press "Stop" and then "Start".'),
    "ov_model_prefix": ("模型：", "Model: "),
    "ov_gpu_unavailable": ("GPU 不可用，改用 CPU（{error}）", "GPU unavailable, using CPU instead ({error})"),
    "ov_transcript_failed": ("逐字稿存不了（{error}）。字幕照常顯示，只是不會留下紀錄。",
                             "Couldn't save the transcript ({error}). Subtitles still work,"
                             " but nothing will be recorded."),
    "ov_models_title": ("\n可用的型號（請修改檔案上方的 GEMINI_MODEL）：",
                        "\nAvailable models (edit GEMINI_MODEL at the top of the file):"),
    "ov_models_failed": ("列出型號失敗：{error}", "Couldn't list models: {error}"),
    "ov_google_conn": ("連線失敗（{error}）", "connection failed ({error})"),
    "ov_google_http": ("Google 回應 HTTP {status}", "Google returned HTTP {status}"),
    "ov_google_429": ("，送太多次被暫時擋下", ", temporarily blocked for too many requests"),
    "ov_google_bad": ("Google 回應的內容看不懂", "Google's response couldn't be read"),
    "ov_google_empty": ("Google 回了空白的結果", "Google returned an empty result"),
    "ov_google_failed": ("第 {seq} 句 Google 翻譯失敗：{error}（{text}）",
                         "Line {seq}: Google Translate failed: {error} ({text})"),
    "ov_gemini_model_gone": ("\n型號 {model} 不可用。", "\nModel {model} is not available."),
    "ov_gemini_failed": ("第 {seq} 句 Gemini 翻譯失敗：{error}（{text}）",
                         "Line {seq}: Gemini failed: {error} ({text})"),
    "ov_gemini_no_text": ("回應裡沒有譯文", "no translation in the response"),
    "ov_gemini_fallback": ("Google 沒翻到，改用 Gemini 補翻中⋯", "Google failed, trying Gemini…"),
    "ov_gemini_fallback_ok": ("第 {seq} 句已改用 Gemini 補翻成功", "Line {seq}: translated with Gemini instead"),
    "ov_hotkeys_taken": ("部分快速鍵被其他程式佔用了，只有部分能用",
                         "Some hotkeys are taken by other programs; only some will work"),
    "ov_pos_offscreen": ("上次的字幕位置不在目前的螢幕上（外接螢幕沒接？），先顯示在預設位置",
                         "The last subtitle position isn't on any connected screen"
                         " (external monitor unplugged?), so using the default position"),
    "ov_locked": ("字幕已鎖定：滑鼠可以穿透，改用快速鍵移動",
                  "Subtitles locked: clicks pass through; use the hotkeys to move them"),
    "ov_unlocked": ("字幕已解鎖：可以用滑鼠拖曳，右下角握把可以調寬度",
                    "Subtitles unlocked: drag with the mouse, resize with the grip at the bottom-right"),
    "ov_translating": ("翻譯中⋯", "Translating…"),
    "ov_bad_translator": ("--translator 只能是 google 或 gemini", "--translator must be google or gemini"),
    "ov_bad_direction": ("--direction 只能是 {options}", "--direction must be {options}"),
    "ov_bad_seconds": ("--max-seconds 只能在 {low:.0f} 到 {high:.0f} 之間",
                       "--max-seconds must be between {low:.0f} and {high:.0f}"),
    "ov_direction": ("翻譯方向：{label}", "Translation: {label}"),
    "ov_dir_en_zh": ("英文 → 中文", "English → Chinese"),
    "ov_dir_zh_en": ("中文 → 英文", "Chinese → English"),
    "ov_tr_google_fallback": ("翻譯：Google 翻譯（沒翻到的句子會改用 Gemini 補翻）",
                              "Translator: Google Translate (failed lines fall back to Gemini)"),
    "ov_tr_google_only": ("翻譯：Google 翻譯（沒有 Gemini 金鑰，沒翻到的句子不會補翻）",
                          "Translator: Google Translate (no Gemini key, so failed lines are not retried)"),
    "ov_tr_gemini": ("翻譯：Gemini 已啟用", "Translator: Gemini enabled"),
    "ov_tr_no_key": ("翻譯：未設定 Gemini key，只顯示原文",
                     "Translator: no Gemini key set, showing the original text only"),
    "ov_tr_no_key_hint": ("      （要翻譯的話，在 {file} 寫入 GEMINI_API_KEY=你的key，或改用 Google 翻譯）",
                          "      (To translate, add GEMINI_API_KEY=your_key to {file},"
                          " or use Google Translate)"),
    "ov_loading": ("載入模型中，請稍候⋯", "Loading the model, please wait…"),
    "ov_source_app": ("聲音來源：只錄「{name}」這個程式（含子程序，編號 {pid}）",
                      'Audio source: only "{name}" (including its child processes, PID {pid})'),
    "ov_device": ("裝置：{name}", "Device: {name}"),
    "ov_segment": ("切段：安靜 {silence:.1f} 秒算講完，講不停最多 {max:.0f} 秒切一段",
                   "Segments: {silence:.1f}s of silence ends a line; nonstop speech is cut every {max:.0f}s"),
    "ov_window_open": ("\n字幕視窗已開啟。", "\nSubtitle window is open."),
    "ov_start_locked": ("目前是鎖定狀態：滑鼠會穿透字幕，用快速鍵移動",
                        "Currently locked: clicks pass through the subtitles; use the hotkeys to move them"),
    "ov_start_unlocked": ("可以直接用滑鼠拖曳字幕，右下角的握把可以調寬度。按 Esc 關閉。",
                          "Drag the subtitles with the mouse; resize with the grip at the bottom-right."
                          " Press Esc to close."),
    "ov_hotkey_help": ("快速鍵：Ctrl+Alt+方向鍵 移動字幕、Ctrl+Alt+L 鎖定／解鎖（遊戲中也有效）",
                       "Hotkeys: Ctrl+Alt+Arrow keys move the subtitles, Ctrl+Alt+L locks/unlocks"
                       " (works in game too)"),
    "ov_pos_remembered": ("位置和寬度都會自動記住", "Position and width are remembered automatically"),
    "ov_borderless": ("⚠️ 遊戲請設成「無邊框視窗」，全螢幕獨佔看不到字幕\n",
                      '⚠️ Set your game to "Borderless window"; subtitles can\'t show over'
                      " exclusive fullscreen\n"),
}

_current = "zh-TW"


def system_lang():
    """Windows 顯示語言是中文（繁體、簡體都算）就用中文，其他語言一律用英文"""
    LANG_CHINESE = 0x04
    try:
        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except (AttributeError, OSError):
        return "zh-TW"
    return "zh-TW" if lang_id & 0x3FF == LANG_CHINESE else "en"


def set_lang(lang):
    """切換目前語言；不認得的代號就照 Windows 的語言猜"""
    global _current
    _current = lang if lang in LANGS else system_lang()


def current_lang():
    return _current


def t(key, **values):
    """拿目前語言的文字；有 {空格} 的用 values 填進去"""
    text = TEXTS[key][LANG_INDEX[_current]]
    return text.format(**values) if values else text


def variants(key):
    """這句話所有語言的版本。啟動視窗用來辨認字幕程式印出來的訊息，不管它用哪種語言印"""
    return list(TEXTS[key])
