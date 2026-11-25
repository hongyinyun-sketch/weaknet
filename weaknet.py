# -*- coding: utf-8 -*-
"""弱網限速器 — WeakNet
透過 WinDivert 在網路層攔截封包，以令牌桶排隊後重新注入，達到平滑限速。
依賴：pip install pydivert keyboard pystray pillow
需要系統管理員權限（程式會自動請求提權）。
"""
import sys, os, json, time, ctypes, threading, collections, datetime, queue
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

SCRIPT = os.path.abspath(sys.argv[0])


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


if os.name == "nt" and not is_admin():
    args = [SCRIPT] + sys.argv[1:] if not getattr(sys, "frozen", False) else sys.argv[1:]
    params = " ".join(f'"{a}"' for a in args)
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    sys.exit()

try:
    import pydivert
except ImportError:
    pydivert = None
try:
    import keyboard
except ImportError:
    keyboard = None
try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None

CFG = os.path.join(os.path.dirname(SCRIPT), "weaknet.json")
UNITS = {"KB/s": 1024, "MB/s": 1024 * 1024, "Kbps": 125, "Mbps": 125000}  # -> bytes/sec
DIRS = ["下載", "上傳", "雙向"]
DEFAULT_CFG = {
    "presets": [{"val": "100", "unit": "KB/s", "dir": "雙向"},
                {"val": "1", "unit": "Kbps", "dir": "下載"}],
    "hotkeys": {"toggle": "", "restore": "", "p1": "alt+e", "p2": ""},
    "rules": [],
}


# ───────────────────────── 限速引擎 ─────────────────────────
class Shaper:
    def __init__(self):
        self.rate = {"up": 0, "down": 0}
        self.q = {k: collections.deque() for k in ("up", "down")}
        self.qb = {"up": 0, "down": 0}
        self.tok = {"up": 0.0, "down": 0.0}
        self.dropped = 0
        self.lock = threading.Lock()
        self.w = None
        self.running = False

    def set_rates(self, up, down):
        with self.lock:
            self.rate["up"], self.rate["down"] = up, down
        if up > 0 or down > 0:
            if not self.running:
                self._start()
        else:
            self._stop()

    def _start(self):
        if pydivert is None:
            raise RuntimeError("未安裝 pydivert（pip install pydivert）")
        self.w = pydivert.WinDivert("!loopback")
        self.w.open()
        self.running = True
        self.dropped = 0
        threading.Thread(target=self._rx, args=(self.w,), daemon=True).start()
        threading.Thread(target=self._tx, args=(self.w,), daemon=True).start()

    def _stop(self):
        if not self.running:
            return
        self.running = False
        try:
            self.w.close()
        except Exception:
            pass
        with self.lock:
            for d in ("up", "down"):
                self.q[d].clear()
                self.qb[d] = 0

    def _rx(self, w):
        while self.running:
            try:
                p = w.recv()
            except Exception:
                break
            d = "up" if p.is_outbound else "down"
            n = len(p.raw)
            with self.lock:
                r = self.rate[d]
                if r > 0:
                    # 佇列上限約 0.5 秒資料量，超出丟包（讓 TCP 自行降速）
                    if self.qb[d] + n > max(r * 0.5, 30000):
                        self.dropped += 1
                    else:
                        self.q[d].append(p)
                        self.qb[d] += n
                    continue
            try:
                w.send(p)
            except Exception:
                pass

    def _tx(self, w):
        last = time.perf_counter()
        while self.running:
            time.sleep(0.002)
            now = time.perf_counter()
            dt, last = now - last, now
            out = []
            with self.lock:
                for d in ("up", "down"):
                    r, q = self.rate[d], self.q[d]
                    if r <= 0:  # 該方向不限速：清空佇列直接放行
                        out.extend(q)
                        q.clear()
                        self.qb[d] = 0
                        self.tok[d] = 0.0
                        continue
                    self.tok[d] = min(self.tok[d] + dt * r, max(r * 0.1, 3000))
                    while q and self.tok[d] > 0:
                        p = q.popleft()
                        n = len(p.raw)
                        self.qb[d] -= n
                        self.tok[d] -= n
                        out.append(p)
            for p in out:
                try:
                    w.send(p)
                except Exception:
                    pass


