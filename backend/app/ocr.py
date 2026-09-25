"""开源 OCR 融合层：把扫描件 / 拍照件 / 图片型 PDF 变成可识别的文本。

为什么需要它
------------
规则引擎再准，前提也是「拿得到文字」。而现实中大量发票是手机拍的照片、
扫描件、或是整页只有一张图的 PDF —— 这些文件里一个字符都没有，
不 OCR 就只能人工补录。

选型
----
- **RapidOCR（rapidocr-onnxruntime）**：主力。PP-OCR 检测+识别模型跑在
  onnxruntime 上，纯 pip 安装、无编译、无系统依赖，中文发票的号码/税号/金额
  实测基本全中。约 300MB（onnxruntime + opencv-headless + numpy）。
- **Tesseract**：备选轻量路线。需要系统装 `tesseract-ocr` 与中文包，体积小得多，
  但发票这种密集小字版式的准确率明显不如 RapidOCR。适合「镜像必须瘦」的场景。
- **PDF 文字层（pypdfium2）**：PDF 先看有没有文字层，有就直接抽，又快又准；
  没有（扫描件）才栅格化后送 OCR。顺带修掉纯标准库解析 PDF 时字体编码的各种坑。

设计约束
--------
1. **可选依赖**：三个后端都做运行期探测，任何一个装不上都不影响服务启动，
   只是能力降级（识别结果里会说明是哪一层没生效）。
2. **懒加载**：RapidOCR 初始化要一两秒，服务启动时不做，第一次真正用到才建，
   且全程只建一次（加锁，避免并发重复加载模型）。
3. **按需栅格化**：PDF 最多处理前若干页，超出的页忽略 —— 发票通常就一页，
   不做无谓的算力浪费。

对外接口
--------
    availability()            -> 能力探测结果，给健康检查与前端提示用
    ocr_bytes(data, filename) -> OcrResult | None   图片 / PDF 统一入口
    ocr_image(data)           -> OcrResult | None   只处理位图
    pdf_extract_text(data)    -> str                只要文字层
    is_image_name(name)       -> bool
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

# 单次 OCR 的页数上限与栅格化倍率。发票基本单页，给到 5 页足够容错。
MAX_PDF_PAGES = int(os.getenv("OCR_MAX_PDF_PAGES", "5") or 5)
PDF_RENDER_SCALE = float(os.getenv("OCR_PDF_SCALE", "2.0") or 2.0)
# 单张图片的像素上限，防止把超大图塞进模型导致内存爆掉
MAX_PIXELS = int(os.getenv("OCR_MAX_PIXELS", "40000000") or 40_000_000)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}

_engine_lock = threading.Lock()
_rapid_engine = None
_probe_cache: dict | None = None


@dataclass
class OcrResult:
    """OCR 结果。保留逐行文本，因为「按行匹配标签」比整段正则准得多。"""

    backend: str
    lines: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    pages: int = 1
    avg_score: float = 0.0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def __bool__(self) -> bool:
        return bool(self.lines)


# ---------------------------------------------------------------- 能力探测


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _tesseract_bin() -> str | None:
    return shutil.which("tesseract")


def probe() -> dict:
    """探测各后端是否可用。结果缓存，避免每次请求都走一遍 import 查找。"""
    global _probe_cache
    if _probe_cache is not None:
        return _probe_cache
    pillow = _has_module("PIL")
    # 两个 OCR 后端都要先把图片解成数组，缺 Pillow 则谁也跑不起来
    rapid = (
        pillow
        and _has_module("numpy")
        and _has_module("cv2")
        and _has_module("onnxruntime")
        and _has_module("rapidocr_onnxruntime")
    )
    tess = _tesseract_bin() if pillow else None
    _probe_cache = {
        "rapidocr": bool(rapid),
        "pdfium": _has_module("pypdfium2"),
        "tesseract": bool(tess),
        "tesseract_bin": tess,
        "pillow": pillow,
        "numpy": _has_module("numpy"),
    }
    return _probe_cache


def availability() -> dict:
    """给接口用的能力摘要（不含本机路径这类内部信息）。"""
    got = probe()
    if got["rapidocr"]:
        engine = "rapidocr"
    elif got["tesseract"]:
        engine = "tesseract"
    else:
        engine = "none"
    return {
        "engine": engine,
        "ocr_ready": engine != "none",
        "pdf_text_layer": got["pdfium"],
        "backends": {
            "rapidocr": got["rapidocr"],
            "tesseract": got["tesseract"],
            "pdfium": got["pdfium"],
        },
        "note": {
            "rapidocr": "PP-OCR 中文模型，识别率最高（推荐）",
            "tesseract": "系统级 OCR，体积小但发票版式准确率一般",
            "pdfium": "PDF 文字层直读 + 扫描件栅格化",
        }.get(engine, "未检测到可用 OCR 后端，图片类发票需要人工补录"),
    }


def engine_name() -> str:
    forced = (os.getenv("OCR_ENGINE") or "auto").strip().lower()
    if forced in ("off", "none", "disable", "disabled"):
        return "none"
    if forced in ("rapidocr", "tesseract"):
        return forced if probe().get(forced) else "none"
    return availability()["engine"]


# ---------------------------------------------------------------- RapidOCR


def _get_rapid():
    """取（并缓存）RapidOCR 实例。首次调用会加载 ONNX 模型，约 1–2 秒。"""
    global _rapid_engine
    if _rapid_engine is not None:
        return _rapid_engine
    with _engine_lock:
        if _rapid_engine is not None:
            return _rapid_engine
        from rapidocr_onnxruntime import RapidOCR

        # 关掉它自带的日志噪声，容器日志里只留我们自己的信息
        os.environ.setdefault("RAPIDOCR_LOG_LEVEL", "ERROR")
        _rapid_engine = RapidOCR()
        return _rapid_engine


def _load_image(data: bytes):
    """字节 -> RGB numpy 数组。带上像素上限与 EXIF 方向纠正。"""
    import numpy as np
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    try:
        # 手机拍的照片常带方向标记，不纠正会把发票转 90 度，OCR 直接崩
        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001
        pass
    img = img.convert("RGB")
    w, h = img.size
    if w * h > MAX_PIXELS:
        ratio = (MAX_PIXELS / (w * h)) ** 0.5
        img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))))
    return np.array(img)


def _rapid_ocr(array) -> tuple[list[str], float]:
    engine = _get_rapid()
    res, _elapse = engine(array)
    lines: list[str] = []
    scores: list[float] = []
    for item in res or []:
        # RapidOCR 返回 [box, text, score]，score 是字符串
        try:
            text = str(item[1]).strip()
            score = float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if not text:
            continue
        lines.append(text)
        scores.append(score)
    avg = round(sum(scores) / len(scores), 3) if scores else 0.0
    return lines, avg


# ---------------------------------------------------------------- Tesseract


def _tesseract_ocr(data: bytes, lang: str = "chi_sim+eng") -> tuple[list[str], float]:
    binary = probe().get("tesseract_bin")
    if not binary:
        return [], 0.0
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001
        pass
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    proc = subprocess.run(
        [binary, "stdin", "stdout", "-l", lang, "--psm", "6"],
        input=buf.getvalue(), capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        # 中文包没装时退回英文，至少别整个失败
        proc = subprocess.run(
            [binary, "stdin", "stdout", "-l", "eng", "--psm", "6"],
            input=buf.getvalue(), capture_output=True, timeout=120,
        )
    lines = [ln.strip() for ln in proc.stdout.decode("utf-8", errors="ignore").splitlines()]
    return [ln for ln in lines if ln], 0.0


# ---------------------------------------------------------------- PDF


def pdf_extract_text(data: bytes, max_pages: int | None = None) -> str:
    """用 PDFium 直读文字层。比手写内容流解析可靠得多（正确处理嵌入字体子集）。"""
    if not probe()["pdfium"]:
        return ""
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(data)
    except Exception:  # noqa: BLE001
        return ""
    chunks: list[str] = []
    try:
        for idx in range(min(len(doc), max_pages or MAX_PDF_PAGES)):
            try:
                chunks.append(doc[idx].get_textpage().get_text_range() or "")
            except Exception:  # noqa: BLE001
                continue
    finally:
        close = getattr(doc, "close", None)
        if callable(close):
            close()
    return "\n".join(c.strip() for c in chunks if c and c.strip())


def pdf_to_images(data: bytes, max_pages: int | None = None, scale: float | None = None) -> list[bytes]:
    """把 PDF 每页栅格化成 PNG 字节。没有文字层的扫描件走这条路。"""
    if not probe()["pdfium"]:
        return []
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(data)
    except Exception:  # noqa: BLE001
        return []
    out: list[bytes] = []
    try:
        for idx in range(min(len(doc), max_pages or MAX_PDF_PAGES)):
            try:
                bitmap = doc[idx].render(scale=scale or PDF_RENDER_SCALE)
                buf = io.BytesIO()
                bitmap.to_pil().convert("RGB").save(buf, format="PNG")
                out.append(buf.getvalue())
            except Exception:  # noqa: BLE001
                continue
    finally:
        close = getattr(doc, "close", None)
        if callable(close):
            close()
    return out


# ---------------------------------------------------------------- 入口


def is_image_name(name: str) -> bool:
    return os.path.splitext((name or "").lower())[1] in IMAGE_EXT


def ocr_image(data: bytes, backend: str = "") -> OcrResult | None:
    """识别一张位图。任何异常都吞掉并返回 None —— OCR 失败不该让整条收票链路挂掉。"""
    if not data:
        return None
    backend = backend or engine_name()
    if backend == "none":
        return None
    started = time.time()
    try:
        if backend == "rapidocr":
            lines, avg = _rapid_ocr(_load_image(data))
        elif backend == "tesseract":
            lines, avg = _tesseract_ocr(data)
        else:
            return None
    except Exception as exc:  # noqa: BLE001
        print(f"[ocr] {backend} 识别失败：{exc!r}")
        return None
    if not lines:
        return None
    return OcrResult(
        backend=backend, lines=lines,
        elapsed_ms=int((time.time() - started) * 1000), pages=1, avg_score=avg,
    )


def ocr_bytes(data: bytes, filename: str = "") -> OcrResult | None:
    """图片 / PDF 统一入口。

    图片直接识别；PDF 先试文字层（有的话没必要跑模型），确实没文字层才栅格化。
    """
    if not data:
        return None
    lower = (filename or "").lower()
    started = time.time()

    if data[:4] == b"%PDF" or lower.endswith(".pdf"):
        text = pdf_extract_text(data)
        # 文字层里至少有若干可读字符，才算「有文字层」
        if len(text.strip()) >= 20:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            return OcrResult(
                backend="pdf-text", lines=lines,
                elapsed_ms=int((time.time() - started) * 1000), pages=1, avg_score=1.0,
            )
        pages = pdf_to_images(data)
        merged: list[str] = []
        backend = engine_name()
        for page in pages:
            got = ocr_image(page, backend)
            if got:
                merged.extend(got.lines)
        if not merged:
            return None
        return OcrResult(
            backend=f"pdf+{backend}", lines=merged,
            elapsed_ms=int((time.time() - started) * 1000), pages=len(pages),
        )

    if is_image_name(lower) or data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
        return ocr_image(data)

    return None
