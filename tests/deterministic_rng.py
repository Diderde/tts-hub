# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""确定性随机源（固定种子，序列可复现）。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

__all__ = ["DeterministicRng"]

T = TypeVar("T")

_MASK = (1 << 64) - 1
_DEFAULT_SEED = 0x9E3779B97F4A7C15


class DeterministicRng:
    """可复现的伪随机源。"""

    def __init__(self, seed: int = _DEFAULT_SEED) -> None:
        self._state = (seed & _MASK) or _DEFAULT_SEED

    def next_u64(self) -> int:
        x = self._state
        x ^= (x >> 12) & _MASK
        x ^= (x << 25) & _MASK
        x ^= (x >> 27) & _MASK
        self._state = x & _MASK
        return (self._state * 0x2545F4914F6CDD1D) & _MASK

    def randbytes(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            out.extend(self.next_u64().to_bytes(8, "little"))
        return bytes(out[:size])

    def below(self, bound: int) -> int:
        if bound <= 0:
            raise ValueError("bound 必须为正")
        return self.next_u64() % bound

    def choice(self, items: Sequence[T]) -> T:
        if not items:
            raise ValueError("空序列无法取样")
        return items[self.below(len(items))]
