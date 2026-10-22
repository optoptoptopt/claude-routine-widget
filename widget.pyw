"""排程狀態桌面小窗：螢幕右下角常駐、永遠在最上層，每 5 分鐘自己更新。

2026-10-20 使用者要的是「不用點進去就看得到每支排程現在怎樣」。Claude app 裡的 mod 面板一直畫不出來，
所以改成跟 Claude app 無關的 Windows 小視窗。資料一樣來自 routine_status.build()，不另立判斷。

  pythonw widget.pyw              開視窗（開機捷徑「排程狀態小窗」就是這行）
  python  widget.pyw --selftest   每種狀態都排得出來、讀不到要顯示紅字、點一列會切 session、拖動不算點

可以用滑鼠拖著移動；按右鍵：收起／展開、立即更新、結束。同時只會開一個。
點標題列「▾ 收起」→ 只剩一條（最嚴重那支的顏色＋各狀態幾支），點那條再展開；收起與否記在
%LOCALAPPDATA%\\claude-routine-widget.json，開機後照舊。
點一下有 ↗ 的那列 → Claude app 切到那一輪的 session（被擋的切到卡住它的上一輪）。
用的是 app 自己的 claude://code/continue?session=local_…（工作列跳躍清單同一條路；2026-10-20 實測切得過去）。
封存的 session app 打不開（會落到首頁），所以不給點。
"""
import json
import os
import queue
import re
import sys
import threading
import time

sys.dont_write_bytecode = True     # 不留 __pycache__（.pyc 內嵌來源檔的完整絕對路徑）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routine_status as rs        # noqa: E402

REFRESH_MS = 5 * 60 * 1000
SESSION_URL = "claude://code/continue?session={}"
SESSION_ID = re.compile(r"^local_[A-Za-z0-9-]{1,64}$")     # app 只收這種，其他的它會拒絕
DRAG_PX = 5                        # 按下後移動超過這麼多才算拖；手抖還是當點一下
STATE_FILE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "claude-routine-widget.json")
BG, FG, DIM = "#202124", "#e8eaed", "#9aa0a6"
RED, ORANGE, BLUE, GREEN = "#ff6b6b", "#ffb74d", "#8ab4f8", "#81c995"
LOOK = {                           # state → (標籤, 顏色)；要人處理的排前面是 routine_status 排好的
    "waiting": ("等你按權限", RED),
    "blocked": ("被擋沒跑", RED),
    "missed": ("沒有跑", RED),
    "action": ("要你處理", ORANGE),
    "unfinished": ("沒跑完", ORANGE),
    "running": ("執行中", BLUE),
    "pending": ("還沒到", DIM),
    "done": ("完成", GREEN),
}


def fetch():
    base = os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code-sessions")
    log_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Claude", "Logs")
    try:
        return rs.build(base, log_dir, time.time() * 1000)
    except Exception as e:         # noqa  讀檔失敗也要顯示原因，不是安靜空白
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def rows(data, now=None):
    """→ (標題, [(顏色, 狀態＋名稱, 時間與原因, 點了要開的 session 或 "")], 收起時那一條 (顏色, 文字))。
    讀不到要變成一列紅字，不是空白（讀不到就是沒檢查）。"""
    now = now or time.strftime("%H:%M")
    if not data.get("ok"):
        return (f"排程狀態　{now} 讀不到", [(RED, "讀不到排程狀態", data.get("error") or "（沒有說原因）", "")],
                (RED, "排程　讀不到"))
    out, count = [], {}
    for it in data.get("items") or []:
        label, color = LOOK.get(it.get("state"), (f"不認得的狀態 {it.get('state')}", RED))
        count[label] = count.get(label, 0) + 1
        name = rs.short_name(it.get("name") or it.get("taskId") or "")
        when = (f"{it['slot']} 該跑" if it.get("slot") else "") + (f"，{it['time']} 啟動" if it.get("time") else "")
        sid = it.get("sessionId") or ""
        sid = sid if SESSION_ID.match(sid) and not it.get("archived") else ""
        out.append((color, f"{label}　{name}" + ("　↗" if sid else ""),
                    "　".join(x for x in (when, it.get("detail") or "") if x), sid))
    if not out:
        out.append((DIM, "最近 26 小時沒有該跑的排程", "", ""))
        return f"排程狀態　{now} 更新", out, (DIM, "排程　沒有該跑的")
    # 收起時：顏色跟最嚴重那支（routine_status 已把要人處理的排前面），各狀態幾支照同樣順序
    return f"排程狀態　{now} 更新", out, (out[0][0], "排程　" + "・".join(f"{n} {k}" for k, n in count.items()))


