"""排程（Routines）執行狀態：給排程狀態小窗（widget.pyw）讀的 JSON。

問的是「每支排程最近該跑的那一輪，有沒有跑、跑完沒、沒跑是誰擋的」。

資料來源（都是 Claude desktop 自己寫的，只讀不改）：
  %APPDATA%/Claude/claude-code-sessions/<acct>/<org>/scheduled-tasks.json
      scheduledTasks（cronExpression／fireAt）、recordedSkips（每分鐘一筆被擋原因：
      per_task_limit＝同一支上一輪還沒結束、global_limit＝同時已有 3 支沒結束）
  同資料夾 local_*.json   每次執行一個 session（scheduledTaskId、postTurnSummary）
  %LOCALAPPDATA%/Claude/Logs/main.log ＋ main1.log（main.log 滿 10MB 會輪替成 main1.log，
      只讀 main.log 會漏掉輪替前發出的權限請求——2026-10-19 實際發生）
      「Emitted tool permission request X … in session S」沒有對應的
      「Received permission response for X」＝還在等人按

讀不到就回 ok:false 並說原因，不回空清單（讀不到就是沒檢查）。
"""
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta

WINDOW_H = 26          # 最近該跑的時間在這麼多小時內才列出來
GRACE_MIN = 30         # app 實際啟動會晚 3–15 分鐘；超過這麼久沒啟動才算沒跑
STALE_SUMMARY_H = 3    # 啟動超過這麼久還沒有結果摘要 → 沒跑完
ACTIVE_DAYS = 8        # 找「還沒結束、會擋住後面」的舊執行時往回看幾天
LOG_TAIL = 3 * 1024 * 1024
LOG_NAMES = ("main1.log", "main.log")   # 舊的先讀

EMIT = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[info\] Emitted tool permission request (\S+) for (.+?) in session (local_[0-9a-f-]+)")
RESP = re.compile(r"Received permission response for (\S+):")


# ---------- cron ----------

def _field(spec, lo, hi):
    out = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-"))
        else:
            a = b = int(part)
        out.update(range(a, b + 1, step))
    return out


def last_slot(cron, now):
    """cron 最近一次 ≤ now 的觸發時間（本地時間）；找不到回 None。
    日與星期都有限定時取聯集（標準 cron 行為）。"""
    try:
        mi, hr, dom, mon, dow = cron.split()
        mins, hrs = _field(mi, 0, 59), _field(hr, 0, 23)
        doms, mons = _field(dom, 1, 31), _field(mon, 1, 12)
        dows = {d % 7 for d in _field(dow, 0, 7)}
    except (ValueError, AttributeError):
        return None
    for back in range(0, 40):
        day = (now - timedelta(days=back)).date()
        if day.month not in mons:
            continue
        cd = (day.weekday() + 1) % 7          # cron：0＝週日
        m_dom, m_dow = day.day in doms, cd in dows
        if dom != "*" and dow != "*":
            ok = m_dom or m_dow
        else:
            ok = m_dom and m_dow
        if not ok:
            continue
        for h in sorted(hrs, reverse=True):
            for m in sorted(mins, reverse=True):
                t = datetime(day.year, day.month, day.day, h, m)
                if t <= now:
                    return t
    return None


# ---------- 日誌 ----------

def parse_log(text):
    """回傳 {session_id: [(emit_epoch_ms, tool), ...]}，只含沒有回應的請求。"""
    emitted, answered = {}, set()
    for line in text.splitlines():
        m = EMIT.match(line)
        if m:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() * 1000
            emitted[m.group(2)] = (m.group(4), ts, m.group(3))
            continue
        m = RESP.search(line)
        if m:
            answered.add(m.group(1))
    pending = {}
    for rid, (sid, ts, tool) in emitted.items():
        if rid not in answered:
            pending.setdefault(sid, []).append((ts, tool))
    return pending


def read_tail(path, n):
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - n))
        return f.read().decode("utf-8", errors="ignore")


def read_logs(log_dir):
    paths = [os.path.join(log_dir, n) for n in LOG_NAMES]
    if not os.path.isfile(paths[-1]):
        raise FileNotFoundError(f"找不到 app 日誌 {paths[-1]}")
    return "\n".join(read_tail(p, LOG_TAIL) for p in paths if os.path.isfile(p))


# ---------- 判斷 ----------

def hm(ms):
    return f"{datetime.fromtimestamp(ms / 1000):%H:%M}"


def mdhm(ms):
    return f"{datetime.fromtimestamp(ms / 1000):%m/%d %H:%M}"


def dur(ms):
    h = ms / 3600_000
    return f"{h:.0f} 小時" if h >= 1 else f"{ms / 60_000:.0f} 分鐘"


def turn_finished(run):
    return bool(run.get("postTurnSummary")) and bool(run.get("lastAssistantUuid")) \
        and run.get("postTurnSummaryFor") == run.get("lastAssistantUuid")


