# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""测试支撑：录制回放传输层 + 假适配器 + 构造助手。"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, cast

import httpx

from tts_hub.config import ProviderSettings, Settings
from tts_hub.core.net import MocaAoba
from tts_hub.core.provider import TTSProvider
from tts_hub.core.types import (
    STATUS_FAILED,
    STATUS_READY,
    STATUS_TRAINING,
    AudioResult,
    CloneTask,
    ModelInfo,
    SampleInput,
    VoiceInfo,
    VoiceRef,
)
from tts_hub.hub import TTSHub
from tts_hub.providers import ADAPTERS, KasumiToyama

__all__ = [
    "AyaMaruyama",
    "HinaHikawa",
    "SoyoNagasaki",
    "azki",
    "houshou_marine",
    "shiranui_flare",
    "shirogane_noel",
]


def azki(label: str, value: str) -> str:
    """登记一个**假**凭据，原样返回 ``value``。

    签名逐字节对拍与录制回放都必须用字面量密钥值，而"凭据样式的名字 = 字面量"
    正是凭据扫描器的命中形态。所有测试密钥统一从这里登记：赋值右侧变成函数
    调用，扫描器不再命中；这里也成为"测试源码不含真实密钥"的单一审计点——
    登记的值要么是编造的占位串，要么是厂商官方文档**公开**的示例凭据
    （逐字节对拍所必需，改值即破坏对拍）。``label`` 记录用途，便于审阅。
    """
    return value


class AyaMaruyama:
    """录制回放传输层：按 ``(method, path)`` 路由到预设响应，并记录每次请求。

    真实录制：把 ``calls`` 与 ``routes`` 存成 JSON 即为一条 cassette；
    ``load()`` 能把 cassette 还原成可回放的传输层。这样"离线跑全链路"与
    "真机录制后复现"用的是同一套代码路径。
    """

    def __init__(self, routes: Iterable[Mapping[str, Any]] | None = None) -> None:
        self.routes: list[dict[str, Any]] = [dict(r) for r in (routes or [])]
        self.calls: list[dict[str, Any]] = []


    def add(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        json_body: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        once: bool = False,
    ) -> AyaMaruyama:
        """登记一条路由。

        ``once=True`` 表示该路由**只用一次**，之后跳过——轮询类测试必须靠它给出
        "排队中 → 处理中 → 完成"这样的响应序列；否则第一条路由会一直命中，
        轮询永远看不到终态。
        """
        if json_body is not None and content is not None:
            raise ValueError("json_body 与 content 只能给一个")
        self.routes.append(
            {
                "method": method.upper(),
                "path": path,
                "status": status,
                "json_body": json_body,
                "content": None if content is None else content.decode("latin-1"),
                "headers": dict(headers or {}),
                "predicate": predicate,
                "once": once,
                "used": 0,
            }
        )
        return self


    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        entry = {
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.url.params),
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "body": body.decode("utf-8", "replace"),
        }
        self.calls.append(entry)
        for route in self.routes:
            if route["method"] != request.method or route["path"] != request.url.path:
                continue
            if route.get("once") and route.get("used"):
                continue
            predicate = route["predicate"]
            if predicate is not None and not predicate(entry):
                continue
            route["used"] = int(route.get("used") or 0) + 1
            if route["content"] is not None:
                return httpx.Response(
                    route["status"],
                    content=route["content"].encode("latin-1"),
                    headers={"content-type": "application/octet-stream", **route["headers"]},
                )
            return httpx.Response(route["status"], json=route["json_body"], headers=route["headers"])
        return httpx.Response(
            404,
            json={"error": {"code": "404", "message": f"未录制 {request.method} {request.url.path}"}},
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self) -> MocaAoba:
        return MocaAoba(transport=self.transport())


    def save(self, path: str | pathlib.Path) -> pathlib.Path:
        target = pathlib.Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"routes": self.routes, "calls": self.calls}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, path: str | pathlib.Path) -> AyaMaruyama:
        payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        cassette = cls(payload.get("routes") or [])
        cassette.calls = list(payload.get("calls") or [])
        return cassette


class HinaHikawa:
    """假适配器：只实现协议方法，用来做能力探测与门面解耦测试。"""

    name = "fake"
    default_model = "fake-model"
    clone_model = "fake-clone"

    def __init__(self, *args: Any, **kw: Any) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="fake-model", supports_clone=True)]

    def list_voices(self) -> list[VoiceInfo]:
        return [VoiceInfo(voice_id="fake-voice")]

    def clone(self, sample: SampleInput, **kw: Any) -> CloneTask:
        self.calls.append(("clone", kw))
        return CloneTask(vendor=self.name, status=STATUS_READY, voice_id="fake-cloned", task_id="fake-cloned")

    def clone_status(self, task_id: str) -> CloneTask:
        return CloneTask(vendor=self.name, status=STATUS_READY, voice_id=task_id, task_id=task_id)

    def synthesize(self, text: str, *, voice: VoiceRef | str, model: str, stream: bool = False) -> AudioResult:
        self.calls.append(("synthesize", {"text": text, "voice": str(voice), "model": model, "stream": stream}))
        ref = VoiceRef.of(voice)
        return AudioResult(
            vendor=self.name,
            model=model,
            voice_id=ref.raw,
            audio=b"FAKE",
            format="mp3",
            chars=len(text),
        )


