# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""FastAPI 自定义 REST 出口层；默认只监听 127.0.0.1，错误响应统一外形。"""

from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..core.errors import TTSHubError
from ..core.types import SampleInput
from ..hub import TTSHub
from .console import _parse_days, yukihana_lamy
from .models import TtsRequest, VoiceCreateRequest

__all__ = [
    "MAX_SAMPLE_BYTES",
    "MEDIA_TYPES",
    "STATUS_BY_KIND",
    "create_app",
]

STATUS_BY_KIND: dict[str, int] = {
    "auth": 401,
    "quota": 429,
    "review": 422,
    "provider": 502,
    "error": 502,
}

MEDIA_TYPES: dict[str, str] = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "pcm": "audio/L16",
}

MAX_SAMPLE_BYTES = 25 * 1024 * 1024

_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _host_header_name(value: str) -> str:
    """从 ``Host`` 头里取出主机名（剥掉端口；IPv6 形如 ``[::1]:8000``）。"""
    if value.startswith("["):
        return value.split("]", 1)[0].lstrip("[").lower()
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0].lower()
    return value.lower()


def _media_type(audio_format: str) -> str:
    return MEDIA_TYPES.get((audio_format or "").lower(), "application/octet-stream")


def _error_body(message: str, *, kind: str = "error", code: str | None = None) -> dict[str, Any]:
    return {"error": {"kind": kind, "vendor": None, "code": code, "status": None, "message": message}}


def _sample_from(data: bytes | None, url: str | None, filename: str | None) -> SampleInput:
    """把上传内容或 URL 归一成 ``SampleInput``；两者都没给就是 400。"""
    if data is not None:
        if not data:
            raise HTTPException(status_code=400, detail=_error_body("上传的样本文件为空"))
        return SampleInput(data=data, filename=filename or "sample.wav")
    if url:
        return SampleInput.from_url(url)
    raise HTTPException(status_code=400, detail=_error_body("必须提供 file 或 url 之一"))


