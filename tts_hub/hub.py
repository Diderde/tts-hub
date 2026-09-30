# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""聚合门面：把配置、适配器、注册表与记账串成统一的 speak / clone / voices 调用链。"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from typing import Any

from .config import RimiUshigome, Settings
from .core.errors import ProviderError, TTSHubError
from .core.net import MocaAoba
from .core.polling import ChisatoShirasagi
from .core.types import (
    STATUS_FAILED,
    STATUS_READY,
    AudioResult,
    CloneTask,
    ModelInfo,
    SampleInput,
    VoiceInfo,
    VoiceRef,
)
from .pricing import HimariUehara
from .providers import TomoeUdagawa
from .registry import TaeHanazono

__all__ = ["TTSHub"]


class TTSHub:
    """TTS-Hub 门面。"""

    def __init__(
        self,
        settings: Settings,
        *,
        http: MocaAoba | None = None,
        registry: TaeHanazono | None = None,
        factory: TomoeUdagawa | None = None,
        pricing: HimariUehara | None = None,
    ) -> None:
        self.settings = settings
        self._owns_http = http is None
        self.http = http or MocaAoba(
            timeout=settings.timeout,
            proxy=settings.proxy,
            allow_private=settings.allow_private_urls,
            secrets=settings.secrets(),
        )
        self.registry = registry or TaeHanazono(settings.db_path, samples_dir=settings.samples_dir)
        self.factory = factory or TomoeUdagawa(
            settings, http=self.http, is_enabled=self.is_vendor_enabled
        )
        self.pricing = pricing or HimariUehara(settings.pricing)


    def is_vendor_enabled(self, vendor: str) -> bool:
        """生效的启停状态：注册表的运行期覆盖优先于 providers.yaml 的默认值。"""
        cfg = self.settings.providers.get(vendor)
        default = bool(cfg.enabled) if cfg is not None else False
        return self.registry.is_vendor_enabled(vendor, default=default)

    def set_vendor_enabled(self, vendor: str, enabled: bool) -> None:
        if vendor not in self.settings.providers:
            raise ProviderError(f"providers.yaml 未声明厂商：{vendor!r}", vendor=vendor)
        self.registry.set_vendor_enabled(vendor, enabled)
        self.factory.close()


    @classmethod
    def open(cls, root: str | pathlib.Path | None = None, **kw: Any) -> TTSHub:
        """按目录加载配置并开门（``root`` 缺省用当前工作目录）。"""
        settings = RimiUshigome(root).load()
        return cls(settings, **kw)

    def close(self) -> None:
        self.factory.close()
        self.registry.close()
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> TTSHub:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


    def vendors(self) -> list[dict[str, Any]]:
        """厂商视图：是否启用、是否已配密钥、生效模型、估算单价。

        模型取**适配器上的生效值**（providers.yaml 若覆盖过则以覆盖值为准），
        否则没配密钥的厂商会显示成"单价未登记"，看起来像价目缺失而不是配置缺失。
        """
        out: list[dict[str, Any]] = []
        for name in self.factory.names():
            cfg = self.settings.providers[name]
            default_model = clone_model = None
            if self.factory.probe(name):
                try:
                    adapter = self.factory.make(name, require_key=False)
                except TTSHubError:  # pragma: no cover - 停用的厂商不该拖垮整个视图
                    adapter = None
                if adapter is not None:
                    default_model = getattr(adapter, "default_model", None)
                    clone_model = getattr(adapter, "clone_model", None)
            default_model = default_model or cfg.default_model
            clone_model = clone_model or cfg.clone_model
            out.append(
                {
                    "vendor": name,
                    "enabled": self.is_vendor_enabled(name),
                    "has_key": self.factory.has_key(name),
                    "api_key_env": cfg.api_key_env,
                    "default_model": default_model,
                    "clone_model": clone_model,
                    "pricing": self.pricing.describe(name, default_model),
                }
            )
        return out

    def models(self, vendor: str) -> list[ModelInfo]:
        return self.factory.make(vendor, require_key=False).list_models()

    def voices(self, vendor: str | None = None) -> list[VoiceInfo]:
        """列厂商侧音色（需要密钥）。"""
        return self.factory.make(vendor or self.settings.default_vendor).list_voices()

    def local_voices(self) -> list[dict[str, Any]]:
        """列本地注册表里的逻辑音色及其各厂商绑定。"""
        return self.registry.list_voices()

    def cost(self, *, days: int = 7, by: str = "vendor") -> dict[str, Any]:
        rows = self.registry.cost_summary(days=days, by=by)
        return {
            "days": days,
            "by": by,
            "total": self.registry.total_cost(days=days),
            "rows": rows,
            "currency": "CNY",
        }

    def expiring(self) -> list[dict[str, Any]]:
        """列出已过 TTL 的绑定（MiniMax 复刻音色 168 小时未正式调用会被删除）。"""
        return self.registry.expiring_bindings()

    def preferred_vendor_for(self, voice: VoiceRef | str) -> str | None:
        """该逻辑音色在管理台里设过的首选厂商；没设过就是 ``None``。

        只对**逻辑音色**有意义：厂商内 voice_id（如智谱 ``tongtong``）本就不在注册表里，
        自然查不到首选，调用方回落到 ``default_vendor`` 即可。
        """
        name = voice.raw if isinstance(voice, VoiceRef) else voice
        voice_id = self.registry.find_voice(name)
        if not voice_id:
            return None
        return self.registry.preferred_vendor(voice_id)


    def materialize(self, sample: SampleInput) -> SampleInput:
        """保证样本有本地字节：URL 形式先过出站安全校验再下载。

        §9 要求"克隆样本先落盘归档再上送，保留原始文件便于审计"，因此这里不直接
        把 URL 转给厂商，而是下载到本地后统一走归档。
        """
        if sample.path is not None and sample.data is None:
            return sample.with_data(pathlib.Path(sample.path).read_bytes(), sample.display_name)
        if sample.url is not None and sample.data is None:
            raw = self.http.download(sample.url)
            return sample.with_data(raw, sample.display_name)
        return sample


    def clone(
        self,
        sample: SampleInput,
        *,
        vendor: str | None = None,
        model: str | None = None,
        name: str | None = None,
        transcript: str | None = None,
        preview_text: str | None = None,
        logical: str | None = None,
        wait: bool = False,
        on_progress: Callable[[CloneTask], None] | None = None,
        poller: ChisatoShirasagi | None = None,
    ) -> dict[str, Any]:
        """复刻音色，并把结果落到注册表。

        返回 ``{"task": CloneTask, "voice_id": 逻辑音色ID, "binding_id": ...}``。
        """
        vendor = vendor or self.settings.default_vendor
        provider = self.factory.make(vendor)
        model = model or getattr(provider, "clone_model", None)
        if not sample.supplied():
            raise ProviderError("克隆样本必须恰好提供 path / url / data 之一")
        local = self.materialize(sample)
        data = local.read_bytes()
        sample_hash, sample_path = self.registry.archive_sample(data, local.display_name)

        logical_name = logical or name or local.display_name
        logical_id = self.registry.create_voice(logical_name)
        if sample.url is not None and getattr(provider, "clone_requires_url", False):
            upstream = SampleInput(
                url=sample.url,
                filename=local.display_name,
                mime=local.mime,
                transcript=transcript or local.transcript,
            )
        else:
            upstream = SampleInput(
                data=data,
                filename=local.display_name,
                mime=local.mime,
                transcript=transcript or local.transcript,
            )
        kwargs: dict[str, Any] = {}
        if model:
            kwargs["model"] = model
        try:
            task = provider.clone(
                upstream,
                name=name or logical_name,
                preview_text=preview_text,
                **kwargs,
            )
        except TTSHubError as exc:
            self.registry.log_call(
                vendor=vendor,
                model=model,
                voice_id=None,
                chars=0,
                latency_ms=0,
                cost_estimate=self.pricing.clone_cost(vendor),
                status="error",
                error_code=exc.code or exc.kind,
            )
            raise
        binding_id = self.registry.bind(
            logical_id,
            vendor,
            str(task.voice_id or ""),
            model=task.model or model,
            status=task.status,
            sample_hash=sample_hash,
            sample_path=sample_path,
            ttl_hours=getattr(provider, "clone_ttl_hours", None) if task.done else None,
            task_id=task.task_id,
        )
        self.registry.log_call(
            vendor=vendor,
            model=task.model or model,
            voice_id=str(task.voice_id or ""),
            chars=0,
            latency_ms=0,
            cost_estimate=self.pricing.clone_cost(vendor),
            status="ok" if task.done else task.status,
        )
        outcome = {
            "task": task,
            "voice_id": logical_id,
            "binding_id": binding_id,
            "name": logical_name,
        }
        if wait and not task.done and task.task_id:
            outcome["task"] = self.poll_clone(
                task.task_id, vendor=vendor, on_progress=on_progress, poller=poller
            )
        return outcome

    def poll_clone(
        self,
        task_id: str,
        *,
        vendor: str | None = None,
        on_progress: Callable[[CloneTask], None] | None = None,
        poller: ChisatoShirasagi | None = None,
    ) -> CloneTask:
        """把克隆任务轮询到终态，并把结果回填到注册表。

        "回填"是这条链路的关键：训练期间绑定的 ``vendor_voice_id`` 是空的，
        轮询成功后才有可用的厂商音色 ID——不回填的话，合成时解析逻辑音色会落空。
        """
        vendor = vendor or self.settings.default_vendor
        provider = self.factory.make(vendor)
        task = (poller or ChisatoShirasagi()).wait(provider, task_id, on_progress=on_progress)

        binding = self.registry.find_binding_by_task(vendor, task_id)
        if binding is not None:
            if task.done and task.voice_id:
                self.registry.finish_binding(
                    binding["id"],
                    vendor_voice_id=str(task.voice_id),
                    status=STATUS_READY,
                    ttl_hours=getattr(provider, "clone_ttl_hours", None),
                )
            elif task.failed:
                self.registry.finish_binding(
                    binding["id"],
                    vendor_voice_id=str(binding["vendor_voice_id"] or ""),
                    status=STATUS_FAILED,
                )
        self.registry.log_call(
            vendor=vendor,
            model=task.model,
            voice_id=str(task.voice_id or ""),
            chars=0,
            latency_ms=0,
            cost_estimate=None,
            status="ok" if task.done else task.status,
            error_code=None if task.done else "clone_failed",
        )
        return task

    def clone_status(self, task_id: str, *, vendor: str | None = None) -> CloneTask:
        return self.factory.make(vendor or self.settings.default_vendor).clone_status(task_id)


    def speak(
        self,
        text: str,
        *,
        voice: VoiceRef | str,
        vendor: str | None = None,
        model: str | None = None,
        stream: bool = False,
    ) -> AudioResult:
        """合成语音。``voice`` 可以是逻辑音色名/ID，也可以是厂商内 voice_id。"""
        if not text:
            raise ProviderError("待合成文本为空")
        vendor = vendor or self.preferred_vendor_for(voice) or self.settings.default_vendor
        provider = self.factory.make(vendor)
        model = model or getattr(provider, "default_model", None)
        ref = self._resolve_voice(voice, vendor)
        synth_kwargs: dict[str, Any] = {} if model is None else {"model": model}
        try:
            result = provider.synthesize(text, voice=ref, stream=stream, **synth_kwargs)
        except TTSHubError as exc:
            self.registry.log_call(
                vendor=vendor,
                model=model,
                voice_id=ref.raw,
                chars=len(text),
                latency_ms=0,
                cost_estimate=self.pricing.synth_cost(vendor, model, text),
                status="error",
                error_code=exc.code or exc.kind,
            )
            raise
        billed = result.chars or len(text)
        cost = self.pricing.synth_cost(vendor, model, text)
        self.registry.log_call(
            vendor=vendor,
            model=model,
            voice_id=ref.raw,
            chars=int(billed),
            latency_ms=result.latency_ms,
            cost_estimate=cost,
            status="ok",
        )
        ttl = getattr(provider, "clone_ttl_hours", None)
        if ttl:
            self.registry.mark_used(vendor, ref.raw, ttl_hours=ttl)
        return result


    def _resolve_voice(self, voice: VoiceRef | str, vendor: str) -> VoiceRef:
        """逻辑音色 → 厂商 voice_id；解析不到就当作厂商内 voice_id 直用。

        这里刻意不抛错：厂商系统音色（如智谱 ``tongtong``）本来就不在注册表里，
        误报会让"直接指定系统音色"这条最常用的路径失效。
        """
        ref = VoiceRef.of(voice)
        raw = ref.raw
        resolved = self.registry.resolve(raw, vendor=vendor)
        if resolved:
            return VoiceRef(raw=resolved, vendor=vendor, model=ref.model, logical=True)
        return VoiceRef(raw=raw, vendor=vendor, model=ref.model, logical=False)

    def fallback_chain(self, vendor: str | None = None) -> list[str]:
        """按配置给出回退链（首元素是被显式指定的厂商）。"""
        chain: list[str] = []
        if vendor:
            chain.append(vendor)
        chain.extend(self.settings.fallback)
        if not chain:
            chain.append(self.settings.default_vendor)
        seen: list[str] = []
        for name in chain:
            if name not in seen:
                seen.append(name)
        return seen

    def speak_with_fallback(self, text: str, **kw: Any) -> AudioResult:
        """按回退链依次尝试；只有"厂商侧故障"才切换，鉴权/审核类错误直接抛出。"""
        vendor = kw.pop("vendor", None)
        errors: list[str] = []
        for candidate in self.fallback_chain(vendor):
            try:
                return self.speak(text, vendor=candidate, **kw)
            except TTSHubError as exc:
                errors.append(f"{candidate}: {exc}")
                if exc.kind in ("auth", "review"):
                    raise
        raise ProviderError("回退链全部失败：" + "；".join(errors))