def waiting_on(run, pending):
    waits = [w for w in pending.get(run["sessionId"], [])
             if run.get("lastActivityAt", 0) < w[0] + 90_000]   # 發出請求後就沒動靜
    return min(waits) if waits else None


def classify(run, pending, now_ms):
    """一次執行 → (state, detail)。state: waiting / running / action / done / unfinished"""
    w = waiting_on(run, pending)
    if w:
        return "waiting", f"等你按 {w[1]}（{mdhm(w[0])} 起，已等 {dur(now_ms - w[0])}）"
    if turn_finished(run):
        s = run["postTurnSummary"]
        cat = s.get("status_category")
        if cat == "blocked":
            return "action", s.get("needs_action") or s.get("status_detail") or ""
        if cat in ("completed", "review_ready"):
            return "done", s.get("status_detail") or ""
    if now_ms - run["createdAt"] < STALE_SUMMARY_H * 3600_000:
        return "running", ""
    return "unfinished", "沒有結果摘要，打開看看"


def is_active(run):
    """app 眼中還沒結束、會佔住名額的執行。"""
    return not run.get("isArchived") and not turn_finished(run)


def short_name(name):
    return re.sub(r"^【[^】]*】", "", name).strip() or name


def build(base, log_dir, now_ms):
    task_files = glob.glob(os.path.join(base, "*", "*", "scheduled-tasks.json"))
    if not task_files:
        return {"ok": False, "error": f"找不到 scheduled-tasks.json（{base}）"}
    pending = parse_log(read_logs(log_dir))
    now = datetime.fromtimestamp(now_ms / 1000)

    items = []
    for tf in task_files:
        folder = os.path.dirname(tf)
        with open(tf, encoding="utf-8") as f:
            data = json.load(f)
        tasks = {t["id"]: t for t in data.get("scheduledTasks", [])}
        skips = data.get("recordedSkips") or {}

        runs = {}
        floor = now_ms - ACTIVE_DAYS * 86400_000
        for sf in glob.glob(os.path.join(folder, "local_*.json")):
            if os.path.getmtime(sf) * 1000 < floor:
                continue
            with open(sf, encoding="utf-8") as f:
                run = json.load(f)
            if run.get("scheduledTaskId") and run.get("createdAt", 0) >= floor:
                runs.setdefault(run["scheduledTaskId"], []).append(run)
        for lst in runs.values():
            lst.sort(key=lambda r: r["createdAt"])
        active = {tid: [r for r in lst if is_active(r)] for tid, lst in runs.items()}
        active_names = [short_name(tasks[tid].get("displayName") or tid)
                        for tid, lst in active.items() if lst and tid in tasks]

        for tid, t in tasks.items():
            if not t.get("enabled"):
                continue
            name = t.get("displayName") or tid
            if t.get("cronExpression"):
                slot_dt = last_slot(t["cronExpression"], now)
                slot = slot_dt.timestamp() * 1000 if slot_dt else None
            elif t.get("fireAt"):
                slot = float(t["fireAt"]) if float(t["fireAt"]) <= now_ms else None
            else:
                slot = None
            mine = runs.get(tid, [])
            latest = mine[-1] if mine else None
            in_window = slot is not None and slot >= now_ms - WINDOW_H * 3600_000
            if not in_window and not (latest and is_active(latest)):
                continue

            # slotAt／waitSince：給其他程式認「同一輪、同一次卡住」用
            item = {"taskId": tid, "name": name, "slot": hm(slot) if slot else "",
                    "slotAt": int(slot or 0), "startedAt": 0, "sessionId": "", "archived": False, "time": ""}
            this_run = None
            if slot is not None:
                cand = [r for r in mine if r["createdAt"] >= slot - 60_000]
                this_run = cand[0] if cand else None
            if not in_window:              # 該跑的時間早過了，但上一輪還掛著
                this_run = latest

            if this_run:
                state, detail = classify(this_run, pending, now_ms)
                late = this_run["createdAt"] - slot if slot else 0
                if in_window and late > GRACE_MIN * 60_000:
                    detail = f"晚了 {dur(late)}才啟動" + (f"；{detail}" if detail else "")
                item.update(state=state, detail=detail, startedAt=this_run["createdAt"],
                            sessionId=this_run["sessionId"], time=mdhm(this_run["createdAt"]),
                            archived=bool(this_run.get("isArchived")))   # 封存的 app 連結打不開（小窗據此不給點）
                if state == "waiting":
                    item["waitSince"] = int(waiting_on(this_run, pending)[0])
            else:
                mine_skips = [s for s in skips.get(tid, []) if s.get("at", 0) >= slot]
                if mine_skips:
                    last = mine_skips[-1]
                    span = dur(last["at"] - slot)
                    if last.get("reason") == "per_task_limit" and active.get(tid):
                        b = active[tid][0]
                        w = waiting_on(b, pending)
                        why = f"等你按 {w[1]}" if w else "沒結束"
                        detail = f"上一輪（{mdhm(b['createdAt'])}）還卡著：{why}；這輪被擋 {span}"
                        item.update(sessionId=b["sessionId"], time=mdhm(b["createdAt"]))
                    elif last.get("reason") == "global_limit":
                        others = "、".join(active_names) or "？"
                        detail = f"同時已有 {len(active_names)} 支沒結束（{others}），這輪被擋 {span}"
                    else:
                        detail = f"被擋 {span}（{last.get('reason')}）"
                    item.update(state="blocked", detail=detail)
                elif now_ms - slot > GRACE_MIN * 60_000:
                    item.update(state="missed",
                                detail=f"該在 {mdhm(slot)} 跑，沒有啟動也沒有被擋的紀錄（當時 app 沒開或電腦睡著？）")
                else:
                    item.update(state="pending", detail=f"排定 {hm(slot)}，等 app 啟動")
            items.append(item)

    order = {"waiting": 0, "blocked": 1, "action": 2, "missed": 3, "unfinished": 4,
             "running": 5, "pending": 6, "done": 7}
    items.sort(key=lambda i: (order[i["state"]], -i["startedAt"]))
    return {"ok": True, "generatedAt": now_ms,
            "generatedLabel": f"{now:%H:%M}", "items": items}


