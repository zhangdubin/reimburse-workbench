"""打印用条码 API

GET /api/print/qr?text=ENCODED     → SVG (QR)
GET /api/print/dm?text=ENCODED      → SVG (Data Matrix)

为什么后端做：
  - 前端纯 JS 自实现 QR 码连修两个 bug 仍未稳定
  - pylibdmtx 是 ISO/IEC 16022 标准实现，支持任意二进制 / UTF-8 内容
  - 不引入新 worker / 数据库，纯结构同步
"""
import io

import qrcode
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from qrcode.image.svg import SvgPathImage

# Data Matrix 依赖 C 库 libdmtx（镜像里由 Dockerfile 的 vendor 段离线装入）。
# 这里刻意**不做顶层硬导入**：v2.9.2 及以前写的是
#     from pylibdmtx.pylibdmtx import encode as dmtx_encode
# 一旦镜像里的 libdmtx 不可用（典型场景：在 arm64 机器上现场构建，而 vendor 里只有 amd64 的 .so），
# 这个异常会顺着 main.py 的 routers 导入冒出去，**整个服务都起不来**——
# 明明只有「打印条码」一个功能坏了，却把全站拖死。
# 改成可选导入：坏了只让 /api/print/dm 返回 503 并说清原因，其余功能照常。
try:
    from pylibdmtx.pylibdmtx import encode as dmtx_encode

    DMTX_ERROR = ""
except Exception as _exc:  # noqa: BLE001 - 异常类型随平台/pylibdmtx 版本而变
    dmtx_encode = None
    DMTX_ERROR = f"{type(_exc).__name__}: {_exc}"


def dmtx_ready() -> bool:
    """Data Matrix 是否可用（供健康检查/自检引用）。"""
    return dmtx_encode is not None


router = APIRouter(prefix="/api/print", tags=["打印"])


@router.get("/qr", response_class=Response)
def get_qr(
    text: str = Query(..., max_length=500, description="QR 内容（UTF-8，支持中文）"),
    px: int = Query(8, ge=2, le=20, description="每模块像素（A4 打印 8 较稳）"),
    border: int = Query(2, ge=0, le=4, description="QR 四周白边模块数（ISO 默认 4，打印场景 2 更紧凑）"),
):
    """QR Code（v2.7.4 引入）。"""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=px,
        border=border,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    svg_str = buf.getvalue().decode("utf-8")
    if svg_str.startswith("<?xml"):
        svg_str = svg_str.split("?>", 1)[1].lstrip()
    if "xmlns=" not in svg_str.split(">", 1)[0]:
        svg_str = svg_str.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    return Response(content=svg_str, media_type="image/svg+xml")


def _best_encode(raw: bytes, size: str):
    """两种编码方案都试一遍，取 module 更少的那个。

    实测：短单号 "BX20260924001" 用 Base256 会编成 22×22，用 Ascii 只有 18×18。
    module 数越少 → 同样的打印尺寸下每个格子越大 → 手机拍照越好扫。
    中文等场景 Base256 可能更省，所以两种都算，谁稀疏用谁。
    """
    best = None
    for scheme in ("Ascii", "Base256"):
        try:
            enc = dmtx_encode(raw, size=size, scheme=scheme)
        except Exception:  # noqa: BLE001 - 个别 scheme 对某些内容会拒绝，换下一个
            continue
        cells = (enc.width // 5) * (enc.height // 5)
        if best is None or cells < best[0]:
            best = (cells, enc)
    if best is None:
        raise HTTPException(500, "Data Matrix 编码失败：libdmtx 拒绝了这段内容")
    return best[1]


@router.get("/dm", response_class=Response)
def get_dm(
    text: str = Query(..., max_length=500, description="Data Matrix 内容（UTF-8，支持中文）"),
    px: int = Query(8, ge=2, le=20, description="每模块像素"),
    shape: str = Query("square", pattern="^(square|rectangular)$", description="符号形状"),
    border: int = Query(2, ge=0, le=8, description="静区宽度（module 数，ISO 要求 ≥1，打印取 2 更稳）"),
):
    """Data Matrix（v2.7.7 起，v2.9.21 加大可扫性）。

    pylibdmtx `encode()` 默认 module size = 5px，pixels 是 RGB 三字节数组，
    每 5×5 像素块对应一个 module 的颜色（黑 = (0,0,0)，白 = (255,255,255)）。
    我们按 5×5 步进读取像素，渲染成 px×px 的 <rect>。

    v2.9.21 两处针对「拍照扫不出来」的改进：
      1) 编码方案自动选更稀疏的（Ascii/Base256 谁小用谁）——格子更大
      2) 四周加静区（quiet zone）——贴着别的图案/裁到边就扫不出来了

    libdmtx 不可用时返回 503（而不是让异常冒到框架层），并带上原始报错，便于运维定位。
    """
    if dmtx_encode is None:
        raise HTTPException(
            status_code=503,
            detail=f"Data Matrix 组件不可用（镜像内 libdmtx 未就绪）：{DMTX_ERROR}",
        )
    raw = text.encode("utf-8")
    enc = _best_encode(raw, "SquareAuto" if shape == "square" else "RectAuto")
    mod_w = enc.width // 5
    mod_h = enc.height // 5
    rects = []
    for my in range(mod_h):
        for mx in range(mod_w):
            # 取 5x5 块左上角像素（每个 module 内部颜色一致）
            off = (my * 5 * enc.width + mx * 5) * 3
            if off + 2 < len(enc.pixels) and enc.pixels[off] < 128:
                rects.append(
                    f'<rect x="{(mx + border) * px}" y="{(my + border) * px}" '
                    f'width="{px}" height="{px}"/>'
                )
    w = (mod_w + 2 * border) * px
    h = (mod_h + 2 * border) * px
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" '
        f'shape-rendering="crispEdges">'
        f'<rect width="{w}" height="{h}" fill="#fff"/>'
        + "".join(rects)
        + "</svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")