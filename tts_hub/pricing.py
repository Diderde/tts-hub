# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""成本估算表（元），数值取自各厂商官方定价页（2026-09），仅供量级参考。"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Any

__all__ = ["DEFAULT_TABLE", "HimariUehara", "minato_aqua"]

DEFAULT_TABLE: dict[str, dict[str, Any]] = {
    "minimax": {
        "currency": "CNY",
        "clone_per_voice": 9.9,
        "char_rule": "cjk_x2",
        "synth": {
            "speech-2.8-hd": 3.5,
            "speech-2.8-turbo": 2.0,
            "speech-2.6-hd": 3.5,
            "speech-2.6-turbo": 2.0,
            "speech-02-hd": 3.5,
            "speech-02-turbo": 2.0,
            "speech-01-hd": 3.5,
            "speech-01-turbo": 2.0,
        },
        "note": "复刻音色在首次合成时才计费；168 小时内未正式调用会被删除",
    },
    "stepfun": {
        "currency": "CNY",
        "clone_per_voice": 9.9,
        "char_rule": "cjk_plus_half",
        "synth": {
            "stepaudio-3-tts": 2.5,
            "stepaudio-2.5-tts": 5.8,
            "step-tts-2": 2.8,
            "step-tts-mini": 0.9,
        },
        "note": "官方口径：1 个汉字算 1 字符，2 个英文字母算 1 字符，2 个标点算 1 字符",
    },
    "zhipu": {
        "currency": "CNY",
        "clone_per_call": 6.0,
        "char_rule": "plain",
        "synth": {"glm-tts": 2.0},
        "note": "复刻按次计费，每次调用都扣费（不是每个音色一次）",
    },
    "aliyun": {
        "currency": "CNY",
        "clone_per_voice": 0.0,  # CosyVoice 系创建音色免费
        "char_rule": "plain",
        "synth": {
            "cosyvoice-v2": 2.0,
            "cosyvoice-v3-plus": 2.0,
            "cosyvoice-v3-flash": 1.0,
            "cosyvoice-v3.5-plus": 1.5,
            "cosyvoice-v3.5-flash": 0.8,
            "qwen3-tts-vc-2026-01-22": 0.8,
        },
        "note": "CosyVoice 系复刻免费；Qwen-TTS 系复刻 0.01 元/个。以上均为华北2（北京）价",
    },
    "baidu": {
        "currency": "CNY",
        "clone_per_voice": 8.8,
        "char_rule": "plain",
        "synth": {"baidu-voice-clone": 7.0},
        "note": "创建音色 8.8 元/个（成功创建才计费）、合成 7 元/万字符，均为后付费价",
    },
    "unisound": {
        "currency": "CNY",
        "clone_per_voice": 10.0,
        "char_rule": "plain",
        "synth": {"u2-tts": 2.0, "u2-tts-clone": 2.0, "u2-tts-design": 2.0},
        "note": "音色 10 元/个（首次合成才扣）+ 合成 2 元/万字；"
        "官方资源包页折算约 4.5 元/万字，两页互相矛盾，估算仅供参考",
    },
    "tencent": {
        "currency": "CNY",
        "clone_per_voice": 39.0,
        "char_rule": "plain",
        "synth": {"tts-texttovoice": 8.0},
        "note": "一句话版：音色 39 元/个（预付费阶梯最低 12）+ 合成后付费 8→6.4 元/万字符；"
        "音色存储 0.03 元/个/日（赠 3 个月）。基础版音色 4500→2500 元/个、合成 0.3 元/万字符",
    },
    "huawei": {
        "currency": "CNY",
        "char_rule": "plain",
        "synth": {"sis-vcs": 2.0},
        "note": "按字符按需 2 元/万字符，另有字符套餐包（折算约 1.8→1.4）；"
        "官方价目无音色计费项，音色单价未登记",
    },
    "volcengine": {
        "currency": "CNY",
        "clone_per_voice": 138.0,
        "char_rule": "plain",
        "synth": {"seed-tts-2.0": 3.0, "seed-icl-2.0": 3.0},
        "note": "音色槽位 138 元/个（**首次合成时才扣**，复刻了不合成会在 7 天后删除）；"
        "豆包语音合成 2.0 与声音复刻 2.0 各 3 元/万字符，资源包折算低至 2.8。"
        "注意 8 元/万字符那条是『大模型声音复刻』，不是这两条主商品线",
    },
    "iflytek": {
        "currency": "CNY",
        "char_rule": "plain",
        "note": "官方仅说明「按训练次数 + 合成字符数授权」，具体价格与免费额度未公开；"
        "且合成只有 WebSocket，本 adapter 不实现合成",
    },
}


