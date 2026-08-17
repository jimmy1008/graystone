-- ═══════════════════════════════════════════════════════════
-- 灰石村 SQLite 資料表設計稿（僅設計，本階段不建庫）
-- 對應 docs/08 資料分層 + docs/09 動態系統
-- 原則：FACT 由引擎寫入；beliefs 只掛在個別 NPC；LLM 永不直寫任何 FACT 表
-- ═══════════════════════════════════════════════════════════

-- ── 人物唯一真相（含歷史人物/死者/離村者；人只能經 出生/遷入 進入）──
CREATE TABLE character_registry (
    id          TEXT PRIMARY KEY,          -- npc_01_1 / hist_01 / born_Y3_001
    name        TEXT NOT NULL,
    sex         TEXT,
    born_year   INTEGER NOT NULL,          -- 相對年（Y-20 = -20）
    died_year   INTEGER,                   -- NULL=在世
    status      TEXT NOT NULL,             -- alive / dead / departed / transient(商隊過境)
    origin      TEXT NOT NULL,             -- seed / birth / immigration
    household   TEXT REFERENCES households(id),
    notes       TEXT
);

-- ── NPC 動態狀態（僅在世常駐者）──
CREATE TABLE npcs (
    id          TEXT PRIMARY KEY REFERENCES character_registry(id),
    age         INTEGER NOT NULL,
    labor       TEXT NOT NULL,             -- full / half / none
    job_main    TEXT,
    job_side    TEXT,
    skills      TEXT NOT NULL DEFAULT '{}',-- JSON {"農耕":3}
    aptitude    INTEGER NOT NULL DEFAULT 3,-- 技能上限（docs/09.C）
    skill_progress TEXT NOT NULL DEFAULT '{}', -- JSON 累計有效工作日
    traits      TEXT NOT NULL DEFAULT '[]',-- JSON，可漂移（docs/09.E）
    money       INTEGER NOT NULL DEFAULT 0,
    stamina     INTEGER NOT NULL DEFAULT 100,
    health      INTEGER NOT NULL DEFAULT 100,
    mood        INTEGER NOT NULL DEFAULT 50,
    location    TEXT REFERENCES locations(id),
    condition   TEXT NOT NULL DEFAULT 'normal', -- normal/injured/sick/pregnant/depressed
    condition_until INTEGER                -- 世界日
);

CREATE TABLE households (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    head        TEXT REFERENCES character_registry(id),
    money       INTEGER NOT NULL DEFAULT 0,
    residence   TEXT REFERENCES locations(id),
    active      INTEGER NOT NULL DEFAULT 1 -- 戶可消亡/新立（docs/09.B）
);

-- ── 地點（可新增：開墾/建屋；可荒廢）──
CREATE TABLE locations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,             -- 公共/營業/民居/農田/野外/通路
    danger      INTEGER NOT NULL DEFAULT 0,
    adjacency   TEXT NOT NULL DEFAULT '[]',
    capacity    TEXT,                      -- JSON
    active      INTEGER NOT NULL DEFAULT 1,
    -- 動態屬性（docs/09.C）
    fertility   REAL,                      -- 農田專用
    reserve     INTEGER,                   -- 礦坑剩餘儲量
    extracted   INTEGER DEFAULT 0          -- 累計開採（算枯竭係數）
);

-- ── 散裝庫存（戶/機構 × 資源）──
CREATE TABLE stocks (
    owner       TEXT NOT NULL,             -- household id 或機構 id
    resource    TEXT NOT NULL,
    qty         REAL NOT NULL DEFAULT 0,
    acquired_day INTEGER,                  -- 批次腐壞計算用（肉3天/麥酒90天…）
    PRIMARY KEY (owner, resource, acquired_day)
);

-- ── 個別物品（工具/武器；新增必經製作或商隊）──
CREATE TABLE item_registry (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    type        TEXT NOT NULL,             -- 鋤具/獵弓/鎬/武器
    tier        TEXT,                      -- 引擎依 equipment_tiers 驗證
    stat        TEXT,                      -- JSON
    owner       TEXT NOT NULL,             -- npc 或 household id
    durability  INTEGER,
    tradeable   INTEGER NOT NULL DEFAULT 1,
    created_day INTEGER,                   -- NULL=開局種子
    created_by  TEXT,                      -- 製作者（品級責任追溯）
    destroyed_day INTEGER
);

-- ── 物價（滾動錨，docs/09.D）──
CREATE TABLE prices (
    resource    TEXT NOT NULL,
    day         INTEGER NOT NULL,
    anchor      REAL NOT NULL,             -- 近90日成交均價
    current     REAL NOT NULL,             -- anchor × clamp(需求/供給)
    PRIMARY KEY (resource, day)
);

