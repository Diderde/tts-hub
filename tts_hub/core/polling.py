# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""克隆/合成任务的通用轮询器（退避、超时、终态判定）。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, cast

from .errors import ProviderError
from .types import CloneTask

__all__ = ["ChisatoShirasagi"]

DEFAULT_INTERVAL = 2.0
DEFAULT_TIMEOUT = 900.0
DEFAULT_BACKOFF = 1.5
DEFAULT_MAX_INTERVAL = 20.0


class ChisatoShirasagi:
    """把"任务轮询"型厂商收敛成与同步型相同的结果。"""

    def __init__(
        self,
        *,
        interval: float = DEFAULT_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        backoff: float = DEFAULT_BACKOFF,
        max_interval: float = DEFAULT_MAX_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval 必须为正")
        if timeout <= 0:
            raise ValueError("timeout 必须为正")
        self.interval = interval
        self.timeout = timeout
        self.backoff = max(1.0, backoff)
        self.max_interval = max(interval, max_interval)
        self._sleep = sleep
        self._clock = clock

    def wait(
        self,
        provider: Any,
        task_id: str,
        *,
        on_progress: Callable[[CloneTask], None] | None = None,
    ) -> CloneTask:
        """把克隆任务轮询到终态。

        ``provider`` 只需满足"有 ``name`` 且有 ``clone_status(task_id)``"——不要求它
        实现完整协议，这样轮询器也能用在只做查询的轻量场景里。
        """
        if not task_id:
            raise ProviderError("轮询需要 task_id，收到空值")
        probe = getattr(provider, "clone_status", None)
        if not callable(probe):
            raise ProviderError(f"厂商 {getattr(provider, 'name', '?')} 不支持 clone_status 轮询")

        vendor = getattr(provider, "name", None)
        result = self.wait_until(
            lambda: probe(task_id),
            is_done=lambda task: task.done,
            is_failed=lambda task: task.failed,
            describe=lambda task: f"状态 {task.status}" + (f"：{task.message}" if task.message else ""),
            on_progress=on_progress,
            vendor=vendor,
            task_id=task_id,
        )
        return cast("CloneTask", result)

    def wait_until(
        self,
        probe: Callable[[], Any],
        *,
        is_done: Callable[[Any], bool],
        is_failed: Callable[[Any], bool],
        describe: Callable[[Any], str] | None = None,
        on_progress: Callable[[Any], None] | None = None,
        vendor: str | None = None,
        task_id: str = "",
    ) -> Any:
        """通用轮询：反复调 ``probe()`` 直到 ``is_done`` 或 ``is_failed`` 为真。

        克隆可以用它，**合成任务也可以**——云知声的 REST 合成就是异步任务，轮询逻辑
        与克隆完全同构，没必要写两遍。超时信息由 ``describe`` 决定，默认只报次数。
        """
        started = self._clock()
        attempts = 0
        delay = self.interval
        last: Any = None

        while True:
            attempts += 1
            last = probe()
            if on_progress is not None:
                on_progress(last)
            if is_done(last) or is_failed(last):
                return last

            elapsed = self._clock() - started
            if elapsed + delay > self.timeout:
                detail = describe(last) if describe is not None else f"第 {attempts} 次查询仍未完成"
                raise ProviderError(
                    f"任务 {task_id or '(未命名)'} 在 {self.timeout:.0f}s 内未达终态"
                    f"（轮询 {attempts} 次，最后{detail}）",
                    vendor=vendor,
                )
            self._sleep(delay)
            delay = min(delay * self.backoff, self.max_interval)

    def wait_for(
        self, provider: Any, task_id: str, *, on_progress: Callable[[CloneTask], None] | None = None
    ) -> CloneTask:
        """``wait`` 的别名，读起来更顺。"""
        return self.wait(provider, task_id, on_progress=on_progress)
