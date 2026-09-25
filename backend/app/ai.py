"""大模型对接层：一套 OpenAI 兼容协议打通市面主流厂商（纯标准库）。

为什么是 OpenAI 兼容协议
------------------------
国内主流厂商现在都提供了 `/chat/completions` 兼容端点（DeepSeek、通义千问、
Kimi、智谱 GLM、豆包方舟、混元、硅基流动、百度千帆…），海外有 OpenAI、Azure、
以及本地 Ollama / vLLM / LM Studio。写一个协议适配器，就能覆盖全部，
不必为每家写一套 SDK —— 这也符合本项目「零编译依赖」的底线
（官方 SDK 会拖进 httpx / pydantic 版本链，反而增加部署风险）。

分层
----
- 本模块只负责**传输与配置**：请求、流式解析、错误归一、预设厂商、按需解析数据库配置。
- 业务提示词与结构化任务放在 `ai_tasks.py`。
- HTTP 接口放在 `routers/ai.py`。

安全
----
API Key 用 `crypt.py` 加密后落库（与邮箱密码同一套主密钥），接口只回显掩码，
明文永不出后端。所有调用都写 `ai_usage` 表，谁在什么时候问了什么、花了多少 token 可追溯。
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

# ---------------------------------------------------------------- 预设厂商

# 每家只放「能跑通的最小信息」：端点、推荐模型、是否有视觉模型。
# 用户可以在界面上任意改，这里只是省去查文档的功夫。
PROVIDER_PRESETS: list[dict] = [
    {
        "key": "deepseek",
        "label": "DeepSeek 深度求索",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-chat",
        "vision_models": [],
        "docs": "https://platform.deepseek.com",
        "note": "国内直连、价格低，适合做批量识别与文本分析；不支持读图。",
    },
    {
        "key": "qwen",
        "label": "阿里通义千问（百炼）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-long"],
        "default_model": "qwen-plus",
        "vision_models": ["qwen-vl-max", "qwen-vl-plus"],
        "docs": "https://bailian.console.aliyun.com",
        "note": "qwen-vl 系列可直接读发票图片，是拍照件识别的主力。",
    },
    {
        "key": "kimi",
        "label": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["kimi-k2-0905-preview", "moonshot-v1-32k", "moonshot-v1-128k"],
        "default_model": "moonshot-v1-32k",
        "vision_models": ["moonshot-v1-8k-vision-preview"],
        "docs": "https://platform.moonshot.cn",
        "note": "长文本强，适合把整张发票明细贴进去做结构化。",
    },
    {
        "key": "zhipu",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-flash", "glm-4-air"],
        "default_model": "glm-4-flash",
        "vision_models": ["glm-4v-plus", "glm-4v-flash"],
        "docs": "https://open.bigmodel.cn",
        "note": "glm-4-flash 有免费额度，适合先拿来跑通链路。",
    },
    {
        "key": "doubao",
        "label": "字节豆包（火山方舟）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": [],
        "default_model": "",
        "vision_models": [],
        "docs": "https://console.volcengine.com/ark",
        "note": "模型名要填「接入点 ID」（ep- 开头），在方舟控制台创建后复制过来。",
    },
    {
        "key": "hunyuan",
        "label": "腾讯混元",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "models": ["hunyuan-turbos-latest", "hunyuan-large"],
        "default_model": "hunyuan-turbos-latest",
        "vision_models": ["hunyuan-vision"],
        "docs": "https://cloud.tencent.com/product/hunyuan",
        "note": "与腾讯云账号体系打通，已有云资源的话开通最省事。",
    },
    {
        "key": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
        "default_model": "deepseek-ai/DeepSeek-V3",
        "vision_models": ["Qwen/Qwen2.5-VL-72B-Instruct"],
        "docs": "https://cloud.siliconflow.cn",
        "note": "聚合站，一个 Key 能用多家开源模型，切换模型不用换 Key。",
    },
    {
        "key": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
        "default_model": "gpt-4o-mini",
        "vision_models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1-mini"],
        "docs": "https://platform.openai.com",
        "note": "国内访问需要代理，可在下方「网络代理」里填。",
    },
    {
        "key": "azure",
        "label": "Azure OpenAI",
        "base_url": "https://<资源名>.openai.azure.com/openai/deployments/<部署名>",
        "models": [],
        "default_model": "",
        "vision_models": [],
        "docs": "https://oai.azure.com",
        "note": "地址填到部署名，认证方式选 api-key；系统会自动补 ?api-version=。",
        "auth_style": "api-key",
        "query": {"api-version": "2024-10-21"},
    },
    {
        "key": "ollama",
        "label": "Ollama（本地私有部署）",
        "base_url": "http://host.docker.internal:11434/v1",
        "models": ["qwen2.5:14b", "llama3.1:8b", "qwen2.5vl:7b"],
        "default_model": "qwen2.5:14b",
        "vision_models": ["qwen2.5vl:7b", "llava:13b"],
        "docs": "https://ollama.com",
        "note": "完全离线、数据不出内网。容器里请用 host.docker.internal 指向宿主机。",
        "auth_style": "none",
    },
    {
        "key": "custom",
        "label": "自定义 / 自建网关",
        "base_url": "",
        "models": [],
        "default_model": "",
        "vision_models": [],
        "docs": "",
        "note": "任何兼容 OpenAI 协议的服务都行：One-API、vLLM、FastChat、企业内网网关。",
    },
]

PRESET_MAP = {p["key"]: p for p in PROVIDER_PRESETS}

# ---------------------------------------------------------------- 配置

DEFAULT_TIMEOUT = int(os.getenv("AI_TIMEOUT_SEC", "60") or 60)
DEFAULT_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "2048") or 2048)
# 可选：给大模型请求单独走代理（例如访问 OpenAI）。留空则沿用系统代理环境变量。
AI_PROXY = (os.getenv("AI_PROXY") or "").strip()
AI_VERIFY_SSL = (os.getenv("AI_VERIFY_SSL", "1") or "1") not in ("0", "false", "no")


class AiError(RuntimeError):
    """调用大模型失败。message 已经是可以直接给用户看的一句话。"""

    def __init__(self, message: str, *, status: int | None = None, raw: str = ""):
        super().__init__(message)
        self.status = status
        self.raw = raw


@dataclass
class AiConfig:
    """一次调用所需的全部参数（已从数据库解出明文 Key）。"""

    base_url: str
    api_key: str = ""
    model: str = ""
    vision_model: str = ""
    temperature: float = 0.3
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: int = DEFAULT_TIMEOUT
    auth_style: str = "bearer"
    extra_headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    provider_id: int | None = None
    provider_name: str = ""
    preset: str = ""

    @property
    def can_vision(self) -> bool:
        return bool(self.vision_model)


# ---------------------------------------------------------------- 请求构造


def _endpoint(cfg: AiConfig) -> str:
    """拼出 chat/completions 地址。

    用户可能填三种形式，都要能兼容：
      https://api.deepseek.com            -> 补 /v1/chat/completions
      https://api.deepseek.com/v1         -> 补 /chat/completions
      https://xxx/v1/chat/completions     -> 原样使用
    """
    base = (cfg.base_url or "").strip().rstrip("/")
    if not base:
        raise AiError("未配置接口地址（Base URL）")
    if base.endswith("/chat/completions"):
        url = base
    elif re.search(r"/v\d+$", base) or base.endswith("/compatible-mode/v1"):
        url = base + "/chat/completions"
    elif re.search(r"/deployments/[^/]+$", base):
        # Azure 的地址到部署名即止
        url = base + "/chat/completions"
    else:
        url = base + "/v1/chat/completions"
    if cfg.query:
        from urllib.parse import urlencode

        url += ("&" if "?" in url else "?") + urlencode(cfg.query)
    return url


def _headers(cfg: AiConfig) -> dict[str, str]:
    head = {"Content-Type": "application/json", "Accept": "application/json"}
    head.update(cfg.extra_headers or {})
    if cfg.api_key:
        if cfg.auth_style == "api-key":
            head["api-key"] = cfg.api_key
        elif cfg.auth_style != "none":
            head["Authorization"] = f"Bearer {cfg.api_key}"
    return head


def _opener() -> urllib.request.OpenerDirector:
    handlers: list = []
    if AI_PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": AI_PROXY, "https": AI_PROXY}))
    else:
        # 不显式给 ProxyHandler 时 urllib 会自动读环境变量，这里保持默认行为
        handlers.append(urllib.request.ProxyHandler())
    if not AI_VERIFY_SSL:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _friendly_http_error(status: int, body: str) -> str:
    """把厂商各不相同的报错体压成一句人话。"""
    detail = ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            err = data.get("error") or data.get("message") or data
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("type") or "")
            else:
                detail = str(err)
    except Exception:  # noqa: BLE001
        detail = (body or "").strip()[:300]
    detail = detail.strip()
    hints = {
        400: "请求被拒绝（多为模型名写错或参数不支持）",
        401: "认证失败，请检查 API Key 是否正确",
        403: "无权访问该模型，请确认账号已开通对应权限",
        404: "接口地址或模型不存在，请检查 Base URL 与模型名",
        413: "请求体过大，请减少票据内容后重试",
        429: "调用过于频繁或额度已用尽，请稍后重试",
        500: "服务端错误，请稍后重试",
        502: "网关错误，请稍后重试",
        503: "服务暂时不可用，请稍后重试",
        504: "服务响应超时，请稍后重试",
    }
    hint = hints.get(status, f"请求失败（HTTP {status}）")
    return f"{hint}：{detail}" if detail else hint


# ---------------------------------------------------------------- 调用


def _build_body(
    cfg: AiConfig,
    messages: list[dict],
    *,
    model: str | None = None,
    stream: bool = False,
    json_mode: bool = False,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> dict:
    body: dict[str, Any] = {
        "model": model or cfg.model,
        "messages": messages,
        "stream": stream,
        "temperature": cfg.temperature if temperature is None else temperature,
        "max_tokens": max_tokens or cfg.max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def chat(
    cfg: AiConfig,
    messages: list[dict],
    *,
    model: str | None = None,
    json_mode: bool = False,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> tuple[str, dict]:
    """一次非流式调用，返回 (文本, 用量信息)。"""
    if not cfg.model and not model:
        raise AiError("未配置模型名")
    body = _build_body(
        cfg, messages, model=model, json_mode=json_mode,
        max_tokens=max_tokens, temperature=temperature,
    )
    req = urllib.request.Request(
        _endpoint(cfg), data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=_headers(cfg), method="POST",
    )
    started = time.time()
    try:
        with _opener().open(req, timeout=cfg.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise AiError(_friendly_http_error(exc.code, raw), status=exc.code, raw=raw) from None
    except urllib.error.URLError as exc:
        raise AiError(f"无法连接大模型服务（{exc.reason}）。请检查网络或代理设置") from None
    except TimeoutError:
        raise AiError(f"调用超时（超过 {cfg.timeout} 秒），可调大超时或换更快的模型") from None
    except json.JSONDecodeError:
        raise AiError("大模型返回的内容不是合法 JSON，可能是网关拦截了请求") from None

    text = _pick_content(payload)
    usage = payload.get("usage") or {}
    usage["latency_ms"] = int((time.time() - started) * 1000)
    return text, usage


def chat_stream(
    cfg: AiConfig,
    messages: list[dict],
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
    usage_box: dict | None = None,
) -> Iterator[str]:
    """流式调用，逐段吐出文本增量。

    这是**同步生成器**：FastAPI 的 StreamingResponse 会把同步迭代器丢进线程池执行，
    不会阻塞事件循环，也就不必为了流式而引入 asyncio + 队列的复杂度。

    `usage_box` 是可选的小出口：部分厂商会在最后一个分片里带 `usage`（prompt/completion
    tokens），把它塞进这个 dict，调用方就能在生成结束后照常记用量。
    """
    if not cfg.model:
        raise AiError("未配置模型名")
    body = _build_body(cfg, messages, stream=True, max_tokens=max_tokens, temperature=temperature)
    req = urllib.request.Request(
        _endpoint(cfg), data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={**_headers(cfg), "Accept": "text/event-stream"}, method="POST",
    )
    started = time.time()
    try:
        resp = _opener().open(req, timeout=cfg.timeout)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise AiError(_friendly_http_error(exc.code, raw), status=exc.code, raw=raw) from None
    except urllib.error.URLError as exc:
        raise AiError(f"无法连接大模型服务（{exc.reason}）。请检查网络或代理设置") from None

    try:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if usage_box is not None and isinstance(chunk.get("usage"), dict):
                usage_box.update(chunk["usage"])
            piece = _pick_delta(chunk)
            if piece:
                yield piece
    finally:
        resp.close()
        if usage_box is not None:
            usage_box.setdefault("latency_ms", int((time.time() - started) * 1000))


def _pick_content(payload: dict) -> str:
    """从响应里取正文。兼容 OpenAI / 部分厂商把内容放在 reasoning_content 的情况。"""
    choices = payload.get("choices") or []
    if not choices:
        if payload.get("error"):
            raise AiError(_friendly_http_error(200, json.dumps(payload, ensure_ascii=False)))
        return ""
    msg = choices[0].get("message") or {}
    text = msg.get("content")
    if isinstance(text, list):
        # 少数网关按多模态数组回，取其中的 text 段
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    return (text or "").strip()


def _pick_delta(chunk: dict) -> str:
    choices = chunk.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    text = delta.get("content")
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    return text or ""


def test_connection(cfg: AiConfig) -> dict:
    """连通性测试：发一句最短的话，看能不能拿到回复。"""
    started = time.time()
    text, usage = chat(
        cfg,
        [{"role": "user", "content": "回复两个字：正常"}],
        max_tokens=16,
        temperature=0,
    )
    latency = int((time.time() - started) * 1000)
    return {
        "ok": True,
        "latency_ms": latency,
        "model": cfg.model,
        "reply": (text or "")[:100],
        "usage": usage,
        "message": f"连接正常（{latency} 毫秒）",
    }


# ---------------------------------------------------------------- 多模态


def image_part(data: bytes, mime: str = "") -> dict:
    """把图片字节转成 OpenAI 兼容的 image_url 结构（data URL 内联，不依赖公网图床）。"""
    mime = (mime or _sniff_mime(data) or "image/jpeg").lower()
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _sniff_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"%PDF":
        return "application/pdf"
    return ""


def vision_messages(prompt: str, images: Iterable[tuple[bytes, str]]) -> list[dict]:
    """构造一条带图的用户消息。images 为 (字节, mime) 序列。"""
    content: list[dict] = [{"type": "text", "text": prompt}]
    for data, mime in images:
        content.append(image_part(data, mime))
    return [{"role": "user", "content": content}]


# ---------------------------------------------------------------- 输出解析

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> dict | None:
    """从模型回复里抠出 JSON 对象。

    模型很爱在 JSON 外面包一层解释或 ```json 围栏，这里按「围栏 → 整体 →
    首个平衡花括号块」三级兜底，任何一级命中就返回。
    """
    if not text:
        return None
    for candidate in _FENCE.findall(text):
        got = _loads_loose(candidate)
        if got is not None:
            return got
    got = _loads_loose(text)
    if got is not None:
        return got
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                got = _loads_loose(text[start : i + 1])
                if got is not None:
                    return got
                start = -1
    return None


def _loads_loose(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        got = json.loads(text)
    except json.JSONDecodeError:
        return None
    return got if isinstance(got, dict) else None


# ---------------------------------------------------------------- 数据库配置

def provider_to_config(row, *, model: str | None = None) -> AiConfig:
    """把 AiProvider 行转成调用配置（解密 Key）。"""
    from . import crypt

    extra: dict[str, str] = {}
    if row.extra_headers:
        try:
            got = json.loads(row.extra_headers)
            if isinstance(got, dict):
                extra = {str(k): str(v) for k, v in got.items()}
        except (json.JSONDecodeError, TypeError):
            extra = {}
    query: dict[str, str] = {}
    if row.extra_query:
        try:
            got = json.loads(row.extra_query)
            if isinstance(got, dict):
                query = {str(k): str(v) for k, v in got.items()}
        except (json.JSONDecodeError, TypeError):
            query = {}
    return AiConfig(
        base_url=row.base_url or "",
        api_key=crypt.decrypt(row.api_key_enc) if row.api_key_enc else "",
        model=model or row.model or "",
        vision_model=row.vision_model or "",
        temperature=float(row.temperature if row.temperature is not None else 0.3),
        max_tokens=int(row.max_tokens or DEFAULT_MAX_TOKENS),
        timeout=int(row.timeout_sec or DEFAULT_TIMEOUT),
        auth_style=row.auth_style or "bearer",
        extra_headers=extra,
        query=query,
        provider_id=row.id,
        provider_name=row.name,
        preset=row.preset or "",
    )


def resolve_provider(db, provider_id: int | None = None):
    """挑一个可用的 provider：指定 id > 默认 > 第一个启用的。都没有则 None。"""
    from . import models as m

    if provider_id:
        row = db.get(m.AiProvider, provider_id)
        if row and row.enabled:
            return row
        return None
    row = (
        db.query(m.AiProvider)
        .filter(m.AiProvider.enabled.is_(True), m.AiProvider.is_default.is_(True))
        .order_by(m.AiProvider.id.asc())
        .first()
    )
    if row:
        return row
    return (
        db.query(m.AiProvider)
        .filter(m.AiProvider.enabled.is_(True))
        .order_by(m.AiProvider.id.asc())
        .first()
    )


def record_usage(
    db,
    *,
    user,
    kind: str,
    cfg: AiConfig | None,
    ok: bool,
    usage: dict | None = None,
    error: str = "",
    detail: str = "",
) -> None:
    """记一次调用。写失败绝不能影响主流程，所以整段吞异常。"""
    from . import models as m

    try:
        usage = usage or {}
        db.add(
            m.AiUsage(
                user_id=getattr(user, "id", None),
                username=getattr(user, "username", None),
                kind=kind,
                provider_id=getattr(cfg, "provider_id", None),
                provider_name=getattr(cfg, "provider_name", None),
                model=getattr(cfg, "model", None),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                latency_ms=int(usage.get("latency_ms") or 0),
                ok=bool(ok),
                error=(error or "")[:500] or None,
                detail=(detail or "")[:500] or None,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
