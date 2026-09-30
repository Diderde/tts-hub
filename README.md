# TTS-Hub

把国内多家支持声音克隆的 TTS API 收敛成一套统一调用接口：一个合成入口、一个克隆入口、
一份逻辑音色表，切换厂商只改一个 `vendor` 参数。

- **统一合成**：`speak(文本, 音色) → 音频`，同步与流式走同一条路径。
- **统一克隆**：`clone(样本) → 音色`，同步型与轮询型共用 `CloneTask`。
- **逻辑音色**：一个逻辑音色绑定多家实际 `voice_id`，调用方不感知厂商差异。
- **成本可视**：每次调用记账（字符数 / 耗时 / 估算费用），`tts-hub cost` 汇总。

支持如下厂家：MiniMax、阶跃星辰、智谱 AI（P1）；阿里云百炼、百度智能云、云知声（P3）；
腾讯云、火山引擎、华为云 SIS、科大讯飞（P4）。各家形态差异很大，用之前先看「关键约束」。

双击 `start.bat`（自动建 `.venv`、装依赖、生成 `.env`、起服务，就绪后自动打开浏览器；按任意键停止）。手工：

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
cp .env.example .env                     # 填入三家密钥
cp providers.example.yaml providers.yaml # 按需调整默认厂商与回退链

tts-hub clone ./sample.wav --name 旁白 --vendor stepfun
tts-hub speak "你好" --voice 旁白 -o out/hello.mp3
tts-hub voices --local
tts-hub cost --days 7
```

起 HTTP 服务（P2，需要 `pip install -e ".[server]"`）：

```bash
tts-hub serve                    # 默认 http://127.0.0.1:8000/docs
curl -X POST http://127.0.0.1:8000/api/tts \
  -H "Content-Type: application/json" \
  -d '{"text":"你好","voice":"旁白","vendor":"stepfun"}' --output hello.mp3
```

浏览器打开 `http://127.0.0.1:8000/` 就是**管理台**（P5）：调音台并排试听 A/B 并一键设默认、
音色库（标签/绑定/样本）、厂商启停与限额、成本看板、调用日志检索。页面零构建零 CDN，随包分发。

接口清单：`/api/tts`、`/api/tts/stream`、`/api/clone`、`/api/clone/{task}`、`/api/voices`、
`/api/vendors`、`/api/cost`，外加管理台用的 `/api/calls`、`/api/expiring` 等；
失败响应统一是 `{"error": {"kind", "vendor", "code", "status", "message"}}`。

作为库使用：

```python
from tts_hub import TTSHub, SampleInput

with TTSHub.open(".") as hub:
    hub.clone(SampleInput.from_path("sample.wav"), vendor="stepfun", name="旁白")
    audio = hub.speak("你好", voice="旁白", vendor="stepfun")
    audio.write_to("hello.mp3")
```

## 关键约束

- 密钥只从 `.env` 或环境变量读，不入库、不进日志（错误信息统一过脱敏）。
- 样本先按内容哈希落盘归档（`data/samples/<哈希前两位>/`）；同一样本在多家复用时不会重复归档。
- `providers.yaml` 的 `allow_private_urls` 默认关闭：`http(s)` 音频 URL 必须解析到公网地址，
  才会被下载或转交给厂商；被拒时会给出具体原因（scheme / userinfo / 解析到的地址）。
- ffmpeg 只是可选的本地转码工具，不在启动路径上；缺失时 `tts-hub vendors` 会给出一行降级说明。
- 阶跃复刻只接受 `stepaudio-2.5-tts` / `step-tts-2` / `step-tts-mini`；`stepaudio-3-tts` 仅可用于合成。
- 智谱复刻必须给音色名（`--name`），且 `input`（试听文本）必填，未指定时用内置默认句。
- MiniMax 复刻音色在 168 小时内未被合成接口**正式调用**会被厂商删除（接口内试听不算）；
  注册表记录到期时间，`tts-hub vendors` 会列出已过期的绑定。
