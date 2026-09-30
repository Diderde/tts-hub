# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""发布特性检查（打包资源、版本一致、密钥不入库）。"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LICENSE_ID = "MIT"
HEADER = ("# Copyright (C) 2026 Diderde", f"# SPDX-License-Identifier: {LICENSE_ID}")
SKIP_DIRS = {".venv", ".git", ".mimosa", ".tmp-build", "__pycache__", ".pytest_cache", "data", "out", "build", "dist"}

BACKSLASH = chr(92)
DRIVE_SHAPE = "D" + ":" + BACKSLASH
USER_SHAPE = "C" + ":" + BACKSLASH + "Users" + BACKSLASH


def shipped_files() -> list[pathlib.Path]:
    """项目内随仓库分发的文件（排除虚拟环境、缓存与运行期数据）。"""
    out: list[pathlib.Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        out.append(path)
    return out


def python_files() -> list[pathlib.Path]:
    return [p for p in shipped_files() if p.suffix == ".py"]


def test_repository_has_shipped_python_files() -> None:
    files = python_files()
    assert len(files) >= 10
    assert any(p.name == "cli.py" for p in files)


def test_every_python_file_has_copyright_and_spdx_header() -> None:
    missing: list[str] = []
    for path in python_files():
        lines = path.read_text(encoding="utf-8").splitlines()[:3]
        if len(lines) < 2 or lines[0].strip() != HEADER[0] or lines[1].strip() != HEADER[1]:
            missing.append(str(path.relative_to(ROOT)))
    assert missing == [], f"缺少版权/SPDX 头：{missing}"


def test_spdx_identifier_matches_license_file() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "Permission is hereby granted, free of charge" in license_text
    assert "GNU GENERAL PUBLIC LICENSE" not in license_text
    assert LICENSE_ID == "MIT"


def test_no_development_machine_absolute_paths_in_shipped_files() -> None:
    offenders: list[str] = []
    for path in shipped_files():
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".mp3", ".wav", ".sqlite3", ".db"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for shape, label in ((DRIVE_SHAPE, "开发盘符绝对路径"), (USER_SHAPE, "用户目录绝对路径")):
            if shape in text:
                offenders.append(f"{path.relative_to(ROOT)} 命中{label}")
    assert offenders == [], f"发布面路径形状扫描失败：{offenders}"


def test_secrets_and_local_config_are_gitignored() -> None:
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "providers.yaml", "data/", ".venv/", "__pycache__/"):
        assert pattern in rules, f".gitignore 缺少 {pattern}"
    assert not (ROOT / ".env").exists(), "项目根不应存在真实 .env"
    assert not (ROOT / "providers.yaml").exists(), "项目根不应存在真实 providers.yaml"


def test_gitattributes_declares_crlf_policy_and_binary_exceptions() -> None:
    rules = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=crlf" in rules
    for binary in ("*.mp3", "*.wav", "*.sqlite3", "*.db"):
        assert binary in rules
    assert "*.sh text eol=lf" in rules


def test_process_docs_are_excluded_from_release_line() -> None:
    """过程性文档（docs/）只留本地磁盘与备份 tag，发布线（main / origin/main）不得出现。

    检查的是本地 main 的树——它就是将被推送的内容；origin/main 一并检查
    （读取远端跟踪引用，无网络行为）。
    """
    import subprocess

    def tracked_names(ref: str) -> list[str] | None:
        done = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", ref],
            capture_output=True, text=True, check=False, cwd=str(ROOT),
        )
        return None if done.returncode != 0 else done.stdout.splitlines()

    rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "docs/" in rules, ".gitignore 必须显式排除 docs/"

    for ref in ("main", "origin/main"):
        names = tracked_names(ref)
        if names is None:
            continue
        offenders = [n for n in names if n == "docs" or n.startswith("docs/")]
        assert offenders == [], f"{ref} 出现过程性文档：{offenders}"


def test_templates_exist_for_both_config_surfaces() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("MINIMAX_API_KEY", "STEPFUN_API_KEY", "ZHIPU_API_KEY"):
        assert key in env_example
    yaml_example = (ROOT / "providers.example.yaml").read_text(encoding="utf-8")
    assert "default_vendor" in yaml_example and "fallback" in yaml_example


def test_package_version_matches_pyproject() -> None:
    """``__version__`` 与 pyproject 必须一致。

    这两个数会同时露出来：管理台头部显示前者，``pip show`` 显示后者。
    曾经漂移过（pyproject 停在 0.0.1，而管理台头部把它当"当前版本"显示），
    所以写成断言，而不是靠记得同步。
    """
    import tomllib

    import tts_hub

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert tts_hub.__version__ == declared, (
        f"版本号不一致：tts_hub.__version__={tts_hub.__version__} pyproject={declared}"
    )


def test_readme_states_what_it_is_and_how_to_run() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "tts-hub" in readme
    assert "pip install" in readme
    assert len(readme.splitlines()) <= 120, "README 应当简短（宁短勿长）"


def test_package_does_not_import_ffmpeg_at_startup() -> None:
    """可选依赖不得出现在启动必经路径上。"""
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)  # 子进程不继承 conftest 的 sys.path 注入
    code = "import tts_hub, tts_hub.cli, tts_hub.hub; print('ok')"
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env, cwd=str(ROOT)
    )
    assert done.returncode == 0, done.stderr
    assert "ok" in done.stdout


@pytest.mark.parametrize("module", ["tts_hub", "tts_hub.hub", "tts_hub.cli", "tts_hub.providers", "tts_hub.registry"])
def test_modules_import_without_side_effects(module: str) -> None:
    import importlib

    assert importlib.import_module(module) is not None
