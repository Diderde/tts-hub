# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""命令行出口层。"""

from __future__ import annotations

import argparse
import contextlib
import json
import pathlib
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from .config import Settings
from .core.audio import TsugumiHazawa
from .core.errors import TTSHubError
from .core.polling import ChisatoShirasagi
from .core.types import SampleInput
from .hub import TTSHub

__all__ = ["build_parser", "main", "murasaki_shion", "usada_pekora"]

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_HUB_ERROR = 2
EXIT_USAGE = 3


def usada_pekora() -> None:
    """把标准输出切到 UTF-8/replace。

    控制台代码页会破坏显示（Windows 默认 GBK），不处理的话中文报告会直接抛
    ``UnicodeEncodeError``；``replace`` 保证再差的终端也只是掉字，不会中断命令。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def murasaki_shion(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> str:
    """把二维数据排成等宽表（空数据也给出表头，避免"看起来像没输出"）。

    列宽按**终端显示宽度**算（CJK/全角字符占两格），不然中文列会把表格挤歪。
    """
    widths = [_disp_width(str(h)) for h in headers]
    body = [[("" if cell is None else str(cell)) for cell in row] for row in rows]
    for row in body:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], _disp_width(cell))
    lines = ["  ".join(_pad(str(h), widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in body:
        lines.append("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def _disp_width(text: str) -> int:
    """终端显示宽度：CJK/全角占 2 格，其余占 1 格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """按显示宽度右补空格（中文列不歪的关键——``ljust`` 按 len 数，数不对）。"""
    return text + " " * (width - _disp_width(text))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS, help="项目根目录（含 providers.yaml / .env）")
    common.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径（相对 root）")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="以 JSON 输出，便于脚本消费")

    parser = argparse.ArgumentParser(
        prog="tts-hub",
        parents=[common],
        description="把多家支持声音克隆的 TTS API 收敛成一套统一调用接口",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    speak = sub.add_parser("speak", parents=[common], help="合成语音")
    speak.add_argument("text", help="待合成文本")
    speak.add_argument("--voice", required=True, help="逻辑音色名/ID，或厂商内 voice_id")
    speak.add_argument("--vendor", default=None)
    speak.add_argument("--model", default=None)
    speak.add_argument("--stream", action="store_true", help="流式合成（按块写文件）")
    speak.add_argument("-o", "--output", default=None, help="输出文件；缺省写到 out/ 下")
    speak.add_argument("--fallback", action="store_true", help="按 providers.yaml 的回退链尝试")

    clone = sub.add_parser("clone", parents=[common], help="克隆音色")
    clone.add_argument("sample", help="样本音频路径或 http(s) URL")
    clone.add_argument("--name", required=True, help="逻辑音色名")
    clone.add_argument("--vendor", default=None)
    clone.add_argument("--model", default=None)
    clone.add_argument("--transcript", default=None, help="样本里说的内容（提升复刻质量）")
    clone.add_argument("--preview-text", default=None, help="试听文本（智谱必填，其他厂商可选）")
    clone.add_argument("--wait", action="store_true", help="轮询到训练完成（轮询型厂商需要）")
    clone.add_argument("--poll-interval", type=float, default=None, help="首次轮询间隔秒数，默认 2")
    clone.add_argument("--poll-timeout", type=float, default=None, help="轮询总超时秒数，默认 900")

    status = sub.add_parser("clone-status", parents=[common], help="查询克隆任务状态")
    status.add_argument("task_id", help="厂商返回的任务号")
    status.add_argument("--vendor", default=None)

    voices = sub.add_parser("voices", parents=[common], help="列出音色")
    voices.add_argument("--vendor", default=None)
    voices.add_argument("--local", action="store_true", help="只列本地注册表里的逻辑音色")

    models = sub.add_parser("models", parents=[common], help="列出某厂商的可用模型")
    models.add_argument("--vendor", default=None)

    sub.add_parser("vendors", parents=[common], help="列出厂商配置与密钥状态")

    cost = sub.add_parser("cost", parents=[common], help="调用与成本汇总")
    cost.add_argument("--days", type=int, default=7)
    cost.add_argument("--by", default="vendor", choices=("vendor", "model", "day"))

    bench = sub.add_parser("bench", parents=[common], help="同一文本在多厂商间对比耗时与费用")
    bench.add_argument("--text", default=None, help="文本或文本文件路径")
    bench.add_argument("--voice", required=True, help="逻辑音色名/ID")
    bench.add_argument("--all", action="store_true", help="对所有已配置密钥的厂商跑一遍")
    bench.add_argument("--vendor", action="append", default=None, help="指定厂商（可重复）")
    bench.add_argument("--model", default=None)

    serve = sub.add_parser("serve", parents=[common], help="起 HTTP 服务（P2 出口层）")
    serve.add_argument("--host", default=None, help="缺省取配置，默认 127.0.0.1")
    serve.add_argument("--port", type=int, default=None, help="缺省取配置，默认 8000")
    serve.add_argument("--reload", action="store_true", help="代码改动自动重载（开发用）")

    return parser


def run_speak(hub: TTSHub, args: argparse.Namespace) -> int:
    text = args.text
    if args.stream:
        result = (
            hub.speak_with_fallback(text, voice=args.voice, model=args.model, stream=True)
            if args.fallback
            else hub.speak(text, voice=args.voice, vendor=args.vendor, model=args.model, stream=True)
        )
    else:
        result = (
            hub.speak_with_fallback(text, voice=args.voice, model=args.model)
            if args.fallback
            else hub.speak(text, voice=args.voice, vendor=args.vendor, model=args.model)
        )
    target = args.output or _default_output(hub.settings, result.format)
    written = result.write_to(target)
    if args.json:
        payload = result.to_dict()
        payload["output"] = str(target)
        payload["written_bytes"] = written
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"已写入 {target}（{written} 字节）")
        print(f"厂商={result.vendor} 模型={result.model} 音色={result.voice_id} "
              f"字符={result.chars} 耗时={result.latency_ms}ms 格式={result.format}")
    return EXIT_OK


