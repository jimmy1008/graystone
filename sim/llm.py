# -*- coding: utf-8 -*-
"""Ollama 介面：NPC 抉擇點決策 + 酒館社交生成。
架構鐵律：LLM 只提案，引擎驗證後才執行；驗證退回也落盤。
LLM 離線時所有函式回傳 None，世界退化為純規則模式照常運轉。
"""
import json
import requests

DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["sell", "buy", "borrow_food", "gift", "tavern", "rest", "work_extra"]},
        "resource": {"type": "string"},
        "qty": {"type": "number"},
        "target": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["action", "reason"],
}

CHOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "choice": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["choice", "reason"],
}

SOCIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "line": {"type": "string"},
        "rel_delta": {"type": "integer", "minimum": -3, "maximum": 3},
        "rumor_event_id": {"type": "string"},
    },
    "required": ["line", "rel_delta"],
}


class LLMClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg.get("base_url", "http://localhost:11434")
        self.model = cfg.get("model", "qwen3:8b")
        self.calls = 0
        self.failures = 0

    def available(self):
        try:
            return requests.get(self.base + "/api/version", timeout=3).ok
        except Exception:
            return False

    def _chat(self, system, user, schema):
        self.calls += 1
        r = requests.post(
            self.base + "/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "format": schema,
                "think": False,
                "options": {
                    "temperature": self.cfg.get("temperature", 0.7),
                    "num_ctx": self.cfg.get("num_ctx", 4096),
                },
            },
            timeout=self.cfg.get("timeout_s", 120),
        )
        r.raise_for_status()
        return json.loads(r.json()["message"]["content"])

    def decide(self, npc_context):
        """抉擇點：給 NPC 結構化狀態，回 ACTION 提案。失敗回 None（退快速通道）。"""
        system = (
            "你是中世紀山谷村莊「灰石村」的村民，正在決定今晚的行動。"
            "你只知道下面提供給你的資訊，嚴禁編造不存在的人、物品或事件。"
            "只能從這些行動中選一個：sell(賣出自家庫存)/buy(購買)/borrow_food(向鄰居借糧)/"
            "gift(贈與)/tavern(去酒館)/rest(休息)/work_extra(加班)。"
            "以 JSON 回答。qty 是數量，target 是對象戶ID或NPC ID（僅限資訊中出現過的），"
            "reason 用一句中文說明動機（會成為村莊歷史的一部分）。"
        )
        try:
            return self._chat(system, npc_context, DECIDE_SCHEMA)
        except Exception:
            self.failures += 1
            return None

    def choose(self, context, options):
        """關鍵抉擇：給定情境與可選項清單，NPC 選一個並說明理由。
        choice 必須取自 options（引擎會驗證，取其他值視同失敗）。失敗回 None。"""
        system = (
            "你是中世紀山谷村莊「灰石村」的村民，面對一個需要判斷的狀況。"
            "你只知道提供給你的資訊，嚴禁編造不存在的人、事、物。"
            f"你必須從以下選項中選一個（一字不差地填入 choice）：{options}。"
            "reason 用一兩句中文說明你的判斷（會成為村莊歷史的一部分）。以 JSON 回答。"
        )
        try:
            out = self._chat(system, context, CHOICE_SCHEMA)
            if out and out.get("choice") in options:
                return out
            self.failures += 1
            return None
        except Exception:
            self.failures += 1
            return None

    def social(self, context):
        """酒館社交：兩人同席，生成一句互動與關係變化；可傳播近期真實事件（rumor_event_id
        必須取自提供的清單，取其他值會被引擎丟棄——防幻覺閘門）。失敗回 None。"""
        system = (
            "你在描寫中世紀村莊酒館裡兩位村民的一次同席互動。"
            "你只知道提供的資訊，嚴禁編造人物或事件。"
            "以 JSON 回答：line=一句簡短的互動描寫（中文，含說話內容更好），"
            "rel_delta=這次互動對兩人關係的影響(-3到3)，"
            "rumor_event_id=若其中一人會提起近期事件，從提供的事件ID清單中選一個；沒有就省略。"
        )
        try:
            return self._chat(system, context, SOCIAL_SCHEMA)
        except Exception:
            self.failures += 1
            return None