def load_collapsed():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return bool(json.load(f).get("collapsed"))
    except (OSError, ValueError):  # 沒存過就展開
        return False


def save_collapsed(v):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"collapsed": v}, f)
    except OSError:                # 記不住只是下次開機要再收一次，不影響顯示
        pass


def open_session(sid):
    """叫 Claude app 切到那個 session；app 沒開會先把它開起來。"""
    try:
        os.startfile(SESSION_URL.format(sid))
    except OSError as e:           # 打不開要講，不是安靜沒反應
        from tkinter import messagebox
        messagebox.showerror("排程狀態", f"開不了 session {sid}：{e}")


class Widget:
    def __init__(self):
        import tkinter as tk
        self.tk = tk
        r = self.root = tk.Tk()
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-alpha", 0.94)
        r.configure(bg=BG)
        self.scale = r.winfo_fpixels("1i") / 96
        self.width = int(340 * self.scale)
        self.moved = False
        self.dragging = False
        self.open = open_session       # selftest 換掉它，不真的切畫面
        self.collapsed = load_collapsed()
        self.model = None
        self.body = tk.Frame(r, bg=BG, padx=int(10 * self.scale), pady=int(8 * self.scale))
        self.body.pack(fill="both", expand=True)
        menu = tk.Menu(r, tearoff=0)
        menu.add_command(label="收起／展開", command=self.toggle)
        menu.add_command(label="立即更新", command=self.kick)
        menu.add_command(label="結束", command=r.destroy)
        for w in (r, self.body):
            w.bind("<ButtonPress-1>", self._grab)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        self.menu = menu
        self.q = queue.Queue()
        self.render(None)
        self.refresh()
        r.after(300, self.poll)

    # ---- 移動
    def _grab(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()
        self._press = (e.x_root, e.y_root)
        self.dragging = False

    def _drag(self, e):
        if not self.dragging and abs(e.x_root - self._press[0]) + abs(e.y_root - self._press[1]) < DRAG_PX:
            return
        self.dragging = self.moved = True
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _tap(self, action):
        if not self.dragging:          # 拖完放開不算點
            action()

    def toggle(self):
        self.collapsed = not self.collapsed
        save_collapsed(self.collapsed)
        self.render(self.model)

    # ---- 更新：讀檔約 1 秒，放背景執行緒，畫面不卡
    def kick(self):
        threading.Thread(target=lambda: self.q.put(rows(fetch())), daemon=True).start()

    def refresh(self):
        self.kick()
        self.root.after(REFRESH_MS, self.refresh)

    def poll(self):
        try:
            self.render(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(300, self.poll)

    def render(self, model):
        """model＝rows() 的結果；None＝還在讀。收起時只畫一條，點了展開；展開時點標題列收起。"""
        tk, s = self.tk, self.scale
        self.model = model
        title, items, brief = model or ("排程狀態　讀取中…", [], (DIM, "排程　讀取中…"))
        old = (self.root.winfo_x(), self.root.winfo_y(), self.root.winfo_width(), self.root.winfo_height())
        for w in self.body.winfo_children():
            w.destroy()
        font = "Microsoft JhengHei UI"
        indent = int(14 * s)
        if self.collapsed:
            widgets = [(tk.Label(self.body, text=f"● {brief[1]}　▴", bg=BG, fg=brief[0], font=(font, 10, "bold"),
                                 anchor="w"), 0, 0, self.toggle)]
        else:
            widgets = [(tk.Label(self.body, text=f"{title}　▾ 收起", bg=BG, fg=DIM, font=(font, 9), anchor="w"),
                        0, 0, self.toggle)]
            for color, head, detail, sid in items:
                go = (lambda sid=sid: self.open(sid)) if sid else None
                widgets.append((tk.Label(self.body, text="● " + head, bg=BG, fg=color, font=(font, 10, "bold"),
                                         anchor="w", justify="left", wraplength=self.width), 0, int(6 * s), go))
                if detail:
                    widgets.append((tk.Label(self.body, text=detail, bg=BG, fg=FG, font=(font, 9), anchor="w",
                                             justify="left", wraplength=self.width - indent), indent, 0, go))
        for w, left, top, action in widgets:
            w.pack(fill="x", padx=(left, 0), pady=(top, 0))
            w.bind("<ButtonPress-1>", self._grab)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
            if action:
                w.configure(cursor="hand2")
                w.bind("<ButtonRelease-1>", lambda e, a=action: self._tap(a))
        self.root.update_idletasks()
        w = self.body.winfo_reqwidth() if self.collapsed else self.width + int(20 * s)
        h = self.body.winfo_reqheight()
        if self.moved:                 # 拖過的話右下角不動，往左上長／縮
            x, y = old[0] + old[2] - w, old[1] + old[3] - h
        else:
            right, bottom = work_area(self.root)
            m = int(12 * s)
            x, y = right - w - m, bottom - h - m
        self.root.geometry(f"{w}x{h}+{x}+{y}")


def work_area(root):
    """工作列以外的可用範圍（右下角要貼在工作列上方）。"""
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)   # SPI_GETWORKAREA
        return rect.right, rect.bottom
    except Exception:              # noqa
        return root.winfo_screenwidth(), root.winfo_screenheight() - 48


def single_instance():
    """同時只開一個（開機捷徑和手動各開一次不會疊兩個）。回傳 False＝已經有一個在跑。"""
    try:
        import ctypes
        global _MUTEX
        _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "ClaudeRoutineWidget")
        return ctypes.windll.kernel32.GetLastError() != 183          # ERROR_ALREADY_EXISTS
    except Exception:              # noqa  不是 Windows 就不管
        return True