# ---------- 自測 ----------

def selftest():
    """反向測：每一種狀態都要分得出來，讀不到要 fail。資料寫進暫存資料夾，走 build() 真路徑。"""
    fails, n = [], [0]

    def check(name, got, want):
        n[0] += 1
        if got != want:
            fails.append(f"{name}: got {got!r} want {want!r}")

    D = datetime
    mon = D(2026, 10, 19, 13, 13)                  # 週一
    check("daily", last_slot("0 7 * * *", mon), D(2026, 10, 19, 7, 0))
    check("daily before slot", last_slot("30 18 * * *", mon), D(2026, 10, 18, 18, 30))
    check("weekly mon", last_slot("0 8 * * 1", mon), D(2026, 10, 19, 8, 0))
    check("wed/thu", last_slot("0 8 * * 3,4", mon), D(2026, 10, 15, 8, 0))
    check("sunday as 7", last_slot("0 9 * * 7", mon), D(2026, 10, 18, 9, 0))
    check("dom range", last_slot("0 9 23-31 * *", mon), D(2026, 9, 30, 9, 0))
    check("step", last_slot("*/15 * * * *", mon), D(2026, 10, 19, 13, 0))
    check("dom or dow", last_slot("0 9 1 * 6", mon), D(2026, 10, 17, 9, 0))
    check("bad cron", last_slot("nope", mon), None)

    ms = lambda d: d.timestamp() * 1000
    now = ms(mon)
    stamp = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")
    tmp = tempfile.mkdtemp(prefix="rw_selftest_")
    try:
        folder = os.path.join(tmp, "sessions", "acct", "org")
        logs = os.path.join(tmp, "logs")
        os.makedirs(folder)
        os.makedirs(logs)
        tasks = [
            {"id": "stuck", "displayName": "【每天07:00】產日報", "cronExpression": "0 7 * * *", "enabled": True},
            {"id": "glob", "displayName": "評論提醒", "cronExpression": "0 9 * * *", "enabled": True},
            {"id": "wk", "displayName": "【每週一】週報", "cronExpression": "0 4 * * 1", "enabled": True},
            {"id": "ok", "displayName": "自動備份", "cronExpression": "30 18 * * *", "enabled": True},
            {"id": "act", "displayName": "要處理", "cronExpression": "0 10 * * *", "enabled": True},
            {"id": "nolog", "displayName": "沒啟動", "cronExpression": "0 6 * * *", "enabled": True},
            {"id": "soon", "displayName": "快到了", "cronExpression": "0 13 * * *", "enabled": True},
            {"id": "monthly", "displayName": "月底", "cronExpression": "0 9 23-31 * *", "enabled": True},
            {"id": "off", "displayName": "停用", "cronExpression": "0 7 * * *", "enabled": False},
        ]
        skips = {
            "stuck": [{"at": ms(D(2026, 10, 19, 7, 14)), "reason": "per_task_limit"},
                      {"at": ms(D(2026, 10, 19, 13, 12)), "reason": "per_task_limit"}],
            "glob": [{"at": ms(D(2026, 10, 19, 9, 9)), "reason": "global_limit"}],
            "ok": [{"at": ms(D(2026, 10, 17, 18, 31)), "reason": "global_limit"}],   # 舊的不算
        }
        with open(os.path.join(folder, "scheduled-tasks.json"), "w", encoding="utf-8") as f:
            json.dump({"scheduledTasks": tasks, "recordedSkips": skips}, f, ensure_ascii=False)
        runs = [
            # 昨天那輪卡在權限（請求在輪替前的 main1.log）
            {"sessionId": "local_5a1", "scheduledTaskId": "stuck", "createdAt": ms(D(2026, 10, 18, 7, 14)),
             "lastActivityAt": ms(D(2026, 10, 18, 7, 15)), "isArchived": False},
            # 一啟動就卡、摘要欄位都是空的（舊版把 None==None 當成「有摘要」）
            {"sessionId": "local_7b1", "scheduledTaskId": "wk", "createdAt": ms(D(2026, 10, 19, 4, 3)),
             "lastActivityAt": ms(D(2026, 10, 19, 4, 3)), "isArchived": False},
            {"sessionId": "local_0c1", "scheduledTaskId": "ok", "createdAt": ms(D(2026, 10, 18, 18, 31)),
             "lastActivityAt": ms(D(2026, 10, 18, 18, 32)), "lastAssistantUuid": "u", "postTurnSummaryFor": "u",
             "postTurnSummary": {"status_category": "completed", "status_detail": "沒有新資料"}, "isArchived": True},
            {"sessionId": "local_ad1", "scheduledTaskId": "act", "createdAt": ms(D(2026, 10, 19, 11, 0)),
             "lastActivityAt": ms(D(2026, 10, 19, 11, 5)), "lastAssistantUuid": "u", "postTurnSummaryFor": "u",
             "postTurnSummary": {"status_category": "blocked", "needs_action": "確認客戶回覆"}},
        ]
        for r in runs:
            with open(os.path.join(folder, f"{r['sessionId']}.json"), "w", encoding="utf-8") as f:
                json.dump(r, f)
        with open(os.path.join(logs, "main1.log"), "w", encoding="utf-8") as f:
            f.write(f"{stamp(D(2026, 10, 18, 7, 15))} [info] Emitted tool permission request r1 for Bash in session local_5a1\n"
                    f"{stamp(D(2026, 10, 19, 4, 3))} [info] Emitted tool permission request r2 for Glob in session local_7b1\n"
                    f"{stamp(D(2026, 10, 19, 4, 4))} [info] Emitted tool permission request r3 for Read in session local_0c1\n")
        with open(os.path.join(logs, "main.log"), "w", encoding="utf-8") as f:
            f.write(f"{stamp(D(2026, 10, 19, 4, 5))} [info] Received permission response for r3: once (tool: Read)\n")

        out = build(os.path.join(tmp, "sessions"), logs, now)
        got = {i["taskId"]: i for i in out.get("items", [])}
        st = lambda k: got.get(k, {}).get("state")
        check("ok", out.get("ok"), True)
        check("stuck → blocked", st("stuck"), "blocked")
        check("stuck names blocker", "10/18 07:14" in got.get("stuck", {}).get("detail", "") and "Bash" in got["stuck"]["detail"], True)
        check("global → blocked", st("glob"), "blocked")
        check("global lists actives", "產日報" in got.get("glob", {}).get("detail", "") and "週報" in got["glob"]["detail"], True)
        check("rotated-log wait found", st("wk"), "waiting")
        check("done", st("ok"), "done")
        check("old skip ignored", got.get("ok", {}).get("detail"), "沒有新資料")
        check("action", got.get("act", {}).get("detail"), "晚了 1 小時才啟動；確認客戶回覆")
        check("missed", st("nolog"), "missed")
        check("pending", st("soon"), "pending")
        check("monthly hidden", "monthly" in got, False)
        check("disabled hidden", "off" in got, False)
        check("waiting first", out["items"][0]["taskId"], "wk")
        # 小窗點一列要切到的 session：被擋的指向卡住它的上一輪；封存的要標出來
        check("blocked links blocker", got.get("stuck", {}).get("sessionId"), "local_5a1")
        check("run links itself", (got.get("act", {}).get("sessionId"), got.get("act", {}).get("archived")), ("local_ad1", False))
        check("archived flagged", got.get("ok", {}).get("archived"), True)

        os.remove(os.path.join(logs, "main.log"))
        try:
            build(os.path.join(tmp, "sessions"), logs, now)
            check("missing main.log raises", False, True)
        except FileNotFoundError:
            check("missing main.log raises", True, True)
        check("missing base fails", build(os.path.join(tmp, "nope"), logs, now)["ok"], False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("FAIL", f)
    print(f"selftest: {n[0] - len(fails)}/{n[0]}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    base = os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code-sessions")
    log_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Claude", "Logs")
    try:
        out = build(base, log_dir, time.time() * 1000)
    except Exception as e:  # 讀檔失敗也要顯示原因，不是安靜空白
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(0 if out["ok"] else 1)