-- ── 交易流水（審計用，可定期壓縮）──
CREATE TABLE transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         INTEGER NOT NULL,
    seller      TEXT, buyer TEXT,
    resource    TEXT, item_id TEXT,
    qty         REAL, unit_price REAL, total REAL,
    external    INTEGER NOT NULL DEFAULT 0 -- 1=商隊交易（守恆審計分開算）
);

CREATE TABLE debts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    creditor    TEXT NOT NULL, debtor TEXT NOT NULL,
    amount      REAL NOT NULL,             -- 金額或實物折算
    resource    TEXT,
    created_day INTEGER NOT NULL, due_day INTEGER,
    settled_day INTEGER
);

CREATE TABLE relationships (
    a           TEXT NOT NULL, b TEXT NOT NULL,
    value       INTEGER NOT NULL DEFAULT 0,
    canon_anchor TEXT REFERENCES events(id), -- 有錨者不自然衰減（docs/09.E）
    last_interaction_day INTEGER,
    PRIMARY KEY (a, b)
);

-- ── 事件因果圖（Canon 骨架，docs/07 格式）──
CREATE TABLE events (
    id          TEXT PRIMARY KEY,          -- E-Y1-042 / E-H1
    day         INTEGER NOT NULL,          -- 歷史事件為負
    type        TEXT NOT NULL,
    location    TEXT,
    actors      TEXT NOT NULL DEFAULT '[]',
    effects     TEXT NOT NULL DEFAULT '[]',-- 引擎實際執行的狀態改動
    fact        TEXT,                      -- 真相描述（Canon）
    notable     INTEGER NOT NULL DEFAULT 1,
    archived    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE event_causality (
    cause_id    TEXT NOT NULL REFERENCES events(id),
    effect_id   TEXT NOT NULL REFERENCES events(id),
    PRIMARY KEY (cause_id, effect_id)
);

CREATE TABLE event_knowledge (              -- known_by 清單（情報傳播逐日維護）
    event_id    TEXT NOT NULL REFERENCES events(id),
    npc_id      TEXT NOT NULL REFERENCES character_registry(id),
    scope       TEXT NOT NULL DEFAULT 'full', -- full/partial（部分知情，如 E-H1 村長）
    learned_day INTEGER,
    PRIMARY KEY (event_id, npc_id)
);

-- ── NPC 認知層（BELIEF/HYPOTHESIS；可以錯；Phase 2 唯一 LLM 可寫區）──
CREATE TABLE beliefs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    npc_id      TEXT NOT NULL REFERENCES character_registry(id),
    level       TEXT NOT NULL,             -- BELIEF / HYPOTHESIS
    about_event TEXT REFERENCES events(id),
    content     TEXT NOT NULL,
    is_wrong    TEXT,                      -- true/false/partially/unknown（設計者標注，NPC 不可見）
    created_day INTEGER,
    revised_day INTEGER
);

-- ── 天氣（docs/09.A）──
CREATE TABLE weather (
    day         INTEGER PRIMARY KEY,
    state       TEXT NOT NULL,             -- 晴/陰/雨/雪/暴風
    temp_band   TEXT NOT NULL,             -- 暖/涼/寒/酷寒
    growth_pts  INTEGER NOT NULL DEFAULT 0
);

-- ── 發展計畫（開墾/建屋/擴倉/勘探，docs/09.C）──
CREATE TABLE projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
    started_day INTEGER NOT NULL,
    mandays_done REAL NOT NULL DEFAULT 0,
    mandays_need REAL NOT NULL,
    resources_committed TEXT DEFAULT '{}',
    completed_day INTEGER,
    result_location TEXT
);

-- ── 人生大事（出生/婚配/懷孕/遷徙/繼承/性格漂移；也會同步落 events）──
CREATE TABLE life_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         INTEGER NOT NULL,
    type        TEXT NOT NULL,             -- birth/marriage/pregnancy/emigration/immigration/inheritance/trait_drift/death
    subject     TEXT NOT NULL REFERENCES character_registry(id),
    detail      TEXT DEFAULT '{}'
);

-- ── 世界狀態（單列鍵值）──
CREATE TABLE world_state (
    key         TEXT PRIMARY KEY,          -- day/year/season/treasury/granary/進行中事件/繁榮指數…
    value       TEXT NOT NULL
);

-- ── 審計快照（每30天：貨幣守恆/資源收支/基尼）──
CREATE TABLE audits (
    day         INTEGER PRIMARY KEY,
    money_internal REAL, money_external_net REAL,
    checks      TEXT NOT NULL DEFAULT '{}',-- JSON 各不變量 pass/fail
    gini        REAL
);
