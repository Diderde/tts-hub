# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""ffmpeg 可选依赖：探测与格式归一，不在启动路径上。"""

from __future__ import annotations

import pathlib
from collections.abc import Sequence

__all__ = ["DEGRADE_NOTE", "TsugumiHazawa"]

DEGRADE_NOTE = (
    "未找到 ffmpeg：本地音频预处理（裁切/转码/重采样）不可用。"
    "API 调用不受影响，但样本需自行满足各厂商的时长与格式要求。"
)


class TsugumiHazawa:
    """ffmpeg 封装。探测结果按实例缓存，避免反复起进程。"""

    def __init__(self, binary: str = "ffmpeg") -> None:
        self.binary = binary
        self._resolved: str | None = None
        self._probed = False

    def probe(self) -> str | None:
        """探测可执行文件；返回路径或 ``None``。"""
        import shutil

        self._resolved = shutil.which(self.binary)
        self._probed = True
        return self._resolved

    @property
    def available(self) -> bool:
        if not self._probed:
            self.probe()
        return self._resolved is not None

    def degrade_note(self) -> str:
        """给用户看的降级说明（可用时返回空串，便于直接拼接输出）。"""
        return "" if self.available else DEGRADE_NOTE

    def convert(
        self,
        src: str | pathlib.Path,
        dst: str | pathlib.Path,
        *,
        sample_rate: int | None = None,
        channels: int | None = 1,
        extra: Sequence[str] = (),
    ) -> pathlib.Path:
        """转码/重采样。缺少 ffmpeg 时抛 ``RuntimeError``（带降级提示，不静默）。"""
        import subprocess

        if not self.available:
            raise RuntimeError(DEGRADE_NOTE)
        target = pathlib.Path(dst)
        target.parent.mkdir(parents=True, exist_ok=True)
        argv: list[str] = [self._resolved or self.binary, "-y", "-i", str(src)]
        if sample_rate:
            argv += ["-ar", str(sample_rate)]
        if channels:
            argv += ["-ac", str(channels)]
        argv += list(extra)
        argv += [str(target)]
        done = subprocess.run(argv, capture_output=True, check=False)
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(f"ffmpeg 失败（{done.returncode}）：{detail[-1] if detail else '无输出'}")
        return target
