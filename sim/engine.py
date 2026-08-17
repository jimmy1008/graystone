# -*- coding: utf-8 -*-
"""灰石村世界模擬引擎 M1 — 先運作起來，讓歷史逐漸出現。

設計正本：docs/00~10。本檔為 M1 最小可跑版，已知簡化（M2 待補）：
  - 婚配/生育/遷徙/開墾/建屋 未實作（人口動態僅老化+死亡）
  - 持久化用 JSON/JSONL（schema.sql 的 SQLite 版留待 M2）
  - known_by 傳播簡化：事件對 涉及者+同戶 可見，酒館謠言擴散
所有數值取自 data/params.yaml —— 引擎零魔法數字（docs/09 鐵律）。
LLM：全員接 LLM（docs/06）；離線時退化為純規則模式。
"""
import json
import os
import random
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SEASONS = ["春", "夏", "秋", "冬"]
FOOD_RES = ["肉", "鹽漬肉", "穀物"]  # 進食順序：易腐先吃、肉是食物不是收藏品、穀物耐存放最後


def load_yaml(name):
    with open(ROOT / "data" / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


class World:
    def __init__(self, seed=42, llm=None, out_dir=None):
        self.rng = random.Random(seed)
        self.P = load_yaml("params.yaml")
        self.day = 0            # 世界日（1 起算）；year = day//360+1
        self.llm = llm          # LLMClient 或 None
        self.out = Path(out_dir or ROOT / "out")
        self.out.mkdir(exist_ok=True)
        self.events_f = open(self.out / "events.jsonl", "w", encoding="utf-8")
        self._load_world()
        self._init_relationships()
        # 動態狀態
        self.weather = "晴"
        self.temp = "涼"
        self.growth_pts = 0.0
        self.care_mandays = 0.0
        self.harvest_coef = 1.0
        self.wolf_until = 0
        self.mine_closed_until = 0
        self.mine_extracted = 0
        self.caravan_due = self.P["time"]["caravan_interval"]
        self.external_net = 0.0
        self.event_seq = 0
        self.trades = []        # (day, res, qty, price, external)
        self.demand = defaultdict(float)
        self.supply = defaultdict(float)
        self.ledger = defaultdict(list)   # hid -> [(day, 類別, ±金額)]，戶收支帳本（90 天窗口）
        self.wolf_eid = self.harvest_eid = self.last_hunger_eid = None
        self.prev_evening = {}
        self.prices = {r: float(p) for r, p in load_yaml("prices.yaml")["base_prices"].items()}
        self.anchors = dict(self.prices)
        self.notable_today = []
        self._init_market_prices()   # 開局物價由開局世界狀態推導（不留「設計價 vs 實況」的跳崖）

    # ---------- 載入 ----------
    def _load_world(self):
        hh_data = load_yaml("households.yaml")
        self.hh = {}
        for h in hh_data["households"]:
            self.hh[h["id"]] = {
                "id": h["id"], "name": h["name"], "head": h["head"],
                "money": float(h["money"]), "members": list(h["members"]),
                "stocks": defaultdict(float, {k: float(v) for k, v in h.get("stocks", {}).items()}),
            }
        inst = {i["id"]: i for i in hh_data["institutions"]}
        self.treasury = float(inst["treasury"]["money"])
        self.granary = defaultdict(float, {k: float(v) for k, v in inst["granary"]["stocks"].items()})
        self.smithy = defaultdict(float, {k: float(v) for k, v in inst["smithy"]["stocks"].items()})
        self.initial_money = sum(h["money"] for h in self.hh.values()) + self.treasury

        hh_home = {h["id"]: h["residence"] for h in hh_data["households"]}
        self.npc = {}
        for n in load_yaml("npcs.yaml")["npcs"]:
            self.npc[n["id"]] = {
                "home": hh_home.get(n["household"], "square"), "loc": hh_home.get(n["household"], "square"),
                "id": n["id"], "name": n["name"], "age": n["age"], "hh": n["household"],
                "job": n.get("job_main", "無業"), "side": n.get("job_side"),
                "labor": {"半": 0.5, "無": 0.0}.get(n.get("labor"), 1.0),
                "skills": n.get("skills", {}), "traits": n.get("traits", []),
                "stamina": 100.0, "health": 100.0, "mood": 50.0,
                "alive": True, "injured_until": 0, "sick_until": 0,
                "hunger_streak": 0, "at_tavern": False, "plan": None, "last_harm": None,
            }
        items = load_yaml("items.yaml")
        self.registry = {it["id"]: dict(it) for it in items["item_registry"]}
        self.rare_seq = 0

    def _init_relationships(self):
        self.rel = {}
        ids = [i for i in self.npc]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                v = 0
                na, nb = self.npc[a], self.npc[b]
                if na["hh"] == nb["hh"]:
                    v = 60
                elif na["job"] == nb["job"]:
                    v = 20
                self.rel[(a, b)] = float(v)

    def rel_get(self, a, b):
        return self.rel.get((a, b) if (a, b) in self.rel else (b, a), 0.0)

    def rel_add(self, a, b, d):
        k = (a, b) if (a, b) in self.rel else (b, a)
        self.rel[k] = max(-100.0, min(100.0, self.rel.get(k, 0.0) + d))

    # ---------- 工具 ----------
    @property
    def year(self):
        return (self.day - 1) // self.P["time"]["days_per_year"] + 1

    @property
    def doy(self):
        return (self.day - 1) % self.P["time"]["days_per_year"] + 1

    @property
    def season(self):
        return SEASONS[(self.doy - 1) // 90]

    def log_event(self, etype, actors, text, effects=None, caused_by=None, notable=True, detail=None):
        self.event_seq += 1
        eid = f"E-Y{self.year}-{self.event_seq:04d}"
        known = set(actors)
        for a in actors:
            if a in self.npc:
                known.update(self.hh[self.npc[a]["hh"]]["members"])
        seg = getattr(self, "segment", "白天")
        lo, hi = self.SEG_HOURS.get(seg, (9, 17))
        self._seg_seq = getattr(self, "_seg_seq", 0) + 1
        h = min(hi - 0.02, lo + self._seg_seq * 0.3 + (hash(eid) % 10) / 60)
        t = f"{int(h) % 24:02d}:{int((h % 1) * 60):02d}"
        ev = {"id": eid, "day": self.day, "t": t, "seg": seg, "type": etype, "actors": list(actors),
              "text": text, "effects": effects or [], "caused_by": caused_by or [],
              "known_by": sorted(known), "notable": notable}
        if detail:
            ev["detail"] = detail
        self.events_f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        if notable:
            self.notable_today.append(ev)
        if not hasattr(self, "recent_events"):
            self.recent_events = []
        self.recent_events.append(ev)
        self.recent_events = [e for e in self.recent_events if e["day"] > self.day - 14]
        return eid

    def alive_npcs(self):
        return [n for n in self.npc.values() if n["alive"]]

    def can_work(self, n):
        return (n["alive"] and n["labor"] > 0 and n["health"] >= self.P["needs"]["health_thresholds"]["no_work"]
                and n["stamina"] >= 25 and self.day >= n["injured_until"] and self.day >= n["sick_until"])

    def skill_coef(self, n, skill):
        return 0.7 + n["skills"].get(skill, 1) * 0.1

    def trade(self, buyer_hh, seller_hh, res, qty, price, external=False):
        """buyer/seller: hh id 或 'treasury'/'granary'/'smithy'/'EXT'。回傳成交量。"""
        if qty <= 0:
            return 0.0
        cost = qty * price
        b_money = self._money(buyer_hh)
        if b_money < cost and buyer_hh != "EXT":
            qty = max(0.0, b_money / price)
            cost = qty * price
        if qty <= 0:
            return 0.0
        s_stock = self._stocks(seller_hh)
        if s_stock is not None:
            qty = min(qty, s_stock.get(res, 0.0))
            cost = qty * price
        if qty <= 0:
            return 0.0
        self._money_add(buyer_hh, -cost)
        self._money_add(seller_hh, cost)
        if buyer_hh in self.hh:
            self.ledger[buyer_hh].append((self.day, f"買{res}", -cost))
        if seller_hh in self.hh:
            self.ledger[seller_hh].append((self.day, f"賣{res}", cost))
        elif seller_hh == "smithy":
            self.ledger["h02"].append((self.day, f"鋪賣{res}", cost))
        if s_stock is not None:
            s_stock[res] -= qty
        b_stock = self._stocks(buyer_hh)
        if b_stock is not None:
            b_stock[res] += qty
        if external or buyer_hh == "EXT" or seller_hh == "EXT":
            self.external_net += cost if buyer_hh == "EXT" else -cost
        self.trades.append((self.day, res, qty, price))
        return qty

    def _init_market_prices(self):
        """開局即用同一套供需公式對村產品定價：基準價只提供價值尺度（肉為什麼比穀貴），
        實際價位從第 0 天就是世界狀態（庫存/人口/產能）的函數。外來品（鹽/布等）由磐石鎮定價，不動。"""
        need = sum(self._need(n) for n in self.alive_npcs())
        hunters = [n for n in self.alive_npcs() if n["job"] == "獵人"]
        meat_flow = sum(self.P["production"]["hunting"]["meat"] * n["labor"] * self.skill_coef(n, "狩獵") for n in hunters)
        winter = self.season == "冬"
        est = {  # 資源: (預估週需求, 預估週產出)
            "穀物": (max(need - meat_flow * self.P["needs"]["meat_food_value"], 0) * 7, 0),
            "肉":   (meat_flow * 7, meat_flow * 7),
            "木柴": ((len(self.hh) * 2 * 7, 400) if winter else (5, 5)),
            "麥酒": (25, 19),   # 需求以「買得到的量」估（受釀造產能限制），不是「想喝的人數」
        }
        lo, hi = self.P["economy"]["price_clamp"]
        for res, (d, s) in est.items():
            ratio = max(lo, min(hi, (d + 1) / (s + self._stock_total(res) * 0.05 + 1)))
            self.prices[res] = round(max(0.1, self.anchors[res] * ratio), 2)

    def _stock_total(self, res):
        return (self.granary.get(res, 0) + self.smithy.get(res, 0)
                + sum(h["stocks"].get(res, 0) for h in self.hh.values()))

    def _money(self, owner):
        if owner == "treasury":
            return self.treasury
        if owner in ("EXT", "granary", "smithy"):
            return float("inf")
        return self.hh[owner]["money"]

    def _money_add(self, owner, d):
        if owner == "treasury":
            self.treasury += d
        elif owner == "granary":
            self.treasury += d          # 糧倉營收歸村庫
        elif owner == "smithy":
            self.hh["h02"]["money"] += d  # 鐵匠鋪營收歸鐵匠家
        elif owner == "EXT":
            pass
        else:
            self.hh[owner]["money"] += d

    def _stocks(self, owner):
        if owner == "granary":
            return self.granary
        if owner == "smithy":
            return self.smithy
        if owner == "EXT":
            return None
        return self.hh[owner]["stocks"]

    # ---------- 每日主迴圈 ----------
    SEG_HOURS = {"清晨": (6, 9), "白天": (9, 17), "傍晚": (17, 21), "夜間": (21, 24)}

    def tick(self):
        self.day += 1
        self.notable_today = []
        self._seg_seq = 0
        self._weather()
        self.segment = "清晨"
        self._morning()
        self.segment = "白天"
        self._daytime()
        self.track_work = {n["id"]: n["loc"] for n in self.alive_npcs()}      # 白天崗位快照（地圖用）
        for n in self.alive_npcs():
            n["loc"] = n["home"]
        self.segment = "傍晚"
        self._evening()
        self.track_evening = {n["id"]: n["loc"] for n in self.alive_npcs()}   # 傍晚（酒館/回家）快照
        self.segment = "夜間"
        self._night()
        self.prev_evening = self.track_evening   # 疫病傳播鏈追「昨晚誰跟誰同席」用
        return self.notable_today

    # ---------- 天氣（docs/09.A 馬可夫鏈）----------
    def _weather(self):
        W = self.P["weather"]
        if self.rng.random() > W["persistence"]:
            dist = W["distributions"][self.season]
            r, acc = self.rng.random(), 0.0
            for state, p in dist.items():
                acc += p
                if r <= acc:
                    self.weather = state
                    break
        base = W["temp_bands"][self.season]
        bands = ["暖", "涼", "寒", "酷寒"]
        i = bands.index(base)
        if self.rng.random() < 0.2:
            i = min(len(bands) - 1, max(0, i + self.rng.choice([-1, 1])))
        self.temp = bands[i]
        gp = W["growth_points"]
        T = self.P["time"]
        if T["sowing_day"] <= self.doy <= T["harvest_window"][0]:
            pts = gp.get(self.weather, 0)
            if self.temp in ("寒", "酷寒"):
                pts += gp["寒日"]
            self.growth_pts += pts

    def outdoor_ok(self):
        return self.weather != "暴風" and self.temp != "酷寒"

    def weather_coef(self):
        if not self.outdoor_ok():
            return 0.0
        return self.P["weather"]["rain_efficiency"] if self.weather == "雨" else 1.0

    # ---------- 清晨：決定行動 ----------
    def _morning(self):
        T = self.P["time"]
        if self.doy == T["sowing_day"]:
            need = self.P["production"]["farming"]["seed_per_field"] * 3
            used = min(need, self.granary.get("種糧", 0))
            self.granary["種糧"] -= used
            self.log_event("sowing", ["npc_01_1"], f"春播開始，糧倉撥出種糧 {used:.0f} 份", notable=True)
            if used < need:
                self.log_event("seed_shortage", ["npc_01_1"], f"種糧短缺 {need-used:.0f} 份，今年部分農田將荒著", notable=True)
                self.seed_ratio = used / need
            else:
                self.seed_ratio = 1.0
        if self.doy == T["harvest_window"][0]:
            norm = 300.0
            wcoef = max(0.5, min(1.3, self.growth_pts / norm))
            F = self.P["production"]["farming"]
            days = 90
            req = F["care_manday_per_10d"] * 3 * (days / 10)
            care_ratio = min(1.0, self.care_mandays / max(1, req))
            ccoef = 0.8 + 0.4 * care_ratio
            self.harvest_coef = wcoef * ccoef * getattr(self, "seed_ratio", 1.0)
            self.harvest_eid = self.log_event(
                "harvest_start", ["npc_01_1"],
                f"🌾 秋收開始。生長積分 {self.growth_pts:.0f}（天氣係數 {wcoef:.2f}）×照料 {ccoef:.2f} → 收成係數 {self.harvest_coef:.2f}",
                detail=f"收成係數的因果組成：\n  整個生長季逐日天氣累計出生長積分 {self.growth_pts:.0f}（晴+2 雨+1 陰+1 寒-1 暴風-3）\n  夏季照料人日達成率 → 照料係數 {ccoef:.2f}\n  春播種糧足額率 → {getattr(self, 'seed_ratio', 1.0):.2f}\n這不是擲骰子，是這一年天氣與勞動的總帳。")

    # ---------- 白天：生產 ----------
    def _daytime(self):
        P = self.P["production"]
        T = self.P["time"]
        in_harvest = T["harvest_window"][0] <= self.doy <= T["harvest_window"][1]
        harvesters = 0
        FIELD_OF = {"h05": "fields_a", "h06": "fields_a", "h07": "fields_b", "h08": "fields_b"}
        for n in self.alive_npcs():
            # 臥床者留在家；集中隔離者移到草藥師小屋（地圖上看得到）
            n["loc"] = "house_12" if (self.day < n["sick_until"] and n.get("quarantined")) else n["home"]
            if not self.can_work(n):
                continue
            if n["stamina"] < 40 and not in_harvest and n["plan"] != "work_extra":
                n["stamina"] = min(100, n["stamina"] + 5)  # 累了就歇一天（收成窗口除外）
                continue
            job = n["job"]
            n["loc"] = {"獵人": "forest", "礦工": "mine", "鐵匠": "smithy_loc", "鍛造學徒": "smithy_loc",
                        "酒館老闆": "tavern_loc", "酒館": "tavern_loc", "酒館幫工": "tavern_loc",
                        "商人": "store_loc", "守衛隊長": "square", "守衛": "road",
                        "村長": "granary_loc", "草藥師": "forest" if self.season == "春" else "house_12",
                        "農": FIELD_OF.get(n["hh"], "fields_c") if self.season not in ("秋", "冬") else "logging",
                        }.get(job, "square")
            if in_harvest and (job == "農" or self._is_helper(n)):
                n["loc"] = FIELD_OF.get(n["hh"], "fields_c")
            if job == "礦工" and self.day < self.mine_closed_until:
                n["loc"] = n["home"]
            hhs = self.hh[n["hh"]]["stocks"]
            eff = n["labor"] * self.skill_coef(n, self._job_skill(job)) * (0.7 if n["mood"] < 20 else 1.0)
            wx = self.weather_coef()
            worked = False
            if in_harvest and harvesters < 15 and (job == "農" or self._is_helper(n)):
                if wx > 0:
                    F = P["farming"]
                    base = F["harvest_yield_full"] if job == "農" else F["harvest_yield_helper"]
                    got = base * eff * wx * self.harvest_coef
                    hhs["穀物"] += got
                    self.supply["穀物"] += got
                    harvesters += 1
                    worked = True
            elif job == "農":
                if self.season == "夏" and wx > 0:
                    self.care_mandays += n["labor"]
                    worked = True
                elif self.season in ("秋", "冬") and wx > 0:
                    hhs["木柴"] += P["logging"]["wood"] * eff  # 秋備冬柴、冬季主力伐木
                    self.supply["木柴"] += P["logging"]["wood"] * eff
                    worked = True
                elif wx > 0:
                    worked = True  # 整地/修繕
            elif job == "獵人" and wx > 0:
                H = P["hunting"]
                mult = H["winter_mult"] if self.season == "冬" else (H["autumn_mult"] if self.season == "秋" else 1.0)
                wolf_active = self.day < self.wolf_until
                risk = H["injury_risk_wolf"] if wolf_active else H["injury_risk"]
                if self.rng.random() < risk:
                    self._injure(n, "遭狼群圍攻受傷" if wolf_active else "狩獵受傷", "森林",
                                 caused_by=[self.wolf_eid] if (wolf_active and self.wolf_eid) else None)
                else:
                    hhs["肉"] += H["meat"] * eff * mult * wx
                    hhs["皮毛"] += H["pelt"] * eff * mult * wx
                    self.supply["肉"] += H["meat"] * eff * mult * wx
                    worked = True
            elif job == "礦工" and self.day >= self.mine_closed_until and wx > 0:
                M = P["mining"]
                if self.rng.random() < M["collapse_risk"]:
                    self._mine_collapse()
                else:
                    dep = self.P["development"]["mine"]
                    mult = dep["depletion_mult"] ** (self.mine_extracted // dep["depletion_step"])
                    smult = M["winter_mult"] if self.season == "冬" else 1.0
                    got = M["ore"] * eff * mult * smult * wx
                    hhs["鐵礦"] += got
                    self.supply["鐵礦"] += got
                    self.mine_extracted += got
                    if self.rng.random() < M["rare_find_daily"]:
                        self._rare_find(n)
                    worked = True
            elif job in ("鐵匠", "鍛造學徒"):
                self._smith_work(n)
                worked = True
            elif job in ("酒館老闆", "酒館", "酒館幫工"):
                self._tavern_work()
                worked = True
            elif job == "草藥師":
                if self.season == "春" and wx > 0:
                    hhs["藥草"] += P["herbalism"]["herb_per_day"]
                self._heal_patients(n)
                worked = True
            elif job in ("守衛隊長", "守衛", "商人", "村長"):
                worked = True
            if worked:
                cost = self.P["needs"]["stamina"]["danger_work_cost"] if job in ("礦工", "獵人") else self.P["needs"]["stamina"]["work_cost"]
                n["stamina"] = max(0, n["stamina"] - cost)

    def _job_skill(self, job):
        return {"農": "農耕", "獵人": "狩獵", "礦工": "採礦", "鐵匠": "鍛造", "鍛造學徒": "鍛造",
                "酒館老闆": "經營", "酒館": "經營", "酒館幫工": "經營", "商人": "經營",
                "草藥師": "醫療", "守衛隊長": "戰鬥", "守衛": "戰鬥", "村長": "行政"}.get(job, "農耕")

    def _is_helper(self, n):
        return n["job"] not in ("守衛隊長", "守衛", "草藥師") and n["labor"] >= 0.5

    def _injure(self, n, why, where, caused_by=None):
        dur = self.rng.randint(7, 14)
        n["health"] = max(0, n["health"] - self.rng.randint(20, 40))
        n["injured_until"] = self.day + dur
        n["mood"] -= 10
        for m in self.hh[n["hh"]]["members"]:
            if m != n["id"] and self.npc[m]["alive"]:
                self.npc[m]["mood"] += self.P["needs"]["mood"]["family_hurt"]  # -15
        n["last_harm"] = self.log_event("injury", [n["id"]], f"{n['name']} 在{where}{why}，需休養 {dur} 天",
                                        caused_by=caused_by or [])

    def _mine_collapse(self):
        dur = self.rng.randint(5, 10)
        self.mine_closed_until = self.day + dur
        eid = self.log_event("mine_collapse", [n["id"] for n in self.alive_npcs() if n["job"] == "礦工"],
                             f"⛏ 礦坑淺層支撐崩塌！關閉 {dur} 天（技術落後無坑木支護——這種事遲早發生）")
        for n in self.alive_npcs():
            if n["job"] == "礦工" and self.can_work(n) and self.rng.random() < 0.5:
                self._injure(n, "被塌方波及", "礦坑", caused_by=[eid])

    def _rare_find(self, n):
        M = self.P["production"]["mining"]
        r, acc = self.rng.random(), 0.0
        for kind, p in M["rare_find_table"].items():
            acc += p
            if r <= acc:
                break
        if kind == "銀礦石":
            self.hh[n["hh"]]["stocks"]["銀礦石"] += 1
            self.log_event("rare_find", [n["id"]], f"{n['name']} 在礦坑淺層挖到一塊銀礦石！")
        else:
            self.rare_seq += 1
            lo, hi = M["rare_values"][kind]
            val = self.rng.randint(lo, hi)
            iid = f"RARE-{self.rare_seq:03d}"
            self.registry[iid] = {"id": iid, "type": kind, "owner": n["hh"], "value": val, "found_day": self.day}
            self.log_event("rare_find", [n["id"]], f"{n['name']} 挖到一件{kind}（{iid}，估值 {val}G）——它是什麼、從哪來，無人知曉")

    def _smith_work(self, smith):
        # 修理耐久歸零的工具（工錢3G+料1鐵礦，修至60%）；鐵礦不足則向礦工家進貨
        if self.smithy.get("鐵礦", 0) < 5:
            for hid in ("h10",):
                self.trade("smithy", hid, "鐵礦", 10, self.prices["鐵礦"])
        for it in self.registry.values():
            if it.get("type") in ("鋤具", "獵弓", "鎬") and it.get("durability", 1) <= 0:
                owner = it["owner"]
                fee = self.prices.get("工具修理", 3)
                if self._money(owner) >= fee and self.smithy.get("鐵礦", 0) >= 1:
                    self._money_add(owner, -fee)
                    self._money_add("smithy", fee)
                    if owner in self.hh:
                        self.ledger[owner].append((self.day, "修理工具", -fee))
                    self.ledger["h02"].append((self.day, "修理收入", fee))
                    self.smithy["鐵礦"] -= 1
                    it["durability"] = 108
                    break  # 一天修一件

    def _tavern_work(self):
        # 釀酒節制：自家糧至少 150 份才拿糧釀酒（缺糧年份酒館自然歇業——不跟活命搶糧）
        B = self.P["production"]["brewing"]
        st = self.hh["h03"]["stocks"]
        if st.get("麥酒", 0) < 20 and st.get("穀物", 0) >= 150 and self.day % B["ferment_days"] == 0:
            st["穀物"] -= B["grain_in"]
            st["麥酒"] += B["ale_out"]
            self.demand["穀物"] += B["grain_in"]
            self.supply["麥酒"] += B["ale_out"]
        elif st.get("穀物", 0) < 150 and self.prices["穀物"] <= self.anchors["穀物"] * 1.2:
            for hid in ("h05", "h06", "h07", "h08"):
                if self._hh_food_days(hid) > 60:
                    self.trade("h03", hid, "穀物", 30, self.prices["穀物"])
                    break

    def _heal_patients(self, healer):
        F = self.P["production"]["herbalism"]
        for n in self.alive_npcs():
            if n["id"] == healer["id"]:
                continue
            if n["injured_until"] > self.day or n["sick_until"] > self.day:
                if self.hh[n["hh"]]["money"] >= F["fee"] and self.hh["h12"]["stocks"].get("藥草", 0) >= 0.5:
                    self._money_add(n["hh"], -F["fee"])
                    self._money_add("h12", F["fee"])
                    self.ledger[n["hh"]].append((self.day, "診療", -F["fee"]))
                    self.ledger["h12"].append((self.day, "診療收入", F["fee"]))
                    self.hh["h12"]["stocks"]["藥草"] -= 0.5
                    # 有藥草照護 → 恢復速度 ×2（每天多消掉一天病程/傷勢）
                    if n["injured_until"] > self.day:
                        n["injured_until"] = max(self.day, n["injured_until"] - 1)
                    if n["sick_until"] > self.day:
                        n["sick_until"] = max(self.day, n["sick_until"] - 1)

    # ---------- 傍晚：LLM 抉擇點 + 交易 + 社交 ----------
    def _evening(self):
        self._llm_decisions()
        self._food_market()
        self._tavern_social()

    def _hh_food_days(self, hid):
        h = self.hh[hid]
        need = sum(self._need(self.npc[m]) for m in h["members"] if self.npc[m]["alive"])
        if need <= 0:
            return 99
        units = h["stocks"].get("穀物", 0) + (h["stocks"].get("肉", 0) + h["stocks"].get("鹽漬肉", 0)) * self.P["needs"]["meat_food_value"]
        return units / need

    def _need(self, n):
        f = self.P["needs"]["food_per_day"]
        if n["labor"] == 0:
            return f["老人"]
        if n["labor"] == 0.5:
            return f["少年"]
        return f["成人"]

    def _llm_decisions(self):
        if not self.llm:
            self.pending_crisis = None   # 無 LLM = 無人組織應對，病患各自在家躺著（這也是一種世界）
            return
        self._crisis_decisions()
        cands = []
        # 疫病流行中：部分健康村民面臨「今晚還去不去酒館」的判斷（怕不怕由他們自己決定）
        sick_count = sum(1 for x in self.alive_npcs() if self.day < x["sick_until"] and x.get("sick_src"))
        if sick_count >= 2:
            healthy = [x for x in self.alive_npcs() if self.day >= x["sick_until"] and x["labor"] >= 1.0]
            for pick in self.rng.sample(healthy, min(2, len(healthy))):
                cands.append((1, pick["id"], f"村裡疫病還沒退（{sick_count} 人臥床），你今晚原本想去酒館喝一杯"))
        for hid in self.hh:
            fd = self._hh_food_days(hid)
            if fd < 3:
                cands.append((0, self.hh[hid]["head"], f"你家的糧食只剩約 {fd:.1f} 天份"))
        for n in self.alive_npcs():
            if n["mood"] < 30:
                cands.append((1, n["id"], "你最近心情很差"))
        if self.doy in range(256, 266):
            for hid in ("h05", "h06", "h07", "h08"):
                if self.hh[hid]["stocks"].get("穀物", 0) > 500:
                    cands.append((1, self.hh[hid]["head"], "秋收剛結束，你家糧倉充實，正是決定賣不賣糧的時候"))
        if self.season == "冬" and self.rng.random() < 0.5:
            pick = self.rng.choice(self.alive_npcs())
            cands.append((2, pick["id"], "冬夜漫長，你在想些什麼"))
        cands.sort(key=lambda c: c[0])
        used = set()
        budget = self.P["llm"]["max_decisions_per_day"]
        for _, nid, situation in cands:
            if budget <= 0 or nid in used or not self.npc[nid]["alive"]:
                continue
            used.add(nid)
            budget -= 1
            self._run_decision(self.npc[nid], situation)

    def _crisis_decisions(self):
        """危機應對：不是規則自動觸發，而是把狀況交給當事 NPC（LLM）定奪。"""
        c = getattr(self, "pending_crisis", None)
        if not c or c["type"] != "plague":
            return
        self.pending_crisis = None
        eid, victims = c["eid"], [self.npc[v] for v in c["victims"] if self.npc[v]["alive"]]
        if not victims:
            return
        names = "、".join(v["name"] for v in victims)
        # ① 草藥師先診斷（她的 BELIEF，可能錯——她只看得到病人和他們的接觸面，看不到真相）
        healer = self.npc.get("npc_12_1")
        suspicion = None
        if healer and healer["alive"]:
            hh_groups = len({v["hh"] for v in victims})
            ctx = (f"你是草藥師梅姑，60 歲，行醫一生。昨夜村裡爆發疫病，{names}相繼病倒。\n"
                   f"你診視後掌握的線索：病患來自 {hh_groups} 個不同的戶；發病都在夜裡；現在是{self.season}季"
                   f"{'（融雪時節）' if self.season == '春' else ''}；前陣子商隊來過村裡。\n"
                   f"依你的經驗，病因最可能出在哪？")
            out = self.llm.choose(ctx, ["水源", "外來旅人", "壞掉的食物", "看不出來"])
            if out:
                suspicion = out["choice"]
                right = (suspicion == "水源" and c["source"] == "well") or (suspicion == "外來旅人" and c["source"] == "traveler")
                verdict = f"（她{'恰好接近真相' if right else '猜錯了，但她自己不知道'}）" if suspicion != "看不出來" else ""
                self.log_event("diagnosis", ["npc_12_1"],
                               f"梅姑診治病患後，判斷病因出在「{suspicion}」——{out['reason']}{verdict}",
                               caused_by=[eid],
                               detail=f"這是草藥師的 BELIEF，不是世界真相。\n真相（僅開發者視角）：{c['truth']}\n她的判斷：{suspicion}\n她的理由：{out['reason']}")
        # ② 村長決定村莊怎麼應對（他知道的：誰病了＋梅姑的判斷，僅此而已）
        chief = self.npc.get("npc_01_1")
        decider = chief if chief and chief["alive"] and self.day >= chief["sick_until"] else (healer if healer and healer["alive"] else None)
        if not decider:
            return
        ctx = (f"你是{decider['name']}（{decider['job']}）。村裡爆發疫病，{names}臥床不起。\n"
               f"草藥師的判斷：{'病因可能是' + suspicion if suspicion else '尚無頭緒'}。\n"
               f"村莊只有 30 人，草藥師只有一位，她的小屋不大。你要怎麼安置病患？\n"
               f"「集中隔離」：病患移到草藥師小屋照護——她照顧得過來，家人不易被傳染，但她自己暴露風險高；\n"
               f"「在家休養」：各自家人照顧——分散她的藥草和精力，家人有被傳染的風險；\n"
               f"「暫不處置」：先觀望。")
        out = self.llm.choose(ctx, ["集中隔離", "在家休養", "暫不處置"])
        choice = out["choice"] if out else "在家休養"
        why = out["reason"] if out else "（LLM 未回應，村莊未作組織性應對）"
        if choice == "集中隔離":
            for v in victims:
                v["quarantined"] = True
            self.log_event("quarantine", [decider["id"], "npc_12_1"],
                           f"🏥 {decider['name']}決定把病患集中到草藥師小屋隔離照護——{why}",
                           caused_by=[eid], detail=f"決策者：{decider['name']}\n選項：集中隔離/在家休養/暫不處置\n決定：{choice}\n理由：{why}")
        else:
            self.log_event("crisis_response", [decider["id"]],
                           f"{decider['name']}決定讓病患{choice}——{why}",
                           caused_by=[eid], detail=f"決策者：{decider['name']}\n決定：{choice}\n理由：{why}")

    def _npc_context(self, n, situation):
        h = self.hh[n["hh"]]
        stocks = {k: round(v, 1) for k, v in h["stocks"].items() if v >= 1}
        rels = []
        for other in self.alive_npcs():
            if other["id"] != n["id"]:
                v = self.rel_get(n["id"], other["id"])
                if abs(v) >= 20:
                    rels.append(f"{other['name']}({other['id']}): {v:+.0f}")
        known = [e for e in getattr(self, "recent_events", []) if n["id"] in e["known_by"]][-5:]
        return (
            f"你是 {n['name']}，{n['age']} 歲，{n['job']}，性格{('、'.join(n['traits']))}。\n"
            f"現在是第{self.year}年{self.season}，{self.weather}。\n"
            f"你家（{h['name']}，成員 {len(h['members'])} 人）有金錢 {h['money']:.0f}G，庫存：{stocks}。\n"
            f"糧食約可撐 {self._hh_food_days(n['hh']):.1f} 天。\n"
            f"重要關係：{'; '.join(rels) if rels else '無特別親疏'}。\n"
            f"你知道的近期事件：{'; '.join(e['text'] for e in known) if known else '無'}。\n"
            f"市價：穀物 {self.prices['穀物']:.1f}G、肉 {self.prices['肉']:.1f}G、木柴 {self.prices['木柴']:.1f}G。\n"
            f"目前狀況：{situation}。今晚你要做什麼？"
        )

    def _run_decision(self, n, situation):
        ctx = self._npc_context(n, situation)
        out = self.llm.decide(ctx)
        if not out:
            return
        trace = f"【當時情境（LLM 收到的全部資訊）】\n{ctx}\n\n【LLM 輸出】\n{json.dumps(out, ensure_ascii=False, indent=1)}"
        act, ok, why = out.get("action"), False, out.get("reason", "")
        res, qty, tgt = out.get("resource", ""), float(out.get("qty") or 0), out.get("target", "")
        h = self.hh[n["hh"]]
        if act == "sell" and res in self.prices and h["stocks"].get(res, 0) >= qty > 0:
            buyer = "h04" if res not in ("穀物",) else "granary"
            got = self.trade(buyer, n["hh"], res, qty, self.prices[res])
            ok = got > 0
        elif act == "buy" and res in self.prices and qty > 0:
            seller = "granary" if res == "穀物" and self.granary.get("穀物", 0) > qty else "h04"
            got = self.trade(n["hh"], seller, res, qty, self.prices[res])
            ok = got > 0
        elif act == "borrow_food":
            tgt_h = tgt if tgt in self.hh else None
            if tgt_h and tgt_h != n["hh"]:
                lender_head = self.hh[tgt_h]["head"]
                if self.rel_get(n["id"], lender_head) >= self.P["economy"]["debt"]["min_rel"] and self._hh_food_days(tgt_h) > 14:
                    amt = min(qty or 20, self.hh[tgt_h]["stocks"].get("穀物", 0) * 0.2)
                    self.hh[tgt_h]["stocks"]["穀物"] -= amt
                    h["stocks"]["穀物"] += amt
                    self.rel_add(n["id"], lender_head, 3)
                    ok = amt > 0
                    self.log_event("loan", [n["id"], lender_head], f"{n['name']}向{self.npc[lender_head]['name']}家借了 {amt:.0f} 份穀物——{why}")
                else:
                    self.rel_add(n["id"], self.hh[tgt_h]["head"], -5)
                    self.log_event("loan_refused", [n["id"]], f"{n['name']}想借糧被拒——{why}")
                    return
        elif act == "gift" and tgt in self.hh and res in self.prices and h["stocks"].get(res, 0) >= qty > 0:
            h["stocks"][res] -= qty
            self.hh[tgt]["stocks"][res] += qty
            self.rel_add(n["id"], self.hh[tgt]["head"], 3)
            ok = True
        elif act == "tavern":
            n["at_tavern"] = True
            ok = True
        elif act in ("rest", "work_extra"):
            n["plan"] = act
            ok = True
        if ok and act not in ("borrow_food",):
            self.log_event("decision", [n["id"]], f"{n['name']}決定{self._act_zh(act, res, qty)}——{why}",
                           notable=(act not in ("rest", "tavern")), detail=trace)
        elif not ok:
            self.log_event("rejected", [n["id"]], f"{n['name']}想{self._act_zh(act, res, qty)}但條件不符（世界法則退回）——{why}",
                           notable=True, detail=trace)

    @staticmethod
    def _act_zh(act, res, qty):
        return {"sell": f"賣出 {qty:.0f} {res}", "buy": f"購買 {qty:.0f} {res}", "gift": f"贈送 {qty:.0f} {res}",
                "tavern": "去酒館", "rest": "好好休息", "work_extra": "加班幹活", "borrow_food": "借糧"}.get(act, str(act))

    def _food_market(self):
        # 鹽的流通：家裡有鮮肉沒鹽就去雜貨店買（讓肉醃得起來，不再整批壞掉）
        for hid, h in self.hh.items():
            if h["stocks"].get("肉", 0) > 1 and h["stocks"].get("鹽", 0) < 0.5:
                self.trade(hid, "h04", "鹽", 1, self.prices["鹽"])
        # 缺糧戶自動求糧（規則後備；LLM 決策優先已在前面跑過）
        for hid, h in self.hh.items():
            if self._hh_food_days(hid) < 3:
                got = self.trade(hid, "granary", "穀物", 20, self.prices["穀物"])
                if got < 5 and hid != "h04":
                    # 糧商零售（進口成本 1.5×錨 → 零售 ~1.9×錨：飢荒糧就是貴，這是設計不是 bug）
                    got += self.trade(hid, "h04", "穀物", 15, self.anchors["穀物"] * 1.9)
                if got < 5:
                    # 糧倉也空了 → 向存糧充裕的戶按市價買（真正的村內糧市）
                    for other in self.hh:
                        if other != hid and self._hh_food_days(other) > 45:
                            got += self.trade(hid, other, "穀物", 15, self.prices["穀物"])
                            if got >= 10:
                                break
                if got < 5 and h["money"] < 10 and self.granary.get("穀物", 0) > 0:
                    # 青苗貸（義倉機制）：無錢戶向公倉借糧，秋收後以實物償還 110%
                    amt = min(15.0, self.granary["穀物"])
                    self.granary["穀物"] -= amt
                    h["stocks"]["穀物"] += amt
                    h.setdefault("grain_debt", 0.0)
                    h["grain_debt"] += amt * 1.1
                    got += amt
                    if self.day % 10 == 0:
                        self.log_event("grain_loan", [h["head"]], f"{h['name']}向公倉借糧度日（累計欠 {h['grain_debt']:.0f} 份）", notable=True)
                if got < 5:
                    for other in self.hh:
                        if other != hid and self._hh_food_days(other) > 14:
                            if self.rel_get(h["head"], self.hh[other]["head"]) >= 10:
                                amt = min(15, self.hh[other]["stocks"].get("穀物", 0) * 0.1)
                                self.hh[other]["stocks"]["穀物"] -= amt
                                h["stocks"]["穀物"] += amt
                                self.rel_add(h["head"], self.hh[other]["head"], 3)
                                self.log_event("mutual_aid", [h["head"], self.hh[other]["head"]],
                                               f"{h['name']}缺糧，{self.hh[other]['name']}接濟了 {amt:.0f} 份穀物")
                                break
        # 獵戶賣鮮肉（家裡鹽漬肉夠吃就多賣——肉是村莊蛋白質來源，不是獵戶的收藏品）
        h9 = self.hh["h09"]
        keep = 2 if h9["stocks"].get("鹽漬肉", 0) > 10 else 6
        surplus = h9["stocks"].get("肉", 0) - keep
        if surplus > 0:
            self.trade("h03", "h09", "肉", min(surplus, 4), self.prices["肉"])
            buyers = [hid for hid in self.hh if hid != "h09" and self.hh[hid]["money"] > 10]
            self.rng.shuffle(buyers)
            for b in buyers[:5]:
                surplus = h9["stocks"].get("肉", 0) - keep
                if surplus <= 0:
                    break
                self.trade(b, "h09", "肉", min(2, surplus), self.prices["肉"])

    def _tavern_social(self):
        M = self.P["needs"]["mood"]
        goers = []
        for n in self.alive_npcs():
            if n["labor"] == 0.5 and n["age"] < 16:
                continue
            if self.day < n["sick_until"]:
                continue   # 臥床是生理限制，不是選擇
            if n["plan"] == "rest":
                n["plan"] = None
                continue   # LLM 決定今晚不出門（例如疫病期間的自我判斷）
            p = 0.3
            if "好酒" in n["traits"]:
                p *= 2
            if "孤僻" in n["traits"]:
                p *= 0.5
            if n["at_tavern"]:
                p = 1.0
            n["at_tavern"] = False
            if self.hh[n["hh"]]["money"] >= 2 and self.rng.random() < min(p, 0.95):
                if self.hh["h03"]["stocks"].get("麥酒", 0) >= 1:
                    self.trade(n["hh"], "h03", "麥酒", 1, self.prices["麥酒"])
                    self.hh[n["hh"]]["stocks"]["麥酒"] = max(0, self.hh[n["hh"]]["stocks"].get("麥酒", 0) - 1)
                    self.demand["麥酒"] += 1
                    n["mood"] = min(100, n["mood"] + M["tavern"])
                    n["loc"] = "tavern_loc"
                    goers.append(n)
        # 隨機同席 → 關係微增；LLM 生成有內容的互動（預算內）
        self.rng.shuffle(goers)
        pairs = [(goers[i], goers[i + 1]) for i in range(0, len(goers) - 1, 2)]
        budget = self.P["llm"]["max_social_per_day"] if self.llm else 0
        for a, b in pairs:
            if budget > 0:
                budget -= 1
                evs = [e for e in getattr(self, "recent_events", []) if e["notable"]][-6:]
                ctx = (f"{a['name']}（{a['job']}，性格{'、'.join(a['traits'])}）與"
                       f"{b['name']}（{b['job']}，性格{'、'.join(b['traits'])}）在酒館同席。"
                       f"兩人關係值 {self.rel_get(a['id'], b['id']):+.0f}（-100~100）。\n"
                       f"近期村中事件（可提起）：{json.dumps([{'id': e['id'], 'text': e['text']} for e in evs], ensure_ascii=False)}")
                out = self.llm.social(ctx)
                if out:
                    d = max(-3, min(3, int(out.get("rel_delta", 0))))
                    self.rel_add(a["id"], b["id"], d)
                    rid = out.get("rumor_event_id")
                    valid = {e["id"] for e in evs}
                    caused = [rid] if rid in valid else []
                    if rid and rid in valid:
                        for e in self.recent_events:
                            if e["id"] == rid:
                                e["known_by"] = sorted(set(e["known_by"]) | {a["id"], b["id"]})
                    self.log_event("tavern_talk", [a["id"], b["id"]], out.get("line", "")[:120], caused_by=caused,
                                   notable=bool(caused) or abs(d) >= 2,
                                   detail=f"【當時情境】\n{ctx}\n\n【LLM 輸出】\n{json.dumps(out, ensure_ascii=False, indent=1)}")
                    continue
            self.rel_add(a["id"], b["id"], 1)

    # ---------- 夜間：消耗/恢復/事件/結算 ----------
    def _night(self):
        N = self.P["needs"]
        for hid, h in self.hh.items():
            members = [self.npc[m] for m in h["members"] if self.npc[m]["alive"]]
            if not members:
                continue
            need = sum(self._need(m) for m in members)
            got = 0.0
            for res in FOOD_RES:
                if got >= need:
                    break
                v = N["meat_food_value"] if res in ("肉", "鹽漬肉") else 1.0
                take = min(h["stocks"].get(res, 0), (need - got) / v)
                h["stocks"][res] -= take
                self.demand["穀物" if res == "穀物" else "肉"] += take
                got += take * v
                if res == "肉" and take > 0:
                    for m in members:
                        m["mood"] = min(100, m["mood"] + N["mood"]["meat_meal"])
            ratio = got / need if need else 1.0
            for m in members:
                if ratio < 0.5:
                    m["stamina"] = max(0, m["stamina"] + N["hunger"]["half_ration_stamina"])
                    m["mood"] += N["mood"]["missed_meal"]
                    m["hunger_streak"] += 1
                else:
                    m["hunger_streak"] = 0
                if m["hunger_streak"] >= 7:
                    # 個體化：體質差異讓死亡錯落，不會全村同日倒下
                    m["health"] = max(0, m["health"] + N["hunger"]["streak7_health"] * self.rng.uniform(0.5, 1.5))
                rec = N["stamina"]["recover_fed"] if ratio >= 0.9 else N["stamina"]["recover_hungry"]
                if m["mood"] > 70:
                    rec += N["stamina"]["mood_bonus"]
                m["stamina"] = min(100, m["stamina"] + rec)
                m["mood"] += (50 - m["mood"]) * 0.02
                if m["injured_until"] <= self.day and m["sick_until"] <= self.day:
                    m["health"] = min(100, m["health"] + 2)
            if ratio < 0.5:
                heid = self.log_event("hunger", [m["id"] for m in members], f"{h['name']}今晚只吃了半飽（存糧見底）", notable=True)
                self.last_hunger_eid = heid
                for m in members:
                    if m["hunger_streak"] >= 5:
                        m["last_harm"] = heid
            # 鮮肉過夜處理：有鹽醃起來，沒鹽壞掉
            meat = h["stocks"].get("肉", 0)
            if meat > 0:
                salt_need = meat * 0.1
                if h["stocks"].get("鹽", 0) >= salt_need:
                    h["stocks"]["鹽"] -= salt_need
                    h["stocks"]["鹽漬肉"] += meat
                else:
                    can = h["stocks"].get("鹽", 0) / 0.1
                    h["stocks"]["鹽漬肉"] += can
                    h["stocks"]["鹽"] = 0
                    if meat - can > 1:
                        self.log_event("spoilage", [h["head"]], f"{h['name']}有 {meat-can:.0f} 份鮮肉因沒鹽醃而壞掉", notable=False)
                h["stocks"]["肉"] = 0
            # 冬季柴火
            if self.season == "冬":
                use = N["firewood_per_household_winter"] * (self.P["weather"]["cold_snap_firewood_mult"] if self.temp == "酷寒" else 1.0)
                self.demand["木柴"] += use
                if h["stocks"].get("木柴", 0) >= use:
                    h["stocks"]["木柴"] -= use
                else:
                    h["stocks"]["木柴"] = 0
                    for m in members:
                        m["health"] = max(0, m["health"] - 2)
        # 疫病後續：二次傳染（同戶看護者）與康復
        sick_now = [n for n in self.alive_npcs() if self.day < n["sick_until"] and n.get("sick_src")]
        for sn in sick_now:
            # 隔離的物理後果：家內傳染大減（1%），但照護者草藥師自己暴露（2%）——決策的代價是真實的
            if sn.get("quarantined"):
                healer = self.npc.get("npc_12_1")
                if (healer and healer["alive"] and self.day >= healer["sick_until"]
                        and not healer.get("sick_src") and self.rng.random() < 0.02):
                    healer["sick_until"] = self.day + self.rng.randint(8, 14)
                    healer["health"] = max(0, healer["health"] - 10)
                    healer["sick_src"] = sn["sick_src"]
                    healer["last_harm"] = sn["sick_src"]
                    self.log_event("infection", ["npc_12_1", sn["id"]],
                                   f"🤒 日夜照護病患的梅姑自己也病倒了——隔離救了各家，卻賠上了醫者",
                                   caused_by=[sn["sick_src"]])
            hh_risk = 0.01 if sn.get("quarantined") else 0.04
            for mid in self.hh[sn["hh"]]["members"]:
                m = self.npc[mid]
                if (m["alive"] and m["id"] != sn["id"] and self.day >= m["sick_until"]
                        and not m.get("sick_src") and self.rng.random() < hh_risk):
                    m["sick_until"] = self.day + self.rng.randint(8, 14)
                    m["health"] = max(0, m["health"] - 10)
                    m["sick_src"] = sn["sick_src"]
                    m["quarantined"] = sn.get("quarantined", False)
                    m["last_harm"] = sn["sick_src"]
                    self.log_event("infection", [m["id"], sn["id"]],
                                   f"🤒 {m['name']}照顧病中的{sn['name']}後也病倒了（家內傳染）",
                                   caused_by=[sn["sick_src"]])
        for n in self.alive_npcs():
            if n.get("sick_src") and self.day >= n["sick_until"]:
                self.log_event("recover", [n["id"]], f"💪 {n['name']}病癒下床，恢復勞作",
                               caused_by=[n["sick_src"]], notable=True)
                n["sick_src"] = None
                n["quarantined"] = False
        # 工具耐久
        for it in self.registry.values():
            if it.get("type") in ("鋤具", "獵弓", "鎬") and it.get("durability", 0) > 0:
                it["durability"] -= 1
        self._deaths()
        self._random_events()
        self._fixed_schedule()
        self._weekly_prices()
        if self.doy == self.P["time"]["days_per_year"]:
            self._year_end()

    def _deaths(self):
        for n in self.alive_npcs():
            if n["health"] <= 0:
                n["alive"] = False
                h = self.hh[n["hh"]]
                if n.get("oldage"):
                    txt = f"⚰️ {n['name']}（{n['age']} 歲，{n['job']}）在睡夢中安然離世"
                    cb = []
                else:
                    txt = f"⚰️ {n['name']}（{n['age']} 歲，{n['job']}）去世了"
                    cb = [n["last_harm"]] if n.get("last_harm") else []
                self.log_event("death", [n["id"]], txt, caused_by=cb)
                for m in h["members"]:
                    if m != n["id"] and self.npc[m]["alive"]:
                        self.npc[m]["mood"] -= 30
                if h["head"] == n["id"]:
                    alive = [m for m in h["members"] if self.npc[m]["alive"]]
                    if alive:
                        h["head"] = max(alive, key=lambda m: self.npc[m]["age"] * self.npc[m]["labor"])
                        self.log_event("inheritance", [h["head"]], f"{self.npc[h['head']]['name']}成為{h['name']}的新戶主", caused_by=[])

    def _random_events(self):
        E = self.P["events"]
        if self.day >= self.wolf_until:
            p = E["wolf"]["base_daily"] * (3 if self.season == "冬" else 1)
            if self.rng.random() < p:
                dur = self.rng.randint(*E["wolf"]["duration"])
                self.wolf_until = self.day + dur
                why = "寒冬獵物稀少，狼群下山覓食" if self.season == "冬" else "森林深處的狼群向獵場移動"
                self.wolf_eid = self.log_event("wolf", [], f"🐺 狼群在森林附近出沒——{why}（獵人風險上升，約 {dur} 天）")
        if self.season == "春" and self.rng.random() < E["plague"]["base_daily_spring"]:
            self._plague_outbreak(E)

    def _plague_outbreak(self, E):
        """疫病不擲「誰病了」的骰子——先定病源（世界真相），再沿真實接觸面傳播。
        村民不知道真相；草藥師診治後「起疑」（認知事件，可能對也可能錯）。"""
        source = self.rng.choice(["well", "traveler"])
        zero = self.rng.choice(self.alive_npcs())
        chain = [(zero, "病源（最先發病）")]
        # 同戶必然暴露
        for m in self.hh[zero["hh"]]["members"]:
            n = self.npc[m]
            if n["alive"] and n["id"] != zero["id"] and self.rng.random() < 0.6:
                chain.append((n, f"與{zero['name']}同戶"))
        # 昨晚酒館同席者
        for nid, loc in getattr(self, "prev_evening", {}).items():
            n = self.npc.get(nid)
            if n and n["alive"] and loc == "tavern_loc" and n["id"] not in {c[0]["id"] for c in chain}:
                if self.rng.random() < (0.35 if getattr(self, "_zero_at_tavern", True) else 0.1):
                    chain.append((n, "昨晚與病源在酒館同席"))
        kmax = self.rng.randint(*E["plague"]["victims"])
        chain = chain[:max(2, kmax)]
        for v, _ in chain:
            v["sick_until"] = self.day + self.rng.randint(*E["plague"]["sick_days"])
            v["health"] = max(0, v["health"] - 15)
        names = "、".join(v["name"] for v, _ in chain)
        src_truth = {"well": "融雪期公共水井受污染（上游獸屍）", "traveler": "上次商隊裡的病旅人把病帶進了村莊"}[source]
        detail = (
            f"【世界真相（Canon，村民不可見）】\n病源：{src_truth}\n\n【傳播鏈】\n"
            + "\n".join(f"  {'└' if i == len(chain) - 1 else '├'}─ {v['name']}：{how}" for i, (v, how) in enumerate(chain))
            + "\n\n【村民認知】\n  村長：不知情\n  草藥師：診治後將形成自己的懷疑（見後續事件）\n  其他村民：只知道有人病倒了"
        )
        eid = self.log_event("plague", [v["id"] for v, _ in chain],
                             f"🤒 疫病爆發：{names}入夜後相繼病倒，臥床不起（點擊查看病因調查）", detail=detail)
        for v, _ in chain:
            v["last_harm"] = eid
            v["sick_src"] = eid
            v["quarantined"] = False   # 是否隔離不是規則說了算——那是村長/草藥師的決策（LLM 抉擇點）
        self.plague_eid = eid
        # 掛起危機決策：村莊怎麼應對，明晚由 LLM 扮演的當事人定奪（純規則模式=無人組織，病患各自在家）
        self.pending_crisis = {"type": "plague", "eid": eid, "victims": [v["id"] for v, _ in chain],
                               "truth": src_truth, "source": source}

    def _fixed_schedule(self):
        T = self.P["time"]
        # 守衛薪資
        if self.day % T["caravan_interval"] == 0:
            wage = self.P["economy"]["guard_wage"] * 2
            if self.treasury >= wage:
                self.treasury -= wage
                self.hh["h11"]["money"] += wage
                self.ledger["h11"].append((self.day, "守衛薪資", wage))
            else:
                self.log_event("treasury_broke", ["npc_01_1"], f"⚠️ 村庫付不出守衛薪資（餘額 {self.treasury:.0f}G）")
        # 商隊
        if self.day >= self.caravan_due:
            if self.weather in ("暴風", "雪") and self.season == "冬":
                self.caravan_due += 1
            else:
                self._caravan()
                self.caravan_due = self.day + T["caravan_interval"]
        # 秋稅 + 青苗貸償還
        if self.doy == T["tax_day"]:
            total, repaid = 0.0, 0.0
            for h in self.hh.values():
                cut = h["stocks"].get("穀物", 0) * self.P["economy"]["tax_rate"]
                h["stocks"]["穀物"] -= cut
                self.granary["穀物"] += cut
                total += cut
                debt = h.get("grain_debt", 0.0)
                if debt > 0:
                    pay = min(debt, h["stocks"].get("穀物", 0) * 0.3)
                    h["stocks"]["穀物"] -= pay
                    self.granary["穀物"] += pay
                    h["grain_debt"] = debt - pay
                    repaid += pay
            msg = f"秋稅入倉：{total:.0f} 份穀物（稅率 {self.P['economy']['tax_rate']:.0%}）"
            if repaid:
                msg += f"；青苗貸償還 {repaid:.0f} 份"
            self.log_event("tax", ["npc_01_1"], msg)
        # 帳本修剪（保留 90 天窗口）
        if self.day % 30 == 0:
            for hid in list(self.ledger):
                self.ledger[hid] = [e for e in self.ledger[hid] if e[0] > self.day - 90]
        # 月度審計（守恆不變量）
        if self.day % 30 == 0:
            village = sum(h["money"] for h in self.hh.values()) + self.treasury
            drift = village - (self.initial_money + self.external_net)
            assert abs(drift) < 1.0, f"貨幣守恆破裂 day{self.day}: drift={drift:.2f}"

    def _caravan(self):
        C = self.P["economy"]["caravan_buy_caps"]
        bought = []
        for res, cap in C.items():
            if res not in self.prices:
                continue
            remain = cap if cap > 0 else 9999
            for hid in self.hh:
                if remain <= 0:
                    break
                have = self.hh[hid]["stocks"].get(res, 0)
                keep = 30 if res == "鐵礦" and hid == "h10" else 0
                sell = min(max(0, have - keep), remain)
                if sell >= 1:
                    got = self.trade("EXT", hid, res, sell, self.prices[res] * 0.95)
                    remain -= got
                    if got:
                        bought.append(f"{res}×{got:.0f}")
        # 賣給村莊（經雜貨商）
        store = self.hh["h04"]["stocks"]
        for res, top in (("鹽", 20), ("布", 15)):
            gap = top - store.get(res, 0)
            if gap > 0:
                self.trade("h04", "EXT", res, gap, self.prices[res] * 0.9)
        if self.granary.get("種糧", 0) < 110:
            need = 110 - self.granary["種糧"]
            cost = need * self.prices["種糧"]
            if self.treasury >= cost:
                self.treasury -= cost
                self.external_net -= cost
                self.granary["種糧"] += need
        # 飢荒進口閥門一：村庫出錢買糧入公倉（平準機制）
        G = self.P["economy"].get("grain_import", {"price_mult": 2.0, "per_visit_cap": 600, "trigger_days": 45})
        village_grain = self.granary.get("穀物", 0) + sum(h["stocks"].get("穀物", 0) for h in self.hh.values())
        daily_need = sum(self._need(n) for n in self.alive_npcs())
        imported = 0.0
        if village_grain < daily_need * G["trigger_days"]:
            want = min(G["per_visit_cap"], daily_need * 60 - village_grain)
            price = self.anchors["穀物"] * G["price_mult"]
            afford = self.treasury / price if price > 0 else 0
            imported = max(0.0, min(want, afford))
            if imported >= 1:
                self.treasury -= imported * price
                self.external_net -= imported * price
                self.granary["穀物"] += imported
        # 飢荒進口閥門二：糧商零售——雜貨商用自己的錢批發糧，加價轉賣（全村現金→糧食的主管道）
        if village_grain + imported < daily_need * G["trigger_days"]:
            wholesale = self.anchors["穀物"] * 1.5
            cap = min(300.0, self.hh["h04"]["money"] * 0.8 / wholesale)
            buy = max(0.0, min(cap, daily_need * 60 - village_grain - imported))
            if buy >= 1:
                self.hh["h04"]["money"] -= buy * wholesale
                self.external_net -= buy * wholesale
                self.hh["h04"]["stocks"]["穀物"] += buy
                imported += buy
        msg = f"🚚 商隊抵達：收購 {('、'.join(bought)) if bought else '無'}；補給鹽/布/種糧"
        if imported >= 1:
            msg += f"；村莊糧荒，村庫高價進口 {imported:.0f} 份穀物"
        self.log_event("caravan", ["npc_04_1"], msg, notable=True)

    def _weekly_prices(self):
        if self.day % self.P["economy"]["reprice_interval_days"]:
            return
        lo, hi = self.P["economy"]["price_clamp"]
        win = [t for t in self.trades if t[0] > self.day - self.P["economy"]["price_anchor_window_days"]]
        old = dict(self.prices)
        for res in self.prices:
            recent = [(q, p) for (_, r, q, p) in win if r == res]
            if recent:
                avg = sum(q * p for q, p in recent) / max(1e-9, sum(q for q, _ in recent))
                self.anchors[res] = 0.8 * self.anchors[res] + 0.2 * avg
            # 供給端 = 本週產出 + 全村庫存緩衝（庫存厚 → 價穩；庫存見底 → 稀缺定價）
            d = self.demand.get(res, 0)
            s = self.supply.get(res, 0) + self._stock_total(res) * 0.05
            ratio = max(lo, min(hi, (d + 1) / (s + 1)))
            target = max(0.1, self.anchors[res] * ratio)
            # 調價黏性（世界規則）：商人一週最多調價 ±15%——劇烈供需變化以連續數週的方向性移動呈現
            stick = self.P["economy"].get("price_stickiness", 0.15)
            self.prices[res] = round(min(max(target, old[res] * (1 - stick)), old[res] * (1 + stick)), 2)
        # 物價劇變 → 落盤事件並接因果（漲跌不是憑空，是供需的下游）
        for res in ("穀物", "肉", "木柴"):
            if old[res] <= 0:
                continue
            chg = self.prices[res] / old[res] - 1
            if abs(chg) >= 0.12:   # 黏性上限 15%，週變 ≥12% 即代表強供需壓力，落盤
                cb = []
                if res == "肉" and self.wolf_eid and self.day < self.wolf_until + 7:
                    cb = [self.wolf_eid]
                elif res == "穀物" and chg < 0 and self.harvest_eid and 0 <= self.doy - self.P["time"]["harvest_window"][0] <= 30:
                    cb = [self.harvest_eid]
                elif res == "穀物" and chg > 0 and self.last_hunger_eid:
                    cb = [self.last_hunger_eid]
                self.log_event("price_change", [],
                               f"📈 {res}價{'上漲' if chg > 0 else '下跌'} {abs(chg):.0%}（{old[res]:.1f} → {self.prices[res]:.1f}G）",
                               caused_by=cb, notable=True)
        self.demand.clear()
        self.supply.clear()

    def _year_end(self):
        deaths = []
        for n in self.alive_npcs():
            n["age"] += 1
            a = n["age"]
            p = 0.003 if a < 55 else 0.008 if a < 60 else 0.02 + (a - 60) * 0.004 if a < 70 else 0.06 + (a - 70) * 0.009 if a < 80 else 0.15
            if n["health"] < 50:
                p *= 1.5
            if self.rng.random() < p:
                n["health"] = 0
                n["oldage"] = n["age"] >= 55 and not n.get("last_harm")
                deaths.append(n["name"])
        self.log_event("year_end", [], f"第 {self.year} 年結束。人口 {len(self.alive_npcs())}，村庫 {self.treasury:.0f}G，糧倉 {self.granary.get('穀物',0):.0f} 份穀物")
        self.growth_pts = 0.0
        self.care_mandays = 0.0
        self._deaths()

    # ---------- 收尾 ----------
    def snapshot(self):
        return {
            "day": self.day, "year": self.year, "season": self.season,
            "population": len(self.alive_npcs()),
            "treasury": round(self.treasury, 1),
            "granary": {k: round(v, 1) for k, v in self.granary.items()},
            "external_net": round(self.external_net, 1),
            "money_total": round(sum(h["money"] for h in self.hh.values()) + self.treasury, 1),
            "prices": self.prices,
            "households": {hid: {"money": round(h["money"], 1),
                                 "food_days": round(self._hh_food_days(hid), 1),
                                 "members_alive": sum(1 for m in h["members"] if self.npc[m]["alive"])}
                           for hid, h in self.hh.items()},
            "llm_calls": getattr(self.llm, "calls", 0), "llm_failures": getattr(self.llm, "failures", 0),
        }

    def close(self):
        with open(self.out / "state.json", "w", encoding="utf-8") as f:
            json.dump(self.snapshot(), f, ensure_ascii=False, indent=1)
        self.events_f.close()
