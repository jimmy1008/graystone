# -*- coding: utf-8 -*-
"""灰石村世界觀察窗
左：整體數據｜中：村莊地圖（人物動畫）｜右：事件/對話流（時間戳，可點擊 Replay）

速度檔位：🐢單步（一天一停）/ 🔍除錯 / 👀正常 / ⚡快速 / 🚀批次（無動畫連跑）
點地圖上的人物 → 檢視面板；點右欄事件 → Replay（當時情境與 LLM 輸出）
嚴重事件（死亡/受傷/塌方/疫病/破產/LLM退回）可自動暫停。

用法：
  .venv\\Scripts\\pythonw.exe map_view.py [--llm on] [--seed N]
除錯：崩潰訊息寫入 out\\viewer_error.log
"""
import argparse
import ctypes
import json
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont

from sim.engine import World

ROOT_DIR = Path(__file__).resolve().parent
ERR_LOG = ROOT_DIR / "out" / "viewer_error.log"

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass
try:
    SCALE = ctypes.windll.user32.GetDpiForSystem() / 96.0
except Exception:
    SCALE = 1.0


def S(v):
    return round(v * SCALE)


_POS = {
    "square":      (505, 330), "granary_loc": (600, 262), "shrine": (420, 258),
    "smithy_loc":  (652, 372), "tavern_loc":  (415, 415), "store_loc": (598, 452),
    "house_01": (560, 232),  "house_02": (718, 340),  "house_03": (368, 452),
    "house_04": (650, 505),  "house_05": (268, 360),  "house_06": (250, 428),
    "house_07": (258, 500),  "house_08": (340, 545),  "house_09": (180, 168),
    "house_10": (782, 172),  "house_11": (760, 560),  "house_12": (300, 240),
    "fields_a": (135, 395),  "fields_b": (140, 528),  "fields_c": (455, 618),
    "forest":   (250, 90),   "logging":  (430, 75),   "mine": (860, 105),
    "road":     (880, 640),
}
LOC_POS = {k: (S(x), S(y)) for k, (x, y) in _POS.items()}
LOC_LABEL = {
    "square": "水井廣場", "granary_loc": "公共糧倉", "shrine": "神龕", "smithy_loc": "鐵匠鋪",
    "tavern_loc": "酒館", "store_loc": "雜貨店", "fields_a": "農田・溪東", "fields_b": "農田・溪西",
    "fields_c": "農田・谷口", "forest": "獵場森林", "logging": "伐木林", "mine": "鐵礦坑", "road": "對外道路",
}
JOB_COLOR = {
    "農": "#8bc34a", "獵人": "#a1887f", "礦工": "#b0bec5", "鐵匠": "#ff8a35", "鍛造學徒": "#ffb74d",
    "酒館老闆": "#ba68c8", "酒館": "#ba68c8", "酒館幫工": "#ce93d8", "商人": "#ffd54f",
    "守衛隊長": "#4fc3f7", "守衛": "#4fc3f7", "草藥師": "#4db6ac", "村長": "#f5f5f5", "無業": "#9e9e9e",
}
WEATHER_ICON = {"晴": "☀", "陰": "☁", "雨": "🌧", "雪": "❄", "暴風": "🌪"}
BG, PANEL, FG, DIM, ACCENT = "#10161f", "#0b1017", "#dde3ea", "#5d6b7a", "#8ecae6"

MODES = {   # 名稱: (動畫倍率, 是否動畫, 一天後自動停)
    "🐢 單步": (1.0, True, True),
    "🔍 除錯": (0.35, True, False),
    "👀 正常": (1.0, True, False),
    "⚡ 快速": (3.0, True, False),
    "🚀 批次": (1.0, False, False),
}
PHASES = [  # 名稱, 目標快照, 錯落秒, 停留秒, 時鐘範圍
    ("🌅 清晨 · 出門上工",            "work",    3.0, 1.5, (6, 9)),
    ("☀ 白天 · 各就其位",             "work",    0.2, 3.5, (9, 17)),
    ("🌇 傍晚 · 收工（回家或進酒館）", "evening", 5.0, 2.0, (17, 21)),
    ("🌙 夜間 · 就寢",                "home",    3.0, 1.5, (21, 24)),
]
SEVERE = {"death", "injury", "mine_collapse", "plague", "treasury_broke", "seed_shortage", "rejected", "rare_find"}