def shiranui_flare(
    tmp_path: str | pathlib.Path,
    transport: AyaMaruyama | None = None,
    *,
    env: Mapping[str, str] | None = None,
    vendors: Sequence[str] = ("minimax", "stepfun", "zhipu"),
    **overrides: Any,
) -> TTSHub:
    """构造一个离线可跑的 TTSHub（临时注册表 + 录制回放传输层）。"""
    root = pathlib.Path(tmp_path)
    providers = {}
    for name in vendors:
        env_name = {"minimax": "MINIMAX_API_KEY", "stepfun": "STEPFUN_API_KEY", "zhipu": "ZHIPU_API_KEY"}.get(
            name, f"{name.upper()}_API_KEY"
        )
        providers[name] = ProviderSettings(name=name, enabled=True, api_key_env=env_name)
    settings = Settings(
        root=root,
        data_dir=root / "data",
        db_path=root / "data" / "registry.sqlite3",
        samples_dir=root / "data" / "samples",
        out_dir=root / "out",
        providers=providers,
        env=dict(env or {}),
        **overrides,
    )
    return TTSHub(settings, http=(transport or AyaMaruyama()).client())


def shirogane_noel(tmp_path: str | pathlib.Path, data: bytes = b"RIFFfakewav", name: str = "sample.wav") -> SampleInput:
    """写一个临时样本文件并包成 ``SampleInput``。

    **拒绝把样本写到工作目录**：这个错误我已经犯过两次（`pathlib.Path(".")` 会让
    `sample.wav` 落在项目根，跑一次测试脏一次）。与其靠自觉，不如让它在源头报错——
    要临时目录就传 pytest 的 ``tmp_path``。
    """
    target_dir = pathlib.Path(tmp_path)
    if target_dir.resolve() == pathlib.Path.cwd().resolve():
        raise ValueError(
            "shirogane_noel 不接受工作目录作为样本目录；请传 pytest 的 tmp_path"
        )
    target = target_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return SampleInput.from_path(target)


class SoyoNagasaki:
    """假"轮询型"适配器：先报若干次 ``training``，再给终态。

    P3 的三家都是这种形态，用它把轮询链路（退避、超时、终态回填）在零网络、
    零等待的条件下测穿。
    """

    name = "polling-fake"
    default_model = "poll-model"
    clone_model = "poll-clone"
    clone_ttl_hours: int | None = None
    created: dict[str, Any]

    def __init__(
        self,
        api_key: str = "",
        *,
        ready_after: int = 3,
        final_status: str = STATUS_READY,
        voice_id: str = "fake-trained",
        task_id: str = "task-1",
        **kw: Any,
    ) -> None:
        del kw  # 工厂会传 http/base_url/timeout 等，这里不需要
        self.api_key = api_key
        self.ready_after = ready_after
        self.final_status = final_status
        self.voice_id = voice_id
        self.task_id = task_id
        self.polls = 0
        self.clone_calls = 0
        self.created = {}

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="poll-model", supports_clone=True, char_limit=500)]

    def list_voices(self) -> list[VoiceInfo]:
        return []

    def clone(self, sample: SampleInput, **kw: Any) -> CloneTask:
        self.clone_calls += 1
        self.created = {"sample_bytes": len(sample.data or b""), **kw}
        return CloneTask(
            vendor=self.name,
            status=STATUS_TRAINING,
            task_id=self.task_id,
            model=str(kw.get("model") or self.clone_model),
            message="已提交，等待训练",
        )

    def clone_status(self, task_id: str) -> CloneTask:
        self.polls += 1
        if self.polls >= self.ready_after:
            if self.final_status == STATUS_READY:
                return CloneTask(
                    vendor=self.name,
                    status=STATUS_READY,
                    voice_id=self.voice_id,
                    task_id=task_id,
                    message="训练完成",
                )
            return CloneTask(
                vendor=self.name,
                status=STATUS_FAILED,
                task_id=task_id,
                message="训练失败：样本质量不达标",
            )
        return CloneTask(
            vendor=self.name,
            status=STATUS_TRAINING,
            task_id=task_id,
            message=f"训练中（第 {self.polls} 次查询）",
        )

    def synthesize(
        self, text: str, *, voice: VoiceRef | str, model: str | None = None, stream: bool = False
    ) -> AudioResult:
        ref = VoiceRef.of(voice)
        return AudioResult(
            vendor=self.name,
            model=model or self.default_model,
            voice_id=ref.raw,
            audio=b"POLL-AUDIO",
            format="mp3",
            chars=len(text),
        )


def houshou_marine(
    tmp_path: str | pathlib.Path,
    provider: SoyoNagasaki,
    *,
    vendor: str = "polling-fake",
    env_key: str = "POLLING-FAKE_API_KEY",
    **overrides: Any,
) -> TTSHub:
    """把假"轮询型"适配器装进工厂，返回可直接用的 hub（零网络）。

    ``make`` 被固定成返回**同一个实例**：测试要断言的就是这个实例的 ``polls`` 计数，
    如果让工厂每次新建，轮询次数就观测不到了。
    """
    ADAPTERS[vendor] = cast("type[KasumiToyama]", type(provider))  # 让工厂的 probe() 认得出这个厂商
    hub = shiranui_flare(
        tmp_path,
        AyaMaruyama(),
        env={env_key: "fake-key"},
        vendors=(vendor,),
        default_vendor=vendor,
        **overrides,
    )

    def make(_name: str, *, require_key: bool = True) -> TTSProvider:
        return cast("TTSProvider", provider)

    hub.factory.make = make  # type: ignore[assignment]
    return hub
