"""Jev AI 决策客户端（TypeSafe AI System One）

Jev 是结构化决策模型：状态进去，带概率的类型化答案出来，不生成文本。
适合做分类 / 打分 / 真值判断（路由、异常、重复）。

通过 Vercel AI Gateway 的 HTTP API 调用：

    POST https://ai-gateway.vercel.sh/v1/evaluate
    Authorization: Bearer <AI_GATEWAY_API_KEY>
    {
      "model": "typesafe-ai/jev",
      "state": "<要判定的材料>",
      "questions": {
        "<题 id>": {"type": "boolean", "instructions": "..."},
        "<题 id>": {"type": "choice",  "instructions": "...", "criteria": {"选项key": "判据"}},
        "<题 id>": {"type": "score",   "instructions": "...", "criteria": ["低", "中", "高"]}
      }
    }

    响应：{"answers": {"<题 id>": {"probability": 0.93} / {"choice": "..."} / {"score": 3}}}

**协议坑位（改之前先读，返工过一次）**
  1. 端点是 `/v1/evaluate`。`/v1/ai/jev` 这种路径**从来不存在**，恒 404。
  2. 题型判别符只能是小写 `boolean` / `choice` / `score`；早期资料写的 `Noul` 已改名
     `boolean`，传 `Noul` 会被拒（Invalid discriminator value）。
  3. `questions` 是**对象**（键 = 题 id），不是数组。
  4. 每题的问题文本字段叫 `instructions`，不是 `question`。
  5. `choice` 的选项放在 `criteria`，且必须是**对象**（选项 → 判据描述）；
     `score` 的 `criteria` 反过来是**数组**（从低到高）。两者互换会被拒。
  6. 上下文放 `state`；`context` 不是有效字段。
  7. `confidence` 不在 `answers` 里，在 `providerMetadata.typesafe.confidence`（按题 id 索引）。
  8. 未配置 key 时**优雅降级**：返回 None / -1，AI 工具退回大模型或规则，不报错。
  9. 出错必须留痕（HTTP 状态 + 服务端 message + 可读提示）。早期版本把异常全吞了，
     管理员点「连通性测试」只能看到一句「调用失败」，日志里连错误码都没有——
     排查成本全转嫁给了用户。

实现用 stdlib urllib（与 ai.py 一致，零新依赖）。
参考：https://vercel.com/kb/guide/typesafe-jev-and-ai-sdk
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from . import models as m

JEV_MODEL = "typesafe-ai/jev"
JEV_BASE_URL_DEFAULT = os.getenv("JEV_BASE_URL", "https://ai-gateway.vercel.sh")
JEV_API_KEY_ENV = os.getenv("JEV_API_KEY", "").strip()  # env 仅作 fallback
JEV_TIMEOUT_DEFAULT = float(os.getenv("JEV_TIMEOUT_SEC", "8") or 8)
JEV_VERIFY_SSL = (os.getenv("JEV_VERIFY_SSL", "1") or "1") != "0"
JEV_PROXY = os.getenv("JEV_PROXY", "").strip()  # 留空走 HTTP_PROXY 环境变量

# 运行时配置缓存（admin 在前端改完写库，5 秒内生效）
_config_cache: dict[str, str] = {}
_config_loaded_at: float = 0.0
_CONFIG_TTL = 5.0

# 最近一次调用的错误 / 元信息（给连通性测试与排障用）
_last_error: dict[str, Any] = {}
_last_meta: dict[str, Any] = {}


# ---------------------------------------------------------------- 运行时配置

def _load_runtime_config(db) -> dict[str, str]:
    """从 setting 表读 JEV_* 配置（admin 在前端维护）。"""
    rows = db.query(m.Setting.key, m.Setting.value).filter(
        m.Setting.key.in_(("jev_key", "jev_base_url", "jev_timeout_sec"))
    ).all()
    return {k: (v or "") for (k, v) in rows}


def _load_config_without_db() -> dict[str, str]:
    """没传 db 时的兜底：临时开一个 session 读配置。

    为什么需要：早期 `_cfg(db=None)` 会**直接忽略数据库配置**，只看 env 和缓存。
    只要调用链上有一处忘了把 db 传下来（或传成 None），就会静默退化成「未配置 key」，
    而界面上只会显示一句「连通性测试失败」——极难定位。这里兜一层，让任何调用路径
    都能拿到 admin 在前端配的值。
    """
    try:
        from .database import SessionLocal

        s = SessionLocal()
        try:
            return _load_runtime_config(s)
        finally:
            s.close()
    except Exception:
        # 表还没建好等场景：当作没配置，不抛给调用方
        return {}


def _cfg(db=None) -> tuple[str, str, float]:
    """取运行时配置：api_key / base_url / timeout。db 缺失时也能读到库里的值。"""
    global _config_cache, _config_loaded_at
    now = time.time()
    if now - _config_loaded_at > _CONFIG_TTL:
        loaded = _load_runtime_config(db) if db is not None else _load_config_without_db()
        # 只有真读到东西、或明确查过库（db 不为空）时才刷新时间戳，
        # 避免一次失败的兜底把空配置钉在缓存里 5 秒
        if loaded or db is not None:
            _config_cache = loaded
            _config_loaded_at = now
    api_key = _config_cache.get("jev_key", "") or JEV_API_KEY_ENV
    base_url = _config_cache.get("jev_base_url", "") or JEV_BASE_URL_DEFAULT
    try:
        timeout = float(_config_cache.get("jev_timeout_sec", "") or JEV_TIMEOUT_DEFAULT)
    except (TypeError, ValueError):
        timeout = JEV_TIMEOUT_DEFAULT
    return api_key.strip(), base_url.strip(), timeout


def invalidate_jev_config_cache():
    """admin 改了 jev 配置后调一下，5 秒内生效。"""
    global _config_loaded_at
    _config_loaded_at = 0.0


def evaluate_url(base: str = "") -> str:
    """把各种写法的网关地址规范化成 /v1/evaluate 端点。

    兼容历史值：早期默认值写过 `.../v1/ai/jev`（该路径不存在），这里一并纠正。
    """
    b = (base or "").strip().rstrip("/")
    if not b:
        b = JEV_BASE_URL_DEFAULT.strip().rstrip("/")
    if b.endswith("/ai/jev"):  # 历史错误默认值
        b = b[: -len("/ai/jev")]
    if b.endswith("/evaluate"):
        return b
    if b.endswith("/v1"):
        return b + "/evaluate"
    return b + "/v1/evaluate"


def jev_available(db=None) -> bool:
    """是否配置了 Jev API key。AI 工具调用前快速判断，避免无效 HTTP。"""
    api_key, _, _ = _cfg(db)
    return bool(api_key)


def last_error() -> dict[str, Any]:
    """最近一次失败的结构化信息（无失败则为空 dict）。"""
    return dict(_last_error)


def last_meta() -> dict[str, Any]:
    """最近一次调用的元信息：url / model / status / latency_ms。"""
    return dict(_last_meta)


def _set_error(message: str, status: int = 0, body: str = "", url: str = ""):
    global _last_error
    _last_error = {
        "message": message,
        "status": status,
        "body": (body or "")[:800],
        "url": url or _last_meta.get("url", ""),
    }


# ------------------------------------------------------------------ 底层调用

def _extract_error_message(raw: str) -> str:
    """从 Vercel 错误体里取人话：{"error": {"message": "..."}}。"""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return (raw or "").strip()[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("type") or ""
            return str(msg)[:300]
        if isinstance(err, str):
            return err[:300]
        msg = data.get("message")
        if msg:
            return str(msg)[:300]
    return (raw or "").strip()[:300]


def _diagnose(status: int, message: str) -> str:
    """把网关返回的状态码翻译成可执行的建议。"""
    low = (message or "").lower()
    if status == 401:
        return "API key 无效或已过期，请到 Vercel 控制台重新生成"
    if status == 403:
        if "credit card" in low or "verification" in low:
            return "Vercel AI Gateway 要求账号绑定信用卡（免费额度也需绑卡后才会放行）"
        return "账号或该 key 没有访问 Jev 的权限"
    if status == 404:
        return "网关路径不对，应调用 /v1/evaluate"
    if status == 400:
        return "请求体不符合 Jev 协议（见 jev_client 模块头部的字段说明）"
    if status == 429:
        return "超出配额或被限流，稍后重试"
    if status in (502, 503, 504):
        # 常见于：本机 HTTP_PROXY 指向了不可用的代理，请求被中途接管
        if any(k in low for k in ("connect failed", "connection refused", "upstream")):
            return "连不上网关；若本机设有 HTTP_PROXY，可能是被代理拦了（可用 JEV_PROXY 指定正确代理）"
        return "网关侧故障，稍后重试"
    if status >= 500:
        return "网关侧故障，稍后重试"
    return ""


def _evaluate(questions: dict, state: str = "", db=None) -> tuple[dict | None, str]:
    """发一次 /v1/evaluate。

    返回 (完整响应 dict, 错误串)。成功时错误串为空；失败时响应为 None。
    """
    global _last_meta, _last_error
    api_key, base_url, timeout = _cfg(db)
    url = evaluate_url(base_url)
    _last_meta = {"url": url, "model": JEV_MODEL, "status": 0, "latency_ms": 0}

    if not api_key:
        msg = "未配置 Jev API key（在下方 jev_key 填入 Vercel AI Gateway 的 key）"
        _set_error(msg, 0, "", url)
        return None, msg

    payload = {"model": JEV_MODEL, "state": state or "", "questions": questions or {}}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    handlers: list = []
    if JEV_PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": JEV_PROXY, "https": JEV_PROXY}))
    if not JEV_VERIFY_SSL:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()

    started = time.time()
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        detail = _extract_error_message(raw) or f"HTTP {e.code}"
        hint = _diagnose(e.code, detail)
        msg = f"HTTP {e.code}：{detail}" + (f"（{hint}）" if hint else "")
        _last_meta.update({"status": e.code, "latency_ms": int((time.time() - started) * 1000)})
        _set_error(msg, e.code, raw, url)
        return None, msg
    except urllib.error.URLError as e:
        msg = f"网络不可达：{getattr(e, 'reason', e)}"
        _last_meta.update({"status": 0, "latency_ms": int((time.time() - started) * 1000)})
        _set_error(msg, 0, "", url)
        return None, msg
    except TimeoutError:
        msg = f"请求超时（{timeout:g}s）"
        _last_meta.update({"status": 0, "latency_ms": int((time.time() - started) * 1000)})
        _set_error(msg, 0, "", url)
        return None, msg
    except ValueError as e:
        msg = f"请求构造失败：{e}"
        _set_error(msg, 0, "", url)
        return None, msg

    latency = int((time.time() - started) * 1000)
    _last_meta.update({"status": status, "latency_ms": latency})

    try:
        data = json.loads(body)
    except ValueError:
        msg = f"响应不是合法 JSON：{body[:200]}"
        _set_error(msg, status, body[:800], url)
        return None, msg
    if not isinstance(data, dict):
        msg = f"响应结构异常：{str(data)[:200]}"
        _set_error(msg, status, body[:800], url)
        return None, msg
    if not isinstance(data.get("answers"), dict):
        msg = f"响应缺少 answers 字段：{body[:200]}"
        _set_error(msg, status, body[:800], url)
        return None, msg

    # 成功
    _last_error = {}
    return data, ""


def _answer_of(data: dict, qid: str) -> Any:
    """取某道题的答案值。字段名随题型变化，这里做宽容取值。"""
    node = (data.get("answers") or {}).get(qid)
    if node is None:
        return None
    if not isinstance(node, dict):
        return node
    for k in ("choice", "score", "probability", "value", "answer", "result"):
        if k in node and node[k] is not None:
            return node[k]
    vals = [v for v in node.values() if v is not None]
    return vals[0] if len(vals) == 1 else None


def _confidence_of(data: dict, qid: str) -> float:
    """confidence 不在 answers 里，在 providerMetadata.typesafe.confidence。"""
    try:
        pm = data.get("providerMetadata") or {}
        ts = pm.get("typesafe") or {}
        conf = ts.get("confidence") or {}
        return float(conf.get(qid))
    except (TypeError, ValueError, AttributeError):
        return 0.0


# -------------------------------------------------------------------- 题型封装

QuestionType = str  # "boolean" | "choice" | "score"


def jev_choice(
    question: str,
    options: list[str] | dict[str, str],
    context: str = "",
    db=None,
) -> tuple[str | None, float]:
    """Choice 决策：返回 (选中项, confidence)。

    options 传 list[str] 时，选项名同时作为判据；传 dict{选项: 判据描述} 时按描述判定，
    返回值始终是选项 key。
    """
    if isinstance(options, dict):
        criteria = {str(k): str(v) for k, v in options.items()}
    else:
        criteria = {str(o): str(o) for o in (options or [])}
    if not criteria:
        _set_error("choice 决策需要至少一个选项", 0, "")
        return None, 0.0

    data, _err = _evaluate(
        {"q": {"type": "choice", "instructions": question, "criteria": criteria}},
        state=context or "",
        db=db,
    )
    if data is None:
        return None, 0.0

    ans = _answer_of(data, "q")
    if ans is None:
        return None, 0.0
    ans = str(ans)
    if ans in criteria:
        return ans, _confidence_of(data, "q")
    # 模型偶尔回描述文本而非 key：做一次包含匹配兜底
    for k, desc in criteria.items():
        if desc and (desc == ans or desc in ans or ans in desc):
            return k, _confidence_of(data, "q")
    return ans, _confidence_of(data, "q")


def jev_score(
    question: str,
    scale_min: int = 1,
    scale_max: int = 5,
    context: str = "",
    db=None,
    criteria: list[str] | None = None,
) -> float:
    """Score 决策：返回数值评分（-1 = 未配置 / 失败）。

    criteria 为从低到高的判据数组；不传则按 scale_min~scale_max 生成。
    """
    if criteria is None:
        try:
            lo, hi = int(scale_min), int(scale_max)
        except (TypeError, ValueError):
            lo, hi = 1, 5
        criteria = [str(i) for i in range(lo, hi + 1)] if hi >= lo else ["1"]
    levels = [str(c) for c in criteria]

    data, _err = _evaluate(
        {"q": {"type": "score", "instructions": question, "criteria": levels}},
        state=context or "",
        db=db,
    )
    if data is None:
        return -1.0
    try:
        return float(_answer_of(data, "q"))
    except (TypeError, ValueError):
        return -1.0


def jev_noul(question: str, context: str = "", db=None) -> float:
    """Boolean（旧名 Noul）决策：返回 0~1 真值概率（-1 = 失败）。"""
    data, _err = _evaluate(
        {"q": {"type": "boolean", "instructions": question}},
        state=context or "",
        db=db,
    )
    if data is None:
        return -1.0
    try:
        return max(0.0, min(1.0, float(_answer_of(data, "q"))))
    except (TypeError, ValueError):
        return -1.0


# 语义化别名（新代码建议用这些名字）
jev_boolean = jev_noul


def jev_probe(db=None) -> dict:
    """连通性测试：发一次最小 boolean 决策，返回结构化诊断（成功与失败都返回）。"""
    api_key, base_url, timeout = _cfg(db)
    url = evaluate_url(base_url)
    if not api_key:
        return {
            "ok": False,
            "url": url,
            "model": JEV_MODEL,
            "error": "未配置 Jev API key（在下方 jev_key 填入 Vercel AI Gateway 的 key）",
        }

    started = time.time()
    data, err = _evaluate(
        {"ping": {"type": "boolean", "instructions": "这是连通性测试请求，与材料无关，回答 true 即可。"}},
        state="连通性测试",
        db=db,
    )
    latency = int((time.time() - started) * 1000)
    if data is None:
        return {
            "ok": False,
            "url": url,
            "model": JEV_MODEL,
            "status": _last_meta.get("status", 0),
            "latency_ms": latency,
            "error": err,
            "hint": _diagnose(_last_meta.get("status", 0), err),
        }
    prob = _answer_of(data, "ping")
    try:
        prob = float(prob)
    except (TypeError, ValueError):
        prob = None
    return {
        "ok": True,
        "url": url,
        "model": JEV_MODEL,
        "status": _last_meta.get("status", 200),
        "latency_ms": latency,
        "timeout_sec": timeout,
        "answer": prob,
        "confidence": _confidence_of(data, "ping"),
    }