- HTTP 服务默认只监听 `127.0.0.1`，且**没有鉴权**（设计上不做用户系统，§8.3）；
  保护手段是绑定地址。改 `host` 前请确认你清楚这一点。
- **阿里云百炼**：CosyVoice 系复刻只收**公网可访问且免鉴权**的音频 URL；本地文件请把
  `clone_model` 设成 `qwen3-tts-vc-2026-01-22`（走 base64 Data URL）。音色创建是同步的，
  但要过审核，状态用 `clone-status` 查（`DEPLOYING`/`OK`/`UNDEPLOYED`）。合成返回的是
  24 小时有效的**下载链接**，adapter 会替你再取一次。音色**不可跨模型**使用。
- **百度智能云**：需要**两把**密钥（`BAIDU_API_KEY` + `BAIDU_SECRET_KEY`）。合成成败看
  响应 `Content-Type` 是否为 `audio/*`——失败时它回 JSON 而 HTTP 状态码可能仍是 200。
  非流式单次上限 500 字符；**流式只有 WebSocket，本 adapter 未实现**。
- **云知声**：**没有 REST 同步合成接口**。本 adapter 把合成走成"提交异步任务 → 轮询到
  完成 → 取音频"，调用方看到的仍是一次 `speak`。`u2-tts-clone` 单次上限 2 万字符。
  `voice_id` 由调用方生成（规则同 MiniMax）。请用**按量付费**的 Key，Token Plan 的 Key
  不可互换且禁止脚本调用。
- **腾讯云**：TC3-HMAC-SHA256 签名（SecretId + SecretKey）。一句话复刻要**照官方文本录**：
  先跑一次 `clone` 拿到该念的文本与 `text_id`，录好后带上它再跑；
  音色 ID 是 `FastVoiceType`（`WCHN-...`），合成时会自动配上 `VoiceType=200000000`。
- **火山引擎**：`X-Api-Resource-Id` 取 `seed-tts-2.0` 这类值（不是 `volc.service_type.*`）；
  **合成是纯 HTTP 的 SSE 流式**，本项目唯一能在 httpx 下真流式的一家。
  ⚠️ 后付费音色**首次合成即扣 138 元且不可逆**，adapter 默认拦住，需显式确认。
- **华为云 SIS**：声音复刻**仅华东-上海一**；鉴权是 IAM **项目级** Token（用错成全局 Token
  会报 SIS.0101）；音色**没有 ID**，`voice_name` 即主键，且官方**没有删除接口**。
- **科大讯飞**：训练是纯 HTTP（两级鉴权 + 五步流程 + 轮询），但**合成只有 WebSocket**——
  `synthesize` 会明确报错并给出算好的 `signed_ws_url()`，不假装成功。
  ⚠️ 训练域官方文档写的是**明文 http**，APIKey 签名头随之明文传输（照官方实现；https 未验证）。

## 目录

```
tts_hub/core        类型、四类归一错误、协议、出站网络、可选音频工具
tts_hub/providers   每家一个 adapter
tts_hub/registry    逻辑音色注册表（SQLite，标准库 sqlite3）
tts_hub/hub.py      解析音色 + 记账 + 回退链
tts_hub/cli.py      命令行出口
tts_hub/server/     FastAPI 出口层 + 管理台页面（可选依赖，P2/P5）
tts_hub/pricing.py  成本估算表
tests/              全程离线（录制回放传输层）
```

## 测试

```bash
.venv\Scripts\python.exe -m pytest
```

单测不依赖网络、或任何宿主环境。验收标准以"代码无误"为准：**单测 + lint + 类型检查全绿**——闸门包在 `tests/test_quality_gates.py`，跑一遍 pytest 即完整验收。
真实厂商流量不是门槛，但上线前建议自查。

## 许可

MIT，见 `LICENSE`。