def minato_aqua(text: str, rule: str = "plain") -> float:
    """按厂商口径折算"计费字符数"。

    - ``plain``：1 字符 = 1；
    - ``cjk_x2``：汉字（含全角）按 2 字符计，其余按 1；
    - ``cjk_plus_half``：汉字按 1，其余非 CJK 字符两个折一个。
    """
    if rule == "plain" or not text:
        return float(len(text))
    if rule == "cjk_x2":
        return float(sum(2 if _is_wide(ch) else 1 for ch in text))
    if rule == "cjk_plus_half":
        wide = sum(1 for ch in text if _is_wide(ch))
        narrow = len(text) - wide
        return float(wide + -(-narrow // 2))  # 向上取整
    return float(len(text))


def _is_wide(ch: str) -> bool:
    """是否属于中日韩表意文字/全角区（决定计费权重的那一类）。"""
    return unicodedata.east_asian_width(ch) in ("W", "F") and not ch.isascii()


class HimariUehara:
    """成本估算器：合并内置价目表与 ``providers.yaml`` 的覆盖项。"""

    def __init__(self, overrides: Mapping[str, Any] | None = None) -> None:
        self.table: dict[str, dict[str, Any]] = {
            name: dict(row) for name, row in DEFAULT_TABLE.items()
        }
        for name, row in (overrides or {}).items():
            if not isinstance(row, Mapping):
                continue
            merged = self.table.setdefault(name, {})
            for key, value in row.items():
                if key == "synth" and isinstance(value, Mapping):
                    merged["synth"] = {**merged.get("synth", {}), **value}
                else:
                    merged[key] = value


    def rate(self, vendor: str, model: str | None) -> float | None:
        """合成单价（元/万字符）；未登记返回 ``None``（不假装知道价格）。"""
        row = self.table.get(vendor) or {}
        synth = row.get("synth") or {}
        if model and model in synth:
            return float(synth[model])
        return None

    def char_count(self, vendor: str, text: str) -> float:
        row = self.table.get(vendor) or {}
        return minato_aqua(text, str(row.get("char_rule") or "plain"))

    def synth_cost(self, vendor: str, model: str | None, text: str) -> float | None:
        """一次合成的估算费用（元）。未登记单价时返回 ``None``。"""
        price = self.rate(vendor, model)
        if price is None:
            return None
        return self.char_count(vendor, text) * price / 10000.0

    def clone_cost(self, vendor: str) -> float | None:
        """一次克隆动作的估算费用（MiniMax/阶跃按音色，智谱按次）。"""
        row = self.table.get(vendor) or {}
        if "clone_per_voice" in row:
            return float(row["clone_per_voice"])
        if "clone_per_call" in row:
            return float(row["clone_per_call"])
        return None

    def note(self, vendor: str) -> str:
        return str((self.table.get(vendor) or {}).get("note") or "")

    def describe(self, vendor: str, model: str | None = None) -> str:
        price = self.rate(vendor, model)
        clone = self.clone_cost(vendor)
        bits = [f"{vendor}"]
        bits.append(f"合成 {price} 元/万字符" if price is not None else "合成单价未登记")
        bits.append(f"复刻 {clone} 元" if clone is not None else "复刻单价未登记")
        return "；".join(bits)