def _default_output(settings: Settings, audio_format: str) -> pathlib.Path:
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return settings.out_dir / f"speak-{stamp}.{audio_format or 'mp3'}"


def _clone_progress(task: Any) -> None:
    """轮询进度打到 stderr，避免污染 --json 的 stdout。"""
    detail = f"：{task.message}" if task.message else ""
    print(f"  ... {task.vendor} 状态={task.status}{detail}", file=sys.stderr)


def run_clone(hub: TTSHub, args: argparse.Namespace) -> int:
    raw = args.sample
    sample = SampleInput.from_url(raw) if raw.startswith(("http://", "https://")) else SampleInput.from_path(raw)
    poller = ChisatoShirasagi(
        **{
            key: value
            for key, value in (
                ("interval", args.poll_interval),
                ("timeout", args.poll_timeout),
            )
            if value is not None
        }
    )
    outcome = hub.clone(
        sample,
        vendor=args.vendor,
        model=args.model,
        name=args.name,
        transcript=args.transcript,
        preview_text=args.preview_text,
        wait=args.wait,
        on_progress=_clone_progress if args.wait and not args.json else None,
        poller=poller,
    )
    task = outcome["task"]
    if args.json:
        print(json.dumps({**outcome["task"].to_dict(), "logical_voice": outcome["name"]},
                         ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"逻辑音色 {outcome['name']}（{outcome['voice_id']}）")
    print(f"厂商={task.vendor} 模型={task.model} 厂商音色ID={task.voice_id or '（待训练完成）'} 状态={task.status}")
    if task.message:
        print(task.message)
    if not task.done and task.task_id and not args.wait:
        print(f"提示：该厂商为轮询型，加 --wait 可等到训练完成，"
              f"或用 `tts-hub clone-status {task.task_id} --vendor {task.vendor}` 查询。")
    return EXIT_OK


def run_clone_status(hub: TTSHub, args: argparse.Namespace) -> int:
    task = hub.clone_status(args.task_id, vendor=args.vendor)
    if args.json:
        print(json.dumps(task.to_dict(), ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"厂商={task.vendor} 状态={task.status} 音色ID={task.voice_id or '（尚无）'}")
    if task.message:
        print(task.message)
    return EXIT_OK


def run_voices(hub: TTSHub, args: argparse.Namespace) -> int:
    if args.local:
        rows = [
            (
                item["name"],
                item["id"],
                ", ".join(f"{b['vendor']}:{b['vendor_voice_id']}({b['status']})" for b in item["bindings"])
                or "（未绑定）",
            )
            for item in hub.local_voices()
        ]
        if args.json:
            print(json.dumps(hub.local_voices(), ensure_ascii=False, indent=2))
            return EXIT_OK
        print(murasaki_shion(rows, ("名称", "逻辑ID", "厂商绑定")))
        return EXIT_OK

    vendor = args.vendor or hub.settings.default_vendor
    infos = hub.voices(vendor)
    if args.json:
        print(json.dumps([v.to_dict() for v in infos], ensure_ascii=False, indent=2))
        return EXIT_OK
    remote_rows = [
        (v.voice_id, v.display_name, v.kind, v.model or "", v.created_at or "") for v in infos
    ]
    print(f"# {vendor} 共 {len(infos)} 个音色")
    print(murasaki_shion(remote_rows, ("音色ID", "名称", "类型", "模型", "创建时间")))
    return EXIT_OK


def run_models(hub: TTSHub, args: argparse.Namespace) -> int:
    vendor = args.vendor or hub.settings.default_vendor
    infos = hub.models(vendor)
    if args.json:
        print(json.dumps([m.to_dict() for m in infos], ensure_ascii=False, indent=2))
        return EXIT_OK
    rows = [
        (m.id, "是" if m.supports_clone else "否", "是" if m.supports_stream else "否",
         m.char_limit or "", m.note)
        for m in infos
    ]
    print(murasaki_shion(rows, ("模型", "可复刻", "可流式", "字符上限", "备注")))
    return EXIT_OK


def run_vendors(hub: TTSHub, args: argparse.Namespace) -> int:
    rows = hub.vendors()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK
    table = [
        (
            r["vendor"],
            "启用" if r["enabled"] else "停用",
            "已配置" if r["has_key"] else f"缺 {r['api_key_env']}",
            r["default_model"] or "",
            r["pricing"],
        )
        for r in rows
    ]
    print(murasaki_shion(table, ("厂商", "状态", "密钥", "默认模型", "估算单价")))
    expiring = hub.expiring()
    if expiring:
        print()
        print(f"# 注意：{len(expiring)} 条绑定已过 TTL（厂商侧可能已删除音色）")
        print(murasaki_shion(
            [(b["vendor"], b["vendor_voice_id"], b["expires_at"]) for b in expiring],
            ("厂商", "厂商音色ID", "到期时间"),
        ))
    tool = TsugumiHazawa()
    if not tool.available:
        print()
        print(tool.degrade_note())
    return EXIT_OK


def run_cost(hub: TTSHub, args: argparse.Namespace) -> int:
    report = hub.cost(days=args.days, by=args.by)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_OK
    rows = [
        (r["bucket"] or "(未记录)", r["calls"], r["chars"], f"{float(r['cost'] or 0):.4f}",
         r["ok_calls"], f"{float(r['avg_latency_ms'] or 0):.0f}")
        for r in report["rows"]
    ]
    print(f"# 近 {report['days']} 天，按 {report['by']} 汇总（估算，账单以控制台为准）")
    print(murasaki_shion(rows, ("维度", "调用数", "字符数", "估算费用(元)", "成功数", "平均耗时(ms)")))
    print(f"\n合计估算：{report['total']:.4f} {report['currency']}")
    return EXIT_OK


def run_bench(hub: TTSHub, args: argparse.Namespace) -> int:
    text = _read_text(args.text)
    vendors = args.vendor or [
        name for name in hub.settings.enabled_vendors() if hub.factory.has_key(name)
    ]
    if not vendors:
        print("没有可用厂商：请在 .env 里配置至少一个 API 密钥", file=sys.stderr)
        return EXIT_HUB_ERROR
    rows: list[tuple[Any, ...]] = []
    for vendor in vendors:
        try:
            result = hub.speak(text, voice=args.voice, vendor=vendor, model=args.model)
        except TTSHubError as exc:
            rows.append((vendor, "失败", "", "", "", exc.describe()))
            continue
        target = hub.settings.out_dir / f"bench-{vendor}.{result.format or 'mp3'}"
        result.write_to(target)
        cost = hub.pricing.synth_cost(vendor, result.model, text)
        rows.append(
            (
                vendor,
                "成功",
                result.latency_ms,
                f"{cost:.4f}" if cost is not None else "未登记",
                str(target),
                result.model,
            )
        )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(murasaki_shion(rows, ("厂商", "结果", "耗时(ms)", "估算费用(元)", "输出", "模型")))
    print("\n试听提示：估算费用以控制台账单为准；输出文件可直接播放。")
    return EXIT_OK


def _read_text(value: str | None) -> str:
    """``--text`` 既接受字面文本，也接受一个存在的文本文件路径。"""
    if not value:
        return "你好，这是 TTS-Hub 的多厂商对比试听。"
    candidate = pathlib.Path(value)
    if candidate.is_file():
        return candidate.read_text(encoding="utf-8").strip()
    return value


def run_serve(hub: TTSHub, args: argparse.Namespace) -> int:
    """起 HTTP 服务（P2 出口层）。

    ``fastapi`` / ``uvicorn`` / ``python-multipart`` 是可选依赖：缺失时给出明确
    安装指引并降级退出，而不是抛 ``ImportError`` 把人砸懵；SDK 与其余子命令
    不受影响（它们从不导入这一层）。
    """
    try:
        import uvicorn
    except ImportError:
        print('未安装可选依赖：pip install -e ".[server]"', file=sys.stderr)
        return EXIT_USAGE

    from .server import create_app

    settings = hub.settings
    host = args.host or settings.host
    port = int(args.port or settings.port)
    if (host, port) != (settings.host, settings.port):
        hub.settings = replace(settings, host=host, port=port)
        hub.factory.settings = hub.settings
    if host.strip().lower() not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        print(
            f"警告：监听 {host} 会超出本机范围，且本服务没有鉴权（§8.3 不做用户系统）。"
            "确认这是你要的再继续。",
            file=sys.stderr,
        )
    print(f"TTS-Hub 服务: http://{host}:{port}/docs （默认厂商 {hub.settings.default_vendor}）")
    if args.reload:
        print("提示：--reload 走工厂字符串加载，--root 不生效，工作目录须为项目根。", file=sys.stderr)
        uvicorn.run("tts_hub.server.app:create_app", factory=True, host=host, port=port, reload=True)
    else:
        uvicorn.run(create_app(hub=hub), host=host, port=port)
    return EXIT_OK


HANDLERS = {
    "speak": run_speak,
    "clone": run_clone,
    "clone-status": run_clone_status,
    "voices": run_voices,
    "models": run_models,
    "vendors": run_vendors,
    "cost": run_cost,
    "bench": run_bench,
    "serve": run_serve,
}


def main(argv: Sequence[str] | None = None) -> int:
    usada_pekora()
    parser = build_parser()
    args = parser.parse_args(argv)
    for name, fallback in (("root", None), ("config", "providers.yaml"), ("json", False)):
        if not hasattr(args, name):
            setattr(args, name, fallback)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    from .config import RimiUshigome

    try:
        settings = RimiUshigome(args.root, config_file=args.config).load()
    except (OSError, ValueError) as exc:
        print(f"配置加载失败：{exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        with TTSHub(settings) as hub:
            return HANDLERS[args.command](hub, args)
    except TTSHubError as exc:
        print(f"错误：{exc.describe()}", file=sys.stderr)
        if exc.kind == "auth":
            print("提示：检查 .env 里的密钥，以及账号是否已完成实名认证。", file=sys.stderr)
        return EXIT_HUB_ERROR
    except KeyboardInterrupt:  # pragma: no cover - 交互中断
        print("已中断", file=sys.stderr)
        return EXIT_UNEXPECTED
    except Exception as exc:
        print(f"未预期错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_UNEXPECTED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
