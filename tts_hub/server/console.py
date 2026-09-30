# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""管理台页面与页面专用的管理接口。"""

from __future__ import annotations

import pathlib
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response

from ..core.errors import TTSHubError
from ..hub import TTSHub
from .models import VoiceBindRequest, VoiceDefaultRequest, VoiceUpdateRequest

__all__ = ["ASSETS", "WEB_DIR", "yukihana_lamy"]

WEB_DIR = pathlib.Path(__file__).resolve().parent / "web"

ASSETS: dict[str, tuple[str, str]] = {
    "console.js": ("console.js", "text/javascript; charset=utf-8"),
    "console.css": ("console.css", "text/css; charset=utf-8"),
}

_INDEX = "index.html"


def yukihana_lamy(app: FastAPI, hub: TTSHub) -> None:
    """把管理台的页面与页面接口挂到 ``app`` 上。

    内部命名遵循项目命名约定。参数名 ``app`` / ``hub`` 是语义名——
    池只约束类名与函数名，不约束参数。
    """

    def read_asset(filename: str) -> str:
        """读页面资源；缺文件时给出**能照着修**的错误，而不是一句 500。"""
        target = WEB_DIR / filename
        if not target.is_file():
            raise HTTPException(
                status_code=500,
                detail={
                    "error": {
                        "kind": "error",
                        "vendor": None,
                        "code": "console_missing",
                        "status": None,
                        "message": f"管理台资源缺失：{target}（安装包未带上 web/ 目录？）",
                    }
                },
            )
        return target.read_text(encoding="utf-8")


    @app.get("/", include_in_schema=False)
    def console_page() -> HTMLResponse:
        return HTMLResponse(read_asset(_INDEX))

    @app.get("/assets/{name}", include_in_schema=False)
    def console_asset(name: str) -> Response:
        entry = ASSETS.get(name)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"没有这个资源：{name}")
        filename, media_type = entry
        return Response(read_asset(filename), media_type=media_type)


    def voice_row(voice_id: str) -> dict[str, Any]:
        for row in hub.local_voices():
            if row["id"] == voice_id:
                return row
        raise HTTPException(status_code=404, detail=f"逻辑音色不存在：{voice_id}")

    @app.get("/api/voices/{voice_id}", tags=["console"], summary="逻辑音色详情（含绑定与样本）")
    def voice_detail(voice_id: str) -> dict[str, Any]:
        row = voice_row(voice_id)
        bindings = row["bindings"]
        return {
            **row,
            "preferred_vendor": next((b["vendor"] for b in bindings if b["preferred"]), None),
            "samples": _sample_list(bindings),
        }

    @app.patch("/api/voices/{voice_id}", tags=["console"], summary="改逻辑音色的名字/标签")
    def update_voice(voice_id: str, payload: VoiceUpdateRequest) -> dict[str, Any]:
        voice_row(voice_id)
        if not hub.registry.update_voice(voice_id, name=payload.name, tags=payload.tags):
            raise HTTPException(status_code=400, detail="name 与 tags 至少要给一个")
        return voice_row(voice_id)

    @app.post(
        "/api/voices/{voice_id}/bindings",
        tags=["console"],
        status_code=201,
        summary="给逻辑音色补一条厂商绑定",
    )
    def create_binding(voice_id: str, payload: VoiceBindRequest) -> dict[str, Any]:
        voice_row(voice_id)
        binding_id = hub.registry.bind(
            voice_id,
            payload.vendor,
            payload.vendor_voice_id,
            model=payload.model,
            status=payload.status,
        )
        return {"binding_id": binding_id, "voice_id": voice_id, "vendor": payload.vendor}

    @app.delete(
        "/api/voices/{voice_id}/bindings/{binding_id}",
        tags=["console"],
        summary="解绑（只删本地记录，不去厂商侧删音色）",
    )
    def delete_binding(voice_id: str, binding_id: str) -> dict[str, Any]:
        owned = {b["id"] for b in hub.registry.bindings(voice_id=voice_id)}
        if binding_id not in owned:
            raise HTTPException(status_code=404, detail=f"该音色下没有这条绑定：{binding_id}")
        hub.registry.delete_binding(binding_id)
        return {"deleted": True, "binding_id": binding_id}

    @app.post("/api/voices/{voice_id}/default", tags=["console"], summary="把某厂商绑定设为默认")
    def set_default(voice_id: str, payload: VoiceDefaultRequest) -> dict[str, Any]:
        voice_row(voice_id)
        if not hub.registry.set_preferred_binding(voice_id, payload.vendor):
            raise HTTPException(
                status_code=404,
                detail=f"该音色没有 {payload.vendor} 的绑定，无法设为默认",
            )
        return {"voice_id": voice_id, "preferred_vendor": payload.vendor}

    @app.get(
        "/api/voices/{voice_id}/bindings/{binding_id}/sample",
        tags=["console"],
        summary="取回复刻用的原始样本（调音台里和合成结果对比着听）",
    )
    def binding_sample(voice_id: str, binding_id: str) -> FileResponse:
        row = next(
            (b for b in hub.registry.bindings(voice_id=voice_id) if b["id"] == binding_id), None
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"该音色下没有这条绑定：{binding_id}")
        path = _safe_sample_path(hub, row.get("sample_path"))
        if path is None:
            raise HTTPException(status_code=404, detail="这条绑定没有归档样本")
        return FileResponse(path, media_type="application/octet-stream", filename=path.name)


    @app.get("/api/vendors/{vendor}/models", tags=["console"], summary="某厂商的模型与限额")
    def vendor_models(vendor: str) -> dict[str, Any]:
        try:
            models = hub.models(vendor)
        except TTSHubError as exc:
            raise HTTPException(status_code=404, detail={"error": exc.to_dict()}) from exc
        return {"vendor": vendor, "models": [m.to_dict() for m in models]}

    @app.get("/api/expiring", tags=["console"], summary="TTL 已过期/将过期的绑定")
    def expiring() -> dict[str, Any]:
        return {"bindings": hub.expiring()}


    @app.get("/api/calls", tags=["console"], summary="调用日志检索（厂商/成败/关键字）")
    def calls(
        limit: int = 50,
        vendor: str | None = None,
        status: str | None = None,
        q: str | None = None,
        range: str = "30d",
    ) -> dict[str, Any]:
        days = _parse_days(range)
        rows = hub.registry.search_calls(
            limit=max(1, min(int(limit), 500)),
            vendor=vendor,
            status=status,
            query=q,
            days=days,
        )
        return {"days": days, "count": len(rows), "calls": rows}