def jitter(nid, loc):
    h = hash((nid, loc))
    return S(((h % 29) - 14) * 1.2), S((((h // 29) % 23) - 11) * 1.2)


class MapApp:
    def __init__(self, root, world):
        self.root, self.w = root, world
        root.title("灰石村 Graystone — 世界觀察窗")
        root.configure(bg=BG)
        ff = "Microsoft JhengHei UI"
        self.f_ui = tkfont.Font(family=ff, size=11)
        self.f_small = tkfont.Font(family=ff, size=10)
        self.f_big = tkfont.Font(family=ff, size=14, weight="bold")
        self.f_map = tkfont.Font(family=ff, size=10)
        self.f_map_s = tkfont.Font(family=ff, size=9)
        self.f_mono = tkfont.Font(family="Consolas", size=10)

        self.events_by_id = {}
        self.last_decision = {}     # npc_id -> event
        self.mode = "👀 正常"
        self.paused = False
        self.day_running = False

        left = tk.Frame(root, bg=PANEL, width=S(258))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        mid = tk.Frame(root, bg=BG)
        mid.pack(side="left", fill="both", expand=True)
        right = tk.Frame(root, bg=PANEL, width=S(350))
        right.pack(side="left", fill="y")
        right.pack_propagate(False)

        # ── 左：整體數據 ──
        tk.Label(left, text="📊 整體數據", bg=PANEL, fg=ACCENT, font=self.f_big).pack(anchor="w", padx=S(14), pady=(S(12), S(4)))
        self.stats = tk.Text(left, bg=PANEL, fg=FG, font=self.f_small, bd=0, wrap="word",
                             state="disabled", spacing1=S(2), spacing3=S(2))
        self.stats.pack(fill="both", expand=True, padx=S(14), pady=S(4))
        for tag, color in (("h", ACCENT), ("dim", DIM), ("warn", "#ffb4a2")):
            self.stats.tag_configure(tag, foreground=color)
        for hid in world.hh:   # 左欄戶列可點擊 → 戶檢視窗
            t = f"hh_{hid}"
            self.stats.tag_bind(t, "<Button-1>", lambda e, h=hid: self.inspect_household(h))
            self.stats.tag_bind(t, "<Enter>", lambda e: self.stats.configure(cursor="hand2"))
            self.stats.tag_bind(t, "<Leave>", lambda e: self.stats.configure(cursor="arrow"))

        # ── 中：標題 + 地圖 + 控制 ──
        top = tk.Frame(mid, bg=BG)
        top.pack(fill="x")
        self.title_lbl = tk.Label(top, text="", bg=BG, fg="#ffd166", font=self.f_big)
        self.title_lbl.pack(side="left", padx=S(14), pady=S(8))
        self.clock_lbl = tk.Label(top, text="", bg=BG, fg=FG, font=self.f_big)
        self.clock_lbl.pack(side="right", padx=S(14))
        self.phase_lbl = tk.Label(top, text="", bg=BG, fg=ACCENT, font=self.f_ui)
        self.phase_lbl.pack(side="right", padx=S(10))
        self.cv = tk.Canvas(mid, width=S(960), height=S(680), bg=BG, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)

        ctrl = tk.Frame(mid, bg=BG)
        ctrl.pack(fill="x", pady=(S(2), S(8)))
        self.btn = tk.Button(ctrl, text="⏸ 暫停", command=self.toggle, font=self.f_ui,
                             bg="#2b3a4d", fg=FG, bd=0, padx=S(16), pady=S(4),
                             activebackground="#3c5069", activeforeground=FG)
        self.btn.pack(side="left", padx=S(14))
        self.mode_btns = {}
        for name in MODES:
            b = tk.Button(ctrl, text=name, command=lambda m=name: self.set_mode(m), font=self.f_small,
                          bg=PANEL, fg=FG, bd=0, padx=S(10), pady=S(4),
                          activebackground="#3c5069", activeforeground=FG)
            b.pack(side="left", padx=S(2))
            self.mode_btns[name] = b
        self.auto_pause = tk.BooleanVar(value=True)
        tk.Checkbutton(ctrl, text="事件自動暫停", variable=self.auto_pause, bg=BG, fg=FG,
                       selectcolor=PANEL, font=self.f_small, activebackground=BG, activeforeground=FG).pack(side="left", padx=S(10))
        self.show_names = tk.BooleanVar(value=False)
        tk.Checkbutton(ctrl, text="顯示名字", variable=self.show_names, bg=BG, fg=FG,
                       selectcolor=PANEL, font=self.f_small, activebackground=BG, activeforeground=FG,
                       command=self.refresh_names).pack(side="left", padx=S(6))
        mode_txt = "🧠 LLM 模式" if world.llm else "⚙ 純規則模式"
        tk.Label(ctrl, text=mode_txt, bg=BG, fg="#95d5b2", font=self.f_small).pack(side="right", padx=S(14))
        self._highlight_mode()

        # ── 右：事件/對話流 ──
        hdr = tk.Frame(right, bg=PANEL)
        hdr.pack(fill="x")
        tk.Label(hdr, text="💬 村中動靜", bg=PANEL, fg=ACCENT, font=self.f_big).pack(side="left", padx=S(14), pady=(S(12), S(4)))
        tk.Label(hdr, text="（點事件看當時情境）", bg=PANEL, fg=DIM, font=self.f_map_s).pack(side="left", pady=(S(14), 0))
        self.feed = tk.Text(right, bg=PANEL, fg=FG, font=self.f_small, bd=0, wrap="word",
                            state="disabled", spacing1=S(3), spacing3=S(3), cursor="arrow")
        sb = tk.Scrollbar(right, command=self.feed.yview)
        self.feed.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.feed.pack(fill="both", expand=True, padx=(S(14), 0), pady=S(4))
        for tag, color in (("talk", "#e8e2d0"), ("event", "#ffb4a2"), ("decision", ACCENT),
                           ("death", "#ef476f"), ("day", "#ffd166"), ("sys", DIM), ("rej", "#f4a261")):
            self.feed.tag_configure(tag, foreground=color)

        self._draw_map()
        self._make_dots()
        self.tick_q = queue.Queue()
        self.phase_queue = []
        self.phase_start = self.phase_end = 0
        self.clock_range = (6, 9)
        # 虛擬時鐘：暫停時凍結，動畫/相位/時鐘全部以它為準 → 按暫停立即靜止
        self._vt = 0.0
        self._last_real = time.time()
        self.root.after(300, self._next_day)
        self.root.after(30, self._animate)

    # ══ 控制 ══
    def toggle(self):
        self.paused = not self.paused
        self.btn.configure(text="▶ 繼續" if self.paused else "⏸ 暫停")

    def set_mode(self, m):
        self.mode = m
        self._highlight_mode()
        if self.paused and m != "🐢 單步":
            self.toggle()

    def _highlight_mode(self):
        for name, b in self.mode_btns.items():
            b.configure(bg="#3c5069" if name == self.mode else PANEL,
                        fg="#ffd166" if name == self.mode else FG)

    def refresh_names(self):
        state = "normal" if self.show_names.get() else "hidden"
        for d in self.dots.values():
            self.cv.itemconfigure(d["label"], state=state)

    # ══ 地圖 ══
    def _draw_map(self):
        cv = self.cv
        pts = [(60, 30), (470, 20), (520, 130), (340, 165), (100, 160)]
        cv.create_polygon(*[c for p in pts for c in (S(p[0]), S(p[1]))], fill="#1c3226", outline="#2c4a37", width=2)
        cv.create_text(S(250), S(60), text="🌲 獵場森林", fill="#6fae7f", font=self.f_map)
        cv.create_text(S(430), S(100), text="🪓 伐木林", fill="#6fae7f", font=self.f_map_s)
        cv.create_oval(S(818), S(68), S(908), S(142), fill="#39404a", outline="#59636f", width=2)
        cv.create_text(S(860), S(150), text="⛏ 鐵礦坑", fill="#a6b1bd", font=self.f_map_s)
        for fid in ("fields_a", "fields_b", "fields_c"):
            x, y = LOC_POS[fid]
            cv.create_rectangle(x - S(58), y - S(34), x + S(58), y + S(34), fill="#27391d", outline="#55763b", width=2)
            cv.create_text(x, y - S(46), text="🌾 " + LOC_LABEL[fid], fill="#94b56f", font=self.f_map_s)
        cv.create_line(S(505), S(355), S(880), S(640), fill="#57503f", width=S(8), capstyle="round")
        cv.create_text(S(872), S(662), text="🛤 對外道路 → 磐石鎮", fill="#ad9877", font=self.f_map_s)
        pub = {"square": ("⛲", "#33566f"), "granary_loc": ("🏛", "#6a5a3c"), "shrine": ("⛩", "#584a68"),
               "smithy_loc": ("🔥", "#7c4a28"), "tavern_loc": ("🍺", "#5e3a6d"), "store_loc": ("🏪", "#6d6030")}
        for lid, (icon, color) in pub.items():
            x, y = LOC_POS[lid]
            cv.create_rectangle(x - S(38), y - S(21), x + S(38), y + S(21), fill=color, outline="#0a0d12", width=2)
            cv.create_text(x, y, text=f"{icon} {LOC_LABEL[lid]}", fill="#f0ede6", font=self.f_map)
        for i in range(1, 13):
            hid = f"house_{i:02d}"
            x, y = LOC_POS[hid]
            rect = cv.create_rectangle(x - S(17), y - S(13), x + S(17), y + S(13), fill="#3b3327", outline="#6b5c46", width=1)
            txt = cv.create_text(x, y, text=f"戶{i}", fill="#c3b295", font=self.f_map_s)
            hh_id = f"h{i:02d}"
            for item in (rect, txt):
                cv.tag_bind(item, "<Button-1>", lambda e, h=hh_id: self.inspect_household(h))

    def _make_dots(self):
        self.dots = {}
        r = S(6)
        for n in self.w.alive_npcs():
            x, y = LOC_POS[n["home"]]
            jx, jy = jitter(n["id"], n["home"])
            color = JOB_COLOR.get(n["job"], "#9e9e9e")
            d = self.cv.create_oval(x + jx - r, y + jy - r, x + jx + r, y + jy + r,
                                    fill=color, outline="#05070a", width=2)
            t = self.cv.create_text(x + jx, y + jy - S(14), text=n["name"], fill="#cfd8dc",
                                    font=self.f_map_s, state="hidden")
            self.cv.tag_bind(d, "<Button-1>", lambda e, nid=n["id"]: self.inspect_npc(nid))
            self.dots[n["id"]] = {"item": d, "label": t, "x": x + jx, "y": y + jy,
                                  "tx": x + jx, "ty": y + jy, "delay": 0.0}

    # ══ 一天流程 ══
    def _next_day(self):
        if self.paused:
            self.root.after(200, self._next_day)
            return
        self.day_running = True
        self.phase_lbl.configure(text="⋯ 世界推演中" + ("（LLM 思考較慢）" if self.w.llm else ""))
        threading.Thread(target=lambda: self.tick_q.put(self.w.tick()), daemon=True).start()
        self.root.after(30, self._await_tick)

    def _await_tick(self):
        try:
            notable = self.tick_q.get_nowait()
        except queue.Empty:
            self.root.after(100, self._await_tick)
            return
        w = self.w
        self.title_lbl.configure(text=f"第 {w.year} 年 · {w.season}季 第 {(w.doy - 1) % 90 + 1} 天　{WEATHER_ICON.get(w.weather, '')} {w.weather}")
        for ev in notable:
            self.events_by_id[ev["id"]] = ev
            if ev["type"] in ("decision", "rejected"):
                for a in ev["actors"]:
                    self.last_decision[a] = ev
        batch = not MODES[self.mode][1]
        if not (batch and not notable):
            self._feed_day(notable)
        self._update_stats()
        self._sync_population()
        # 嚴重事件自動暫停
        if self.auto_pause.get() and any(ev["type"] in SEVERE for ev in notable):
            if not self.paused:
                self.toggle()
            self.phase_lbl.configure(text="⏸ 已因事件自動暫停（看右欄）")
        if batch:
            if w.day % 5 == 0:
                self._snap_dots(w.track_evening)
            self.root.after(1 if not self.paused else 200, self._next_day)
            return
        homes = {n["id"]: n["home"] for n in w.alive_npcs()}
        tracks = {"work": w.track_work, "evening": w.track_evening, "home": homes}
        self.phase_queue = [(name, tracks[key], stag, hold, clock) for name, key, stag, hold, clock in PHASES]
        self._start_phase()

    def _start_phase(self):
        if not self.phase_queue:
            self.day_running = False
            if MODES[self.mode][2] and not self.paused:   # 單步：一天一停
                self.toggle()
                self.phase_lbl.configure(text="🐢 單步模式：按「▶ 繼續」跑下一天")
            self.root.after(80, self._next_day)
            return
        name, targets, stagger, hold, clock = self.phase_queue.pop(0)
        self.phase_lbl.configure(text=name)
        self.clock_range = clock
        spd = MODES[self.mode][0]
        now = self._vt
        for nid, d in self.dots.items():
            loc = targets.get(nid)
            if loc is None:
                continue
            x, y = LOC_POS.get(loc, LOC_POS["square"])
            jx, jy = jitter(nid, loc)
            d["tx"], d["ty"] = x + jx, y + jy
            d["delay"] = now + (hash((nid, self.w.day, name)) % 1000) / 1000 * stagger / spd
        self.phase_start = now
        self.phase_end = now + (stagger + hold) / spd
        self.root.after(60, self._check_phase)

    def _check_phase(self):
        now = self._vt
        if now >= self.phase_end:
            self._start_phase()
            return
        if not self.paused:
            lo, hi = self.clock_range
            frac = (now - self.phase_start) / max(0.001, self.phase_end - self.phase_start)
            h = lo + (hi - lo) * frac
            self.clock_lbl.configure(text=f"🕐 {int(h) % 24:02d}:{int((h % 1) * 60):02d}")
        self.root.after(80, self._check_phase)

    def _animate(self):
        real = time.time()
        if not self.paused:
            self._vt += real - self._last_real
        self._last_real = real
        if self.paused:
            self.root.after(30, self._animate)
            return
        now = self._vt
        r = S(6)
        for d in self.dots.values():
            if now < d["delay"]:
                continue
            dx, dy = d["tx"] - d["x"], d["ty"] - d["y"]
            if abs(dx) + abs(dy) < 0.8:
                continue
            d["x"] += dx * 0.08
            d["y"] += dy * 0.08
            self.cv.coords(d["item"], d["x"] - r, d["y"] - r, d["x"] + r, d["y"] + r)
            self.cv.coords(d["label"], d["x"], d["y"] - S(14))
        self.root.after(30, self._animate)

    def _snap_dots(self, targets):
        r = S(6)
        for nid, d in self.dots.items():
            loc = targets.get(nid)
            if not loc:
                continue
            x, y = LOC_POS.get(loc, LOC_POS["square"])
            jx, jy = jitter(nid, loc)
            d["x"], d["y"], d["tx"], d["ty"] = x + jx, y + jy, x + jx, y + jy
            self.cv.coords(d["item"], d["x"] - r, d["y"] - r, d["x"] + r, d["y"] + r)

    def _sync_population(self):
        alive = {n["id"] for n in self.w.alive_npcs()}
        for nid in list(self.dots):
            if nid not in alive:
                d = self.dots.pop(nid)
                self.cv.delete(d["item"])
                self.cv.delete(d["label"])

    # ══ 左欄 ══
    def _update_stats(self):
        w = self.w
        s = w.snapshot()
        st = self.stats
        st.configure(state="normal")
        st.delete("1.0", "end")

        def put(text, tag=None):
            st.insert("end", text + "\n", tag or ())

        put(f"日期　第 {w.year} 年 第 {w.doy} 天（{w.season}）")
        put(f"天氣　{WEATHER_ICON.get(w.weather, '')} {w.weather}／{w.temp}")
        put("")
        put(f"人口　{s['population']} / 30")
        put(f"村庫　{s['treasury']:.0f} G")
        put(f"貨幣總量　{s['money_total']:.0f} G")
        put(f"　外部淨流入 {s['external_net']:+.0f} G", "dim")
        put(f"糧倉　穀物 {s['granary'].get('穀物', 0):.0f}｜種糧 {s['granary'].get('種糧', 0):.0f}")
        put("")
        put("物價（G）", "h")
        put(f"　穀物 {w.prices['穀物']:.1f}　肉 {w.prices['肉']:.1f}　麥酒 {w.prices['麥酒']:.1f}")
        put(f"　鐵礦 {w.prices['鐵礦']:.1f}　木柴 {w.prices['木柴']:.1f}　鹽 {w.prices['鹽']:.1f}")
        put("")
        put("各戶　　　　糧天數　金錢（點擊看明細）", "h")
        for hid, hh in s["households"].items():
            name = w.hh[hid]["name"]
            base = "warn" if hh["food_days"] < 14 else "dim"
            st.insert("end", f"　{name}　{hh['food_days']:>5.0f}　{hh['money']:>6.0f}\n", (base, f"hh_{hid}"))
        extra = []
        if w.day < w.wolf_until:
            extra.append(f"🐺 狼群出沒（剩 {w.wolf_until - w.day} 天）")
        if w.day < w.mine_closed_until:
            extra.append(f"⛏ 礦坑封閉（剩 {w.mine_closed_until - w.day} 天）")
        if extra:
            put("")
            for x in extra:
                put(x, "warn")
        if w.llm:
            put("")
            put(f"LLM 呼叫 {s['llm_calls']}（失敗 {s['llm_failures']}）", "dim")
        st.configure(state="disabled")

    # ══ 右欄（可點擊 Replay）══
    def _feed_day(self, notable):
        f = self.feed
        f.configure(state="normal")
        f.insert("end", f"\n── 第 {self.w.year} 年 第 {self.w.doy} 天 ──\n", "day")
        if not notable:
            f.insert("end", "（平靜的一天）\n", "sys")
        for ev in notable:
            tag = {"tavern_talk": "talk", "decision": "decision", "rejected": "rej",
                   "death": "death"}.get(ev["type"], "event")
            prefix = {"talk": "💬", "decision": "🧠", "rej": "🚫", "death": "⚰", "event": "・"}[tag]
            evtag = f"ev_{ev['id']}"
            f.insert("end", f"{ev.get('t', '')} {prefix} {ev['text']}\n", (tag, evtag))
            f.tag_bind(evtag, "<Button-1>", lambda e, eid=ev["id"]: self.replay(eid))
            f.tag_bind(evtag, "<Enter>", lambda e: f.configure(cursor="hand2"))
            f.tag_bind(evtag, "<Leave>", lambda e: f.configure(cursor="arrow"))
        if float(f.index("end-1c").split(".")[0]) > 2500:
            f.delete("1.0", "600.0")
        f.configure(state="disabled")
        f.see("end")

    # ══ Replay 視窗 ══
    def replay(self, eid):
        ev = self.events_by_id.get(eid)
        if not ev:
            return
        win = self._popup(f"事件 Replay — {eid}", S(760), S(560))
        t = tk.Text(win, bg=PANEL, fg=FG, font=self.f_small, bd=0, wrap="word", spacing1=S(2))
        t.pack(fill="both", expand=True, padx=S(12), pady=S(12))
        t.tag_configure("h", foreground="#ffd166")
        t.tag_configure("mono", font=self.f_mono, foreground="#c8d3de")
        names = "、".join(self.w.npc[a]["name"] if a in self.w.npc else a for a in ev["actors"]) or "（世界）"
        t.insert("end", f"{ev['text']}\n\n", "h")
        t.insert("end", f"時間　第 {ev['day']} 天 {ev.get('t', '')}（{ev.get('seg', '')}）\n")
        t.insert("end", f"類型　{ev['type']}　　涉及　{names}\n")
        t.insert("end", f"知情者　{len(ev.get('known_by', []))} 人\n")
        if ev.get("caused_by"):
            t.insert("end", "\n因果上游：\n", "h")
            for cid in ev["caused_by"]:
                src = self.events_by_id.get(cid)
                t.insert("end", f"  ↖ {cid}：{src['text'] if src else '（已捲動出視窗）'}\n")
        if ev.get("effects"):
            t.insert("end", f"\n狀態改動　{json.dumps(ev['effects'], ensure_ascii=False)}\n")
        if ev.get("detail"):
            t.insert("end", "\n── 決策追溯（當時 NPC 知道什麼、LLM 輸出了什麼）──\n", "h")
            t.insert("end", ev["detail"] + "\n", "mono")
        t.configure(state="disabled")

    # ══ NPC 檢視 ══
    def inspect_npc(self, nid):
        n = self.w.npc.get(nid)
        if not n:
            return
        h = self.w.hh[n["hh"]]
        win = self._popup(f"{n['name']} — 檢視", S(560), S(600))
        t = tk.Text(win, bg=PANEL, fg=FG, font=self.f_small, bd=0, wrap="word", spacing1=S(2))
        t.pack(fill="both", expand=True, padx=S(12), pady=S(12))
        t.tag_configure("h", foreground="#ffd166")
        t.tag_configure("dim", foreground=DIM)
        t.tag_configure("mono", font=self.f_mono, foreground="#c8d3de")

        def bar(v):
            filled = int(v / 10)
            return "█" * filled + "░" * (10 - filled)

        t.insert("end", f"{n['name']}　{n['age']} 歲　{n['job']}\n", "h")
        t.insert("end", f"{h['name']}（{'戶主' if h['head'] == nid else '成員'}）　性格：{'、'.join(n['traits'])}\n\n")
        t.insert("end", f"健康 {bar(n['health'])} {n['health']:.0f}\n體力 {bar(n['stamina'])} {n['stamina']:.0f}\n心情 {bar(n['mood'])} {n['mood']:.0f}\n\n")
        cond = []
        if n["injured_until"] > self.w.day:
            cond.append(f"🩹 受傷休養中（剩 {n['injured_until'] - self.w.day} 天）")
        if n["sick_until"] > self.w.day:
            cond.append(f"🤒 臥床病中（剩 {n['sick_until'] - self.w.day} 天）" + ("，隔離於草藥師小屋" if n.get("quarantined") else ""))
        t.insert("end", ("狀態　" + "；".join(cond) if cond else "狀態　正常") + "\n")
        t.insert("end", f"位置　{LOC_LABEL.get(n['loc'], n['loc'])}\n")
        t.insert("end", f"技能　{'、'.join(f'{k} Lv.{v}' for k, v in n['skills'].items())}\n\n")
        t.insert("end", f"戶金錢 {h['money']:.0f}G　戶存糧 {self.w._hh_food_days(n['hh']):.0f} 天\n\n")
        rels = sorted(((self.w.rel_get(nid, o["id"]), o["name"]) for o in self.w.alive_npcs() if o["id"] != nid),
                      key=lambda x: -abs(x[0]))[:5]
        t.insert("end", "重要關係\n", "h")
        for v, name in rels:
            t.insert("end", f"　{name}　{v:+.0f}\n")
        t.insert("end", "\n他知道的近期事件\n", "h")
        known = [e for e in getattr(self.w, "recent_events", []) if nid in e.get("known_by", [])][-8:]
        if not known:
            t.insert("end", "　（沒什麼新鮮事）\n", "dim")
        for e in known:
            t.insert("end", f"　第{e['day']}天 {e.get('t', '')}　{e['text']}\n", "dim")
        last = self.last_decision.get(nid)
        if last:
            t.insert("end", f"\n最近一次 LLM 決策（第 {last['day']} 天）\n", "h")
            t.insert("end", f"　{last['text']}\n", "dim")
            if last.get("detail"):
                t.insert("end", "\n" + last["detail"] + "\n", "mono")
        t.configure(state="disabled")

    # ══ 戶檢視 ══
    def inspect_household(self, hid):
        w = self.w
        h = w.hh.get(hid)
        if not h:
            return
        win = self._popup(f"{h['name']} — 戶檢視", S(600), S(640))
        t = tk.Text(win, bg=PANEL, fg=FG, font=self.f_small, bd=0, wrap="word", spacing1=S(2))
        t.pack(fill="both", expand=True, padx=S(12), pady=S(12))
        t.tag_configure("h", foreground="#ffd166")
        t.tag_configure("dim", foreground=DIM)
        t.tag_configure("pos", foreground="#95d5b2")
        t.tag_configure("neg", foreground="#ffb4a2")
        t.insert("end", f"{h['name']}\n", "h")
        t.insert("end", f"金錢 {h['money']:.0f}G　存糧約 {w._hh_food_days(hid):.0f} 天")
        if h.get("grain_debt", 0) > 0.5:
            t.insert("end", f"　⚠ 欠公倉 {h['grain_debt']:.0f} 份糧", "neg")
        t.insert("end", "\n\n成員\n", "h")
        for mid in h["members"]:
            n = w.npc[mid]
            if not n["alive"]:
                t.insert("end", f"　{n['name']}（歿）\n", "dim")
                continue
            flags = []
            if n["injured_until"] > w.day:
                flags.append("🩹傷")
            if n["sick_until"] > w.day:
                flags.append("🤒病")
            t.insert("end", f"　{n['name']}　{n['age']}歲 {n['job']}　健康{n['health']:.0f} 體力{n['stamina']:.0f} 心情{n['mood']:.0f} {' '.join(flags)}\n")
        t.insert("end", "\n庫存\n", "h")
        inv = {k: v for k, v in sorted(h["stocks"].items(), key=lambda kv: -kv[1]) if v >= 1}
        t.insert("end", "　" + ("　".join(f"{k} {v:.0f}" for k, v in inv.items()) or "（家徒四壁）") + "\n")
        # 90 天收支帳本（分類彙整）——「這個戶是真的活著，還是只是在跑數字」看這裡
        led = [e for e in w.ledger.get(hid, []) if e[0] > w.day - 90]
        t.insert("end", "\n近 90 天收支\n", "h")
        if not led:
            t.insert("end", "　（無金錢往來）\n", "dim")
        else:
            agg = {}
            for _, cat, amt in led:
                agg[cat] = agg.get(cat, 0) + amt
            income = sum(v for v in agg.values() if v > 0)
            expense = sum(v for v in agg.values() if v < 0)
            for cat, amt in sorted(agg.items(), key=lambda kv: -abs(kv[1])):
                t.insert("end", f"　{'+' if amt > 0 else ''}{amt:.0f}G　{cat}\n", "pos" if amt > 0 else "neg")
            t.insert("end", f"　── 合計 收入 +{income:.0f}G／支出 {expense:.0f}G／淨 {income + expense:+.0f}G\n", "dim")
        t.insert("end", "\n本戶捲入的近期事件\n", "h")
        mems = set(h["members"])
        evs = [e for e in getattr(w, "recent_events", []) if mems & set(e.get("actors", []))][-8:]
        if not evs:
            t.insert("end", "　（平安無事）\n", "dim")
        for e in evs:
            t.insert("end", f"　第{e['day']}天 {e.get('t', '')}　{e['text']}\n", "dim")
        t.configure(state="disabled")

    def _popup(self, title, w, h):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=PANEL)
        win.geometry(f"{w}x{h}+{self.root.winfo_x() + S(120)}+{self.root.winfo_y() + S(80)}")
        win.transient(self.root)
        return win


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", choices=["on", "off"], default="off")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    llm = None
    if args.llm == "on":
        import yaml
        from sim.llm import LLMClient
        cfg = yaml.safe_load(open(ROOT_DIR / "data" / "params.yaml", encoding="utf-8"))["llm"]
        llm = LLMClient(cfg)
        if not llm.available():
            llm = None
    w = World(seed=args.seed, llm=llm)
    root = tk.Tk()
    root.geometry(f"{S(1580)}x{S(830)}+30+30")
    app = MapApp(root, w)

    def on_tk_error(exc, val, tb):
        ERR_LOG.parent.mkdir(exist_ok=True)
        with open(ERR_LOG, "a", encoding="utf-8") as fh:
            fh.write("".join(traceback.format_exception(exc, val, tb)) + "\n")
    root.report_callback_exception = on_tk_error
    # 確保視窗浮到最前（避免開在其他視窗後面沒被發現）
    root.lift()
    root.attributes("-topmost", True)
    root.after(1500, lambda: root.attributes("-topmost", False))
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        ERR_LOG.parent.mkdir(exist_ok=True)
        with open(ERR_LOG, "a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc() + "\n")
        raise
