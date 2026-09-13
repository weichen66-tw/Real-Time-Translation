# CLAUDE.md

這份檔案提供給 Claude Code（claude.ai/code）參考，說明在這個專案裡該怎麼工作。

## 與我協作的規則（請每次遵守）

1. 我沒有程式背景。所有說明用白話繁體中文，不要用術語；必須用術語時，第一次出現要用一句話解釋。
2. 不要順手重構、不要順手改名、不要順手升級套件版本。如果你認為需要，寫在建議裡，由我決定。
3. 每次改完，用白話告訴我：改了哪些檔案、各改了什麼、我要怎麼測試才知道有沒有成功。
4. 遇到不確定的地方，停下來問我，不要自己假設。
5. 需要安裝新套件時，先說明用途、維護者、對系統的影響，等我同意。
6. 絕對不要刪除或改寫我沒有要求你動的檔案。
7. `~/.claude/rules/ecc/common/` 裡的開發流程規範（先上網搜尋、寫規劃文件、先寫自動測試、測試覆蓋率 80%、改完一律派 AI 助手審查）不用照做，除非我特別要求；改完做必要的簡單測試、確認能正常運作就好。

## 這個專案是什麼

這是一套只能在 Windows 上跑的三階段流程：先把 Discord 或遊戲裡別人講話的聲音，從電腦的喇叭輸出端錄下來，
交給 faster-whisper（一套語音轉文字的程式）聽成英文字，再送去翻成中文（預設 Google 翻譯，可切換成 Gemini），
最後用一條浮在遊戲畫面上的半透明字幕條顯示出來。

一般使用者的入口是 `launcher.py`（啟動視窗）：用點的選設定、按「開始」，不用記指令參數。
要給沒裝 Python 的朋友用時，用 `build_exe.py` 打包成 exe。
這個專案沒有自動測試、也沒有記錄套件清單的檔案。
程式裡的註解和所有跑出來給人看的訊息都是繁體中文，之後修改也請維持這個習慣。

## 指令

```powershell
# 安裝需要的套件（專案裡沒有 requirements.txt，要自己手動裝）
py -m pip install pyaudiowpatch numpy scipy faster-whisper
py -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"   # 讓語音辨識用 NVIDIA 顯示卡
py -m pip install pyinstaller                                  # 只有要打包 exe 才需要

# 啟動視窗——一般使用就開這個
py launcher.py

# 打包成 exe，產出 dist/遊戲即時字幕/ 和 dist/遊戲即時字幕.zip（約數 GB，要跑好幾分鐘）
py build_exe.py

# 第一階段——先確認錄得到電腦播放的聲音；會產生 capture_test.wav
py capture_test.py            # 錄 15 秒
py capture_test.py 30         # 錄 30 秒
py capture_test.py --list     # 列出可以錄的音訊裝置

# 第二階段——在終端機（就是黑底白字的指令視窗）持續印出聽到的英文；會一直寫進 transcript.txt
py live_transcribe.py
py live_transcribe.py --model base --min-rms 300 --device CABLE --cpu
py live_transcribe.py --list-devices

# 第三階段——真正要用的成品：浮在畫面上的英中雙語字幕條
py overlay_subtitle.py
py overlay_subtitle.py --click-through --lines 2 --model base --min-rms 400
py overlay_subtitle.py --device CABLE      # 只錄 VB-Cable 這個裝置，可以避開遊戲音效
py overlay_subtitle.py --translator gemini # 改用 Gemini 翻譯（預設 google）
py overlay_subtitle.py --max-seconds 5     # 講不停時最多幾秒切一段（預設 8）
py overlay_subtitle.py --app Discord.exe   # 只錄這個程式的聲音，不錄遊戲音效
```

第二、三階段共用的幾個選項：`--model`（選 whisper 模型大小，預設 `small`，越大越準但越慢）、
`--min-rms`（音量門檻，預設 200，決定多大聲才算「有人在講話」）、
`--device`（指定音訊裝置，只要打名字的一小段就會自動比對）、
`--cpu`（不要嘗試用顯示卡，直接用 CPU 跑）、`--list-devices`（列出可選的裝置）。

