# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""质量闸门：ruff 与 mypy 全绿。"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 900


def tool_available(module: str) -> bool:
    """工具是否可用。

    只认 ``importlib``：命令是按 ``python -m <module>`` 跑的，能不能 import 才是
    真正的判据。**不要**改成查 PATH——直接跑虚拟环境里的解释器时 ``Scripts`` 并不
    在 PATH 上，那会让闸门静默变成 skip。
    """
    return importlib.util.find_spec(module) is not None


def run_tool(*args: str) -> subprocess.CompletedProcess[str]:
    """在仓库根目录跑一条工具命令，把输出原样带回（断言失败时要能看见原因）。

    **显式按 UTF-8 解码**：``text=True`` 默认用系统区域编码（这台机器上是 GBK），
    工具输出的非 ASCII 字节会把测试打成 ``UnicodeDecodeError``，看起来像闸门坏了。
    """
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def require(module: str) -> None:
    if not tool_available(module):
        pytest.skip(f"未安装 {module}：pip install -e '.[dev]' 后本项才会真正执行")


def test_ruff_check_is_clean() -> None:
    """lint 必须全绿；规则集与豁免项都写在 pyproject.toml 的 ``[tool.ruff]`` 里。"""
    require("ruff")
    result = run_tool("-m", "ruff", "check", ".")
    assert result.returncode == 0, f"ruff check 未通过：\n{result.stdout}{result.stderr}"


def test_mypy_is_clean() -> None:
    """类型检查必须全绿；``tts_hub`` 走严格档，故意不放过缺注解的函数。"""
    require("mypy")
    result = run_tool("-m", "mypy")
    assert result.returncode == 0, f"mypy 未通过：\n{result.stdout}{result.stderr}"


def test_pyproject_declares_both_gates() -> None:
    """闸门本身也要被守住：删掉配置就等于把两道闸门一起撤了。

    只做**存在性与范围**检查，不复制具体规则——规则该改就改，但"扫过哪些目录"
    不能被悄悄改窄（把 ``files`` 改成空表是最省事的假绿）。
    """
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.ruff]" in config
    assert "[tool.mypy]" in config
    assert 'files = ["tts_hub", "tests"]' in config