# ───────────────────────── 主程式 ─────────────────────────
class App:
    def __init__(self, root):
        self.root = root
        self.sh = Shaper()
        self.cmdq = queue.Queue()
        self.timer = None
        self.desc = ""
        self.sched = None
        self.cfg = self.load()
        self.rules = self.cfg["rules"]

        root.title("弱網限速器 — WeakNet")
        root.geometry("760x560")
        root.protocol("WM_DELETE_WINDOW", self.hide)
        self.build()
        self.setup_tray()
        self.bind_hotkeys()
        self.refresh()
        self.poll()
        self.tick()
        if pydivert is None:
            self.log("⚠ 未安裝 pydivert，無法限速：pip install pydivert")

    # ── 設定 ──
    def load(self):
        try:
            with open(CFG, encoding="utf-8") as f:
                c = json.load(f)
            for k, v in DEFAULT_CFG.items():
                c.setdefault(k, v)
            return c
        except Exception:
            return json.loads(json.dumps(DEFAULT_CFG))

    def save(self):
        self.cfg["presets"] = [{"val": v.get(), "unit": u.get(), "dir": d.get()}
                               for v, u, d in self.pvars]
        self.cfg["hotkeys"] = {k: v.get().strip() for k, v in self.hkvars.items()}
        self.cfg["rules"] = self.rules
        try:
            with open(CFG, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"儲存設定失敗：{e}")

    # ── 介面 ──
    def build(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=12, pady=(10, 0))
        left = ttk.Frame(top)
        left.pack(side="left")
        self.lbl_state = tk.Label(left, text="", font=("Microsoft JhengHei UI", 16, "bold"))
        self.lbl_state.pack(anchor="w")
        self.lbl_sub = tk.Label(left, text="", fg="#777")
        self.lbl_sub.pack(anchor="w")
        self.btn_one = ttk.Button(top, text="一鍵限速", width=16, command=self.toggle)
        self.btn_one.pack(side="right", ipady=10)

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        t1, t2, t3, t4 = (ttk.Frame(nb) for _ in range(4))
        for t, n in ((t1, "手動限速"), (t2, "定時規則"), (t3, "快捷鍵"), (t4, "日誌")):
            nb.add(t, text=n)
        self.build_manual(t1)
        self.build_rules(t2)
        self.build_hotkeys(t3)
        self.logbox = ScrolledText(t4, state="disabled", height=10)
        self.logbox.pack(fill="both", expand=True, padx=6, pady=6)
        tk.Label(self.root, fg="#888", anchor="w",
                 text="提示：點擊視窗關閉按鈕會最小化到托盤；退出請用托盤右鍵選單。").pack(fill="x", padx=10, pady=(0, 6))

    def build_manual(self, p):
        g = ttk.LabelFrame(p, text="限速參數")
        g.pack(fill="x", padx=8, pady=8)
        for i, t in enumerate(("方向", "數值", "單位", "持續(秒)")):
            ttk.Label(g, text=t).grid(row=0, column=i, padx=6, sticky="w")
        self.m_dir = ttk.Combobox(g, values=DIRS, state="readonly", width=8)
        self.m_dir.set("下載")
        self.m_val = ttk.Entry(g, width=12)
        self.m_val.insert(0, "100")
        self.m_unit = ttk.Combobox(g, values=list(UNITS), state="readonly", width=8)
        self.m_unit.set("KB/s")
        self.m_dur = ttk.Entry(g, width=10)
        self.m_dur.insert(0, "0")
        for i, w in enumerate((self.m_dir, self.m_val, self.m_unit, self.m_dur)):
            w.grid(row=1, column=i, padx=6, pady=(0, 8), sticky="w")
        ttk.Button(g, text="立即限速", command=self.manual_apply).grid(row=1, column=4, padx=6)
        ttk.Button(g, text="恢復不限速", command=lambda: self.restore("手動")).grid(row=1, column=5, padx=6)
        ttk.Label(g, text="0 = 一直保持").grid(row=1, column=6, padx=6)

        pf = ttk.LabelFrame(p, text="預設(可綁定全域快捷鍵，一鍵套用)")
        pf.pack(fill="x", padx=8, pady=8)
        self.pvars, self.hklabels = [], []
        for i, pr in enumerate(self.cfg["presets"][:2]):
            v, u, d = tk.StringVar(value=pr["val"]), tk.StringVar(value=pr["unit"]), tk.StringVar(value=pr["dir"])
            self.pvars.append((v, u, d))
            ttk.Label(pf, text=f"預設 {i+1}").grid(row=i, column=0, padx=8, pady=8)
            ttk.Entry(pf, textvariable=v, width=12).grid(row=i, column=1, padx=6)
            ttk.Combobox(pf, textvariable=u, values=list(UNITS), state="readonly", width=8).grid(row=i, column=2, padx=6)
            ttk.Combobox(pf, textvariable=d, values=DIRS, state="readonly", width=8).grid(row=i, column=3, padx=6)
            ttk.Button(pf, text=f"套用預設 {i+1}", command=lambda k=i: self.apply_preset(k)).grid(row=i, column=4, padx=6)
            lb = tk.StringVar()
            self.hklabels.append(lb)
            ttk.Label(pf, textvariable=lb, foreground="#777").grid(row=i, column=5, padx=6)
        tk.Label(p, justify="left", fg="#333", anchor="w",
                 text="工作原理：透過 WinDivert 在網路層攔截封包，按設定速率排隊後重新注入，達到平滑限速。\n"
                      "上傳/下載分別整形；數值越大越寬鬆，設為 0 或點「恢復不限速」即取消所有限制。"
                 ).pack(fill="x", padx=12, pady=6)

    def build_rules(self, p):
        f = ttk.Frame(p)
        f.pack(fill="x", padx=8, pady=8)
        self.r_s, self.r_e = ttk.Entry(f, width=7), ttk.Entry(f, width=7)
        self.r_s.insert(0, "09:00")
        self.r_e.insert(0, "18:00")
        self.r_d = ttk.Combobox(f, values=DIRS, state="readonly", width=6)
        self.r_d.set("雙向")
        self.r_v = ttk.Entry(f, width=8)
        self.r_v.insert(0, "100")
        self.r_u = ttk.Combobox(f, values=list(UNITS), state="readonly", width=6)
        self.r_u.set("KB/s")
        for t, w in (("開始", self.r_s), ("結束", self.r_e), ("方向", self.r_d), ("速率", self.r_v), ("", self.r_u)):
            if t:
                ttk.Label(f, text=t).pack(side="left", padx=(6, 2))
            w.pack(side="left")
        ttk.Button(f, text="新增規則", command=self.add_rule).pack(side="left", padx=8)
        ttk.Button(f, text="刪除選取", command=self.del_rule).pack(side="left")
        self.tree = ttk.Treeview(p, columns=("s", "e", "d", "r"), show="headings", height=8)
        for c, t in (("s", "開始"), ("e", "結束"), ("d", "方向"), ("r", "速率")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=120, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)
        ttk.Label(p, text="時間格式 HH:MM（24 小時制），可跨午夜；程式需保持運行。").pack(anchor="w", padx=10, pady=4)
        self.fill_rules()

    def build_hotkeys(self, p):
        names = (("toggle", "一鍵限速 / 恢復（切換）"), ("restore", "恢復不限速"),
                 ("p1", "套用預設 1"), ("p2", "套用預設 2"))
        self.hkvars = {}
        for i, (k, t) in enumerate(names):
            ttk.Label(p, text=t).grid(row=i, column=0, padx=12, pady=10, sticky="w")
            v = tk.StringVar(value=self.cfg["hotkeys"].get(k, ""))
            self.hkvars[k] = v
            ttk.Entry(p, textvariable=v, width=20).grid(row=i, column=1, padx=6)
        ttk.Button(p, text="儲存並套用", command=self.bind_hotkeys).grid(row=len(names), column=1, pady=10, sticky="w")
        ttk.Label(p, foreground="#777", text="格式例：alt+e、ctrl+shift+f1；留空 = 不綁定").grid(
            row=len(names) + 1, column=0, columnspan=3, padx=12, sticky="w")
        if keyboard is None:
            ttk.Label(p, foreground="red", text="未安裝 keyboard 套件，快捷鍵不可用（pip install keyboard）").grid(
                row=len(names) + 2, column=0, columnspan=3, padx=12, pady=6, sticky="w")

    # ── 規則 ──
    def fill_rules(self):
        self.tree.delete(*self.tree.get_children())
        for r in self.rules:
            self.tree.insert("", "end", values=(r["s"], r["e"], r["dir"], f'{r["val"]} {r["unit"]}'))

    def add_rule(self):
        s, e = self.r_s.get().strip(), self.r_e.get().strip()
        try:
            datetime.datetime.strptime(s, "%H:%M")
            datetime.datetime.strptime(e, "%H:%M")
            float(self.r_v.get())
        except ValueError:
            messagebox.showerror("錯誤", "時間或速率格式不正確")
            return
        self.rules.append({"s": s.zfill(5), "e": e.zfill(5), "dir": self.r_d.get(),
                           "val": self.r_v.get(), "unit": self.r_u.get()})
        self.fill_rules()
        self.save()

    def del_rule(self):
        for it in sorted((self.tree.index(i) for i in self.tree.selection()), reverse=True):
            del self.rules[it]
        self.fill_rules()
        self.save()

    def tick(self):
        now = datetime.datetime.now().strftime("%H:%M")
        act = None
        for i, r in enumerate(self.rules):
            s, e = r["s"], r["e"]
            if (s <= now < e) if s <= e else (now >= s or now < e):
                act = i
                break
        if act != self.sched:
            if act is not None:
                r = self.rules[act]
                self.apply(r["dir"], r["val"], r["unit"], 0, "定時規則")
            elif self.sched is not None:
                self.restore("定時規則結束")
            self.sched = act
        self.root.after(1000, self.tick)

    # ── 限速動作 ──
    def apply(self, d, val, unit, dur=0, src="手動"):
        try:
            v = float(val)
        except ValueError:
            messagebox.showerror("錯誤", "數值格式不正確")
            return
        if v <= 0:
            return self.restore(src)
        bps = v * UNITS[unit]
        up = bps if d in ("上傳", "雙向") else 0
        down = bps if d in ("下載", "雙向") else 0
        try:
            self.sh.set_rates(up, down)
        except Exception as e:
            self.log(f"啟動失敗：{e}")
            messagebox.showerror("啟動失敗", str(e))
            return
        if self.timer:
            self.root.after_cancel(self.timer)
            self.timer = None
        try:
            dur = int(float(dur))
        except ValueError:
            dur = 0
        if dur > 0:
            self.timer = self.root.after(dur * 1000, lambda: self.restore("時間到"))
        self.desc = f"{d} {val} {unit}"
        self.log(f"[{src}] 限速：{self.desc}" + (f"，持續 {dur} 秒" if dur > 0 else ""))
        self.refresh()

    def restore(self, src="手動"):
        if self.timer:
            self.root.after_cancel(self.timer)
            self.timer = None
        was = self.sh.running
        self.sh.set_rates(0, 0)
        self.desc = ""
        if was:
            self.log(f"[{src}] 已恢復不限速（期間丟包 {self.sh.dropped}）")
        self.refresh()

    def manual_apply(self):
        self.apply(self.m_dir.get(), self.m_val.get(), self.m_unit.get(), self.m_dur.get(), "手動")

    def apply_preset(self, i):
        v, u, d = self.pvars[i]
        self.apply(d.get(), v.get(), u.get(), 0, f"預設{i+1}")

    def toggle(self):
        if self.sh.running:
            self.restore("一鍵")
        else:
            self.apply_preset(0)

    def refresh(self):
        if self.sh.running:
            self.lbl_state.config(text=f"當前:限速中({self.desc})", fg="#d35400")
            self.lbl_sub.config(text="WinDivert 運行中 · 已接管流量")
            self.btn_one.config(text="一鍵恢復")
        else:
            self.lbl_state.config(text="當前:不限速(全速)", fg="#1e8e1e")
            self.lbl_sub.config(text="就緒 · 未接管流量")
            self.btn_one.config(text="一鍵限速")
        if pystray and getattr(self, "icon", None):
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def log(self, msg):
        line = f"{datetime.datetime.now():%H:%M:%S}  {msg}\n"
        self.logbox.config(state="normal")
        self.logbox.insert("end", line)
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    # ── 快捷鍵 ──
    def bind_hotkeys(self):
        self.save()
        hk = self.cfg["hotkeys"]
        for i in range(2):
            self.hklabels[i].set(f"快捷鍵:{hk.get('p%d' % (i+1)) or '未綁定'}")
        if keyboard is None:
            return
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        acts = {"toggle": "toggle", "restore": "restore", "p1": "p1", "p2": "p2"}
        for k, a in acts.items():
            key = hk.get(k, "")
            if key:
                try:
                    keyboard.add_hotkey(key, lambda a=a: self.cmdq.put(a))
                except Exception as e:
                    self.log(f"快捷鍵 {key} 綁定失敗：{e}")

    def poll(self):
        try:
            while True:
                a = self.cmdq.get_nowait()
                {"toggle": self.toggle, "restore": lambda: self.restore("快捷鍵"),
                 "p1": lambda: self.apply_preset(0), "p2": lambda: self.apply_preset(1),
                 "show": self.show, "quit": self.quit}[a]()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    # ── 托盤 ──
    def setup_tray(self):
        self.icon = None
        if not pystray:
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        dr.ellipse((4, 4, 60, 60), fill=(30, 100, 200, 255))
        dr.polygon([(20, 40), (32, 18), (44, 40)], fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("顯示視窗", lambda: self.cmdq.put("show"), default=True),
            pystray.MenuItem(lambda _: "一鍵恢復" if self.sh.running else "一鍵限速", lambda: self.cmdq.put("toggle")),
            pystray.MenuItem("退出", lambda: self.cmdq.put("quit")))
        self.icon = pystray.Icon("weaknet", img, "弱網限速器", menu)
        self.icon.run_detached()

    def hide(self):
        if self.icon:
            self.root.withdraw()
        else:
            self.root.iconify()

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def quit(self):
        self.save()
        self.sh.set_rates(0, 0)
        if keyboard:
            try:
                keyboard.unhook_all()
            except Exception:
                pass
        if self.icon:
            self.icon.stop()
        self.root.destroy()


if __name__ == "__main__":
    r = tk.Tk()
    App(r)
    r.mainloop()
