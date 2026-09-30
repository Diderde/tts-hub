# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""CLI 出口层测试（离线）。"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import types
from typing import Any

import pytest
from support import AyaMaruyama, shiranui_flare, shirogane_noel

from tts_hub.cli import (
    EXIT_HUB_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    build_parser,
    main,
    murasaki_shion,
    run_bench,
    run_clone,
    run_cost,
    run_models,
    run_serve,
    run_speak,
    run_vendors,
    run_voices,
)

AUDIO = b"ID3-cli-audio"


def stepfun_hub(tmp_path: pathlib.Path, cassette: AyaMaruyama):
    return shiranui_flare(tmp_path, cassette, env={"STEPFUN_API_KEY": "k"}, vendors=("stepfun",))


def speak_args(**kw) -> argparse.Namespace:
    base = {
        "text": "你好",
        "voice": "cixingnansheng",
        "vendor": "stepfun",
        "model": None,
        "stream": False,
        "output": None,
        "fallback": False,
        "json": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_parser_exposes_documented_subcommands() -> None:
    parser = build_parser()
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    names = set(actions[0].choices)
    assert {"speak", "clone", "voices", "models", "vendors", "cost", "bench"} <= names


def test_no_subcommand_prints_help_and_returns_usage_code(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_USAGE
    assert "speak" in capsys.readouterr().out


def test_unknown_subcommand_exits_via_argparse() -> None:
    with pytest.raises(SystemExit):
        main(["nope"])


def test_vendors_lists_config_and_key_state(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["vendors", "--root", str(tmp_path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "minimax" in out and "stepfun" in out and "zhipu" in out
    assert "MINIMAX_API_KEY" in out


def test_vendors_accepts_json_flag(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["vendors", "--root", str(tmp_path), "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert {row["vendor"] for row in payload} >= {"minimax", "stepfun", "zhipu"}


def test_models_is_pure_local_data(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["models", "--vendor", "stepfun", "--root", str(tmp_path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "step-tts-mini" in out
    assert "stepaudio-3-tts" in out


def test_cost_with_empty_registry(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cost", "--root", str(tmp_path)]) == EXIT_OK
    assert "合计估算" in capsys.readouterr().out


def test_vendors_reports_ffmpeg_degradation_when_missing(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg 缺失必须是明确降级提示，而不是崩溃或静默。"""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert main(["vendors", "--root", str(tmp_path)]) == EXIT_OK
    assert "ffmpeg" in capsys.readouterr().out


def test_run_speak_writes_file_and_reports(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    cassette = AyaMaruyama().add("POST", "/v1/audio/speech", content=AUDIO)
    hub = stepfun_hub(tmp_path, cassette)
    target = tmp_path / "out" / "hello.mp3"
    assert run_speak(hub, speak_args(output=str(target))) == EXIT_OK
    assert target.read_bytes() == AUDIO
    out = capsys.readouterr().out
    assert "已写入" in out and "stepfun" in out
    hub.close()


def test_run_speak_defaults_to_timestamped_output(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    cassette = AyaMaruyama().add("POST", "/v1/audio/speech", content=AUDIO)
    hub = stepfun_hub(tmp_path, cassette)
    assert run_speak(hub, speak_args()) == EXIT_OK
    produced = sorted((tmp_path / "out").glob("speak-*.mp3"))
    assert len(produced) == 1
    capsys.readouterr()
    hub.close()


def test_run_speak_json_mode(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    cassette = AyaMaruyama().add("POST", "/v1/audio/speech", content=AUDIO)
    hub = stepfun_hub(tmp_path, cassette)
    assert run_speak(hub, speak_args(json=True, output=str(tmp_path / "a.mp3"))) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["vendor"] == "stepfun"
    assert payload["written_bytes"] == len(AUDIO)
    hub.close()


def clone_args(sample: str, *, json_mode: bool = False, extra: list[str] | None = None) -> argparse.Namespace:
    """用真解析器造参数，避免手写 Namespace 随着子命令加参数而失配。"""
    argv = ["clone", sample, "--name", "旁白", "--vendor", "stepfun", *(extra or [])]
    ns = build_parser().parse_args(argv)
    if not hasattr(ns, "json"):  # 全局开关用 SUPPRESS，解析器不会给默认值
        ns.json = json_mode
    return ns


def test_run_clone_reports_binding(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files", json_body={"id": "file-1"})
        .add("POST", "/v1/audio/voices", json_body={"id": "voice-tone-7"})
    )
    hub = stepfun_hub(tmp_path, cassette)
    sample = shirogane_noel(tmp_path, b"SAMPLE")
    assert run_clone(hub, clone_args(str(sample.path))) == EXIT_OK
    out = capsys.readouterr().out
    assert "voice-tone-7" in out and "旁白" in out
    hub.close()


def test_run_clone_json_mode(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    cassette = (
        AyaMaruyama()
        .add("POST", "/v1/files", json_body={"id": "file-1"})
        .add("POST", "/v1/audio/voices", json_body={"id": "voice-tone-7"})
    )
    hub = stepfun_hub(tmp_path, cassette)
    sample = shirogane_noel(tmp_path, b"SAMPLE")
    assert run_clone(hub, clone_args(str(sample.path), json_mode=True)) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["voice_id"] == "voice-tone-7"
    hub.close()


def test_run_voices_local_view(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    hub = stepfun_hub(tmp_path, AyaMaruyama())
    logical = hub.registry.create_voice("旁白")
    hub.registry.bind(logical, "stepfun", "voice-tone-1")
    args = argparse.Namespace(local=True, vendor=None, json=False)
    assert run_voices(hub, args) == EXIT_OK
    out = capsys.readouterr().out
    assert "旁白" in out and "voice-tone-1" in out
    hub.close()


def test_run_voices_remote_view(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    cassette = AyaMaruyama().add(
        "GET", "/v1/audio/voices", json_body={"object": "list", "data": [{"id": "voice-tone-2"}]}
    )
    hub = stepfun_hub(tmp_path, cassette)
    assert run_voices(hub, argparse.Namespace(local=False, vendor="stepfun", json=False)) == EXIT_OK
    assert "voice-tone-2" in capsys.readouterr().out
    hub.close()


def test_run_models_and_vendors_and_cost(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    cassette = AyaMaruyama().add("POST", "/v1/audio/speech", content=AUDIO)
    hub = stepfun_hub(tmp_path, cassette)
    assert run_models(hub, argparse.Namespace(vendor="stepfun", json=False)) == EXIT_OK
    assert run_vendors(hub, argparse.Namespace(json=False)) == EXIT_OK
    hub.speak("你好世界", voice="v", vendor="stepfun")
    assert run_cost(hub, argparse.Namespace(days=7, by="vendor", json=False)) == EXIT_OK
    out = capsys.readouterr().out
    assert "合计估算" in out and "step-tts-mini" in out
    hub.close()


def test_run_bench_reports_failure_row_for_broken_vendor(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cassette = AyaMaruyama().add("POST", "/v1/audio/speech", status=401, json_body={"error": {"code": "401"}})
    hub = stepfun_hub(tmp_path, cassette)
    args = argparse.Namespace(text="你好", voice="v", all=True, vendor=["stepfun"], model=None, json=False)
    assert run_bench(hub, args) == EXIT_OK
    out = capsys.readouterr().out
    assert "失败" in out
    hub.close()


def test_run_bench_without_any_key(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    hub = shiranui_flare(tmp_path, AyaMaruyama())
    args = argparse.Namespace(text="你好", voice="v", all=True, vendor=None, model=None, json=False)
    assert run_bench(hub, args) == EXIT_HUB_ERROR
    assert "密钥" in capsys.readouterr().err
    hub.close()


def test_table_renderer_handles_empty_rows() -> None:
    text = murasaki_shion([], ("厂商", "状态"))
    assert "厂商" in text and "状态" in text
    assert len(text.splitlines()) == 2


def test_table_renderer_pads_columns() -> None:
    text = murasaki_shion([("a", "bbbb"), ("cccc", "d")], ("k", "v"))
    lines = text.splitlines()
    assert lines[0].startswith("k ")
    assert len(lines) == 4


def test_main_maps_hub_error_to_exit_code(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    """归一错误要转成可读提示 + 退出码 2，而不是裸 traceback。

    用"未声明的厂商"触发 ProviderError：在工厂里就失败，不发任何网络请求，
    因此这条断言离线且确定。
    """
    code = main(["speak", "你好", "--voice", "v", "--vendor", "no-such-vendor", "--root", str(tmp_path)])
    assert code == EXIT_HUB_ERROR
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "错误" in captured.err


def test_global_flags_work_before_subcommand(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--root", str(tmp_path), "vendors"]) == EXIT_OK
    assert "minimax" in capsys.readouterr().out


class ChisatoShirasagi(types.ModuleType):
    """替身 uvicorn：只记录 ``run`` 的参数，不做任何绑定。"""

    def __init__(self) -> None:
        super().__init__("uvicorn")
        self.calls: list[dict[str, Any]] = []

    def run(self, app: Any = None, **kw: Any) -> None:
        self.calls.append({"app": app, **kw})


def serve_args(**kw) -> argparse.Namespace:
    base = {"host": None, "port": None, "reload": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_run_serve_degrades_when_optional_deps_missing(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """可选依赖缺失要给安装指引，而不是抛 ImportError。"""
    monkeypatch.setitem(sys.modules, "uvicorn", None)  # `import uvicorn` 会因此抛 ImportError
    hub = stepfun_hub(tmp_path, AyaMaruyama())
    assert run_serve(hub, serve_args()) == EXIT_USAGE
    assert ".[server]" in capsys.readouterr().err
    hub.close()


def test_run_serve_defaults_to_loopback_without_warning(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = ChisatoShirasagi()
    monkeypatch.setitem(sys.modules, "uvicorn", stub)
    hub = stepfun_hub(tmp_path, AyaMaruyama())
    assert run_serve(hub, serve_args()) == EXIT_OK
    assert "超出本机范围" not in capsys.readouterr().err
    assert stub.calls[0]["host"] == "127.0.0.1"
    assert stub.calls[0]["port"] == 8000
    assert hub.settings.exposes_lan is False
    hub.close()


def test_run_serve_warns_on_non_loopback_and_reflects_effective_address(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """对外暴露要显式告警；且配置对象要反映真实监听地址，别让 /api/health 报错端口。"""
    stub = ChisatoShirasagi()
    monkeypatch.setitem(sys.modules, "uvicorn", stub)
    hub = stepfun_hub(tmp_path, AyaMaruyama())
    assert run_serve(hub, serve_args(host="0.0.0.0", port=8123)) == EXIT_OK
    assert "超出本机范围" in capsys.readouterr().err
    assert stub.calls[0]["host"] == "0.0.0.0"
    assert stub.calls[0]["port"] == 8123
    assert hub.settings.host == "0.0.0.0"
    assert hub.settings.port == 8123
    assert hub.settings.exposes_lan is True
    hub.close()


def test_run_serve_reload_uses_factory_string(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = ChisatoShirasagi()
    monkeypatch.setitem(sys.modules, "uvicorn", stub)
    hub = stepfun_hub(tmp_path, AyaMaruyama())
    assert run_serve(hub, serve_args(reload=True)) == EXIT_OK
    call = stub.calls[0]
    assert call["app"] == "tts_hub.server.app:create_app"
    assert call["factory"] is True and call["reload"] is True
    assert "--root 不生效" in capsys.readouterr().err
    hub.close()
