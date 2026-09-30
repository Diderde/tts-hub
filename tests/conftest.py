# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""pytest 共享装置。

- 把项目根加入 ``sys.path``（包未安装时也能跑）；
- 提供确定性随机源，避免测试直接依赖 ``random`` 模块；
- 不依赖任何宿主环境（FFmpeg / 字体 / 网络）。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deterministic_rng import DeterministicRng  # noqa: E402
from support import AyaMaruyama  # noqa: E402


@pytest.fixture
def cassette() -> AyaMaruyama:
    """空白的录制回放传输层。"""
    return AyaMaruyama()


@pytest.fixture
def rng() -> DeterministicRng:
    """确定性随机源（固定种子，序列可复现）。"""
    return DeterministicRng(seed=20260925)
