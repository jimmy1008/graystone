# 灰石村（Graystone）— AI 文字 MMO 的世界模擬器

> 一個**沒有玩家也會自己活**的 30 人山谷聚落。這是文字 MMO 構想的第一塊基石：先證明世界能自行運轉出歷史，再談玩家、再談 MMO。

## English Summary

**Graystone** is a world simulator for a 30-person valley village that lives on its own — no players, no scripts. It is the first building block of a text-MMO concept: prove the world can generate its own history before adding players.

- **Anti-hallucination architecture**: the local LLM (Ollama qwen3:8b) can only *propose* actions and write individual NPC *beliefs* — world *facts* are written exclusively by the rule engine. If the LLM is offline, the world degrades gracefully to pure-rules mode and keeps running.
- **Zero pre-scripted plot**: famines emerge from seasonal weather integrals, inflation from caravan money flow, gossip spreads through the tavern (a mine-collapse rumor took six in-world days to travel from a miner's household to the village chief). If a year of simulation produces no stories, that's a system failure — fix the system, not the script.
- **Conservation audits**: money and goods are strictly conserved and audited every tick; year one validated with 29/30 survival and ~9,800-unit autumn harvest.
- **Live map view** (`map_view.py`): a tkinter window with village map, per-profession colored agents walking a four-phase daily routine, real-time economy panels, and an NPC dialogue/event stream.
- Every tunable number lives in `data/params.yaml` — the engine has zero magic numbers. Design docs (world rules, economy, NPC decision rules, canon layering) are in `docs/` (Chinese).

Run it: `python run.py --days 365 --quiet` (pure rules, ~0.1s/year) or `--llm on` for full LLM mode.

## 目前狀態：M1 引擎可跑（2026-08-09）

```powershell
cd C:\dev\graystone
.venv\Scripts\python run.py --days 365 --quiet          # 純規則模式（0.1 秒/年）
.venv\Scripts\python run.py --days 30 --llm on          # 全員 LLM 模式（qwen3:8b，~5s/呼叫）
.venv\Scripts\pythonw.exe map_view.py                   # 🗺 地圖觀察窗（獨立視窗，見下）
.venv\Scripts\pythonw.exe map_view.py --llm on          # 地圖 + LLM（每天多幾十秒思考時間）
# 產出：out\events.jsonl（事件史）、out\state.json（末態快照）
```

**地圖觀察窗**（`map_view.py`）：左欄整體數據（人口/村庫/物價/各戶存糧金錢/狼群礦坑狀態），中間村莊地圖——人物是按職業著色的小點，每天走完「清晨出門上工 → 白天各就其位 → 傍晚陸陸續續收工回家或進酒館 → 夜間就寢」四段動畫（森林/三塊田/礦坑/鐵匠鋪/酒館/12 戶民居都在圖上），右欄 NPC 對話與事件文字流。控制列可暫停、調速（0.25×~4×）、切換顯示人名。

**已驗證**：第一年 29/30 人存活、秋收 ~9,800 份、貨幣守恆審計全程通過；LLM 模式下 NPC 依性格對真實事件做出反應並在酒館傳播（塌方消息六天內從礦工家傳到村長耳裡）。第二年起因天氣歉收出現饑荒週期與人口衰退——生育/遷入未實作（M2）之前這是預期行為。

**調參戰史**（一天內抓到的世界級 bug，全部已修）：初版 163 天糧盡→秋收無人能下田→276 天滅村。依序修了：釀酒偷糧、鮮肉無鹽全腐、鹽漬肉只囤不吃、獵戶惜售肉不進市場、無錢戶抱著空錢包餓死在滿倉糧旁（→青苗貸/義倉機制）、春季開局存糧違反農業現實（120/人→150/人）。每一刀都是機制或參數，不是硬補丁。

## 讀法

| 想知道 | 看 |
|---|---|
| 這是什麼、設計鐵律 | `docs/00_世界觀.md` |
| 世界怎麼動（時間/生產/需求/經濟/決策/事件） | `docs/01` ~ `07` |
| LLM 幻覺怎麼被關在籠子裡 | `docs/08_Canon與資料分層.md`（含零預埋劇本原則） |
| **為什麼沒有東西是寫死的** | `docs/09_動態系統.md`（優先級最高，覆蓋 01~07 的固定數值） |
| 小概率奇遇的定奪規則 | `docs/10_機緣系統.md`（LLM 定奪；嚴禁先射箭後畫靶） |
| 所有可調參數的正本 | `data/params.yaml`（引擎程式碼零魔法數字） |
| 第 0 天的世界快照 | `data/` 其餘檔案（名冊/戶口/地點/物品/物價/庫存/關係——**無任何預埋劇情**） |
| 資料表設計 | `schema.sql`（設計稿，未建庫） |

## 核心架構決策（三句話版）

1. **世界法則 > NPC > 玩家**：品級上限、守恆律、Registry 鐵律不可變；其他一切是參數。
2. **LLM 永不直寫世界**：Phase 2 接入的本地模型（qwen3:8b）只能提出 ACTION 提案與寫入個別 NPC 的 BELIEF；FACT 只有引擎能寫。
3. **能湧現的不擲骰，劇情零預埋**：歉收來自整季天氣積分、通膨來自商隊金流；世界開局沒有任何劇本——恩怨、秘密、故事全部由人物的關係、行為、話語在模擬中長出來（跑一年沒長出故事＝系統失敗，修系統而不是補劇本）。

## 第一年紙上推演（引擎階段的預期基線）

以 `params.yaml` 初始值靜態試算（天氣取期望值）：

### 糧食收支
- 全村日需求 28.8 食物單位（25 成人＋3 少年＋2 老人）＝ 全年 10,368
- 獵戶日產肉 6 份 ≈ 9 單位 → 穀物日耗 ≈ 20 份
- **開局存糧 5,400**（各戶 3,600＋糧倉 1,800）÷ 20 ＝ 270 天 > 秋收第 241 天 ✅ 緩衝 ~6%
  - ⚠️ 推演發現：直覺值「1,100 份存糧」差了 5 倍——集中收成制下，開局存糧必須撐 255 天，這就是「糧倉為什麼是村莊命脈」的數學版
- 秋收基線：12 全職農民 ×15 天 ×45 ＋ 5 幫手 ×15×22 ≈ **9,750 穀物**（天氣係數 1.0、地力 1.0）
- 年需求：食用 7,200 ＋ 釀酒 ~400 ＋ 種糧 110 ≈ 7,710 → **產出/需求 ≈ 1.26**，一次歉收（×0.8）後 ≈ 1.01——剛好貼地，這是刻意的張力設定
- 冬季柴火：12 戶 ×2×90 ＝ 2,160 捆；冬季閒置農力 12 人 × 日產 5 ＝ 60/天 ≫ 日耗 24 ✅

### 貨幣與村庫
- 開局村內 3,000G（各戶 2,700＋村庫 300），守恆式審計
- 村庫年支出（守衛薪資）480G vs 年收入（5% 稅糧 ~488 份折現）~500G → **勉強自持，一次歉收即赤字**（刻意脆弱點，Phase 2 村長的核心決策題）
- ⚠️ 推演發現（已用設定解決一半）：礦石出口曾推演出年流入 ~3,600G ≈ 再造一個貨幣總量。**「技術落後只能挖淺層」設定**（docs/03）把日產壓到 3、淺層儲量僅 9,000 塊（~5 年見底）、商隊收購上限 100/次 → 年流入降至 ~2,000G，且逐年遞減（0.95 貧化係數）。殘餘通膨壓力交給滾動錨物價自然消化，M2 驗證。副作用是設計好的**第一場結構性危機**：礦脈見底 → 礦工失業 → 轉職/遷出鏈
- 珍稀發現（0.5%/礦工日 ≈ 每年 3~4 次）：銀礦石/寶石原石/古物——低頻高價收入 + 「挖到寶」的酒館話題 + 古物必掛敘事鉤子（深層有什麼？）

### 預期的第一年劇本（如果引擎跑出類似節奏，代表系統健康）
春播 → 青黃不接的物價小高峰 → 夏季天氣決定收成 → 秋收＋徵稅＋物價崩落 → 商人囤貨 → 冬季狼群/暴雪壓力 → 酒館旺季（情報傳播峰值）→ 年末審計。10 年尺度則應看到：礦脈衰減、地力輪動、婚喪嫁娶、人口 ±5、至少一次職業斷層危機。

## 驗收標準（365 天模擬，M2）
1. 無人餓死（或餓死原因可歸因到具體事件鏈，非規則 bug）
2. 貨幣守恆審計全過；物價不長期貼 clamp 邊界
3. 資源無憑空增減；個別物品流轉可全程追溯
4. 事件日誌是「故事」而非流水帳（抽查 10 條事件都有因果邊）
5. 不存在的人物 0 次被引用（registry 驗證 0 違規）

## 路線圖
- **M0 ✅** 規則書＋初始資料＋schema 設計
- **M1 ✅** 引擎可跑：時間迴圈＋天氣馬可夫鏈＋生產經濟＋事件史＋**全員 LLM 抉擇點**（qwen3:8b，離線退化純規則）；第一年存活驗證通過
- **M2** 人口動態（生育/婚配/遷徙——止住人口單向衰退）＋饑荒週期調參＋SQLite 持久化＋LLM 整年跑（~3,300 呼叫/年 ≈ 一個晚上）
- **M3** 情報傳播完整化（known_by 嚴格化——目前簡化版）＋發展系統（開墾/建屋/技能成長）＋機緣系統啟用（docs/10）
- **M4** 觀察介面（把 events.jsonl 變成可讀的村莊編年史）＋多年連跑的歷史一致性驗收
- **M5+** 多村莊 → 玩家 → MMO（見原始構想對話，遠期）

## 技術備忘
- 引擎：Python + SQLite，專屬 `.venv`（**不要用 PATH 上的 Store Python**）
- LLM：Ollama qwen3:8b（5.2GB，RTX 3070 Ti 8GB 裝得下），按需啟停，跑完 `ollama stop` 還 GPU
- 模擬與遊戲徹底分離：世界是結構化資料，「文字介面」只是未來讀取世界的一扇窗