這個專案沒有寫自動測試，出問題是靠眼睛看出來的：第二、三階段執行時，畫面上會即時顯示一條音量長條，
上面標著門檻在哪裡；按 Ctrl+C 結束時，還會告訴你這次錄到的最大音量是多少，
可以照這個數字回頭調整 `--min-rms`。

## 程式架構

**三個階段的關係。** `capture_test.py` → `live_transcribe.py` → `overlay_subtitle.py`
這三支程式是「功能一層層疊上去」，而不是互相呼叫的關係。每一支都是完整獨立的，
後面那支會把前面那支的音訊處理程式碼原封不動再抄一份。真正在用的只有 `overlay_subtitle.py`，
另外兩支存在的目的是分段找問題：如果字幕沒出來，就往回退一階，
看看是「完全錄不到聲音」、還是「錄得到但聽不出文字」、還是「都好只是視窗沒顯示」。

**這代表：** 只要修到 `find_loopback`、`to_mono_16k`、`load_model`、`add_nvidia_dll_dirs` 或聲音切段那段邏輯，
通常 `live_transcribe.py` 和 `overlay_subtitle.py` **兩個檔案都要改**，不能只改一邊。

**啟動視窗（`launcher.py`）。** 它本身不做錄音或辨識，按「開始」時是另外開一個程序
（程序 = 一個獨立在跑的程式）去跑 `overlay_subtitle.main()`，把畫面上的設定轉成
`--model`、`--translator` 這些參數傳過去，再把對方印出來的訊息一行行接回來顯示在「執行紀錄」區，
也靠這些訊息（例如「字幕視窗已開啟」）更新狀態列。所以**改 `overlay_subtitle.py` 印出來的文字時，
要一起檢查 `launcher.py` 的 `update_status`**。按「停止」就直接把那個程序關掉。
打包成 exe 後同一支 exe 身兼兩職：不帶參數是啟動視窗，帶 `--overlay` 就是字幕程序。
啟動視窗記住的設定存在 `launcher_settings.json`，Gemini 金鑰從視窗填的話會寫進 `.env`。

**打包 exe（`build_exe.py`）。** 用 PyInstaller（把 Python 程式連同需要的東西包成 exe 的工具）。
pip 裝的 NVIDIA 元件 PyInstaller 不會自動收，要在 `nvidia_binaries()` 手動加，這些元件就佔了約 2GB。
程式執行時產生的檔案（`.env`、`overlay_pos.json`、`transcript.txt`、`launcher_settings.json`）
在 exe 版會存在 exe 旁邊（程式裡用 `sys.frozen` 判斷），壓縮檔會自動排除這些個人檔案。

**多執行緒的分工（`overlay_subtitle.py`）。**
（執行緒 = 讓程式同時做好幾件事的機制，可以想成同時有好幾個人在做不同工作。）
這裡有四種角色，中間用三個「佇列」（排隊用的傳遞區，前面的人做完丟進去，後面的人再拿出來）串起來。
這樣設計是為了讓語音辨識跑很慢、或翻譯的網路連線卡住時，都不會害錄音停掉或畫面凍住：

```
capture_worker ──audio_q──> stt_worker ──trans_q──> translate_worker ×2
   （錄音）                （聽成文字）              （翻譯，兩個同時跑）
                                │                          │
                                └────────ui_q─────────────┘
                                          ↓
                             Overlay.poll() 在畫面主執行緒上，每 0.1 秒檢查一次
```

`seq` 是一個從 1 開始不斷往上加的編號，每聽完一句話就給它一個號碼。英文字會先送到畫面上
（`("new", seq, en)`），中文翻譯晚一點才到（`("zh", seq, zh)`），靠這個編號對回原本那一句。
正因為有編號，兩個翻譯工作才可以同時跑而不會讓字幕順序亂掉。
`live_transcribe.py` 沒這麼複雜，只有一個錄音執行緒加上主程式處理。

