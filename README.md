# 排程狀態小窗（Claude Routine Widget）

Claude desktop app 的 **排程（Code 分頁的 scheduled tasks）** 常常「沒跑」，但 app 裡看不出來為什麼。
這是一個常駐在 Windows 螢幕右下角、永遠在最上層的小視窗，每 5 分鐘自己更新，
一眼告訴你**每一支排程最近該跑的那一輪，有沒有跑、跑完沒、沒跑是誰擋的**。

```
排程狀態　11:17 更新　▾ 收起
● 等你按權限　每晚財經紀錄　↗
    21:40 該跑，10/21 21:47 啟動　等你按 Bash（10/21 21:48 起，已等 13 小時）
● 被擋沒跑　每天查 AdSense 審查　↗
    09:00 該跑，10/15 09:03 啟動　上一輪（10/15 09:03）還卡著：沒結束；這輪被擋 2 小時
● 沒有跑　Shorts 抓數字進追蹤表
    09:30 該跑　該在 10/22 09:30 跑，沒有啟動也沒有被擋的紀錄（當時 app 沒開或電腦睡著？）
```

## 為什麼需要它

排程沒跑，真正的原因幾乎都是同一個連鎖反應：

1. 某一輪跑到一半跳出**權限詢問**（例如要執行 Bash），沒人按，那一輪就一直不會結束。
2. 同一支排程的下一輪被 app 擋掉（`per_task_limit`：上一輪還沒結束）。
3. 同時卡住 3 支以上，**其他無關的排程也一起被擋**（`global_limit`）。

app 本身不會告訴你這些。小窗會直接講出「是哪一輪、卡在哪個工具的權限」擋住它，
點那一列就跳到那個 session，按掉權限，後面就恢復了。

## 狀態一覽

| 狀態 | 顏色 | 意思 |
|---|---|---|
| 等你按權限 | 紅 | 這一輪卡在權限詢問，沒人按 |
| 被擋沒跑 | 紅 | 這一輪沒啟動，被上一輪或其他排程佔住名額（會講出是誰） |
| 沒有跑 | 紅 | 該跑的時間過了 30 分鐘，沒啟動也沒被擋的紀錄（通常是 app 沒開或電腦睡著） |
| 要你處理 | 橘 | 跑完了，但結果摘要說需要你動手 |
| 沒跑完 | 橘 | 啟動超過 3 小時還沒有結果摘要 |
| 執行中 | 藍 | 正在跑 |
| 還沒到 | 灰 | 排定時間剛到，app 還沒啟動（app 常晚 3~15 分鐘） |
| 完成 | 綠 | 跑完了 |

要人處理的排最上面。只列最近 26 小時內該跑的排程，以及還掛著沒結束的舊執行。

## 操作

- **點有 ↗ 的那一列**：叫 Claude app 切到那一輪的 session（被擋的會切到卡住它的上一輪）。已封存的 session app 打不開，所以不給點。
- **點標題列「▾ 收起」**：縮成一條（顏色跟最嚴重那支一樣，並列出各狀態幾支）；再點那條展開。收起與否開機後照舊。
- **拖曳**：可以移到任何位置。
- **右鍵**：收起／展開、立即更新、結束。
- 同時只會開一個（有 mutex），重複開不會疊兩個。

## 它讀什麼（只讀，不改任何東西）

都是 Claude desktop app 自己寫的檔：

- `%APPDATA%\Claude\claude-code-sessions\<帳號>\<組織>\scheduled-tasks.json`：排程設定（cron／單次）與 `recordedSkips`（被擋原因）
- 同資料夾的 `local_*.json`：每次執行一個 session（啟動時間、結果摘要）
- `%LOCALAPPDATA%\Claude\Logs\main.log` 與 `main1.log`：找「發出了權限詢問、但沒有對應回應」的紀錄。main.log 滿 10MB 會輪替成 main1.log，兩個都要讀，否則會漏。

讀不到就顯示紅字「讀不到」並寫原因，不會安靜顯示空白（讀不到＝沒檢查，不等於沒問題）。

## 需求

- Windows（用到 `ctypes.windll`、`os.startfile` 與 Windows 的資料路徑）
- Claude desktop app
- Python 3.10 以上，含 tkinter（python.org 的安裝程式預設就有）。不需要任何第三方套件。

## 安裝

```powershell
# 1. 兩支放同一個資料夾（widget 會 import 旁邊的 routine_status）
git clone https://github.com/optoptoptopt/claude-routine-widget "$env:USERPROFILE\.claude\routine-widget"
cd "$env:USERPROFILE\.claude\routine-widget"

# 2. 自測（-B 不留 __pycache__）
python -B routine_status.py --selftest    # 要 28/28
python -B widget.pyw --selftest           # 要 32/32
python -B routine_status.py               # 要印出 "ok": true

# 3. 開機自動啟動：在啟動資料夾建捷徑
$pyw = (Get-Command pythonw).Source
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) '排程狀態小窗.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $pyw
$s.Arguments = "`"$PWD\widget.pyw`""
$s.WorkingDirectory = "$PWD"
$s.Save()

# 4. 現在就開
explorer.exe $lnk
```

用 `explorer.exe` 開捷徑，不要從 Claude 的終端機直接跑 `pythonw`：那樣會帶進 Claude session 的環境變數，session 關掉時小窗也會跟著被關。

如果 Claude 是從 Microsoft Store 裝的，資料可能在 `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\...` 底下。
找到真正的位置後，只改組路徑的兩處：`routine_status.py` 最底下的 `__main__`、`widget.pyw` 的 `fetch()`。

想讓另一台電腦上的 Claude Code 代裝，把 [安裝提示詞.md](安裝提示詞.md) 裡那段貼給它即可。

## 設定值

在 `routine_status.py` 開頭，通常不用改：

| 常數 | 預設 | 意思 |
|---|---|---|
| `WINDOW_H` | 26 | 最近該跑的時間在這麼多小時內才列出來 |
| `GRACE_MIN` | 30 | app 實際會晚 3~15 分鐘才啟動；超過這麼久沒啟動才算沒跑 |
| `STALE_SUMMARY_H` | 3 | 啟動超過這麼久還沒有結果摘要 → 算沒跑完 |
| `ACTIVE_DAYS` | 8 | 往回找「還沒結束、會擋住後面」的舊執行看幾天 |

更新頻率在 `widget.pyw` 的 `REFRESH_MS`（預設 5 分鐘）。

## 修改後

結束命令列含 `widget.pyw` 的那個 `pythonw`，再用 explorer 開捷徑。
改了判斷就跑 `routine_status.py --selftest`，改了畫面就跑 `widget.pyw --selftest`，都要全過。

## 移除

刪啟動資料夾裡的捷徑、刪程式資料夾、刪 `%LOCALAPPDATA%\claude-routine-widget.json`（只記收起與否）。

## 限制

- 讀的是 Claude desktop 的**內部**檔案與日誌格式，不是公開介面。app 改版若改了格式，會顯示紅字「讀不到」或狀態不準，那時要對照新格式改 `routine_status.py`。
- 只支援 Windows。Mac 要改路徑與視窗程式碼。
- 切 session 用的是 app 的 `claude://code/continue?session=local_…` 連結。
- 非 Anthropic 官方工具。

## 授權

[MIT](LICENSE)：可自由使用、修改、散布（含商用），保留著作權聲明即可；不附任何保證。
