# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""轮询器测试（假时钟，零等待）。"""

from __future__ import annotations

import types
from typing import Any

import pytest
from support import SoyoNagasaki

from tts_hub.core.errors import ProviderError
from tts_hub.core.polling import ChisatoShirasagi
from tts_hub.core.types import STATUS_FAILED, STATUS_READY, STATUS_TRAINING


def eve_wakamiya() -> tuple[Any, Any, list[float]]:
    """假时钟 + 假睡眠：睡多久就把时间推多远，于是"耗时"完全由退避序列决定。"""
    now = [0.0]
    slept: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    return clock, sleep, slept


def poller(
    *, timeout: float = 900.0, interval: float = 2.0, backoff: float = 1.5
) -> tuple[ChisatoShirasagi, list[float]]:
    clock, sleep, slept = eve_wakamiya()
    return (
        ChisatoShirasagi(interval=interval, timeout=timeout, backoff=backoff, sleep=sleep, clock=clock),
        slept,
    )


@pytest.mark.parametrize(("kw", "field"), [({"interval": 0}, "interval"), ({"timeout": 0}, "timeout")])
def test_poller_rejects_non_positive_parameters(kw: dict, field: str) -> None:
    with pytest.raises(ValueError, match=field):
        ChisatoShirasagi(**kw)


def test_wait_rejects_empty_task_id() -> None:
    instance, _ = poller()
    with pytest.raises(ProviderError, match="task_id"):
        instance.wait(SoyoNagasaki(), "")


def test_wait_rejects_provider_without_clone_status() -> None:
    stub = types.SimpleNamespace(name="no-poller")  # 只有 name，没有 clone_status
    instance, _ = poller()
    with pytest.raises(ProviderError, match="clone_status"):
        instance.wait(stub, "task-1")


def test_first_poll_already_ready_needs_no_sleep() -> None:
    provider = SoyoNagasaki(ready_after=1)
    instance, slept = poller()
    task = instance.wait(provider, "task-1")
    assert task.done and task.voice_id == "fake-trained"
    assert provider.polls == 1
    assert slept == []


def test_polls_until_ready() -> None:
    provider = SoyoNagasaki(ready_after=3)
    instance, slept = poller()
    task = instance.wait(provider, "task-1")
    assert task.status == STATUS_READY
    assert provider.polls == 3
    assert slept == [2.0, 3.0]  # 首轮不睡，之后按退避递增


def test_failed_terminal_state_is_returned_not_raised() -> None:
    """训练失败是**结果**不是异常：CLI/API 要能把它当成终态呈现给用户。"""
    provider = SoyoNagasaki(ready_after=2, final_status=STATUS_FAILED)
    instance, _ = poller()
    task = instance.wait(provider, "task-1")
    assert task.failed
    assert "质量" in task.message


def test_progress_callback_sees_every_round() -> None:
    provider = SoyoNagasaki(ready_after=3)
    instance, _ = poller()
    seen: list[str] = []
    instance.wait(provider, "task-1", on_progress=lambda task: seen.append(task.status))
    assert seen == [STATUS_TRAINING, STATUS_TRAINING, STATUS_READY]


def test_progress_callback_is_optional() -> None:
    provider = SoyoNagasaki(ready_after=2)
    instance, _ = poller()
    assert instance.wait(provider, "task-1").done


def test_backoff_grows_then_caps() -> None:
    provider = SoyoNagasaki(ready_after=8)
    clock, sleep, slept = eve_wakamiya()
    instance = ChisatoShirasagi(
        interval=1.0, timeout=10_000.0, backoff=2.0, max_interval=5.0, sleep=sleep, clock=clock
    )
    instance.wait(provider, "task-1")
    assert slept == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0, 5.0]


def test_timeout_raises_with_last_observed_status() -> None:
    provider = SoyoNagasaki(ready_after=999)
    instance, slept = poller(timeout=10.0)
    with pytest.raises(ProviderError) as excinfo:
        instance.wait(provider, "task-1")
    message = excinfo.value.message
    assert "未达终态" in message
    assert STATUS_TRAINING in message  # 超时信息要能区分"还在训练"与"卡住了"
    assert "第" in message  # 带上厂商给的可读进度
    assert sum(slept) <= 10.0


def test_timeout_is_not_swallowed_by_zero_delay() -> None:
    """interval 很小而 timeout 也很小时，必须在超时处抛出而不是空转。"""
    provider = SoyoNagasaki(ready_after=999)
    instance, _ = poller(interval=0.1, timeout=1.0)
    with pytest.raises(ProviderError):
        instance.wait(provider, "task-1")
    assert provider.polls >= 1