def selftest():
    fails, n = [], [0]

    def check(name, cond):
        n[0] += 1
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            fails.append(name)

    src = open(rs.__file__, encoding="utf-8").read()
    states = set(re.findall(r'"(\w+)": \d', re.search(r"order = \{([^}]*)\}", src).group(1)))
    check("routine_status 會給的每種狀態都有標籤與顏色", states and states <= set(LOOK))
    items = [{"taskId": f"t{i}", "name": "【每天07:00】產日報", "slot": "07:00", "time": "10/20 07:13",
              "state": st, "detail": "等你按 PowerShell"} for i, st in enumerate(sorted(states))]
    title, out, brief = rows({"ok": True, "items": items}, "11:40")
    check("每一支都列出來（含完成的）", len(out) == len(states))
    check("收起那條：每種狀態各幾支都講", all(f"1 {LOOK[st][0]}" in brief[1] for st in states))
    check("名稱去掉【時間】", all(h.endswith("　產日報") for _, h, _, _ in out))
    check("時間與原因都在第二行", all("07:00 該跑，10/20 07:13 啟動" in d and "PowerShell" in d for _, _, d, _ in out))
    check("要人處理的是紅或橘", all(LOOK[s][1] in (RED, ORANGE) for s in ("waiting", "blocked", "missed", "action", "unfinished")))
    check("完成是綠的", LOOK["done"][1] == GREEN)
    _, out, _ = rows({"ok": True, "items": [{"taskId": "x", "state": "新狀態"}]}, "11:40")
    check("不認得的狀態顯示紅字而不是崩潰", out[0][0] == RED and "不認得" in out[0][1])
    title, out, brief = rows({"ok": False, "error": "找不到 scheduled-tasks.json"}, "11:40")
    check("讀不到 → 紅字寫原因，不是空白", out[0][0] == RED and "找不到" in out[0][2] and "讀不到" in title)
    check("讀不到 → 收起那條也是紅字", brief[0] == RED and "讀不到" in brief[1])
    _, out, brief = rows({"ok": True, "items": []}, "11:40")
    check("沒有該跑的也要講出來，不是空白", len(out) == 1 and "沒有該跑" in out[0][1] and "沒有該跑" in brief[1])
    long = "x" * 300
    _, out, _ = rows({"ok": True, "items": [{"taskId": "x", "state": "action", "detail": long}]}, "11:40")
    check("原因不截斷", long in out[0][2])
    _, _, brief = rows({"ok": True, "items": [{"taskId": "a", "state": "waiting"}, {"taskId": "b", "state": "done"},
                                              {"taskId": "c", "state": "done"}]}, "11:40")
    check("收起那條跟最嚴重那支同色、同狀態合計", brief == (RED, "排程　1 等你按權限・2 完成"))

    # ---- 點一列切到 session
    sid = "local_11b70909-3a77-4137-82d6-21740bd99215"
    _, out, _ = rows({"ok": True, "items": [
        {"taskId": "a", "state": "waiting", "sessionId": sid},
        {"taskId": "b", "state": "done", "sessionId": "local_0c1", "archived": True},
        {"taskId": "c", "state": "blocked", "sessionId": ""},
        {"taskId": "d", "state": "done", "sessionId": "local_x & calc"}]}, "11:40")
    check("有 session 的列帶 session、標 ↗", out[0][3] == sid and out[0][1].endswith("↗"))
    check("封存的不給點（app 打不開）", out[1][3] == "" and "↗" not in out[1][1])
    check("沒有 session 的不給點", out[2][3] == "" and "↗" not in out[2][1])
    check("不像 session id 的不給點", out[3][3] == "")
    check("連結是 app 收的格式", SESSION_URL.format(sid) == f"claude://code/continue?session={sid}")

    global STATE_FILE
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="rw_widget_")
    STATE_FILE = os.path.join(tmp, "state.json")    # 不動到使用者真的收起設定
    Widget.refresh = lambda self: None          # 不讀真資料、不排定時
    w = Widget()
    w.root.attributes("-alpha", 0.0)            # 要畫出來才收得到滑鼠事件（withdraw 的收不到），所以透明放螢幕外
    w.root.geometry("+-3000+-3000")
    w.moved = True
    opened = []
    w.open = opened.append
    w.render(rows({"ok": True, "items": [{"taskId": "a", "state": "waiting", "detail": "等你按 Bash", "sessionId": sid},
                                          {"taskId": "c", "state": "blocked", "detail": "被擋"}]}, "11:40"))
    w.root.update()
    head, *labels = w.body.winfo_children()
    link = [x for x in labels if str(x.cget("cursor")) == "hand2"]
    plain = [x for x in labels if str(x.cget("cursor")) != "hand2"]
    check("可點的列（標題＋原因兩行）是手指游標", len(link) == 2)

    def gesture(lbl, dx):
        lbl.event_generate("<ButtonPress-1>", x=5, y=5, rootx=100, rooty=100)
        if dx:
            lbl.event_generate("<B1-Motion>", x=5 + dx, y=5, rootx=100 + dx, rooty=100)
        lbl.event_generate("<ButtonRelease-1>", x=5 + dx, y=5, rootx=100 + dx, rooty=100)

    gesture(link[0], 0)
    check("點一下 → 開那個 session", opened == [sid])
    gesture(link[1], 2)
    check("手抖 2px 還是算點", opened == [sid, sid])
    opened.clear()
    gesture(link[0], 40)
    check("拖著移動放開不算點", opened == [])
    for x in plain:
        gesture(x, 0)
    check("沒有 session 的列點了沒反應", opened == [])

    # ---- 收起／展開
    check("預設展開（沒存過）", not w.collapsed and len(w.body.winfo_children()) == 5)
    w.root.update()
    corner = (w.root.winfo_x() + w.root.winfo_width(), w.root.winfo_y() + w.root.winfo_height())
    gesture(head, 0)
    w.root.update()
    kids = w.body.winfo_children()
    check("點標題列 → 收成一條", w.collapsed and len(kids) == 1 and "等你按權限" in kids[0].cget("text"))
    check("收起那條是最嚴重那支的顏色", str(kids[0].cget("fg")) == RED)
    check("收起後右下角不動",
          (w.root.winfo_x() + w.root.winfo_width(), w.root.winfo_y() + w.root.winfo_height()) == corner)
    check("收起記下來（下次開機還是收著）", load_collapsed() is True)
    gesture(kids[0], 40)
    check("拖收起那條不會展開", w.collapsed)
    w.render(rows({"ok": True, "items": [{"taskId": "a", "state": "done", "sessionId": sid}]}, "11:45"))
    check("更新資料時還是收著", w.collapsed and len(w.body.winfo_children()) == 1)
    gesture(w.body.winfo_children()[0], 0)
    check("點收起那條 → 展開", not w.collapsed and len(w.body.winfo_children()) == 2 and load_collapsed() is False)
    check("點來點去都沒誤開 session", opened == [])
    w.root.destroy()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"selftest: {n[0] - len(fails)}/{n[0]}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.exit(selftest())
    if not single_instance():
        sys.exit(0)
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)    # 高解析度螢幕上字才不會糊
    except Exception:              # noqa
        pass
    Widget().root.mainloop()
