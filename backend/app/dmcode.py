"""Data Matrix 服务端解码（v2.9.21）

为什么需要它：
  单据上的 Data Matrix 是给手机拍照扫的。浏览器里的 ZXing 对 Data Matrix
  的支持远不如 QR——图一糊、歪一点、码在画面里占比小，就解不出来。
  libdmtx 是 ISO/IEC 16022 的参考实现，解码能力明显更强，而且能调
  shrink / min_edge / max_edge / corrections / deviation 这些参数做重试。

  所以扫码链路改成两段：
    1) 前端 ZXing 本地先解（快、不传图、离线也能用）
    2) 解不出来才把图发到 POST /api/scan/decode，由这里多策略重试

  libdmtx 依赖 C 库，跟 print_qr.py 一样**不做顶层硬导入**：不可用只是
  这个接口降级返回 503-ish 的结果，绝不能把整个服务拖死。
"""
from __future__ import annotations

import io
import time

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

# 解码用的像素上限：手机原图动辄 4000px，全尺寸喂给 libdmtx 又慢又没必要
MAX_PIXELS = 4_000_000
# 整轮解码的软超时（毫秒）：宁可让用户重拍，也别让界面一直转圈
BUDGET_MS = 6000

try:
    from pylibdmtx.pylibdmtx import decode as dmtx_decode

    DMTX_ERROR = ""
except Exception as _exc:  # noqa: BLE001 - 异常类型随平台/版本而变
    dmtx_decode = None
    DMTX_ERROR = f"{type(_exc).__name__}: {_exc}"


def decode_ready() -> bool:
    return dmtx_decode is not None


def _load(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001
        pass
    img = img.convert("RGB")
    w, h = img.size
    if w * h > MAX_PIXELS:
        ratio = (MAX_PIXELS / (w * h)) ** 0.5
        img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
    return img


def _variants(img: Image.Image):
    """由清晰到"用力"的候选图序列。libdmtx 对小而糊的码放大后反而更好解。"""
    w, h = img.size
    gray = ImageOps.grayscale(img)
    yield img
    yield ImageOps.autocontrast(gray).convert("RGB")
    # 码在画面里占比小 / 拍得远：放大 2x、3x 让每个 module 有足够像素
    if max(w, h) < 1400:
        yield img.resize((w * 2, h * 2), Image.LANCZOS)
    if max(w, h) < 900:
        yield img.resize((w * 3, h * 3), Image.LANCZOS)
    yield ImageEnhance.Sharpness(gray).enhance(2.0).convert("RGB")
    # 拍照手歪 / 单据横着拍
    for deg in (90, 180, 270):
        yield img.rotate(deg, expand=True, fillcolor=(255, 255, 255))


def _attempts(img: Image.Image):
    """候选图 × libdmtx 参数组合。先试便宜的，再上贵参数。"""
    import numpy as np

    for cand in _variants(img):
        arr = np.array(cand)
        yield arr, {}
        yield arr, {"shrink": 1, "corrections": True, "timeout": 1500}
        yield arr, {"min_edge": 20, "max_edge": 220, "timeout": 1500}
        yield arr, {"deviation": 60, "threshold": 5, "timeout": 1200}


def decode_dm(data: bytes, budget_ms: int = BUDGET_MS) -> str | None:
    """多策略解码，返回码里的文本；解不出返回 None。"""
    if dmtx_decode is None or not data:
        return None
    started = time.time()
    try:
        img = _load(data)
    except Exception as exc:  # noqa: BLE001
        print(f"[dmcode] 图片读取失败：{exc!r}")
        return None

    for arr, kw in _attempts(img):
        if (time.time() - started) * 1000 > budget_ms:
            break
        try:
            res = dmtx_decode(arr, **kw)
        except Exception as exc:  # noqa: BLE001
            print(f"[dmcode] decode 异常（{kw}）：{exc!r}")
            continue
        if not res:
            continue
        for r in res:
            raw = getattr(r, "data", b"") or b""
            if not raw:
                continue
            try:
                text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            except UnicodeDecodeError:
                text = raw.decode("latin-1", errors="replace") if isinstance(raw, bytes) else str(raw)
            text = text.strip()
            if text:
                return text
    return None