**怎麼判斷一句話講完了。** 這裡沒有用現成的套件，是自己寫的：算出當下音量（rms，簡單說就是聲音大小），
音量超過 `min_rms` 就開始收音；安靜超過 `SILENCE_TO_FLUSH`（1 秒）就當作這句講完，送去辨識；
如果有人一直講不停，`MAX_SEGMENT_SEC`（預設 8 秒，用 `--max-seconds` 或啟動視窗的「切段秒數」調整）
會強制切一段。這個值不要設太大：實測切成 30 秒一段時，whisper 的辨識結果會整段變成亂碼。

另外還準備了一個 `PREROLL_SEC`（0.5 秒）的小緩衝區，永遠偷偷留著「開始講話前那半秒」的聲音，
在開始收音時補到最前面。沒有這個的話，每句話開頭的第一個音會被切掉。
除了這套自己寫的判斷，交給 whisper 辨識時還會再開一層它內建的過濾（`vad_filter=True`）。

**聲音格式的轉換。** Windows 錄下來的聲音是裝置原本的取樣率和聲道數（左右聲道交錯排在一起）。
`to_mono_16k` 負責把它轉成 whisper 唯一能接受的格式：先換算數值範圍，把多聲道平均成單聲道，
再用 `resample_poly` 轉成 16000 Hz。這步不能省，格式不對 whisper 就不會動。
`overlay_subtitle.py` 把後半段抽成 `float_to_mono_16k`（只錄單一程式時拿到的已經是 float32，
不用換算數值範圍），`live_transcribe.py` 那邊仍是完整的一份，行為要保持一致。

**只錄單一程式的聲音（`--app`）。** 用 proc-tap 套件，背後是 Windows 10 2004 之後才有的
「程式音訊迴路」：指定一個程序編號，只錄那個程序**和它底下的子程序**播出來的聲音
（Discord 會開一堆子程序，所以一定要抓最上層那個，`find_app_pid` 在做這件事）。
實測對無關的程式錄到的是完全靜音。啟動視窗的程式清單用 pycaw 列出「已經發出過聲音」的程式，
Windows 要程式真的播過聲音才會有這筆資料，所以清單旁邊要有「重新整理」。
指定 `--app` 時就不會去開 pyaudio 的 loopback 裝置，`--device` 會被忽略。

**顯示卡跑不動時的退路。** `load_model` 在建立模型之後，一定要真的拿一段假聲音跑一次（程式裡叫 `warmup`）。
原因是：光「建立模型」這個動作不會真的去載入顯示卡的函式庫，所以就算顯示卡環境是壞的，
這時候也不會報錯，要等到第一句話進來才爆掉。先跑一次假的，壞掉會當場發現，
就能安靜地改用 CPU。兩支程式裡都有這段註解，請不要把這個行為改掉。

pip 裝的 NVIDIA 元件（cuBLAS、cuDNN）放在 `site-packages/nvidia/*/bin`，Windows 預設找不到，
所以 `load_model` 試顯示卡之前會先呼叫 `add_nvidia_dll_dirs()` 把那些資料夾加進搜尋路徑。
少了這步會出現 `cublas64_12.dll is not found`，然後默默退回 CPU（實測慢 14 倍）。

**翻譯怎麼做的。** 兩種都沒有用官方套件，只用 Python 內建的網路功能：
- **Google 翻譯（預設，`google_translate`）**：走 Google 翻譯網頁版背後的免費管道
  `translate.googleapis.com/translate_a/single?client=gtx`，不用金鑰，通常 0.1 秒內回來。
  **這不是 Google 正式對外的服務**，哪天可能被擋。每個翻譯執行緒會重複使用同一條連線
  （不重複使用每句多等 1～2 秒），斷線或 Google 回一次性錯誤時會自動重試一次。