def _parse_days(value: str, *, default: int = 30) -> int:
    """``"30d"`` / ``"30"`` -> 天数，并夹到 1..3650。

    放在本模块而由 :mod:`tts_hub.server.app` 导入，是因为依赖方向只能是
    ``app -> console``（app 要把本模块的路由挂上去）；反过来会成环。
    """
    text = (value or "").strip().lower().rstrip("d")
    try:
        days = int(text)
    except ValueError:
        return default
    return max(1, min(days, 3650))


def _sample_list(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把各绑定上的样本合成一份去重清单（同一段录音绑多家只列一次）。"""
    seen: dict[str, dict[str, Any]] = {}
    for row in bindings:
        path = row.get("sample_path")
        if not path:
            continue
        key = str(row.get("sample_hash") or path)
        entry = seen.setdefault(
            key, {"sample_hash": row.get("sample_hash"), "sample_path": path, "bindings": []}
        )
        entry["bindings"].append({"binding_id": row["id"], "vendor": row["vendor"]})
    return list(seen.values())


def _safe_sample_path(hub: TTSHub, raw: Any) -> pathlib.Path | None:
    """样本路径**必须落在 samples_dir 之内**才允许读。

    路径来自注册表，注册表只有本进程写过——但"能不能读"这件事不该建立在
    "上游一定没写坏"的假设上：真被写进去一条 ``../`` 路径时，这里要挡住。
    """
    if not raw:
        return None
    root = pathlib.Path(hub.settings.samples_dir).resolve()
    target = pathlib.Path(str(raw)).resolve()
    if not target.is_file() or root not in target.parents:
        return None
    return target