def create_app(
    settings: Any = None,
    *,
    hub: TTSHub | None = None,
    root: str | pathlib.Path | None = None,
) -> FastAPI:
    """装配 ASGI 应用。

    ``hub`` 可注入（测试用录播传输层构造）；否则按 ``settings`` 或 ``root`` 现建一个，
    并在应用关闭时由应用负责释放。**外部注入的 hub 由注入方自己关**。
    """
    owns_hub = hub is None
    if hub is None:
        hub = TTSHub.open(root) if settings is None else TTSHub(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_hub:
            hub.close()

    app = FastAPI(
        title="TTS-Hub",
        version=__version__,
        description="多家声音克隆 TTS API 的统一出口（服务默认只监听 127.0.0.1）",
        lifespan=lifespan,
    )
    app.state.hub = hub
    app.state.owns_hub = owns_hub

    if not hub.settings.exposes_lan:

        @app.middleware("http")
        async def host_guard(request: Any, call_next: Any) -> Any:
            hostname = _host_header_name(request.headers.get("host") or "")
            if hostname not in _LOOPBACK_HOSTNAMES:
                return JSONResponse(
                    status_code=421,
                    content=_error_body(
                        f"Host {hostname!r} 不是本机回环地址。本服务只允许从 127.0.0.1 / "
                        "localhost 访问；若你是从别的主机来的，这是服务端刻意拒绝"
                        "（防 DNS rebinding）。",
                        code="host_not_allowed",
                    ),
                )
            return await call_next(request)


    @app.exception_handler(TTSHubError)
    async def hub_error_handler(_request: Any, exc: TTSHubError) -> JSONResponse:
        return JSONResponse(
            status_code=STATUS_BY_KIND.get(exc.kind, 502), content={"error": exc.to_dict()}
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Any, exc: StarletteHTTPException) -> JSONResponse:
        detail: Any = exc.detail
        body = detail if isinstance(detail, dict) and "error" in detail else _error_body(str(detail))
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Any, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', '')}"
            for err in exc.errors()[:5]
        )
        return JSONResponse(
            status_code=422,
            content=_error_body(f"请求参数不合法：{problems}", code="validation"),
        )


    @app.get("/api/health", tags=["meta"], summary="存活探针")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "default_vendor": hub.settings.default_vendor,
            "host": hub.settings.host,
            "port": hub.settings.port,
            "exposes_lan": hub.settings.exposes_lan,
            "vendors": {r["vendor"]: {"enabled": r["enabled"], "has_key": r["has_key"]}
                        for r in hub.vendors()},
        }


    def synth_or_raise(payload: TtsRequest, *, stream: bool) -> Any:
        kwargs: dict[str, Any] = {
            "voice": payload.voice,
            "model": payload.model,
            "stream": stream,
        }
        if payload.fallback:
            return hub.speak_with_fallback(payload.text, vendor=payload.vendor, **kwargs)
        return hub.speak(payload.text, vendor=payload.vendor, **kwargs)

    @app.post("/api/tts", tags=["tts"], summary="合成语音（一次性返回音频）")
    def tts(payload: TtsRequest) -> Response:
        result = synth_or_raise(payload, stream=False)
        return Response(
            content=result.audio or b"",
            media_type=_media_type(result.format),
            headers={
                "X-TTS-Vendor": result.vendor,
                "X-TTS-Model": result.model or "",
                "X-TTS-Voice": result.voice_id,
                "X-TTS-Chars": str(result.chars),
                "X-TTS-Latency-Ms": str(result.latency_ms),
            },
        )

    @app.post("/api/tts/stream", tags=["tts"], summary="合成语音（分块流式）")
    def tts_stream(payload: TtsRequest) -> StreamingResponse:
        result = synth_or_raise(payload, stream=True)

        def chunks() -> Iterator[bytes]:
            yield from result.iter_bytes()

        return StreamingResponse(
            chunks(),
            media_type=_media_type(payload.format or result.format),
            headers={"X-TTS-Vendor": result.vendor, "X-TTS-Voice": result.voice_id},
        )


    @app.post("/api/clone", tags=["clone"], summary="克隆音色（multipart：file 或 url）")
    async def clone(
        name: str = Form(..., description="逻辑音色名"),
        vendor: str | None = Form(None),
        model: str | None = Form(None),
        transcript: str | None = Form(None, description="样本里说的内容"),
        preview_text: str | None = Form(None, description="试听文本；智谱必填，缺省用内置句"),
        url: str | None = Form(None, description="公网音频 URL，与 file 二选一"),
        file: UploadFile | None = File(None),
    ) -> dict[str, Any]:
        raw = await file.read() if file is not None else None
        if raw is not None and len(raw) > MAX_SAMPLE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=_error_body(f"样本超过 {MAX_SAMPLE_BYTES} 字节上限"),
            )
        sample = _sample_from(raw, url, file.filename if file is not None else None)
        outcome = hub.clone(
            sample,
            vendor=vendor,
            model=model,
            name=name,
            transcript=transcript,
            preview_text=preview_text,
        )
        return {
            "voice_id": outcome["voice_id"],
            "name": outcome["name"],
            "binding_id": outcome["binding_id"],
            "task": outcome["task"].to_dict(),
        }

    @app.get("/api/clone/{task_id}", tags=["clone"], summary="查询克隆任务状态")
    def clone_status(task_id: str, vendor: str | None = None) -> dict[str, Any]:
        return hub.clone_status(task_id, vendor=vendor).to_dict()


    @app.get("/api/voices", tags=["voices"], summary="列逻辑音色；给 remote 则列厂商侧音色")
    def list_voices(remote: str | None = None) -> dict[str, Any]:
        if remote:
            return {"vendor": remote, "voices": [v.to_dict() for v in hub.voices(remote)]}
        return {"voices": hub.local_voices()}

    @app.post("/api/voices", tags=["voices"], status_code=201, summary="建逻辑音色（可同时绑定）")
    def create_voice(payload: VoiceCreateRequest) -> dict[str, Any]:
        if payload.vendor and not payload.vendor_voice_id:
            raise HTTPException(
                status_code=400,
                detail=_error_body("给了 vendor 就必须同时给 vendor_voice_id"),
            )
        voice_id = hub.registry.create_voice(payload.name, tags=payload.tags)
        result: dict[str, Any] = {"id": voice_id, "name": payload.name, "tags": payload.tags}
        if payload.vendor and payload.vendor_voice_id:
            result["binding_id"] = hub.registry.bind(
                voice_id,
                payload.vendor,
                payload.vendor_voice_id,
                model=payload.model,
                status=payload.status,
            )
        return result

    @app.delete("/api/voices/{voice_id}", tags=["voices"], summary="删除逻辑音色及其绑定")
    def delete_voice(voice_id: str) -> dict[str, Any]:
        if not hub.registry.delete_voice(voice_id):
            raise HTTPException(status_code=404, detail=_error_body(f"逻辑音色不存在：{voice_id}"))
        return {"deleted": True, "id": voice_id}


    @app.get("/api/vendors", tags=["vendors"], summary="厂商配置与密钥状态")
    def list_vendors() -> dict[str, Any]:
        return {"vendors": hub.vendors()}

    def flip_vendor(vendor: str, enabled: bool) -> dict[str, Any]:
        try:
            hub.set_vendor_enabled(vendor, enabled)
        except TTSHubError as exc:
            raise HTTPException(status_code=404, detail={"error": exc.to_dict()}) from exc
        return next(r for r in hub.vendors() if r["vendor"] == vendor)

    @app.post("/api/vendors/{vendor}/enable", tags=["vendors"], summary="启用厂商")
    def enable_vendor(vendor: str) -> dict[str, Any]:
        return flip_vendor(vendor, True)

    @app.post("/api/vendors/{vendor}/disable", tags=["vendors"], summary="停用厂商")
    def disable_vendor(vendor: str) -> dict[str, Any]:
        return flip_vendor(vendor, False)


    @app.get("/api/cost", tags=["cost"], summary="调用与成本汇总（估算）")
    def cost(range: str = "7d", by: str = "vendor") -> dict[str, Any]:
        if by not in ("vendor", "model", "day"):
            raise HTTPException(status_code=400, detail=_error_body("by 只支持 vendor/model/day"))
        return hub.cost(days=_parse_days(range, default=7), by=by)


    yukihana_lamy(app, hub)

    return app