- **Gemini（`gemini_translate`）**：送請求給 `generateContent` 網址。免費版伺服器很不穩，
  實測一句 0.6～28 秒。模型要用 lite 版（目前 `gemini-flash-lite-latest`），
  非 lite 版會先「思考」把 `maxOutputTokens` 吃光，翻譯被切一半。
  如果遇到 404（通常代表模型被下架），程式只會提醒一次，並用 `show_available_models`
  印出這把金鑰還能用哪些模型，方便回去改檔案最上面的 `GEMINI_MODEL`。

翻譯只要出任何問題，就當作沒翻到，字幕那一行顯示 `—`，英文字幕照常繼續跑，不會整個當掉。

**字幕視窗怎麼做的。** 用 Python 內建的 `tkinter`（做視窗介面的工具）把視窗邊框拿掉
（`overrideredirect(True)`）、永遠釘在最上層（`-topmost`）、再調成半透明。
視窗的底邊是固定的（程式裡的 `self.bottom_y`），字太多需要換行時，視窗會往上長，
這樣字幕才不會上下亂跳。位置和寬度存在 `overlay_pos.json`（`x`、`y`、`w`），下次開啟會記得。

**移動與大小。** 位置一律由 `self.x`／`self.y` 記著，**不要改成去問 `winfo_x()`**：
剛下完移動指令時 Windows 還沒更新那個值，連續移動（按住快速鍵）會算錯、
移完馬上存檔會存到舊位置，這兩個問題都發生過。`place_at` 負責移動並夾在所有螢幕範圍內，
`fit_height` 改高度時也要同步更新 `self.y`。
拖曳一定要綁到 `draggable_widgets()` 回傳的**每一個**元件：字幕文字外面還包了一層 Frame，
漏掉它的話，點到文字周邊的空白就拖不動（早期就是只綁 root 和 Label，使用者反映「拖不動」）。
右下角的握把（`self.grip`）負責調寬度，所以要從拖曳清單排除。

**鎖定（滑鼠穿透）。** `set_locked()` 用 `ctypes` 加上或移除 `WS_EX_TRANSPARENT`，
所以可以隨時切換，不必重開。鎖定時滑鼠會穿過字幕點到遊戲，代價是不能拖曳、不能按 Esc，
握把也會收起來。`--click-through` 只是決定「一開始是不是鎖定」。

**快速鍵。** `hotkey_worker` 用 `RegisterHotKey` 註冊全系統快速鍵
（Ctrl+Alt+方向鍵移動、Ctrl+Alt+L 切換鎖定），這樣遊戲在前景或字幕鎖定時也按得到。
Windows 規定註冊和收訊息要在同一個執行緒，所以兩件事都在那個執行緒裡；
按鍵只丟指令進 `cmd_q`，真正動視窗的是畫面主執行緒的 `handle_hotkeys`
（tkinter 的東西只能在主執行緒動）。

**注意：** 遊戲如果開「全螢幕獨佔」模式，是看不到字幕的，一定要設成「無邊框視窗」。

## 這個資料夾要注意的事

- **金鑰放在 `.env`**（格式是一行 `GEMINI_API_KEY=金鑰`），`.env` 已列在 `.gitignore`，不會被上傳。
  `overlay_subtitle.py` 的 `read_key()` 會先讀 `.env`，找不到再讀 `GEMINI_API_KEY` 這個系統環境變數
  （存在電腦裡、不會跟著程式碼一起上傳的設定值）。請不要再往專案裡放任何密碼或金鑰。
- `transcript.txt`（每次執行都會往後接的逐字稿）和 `overlay_pos.json`（記住字幕視窗位置）
  這兩個檔案是程式跑出來的，但也被上傳到 git 了。所以只要手動跑過程式，
  git 就會顯示這兩個檔案「有變動」，這是正常的。
- `.gitignore`（用來指定哪些檔案不要上傳 git 的清單檔）列了金鑰（`.env`）、
  `launcher_settings.json`，以及打包產生的 `build/`、`dist/`、`*.spec`、`__pycache__/`。
- `dist/遊戲即時字幕/` 每次打包都會被整個清掉重做，不要把自己的東西放在裡面。
