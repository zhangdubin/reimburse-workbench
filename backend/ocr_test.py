"""OCR 融合层自测：图片 / 文字层 PDF / 扫描件 PDF 三条路径都要真的通。

设计原则
--------
1. **样本全部现场合成**，不依赖任何外部素材文件，任何环境直接跑：
       python ocr_test.py
2. **构造的 PDF 是结构合法的**（含 catalog / pages / xref）。随手拼的「最小 PDF」
   pdfium 打不开——`recognize_test.py` 里那个合成 PDF 就是给纯标准库解析器看的，
   拿来做文字层测试会得到空串，别搞混。
3. **分两类断言**：
   - 结构类（探测、派发、边界）任何环境都必须过；
   - 模型类需要真正的 OCR 引擎与中文字体，缺失时**显式 SKIP**并打印原因，
     不算失败（镜像可能刻意不带 OCR 依赖做瘦身），但会在结尾汇总里点出来。

不含服务端依赖，纯离线。
"""

from __future__ import annotations

import io
import os
import sys
import zlib

sys.path.insert(0, ".")

from app import ocr  # noqa: E402

PASS = 0
FAIL = 0
SKIP = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} -> got={got!r} want={want!r}")


def ok(label: str, cond: bool) -> None:
    check(label, bool(cond), True)


def skip(label: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  SKIP {label}（{why}）")


# ------------------------------------------------------------------ 样本合成


def make_pdf(lines: list[str], pages: int = 1) -> bytes:
    """结构合法的 PDF：catalog → pages → N 个 page，带正确的 xref。

    pdfium 对文档结构的完整性有要求，缺 xref / catalog 一律拒开（返回 0 页），
    所以这里老老实实算偏移量表。
    """
    ops = ["BT /F1 12 Tf 40 760 Td 16 TL"]
    for ln in lines:
        esc = ln.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        ops.append(f"({esc}) Tj T*")
    ops.append("ET")
    content = zlib.compress("\n".join(ops).encode("latin-1"))
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(pages))
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        ("<< /Type /Pages /Kids [" + kids + "] /Count " + str(pages) + " >>").encode(),
    ]
    for i in range(pages):
        objs.append(
            ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
             "/Resources << /Font << /F1 " + str(3 + 2 * pages) + " 0 R >> >> /Contents "
             + str(3 + 2 * i + 1) + " 0 R >>").encode()
        )
        objs.append(
            b"<< /Length " + str(len(content)).encode() + b" /Filter /FlateDecode >>\nstream\n"
            + content + b"\nendstream"
        )
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    return _assemble(objs)


def make_scan_pdf(jpeg: bytes, w: int, h: int) -> bytes:
    """整页只有一张位图的 PDF（模拟扫描件）：没有任何文字层。"""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " + str(w).encode() + b" " + str(h).encode()
        + b"] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /XObject /Subtype /Image /Width " + str(w).encode() + b" /Height " + str(h).encode()
        + b" /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
        + str(len(jpeg)).encode() + b" >>\nstream\n" + jpeg + b"\nendstream",
        b"<< /Length 30 >>\nstream\nq " + str(w).encode() + b" 0 0 " + str(h).encode()
        + b" 0 0 cm /Im0 Do Q\nendstream",
    ]
    return _assemble(objs)


def _assemble(objs: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += (b"trailer\n<< /Size " + str(len(objs) + 1).encode() + b" /Root 1 0 R >>\nstartxref\n"
            + str(xref_at).encode() + b"\n%%EOF\n")
    return bytes(out)


# 合成发票照片用的中文字体。容器里可能一个都没有（python:slim 不带字体），
# 那就把依赖字体的用例标 SKIP —— 但下面「栅格化后 OCR」那组是字体无关的，照样跑。
CJK_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]


def cjk_font(size: int = 30):
    from PIL import ImageFont

    for path in CJK_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                continue
    return None


INVOICE_LINES = [
    "销售方名称：深圳市云图科技有限公司",
    "购买方名称：深圳市恒信商贸有限公司",
    "发票号码：24417000000012345678",
    "开票日期：2026年08月14日",
    "价税合计：1280.00",
]


def make_invoice_image(fmt: str = "PNG", exif_rotate: bool = False) -> bytes:
    from PIL import Image, ImageDraw

    font = cjk_font()
    img = Image.new("RGB", (900, 60 * len(INVOICE_LINES) + 40), "white")
    draw = ImageDraw.Draw(img)
    for i, ln in enumerate(INVOICE_LINES):
        draw.text((20, 20 + i * 60), ln, fill="black", font=font)
    buf = io.BytesIO()
    if exif_rotate:
        exif = Image.Exif()
        exif[274] = 6  # Orientation=6：拍摄时顺时针转了 90 度
        img.save(buf, format=fmt, exif=exif)
    else:
        img.save(buf, format=fmt)
    return buf.getvalue()


# ================================================================== 开跑

probe = ocr.probe()
avail = ocr.availability()
engine = ocr.engine_name()
print(f"环境：OCR 引擎={engine}  探测={ {k: v for k, v in probe.items() if k != 'tesseract_bin'} }")

print("\n== 能力探测与降级 ==")
ok("probe 返回全部能力位", set(probe) >= {"rapidocr", "pdfium", "tesseract", "pillow", "numpy"})
ok("availability.engine 合法", avail["engine"] in ("rapidocr", "tesseract", "none"))
ok("ocr_ready 与 engine 一致", avail["ocr_ready"] == (avail["engine"] != "none"))
ok("backends 三个键齐全", set(avail["backends"]) == {"rapidocr", "tesseract", "pdfium"})
ok("note 非空", bool(avail["note"]))
_forced_off = os.environ.get("OCR_ENGINE")
os.environ["OCR_ENGINE"] = "off"
check("OCR_ENGINE=off 时主动降级为 none", ocr.engine_name(), "none")
os.environ["OCR_ENGINE"] = "tesseract"
# 「强制指定后端生效」只有在**装了** tesseract 的环境才成立。部署镜像刻意只带 RapidOCR
# （不跑 apt 装 tesseract），此时强制指定会正确降级为 none —— 这是预期行为，按本脚本
# 「缺依赖显式 SKIP」的约定处理，别把镜像的精简当成缺陷报出来。
if probe.get("tesseract"):
    check("强制指定可用后端生效", ocr.engine_name(), "tesseract")
else:
    check("未装 tesseract 时强制指定降级为 none", ocr.engine_name(), "none")
    skip("强制指定可用后端生效", "本环境未安装 tesseract（部署镜像走 RapidOCR）")
if _forced_off is None:
    os.environ.pop("OCR_ENGINE", None)
else:
    os.environ["OCR_ENGINE"] = _forced_off
check("环境变量还原后回到自动探测", ocr.engine_name(), engine)

print("\n== 扩展名判定 ==")
for name in (".JPG", "a.JPEG", "b.png", "c.webp", "d.bmp", "e.tif", "f.TIFF", "g.gif"):
    ok(f"图片 {name}", ocr.is_image_name(f"发票{name}"))
for name in ("a.pdf", "b.ofd", "c.xml", "d.xlsx", "e.txt", "", "f"):
    ok(f"非图片 {name!r}", not ocr.is_image_name(name))

print("\n== PDF 文字层（结构合法 PDF）==")
text_pdf = make_pdf(["Invoice No: 24417000000099998888", "Total Amount: 520.00", "Seller Name: ACME Ltd"])
text = ocr.pdf_extract_text(text_pdf)
ok("文字层能抽到内容", "24417000000099998888" in text)
got = ocr.ocr_bytes(text_pdf, "invoice.pdf")
ok("派发到 pdf-text 分支", got is not None and got.backend == "pdf-text")
check("行数", len(got.lines) if got else 0, 3)
check("文字层置信度视为 1.0", got.avg_score if got else -1, 1.0)
check("text 属性按行拼接", got.text.splitlines()[0] if got else "", "Invoice No: 24417000000099998888")
ok("文字层不触发模型（耗时极短）", got is not None and got.elapsed_ms < 2000)
ok("按 .pdf 扩展名也能派发", ocr.ocr_bytes(text_pdf, "x.pdf") is not None)

multi = make_pdf(["Invoice No: 24417000000099998888"], pages=3)
check("多页文字层按 max_pages 截断", len(ocr.pdf_extract_text(multi, max_pages=2).splitlines()), 2)
check("默认上限内全取", len(ocr.pdf_extract_text(multi).splitlines()), 3)

print("\n== PDF 栅格化 ==")
pages = ocr.pdf_to_images(text_pdf)
check("单页 PDF 出一张图", len(pages), 1)
if pages:
    ok("输出是 PNG", pages[0][:8] == b"\x89PNG\r\n\x1a\n")
    from PIL import Image

    size = Image.open(io.BytesIO(pages[0])).size
    # MediaBox 595x842 × 默认 scale 2.0
    check("按 scale 放大（595×2）", size[0], 1190)
    check("按 scale 放大（842×2）", size[1], 1684)
check("多页 PDF 全栅格化", len(ocr.pdf_to_images(multi)), 3)
check("栅格化也吃 max_pages", len(ocr.pdf_to_images(multi, max_pages=2)), 2)
check("scale 可覆盖", ocr.pdf_to_images(text_pdf, scale=1.0)[0][:8], b"\x89PNG\r\n\x1a\n")
check("非 PDF 字节不崩", ocr.pdf_to_images(b"not a pdf"), [])

print("\n== 扫描件 PDF（无文字层）==")
from PIL import Image  # noqa: E402

# 关键样本：把「有文字层的 PDF」栅格化成图，再包一层做成扫描件 PDF。
# 这样它的文字层必然是空的，号码只可能来自 OCR —— 才能证明是 OCR 救回来的，
# 而不是又从别处蹭到了字段。
_jpg_buf = io.BytesIO()
_raster = Image.open(io.BytesIO(ocr.pdf_to_images(text_pdf)[0])).convert("RGB")
_raster.save(_jpg_buf, format="JPEG", quality=95)
scan_pdf = make_scan_pdf(_jpg_buf.getvalue(), _raster.size[0], _raster.size[1])
check("扫描件抽不出文字（不许凭空造）", ocr.pdf_extract_text(scan_pdf), "")
check("扫描件可栅格化", len(ocr.pdf_to_images(scan_pdf)), 1)
if engine == "none":
    skip("扫描件走 OCR", "本机无可用 OCR 后端")
else:
    got = ocr.ocr_bytes(scan_pdf, "发票扫描件.pdf")
    ok("走「栅格化 + OCR」分支", got is not None and got.backend == f"pdf+{engine}")
    check("页数记为实际处理页数", got.pages if got else -1, 1)
    ok("号码靠 OCR 救回（文字层为空却认了出来）",
       got is not None and "24417000000099998888" in got.text)
    check("整条链路不上报文字层来源", (got.backend if got else "").startswith("pdf+"), True)

print("\n== 图片直读（栅格化产物，不依赖中文字体）==")
if engine == "none":
    skip("栅格图 OCR", "本机无可用 OCR 后端")
else:
    raster = ocr.pdf_to_images(text_pdf)[0]
    got = ocr.ocr_image(raster, engine)
    ok("识别出内容", bool(got and got.lines))
    joined = got.text if got else ""
    ok("认出英文标签", "Invoice" in joined)
    ok("认出 20 位号码", "24417000000099998888" in joined)
    if got:
        # RapidOCR 的置信度；tesseract 后端不产出分数，故只在有分数时断言
        if got.avg_score:
            ok("平均置信度 > 0.5", got.avg_score > 0.5)
        else:
            skip("平均置信度", f"{engine} 不产出分数")
    check("显式指定 none 后端返回空", ocr.ocr_image(raster, "none"), None)

print("\n== 合成发票照片（中文字体，端到端到字段）==")
if engine == "none":
    skip("中文发票照片识别", "本机无可用 OCR 后端")
elif cjk_font() is None:
    skip("中文发票照片识别", "本机没有中文字体，无法合成样本")
else:
    from app import recognize as R

    png = make_invoice_image()
    got = ocr.ocr_bytes(png, "电子发票.png")
    ok("图片直读命中", got is not None and got.backend == engine)
    ok("行数 >= 5", got is not None and len(got.lines) >= 5)
    r = R.recognize("电子发票.png", png)
    check("发票号码", r["invoice_no"], "24417000000012345678")
    check("开票日期", r["invoice_date"], "2026-08-14")
    check("销售方", r["seller_name"], "深圳市云图科技有限公司")
    check("购买方", r["buyer_name"], "深圳市恒信商贸有限公司")
    check("价税合计", r["amount"], 1280.0)
    check("来源标为 ocr", r["source"], "ocr")
    ok("layers 里记录了 ocr 层", "ocr" in (r.get("layers") or []))
    ok("字段来源逐项可追溯", (r.get("field_sources") or {}).get("invoice_no") == "ocr")

    exif_png = make_invoice_image("JPEG", exif_rotate=True)
    r2 = R.recognize("手机拍照.jpg", exif_png)
    check("带 EXIF 方向的照片仍能识别（号码）", r2["invoice_no"], "24417000000012345678")

    jpg3 = make_invoice_image("JPEG")
    ok("JPEG 字节签名直读（无文件名也认）", ocr.ocr_bytes(jpg3, "") is not None)

print("\n== 图像预处理 ==")
_arr = ocr._load_image(make_invoice_image())
check("转成 RGB 三通道", _arr.shape[2], 3)
if cjk_font() is None:
    skip("EXIF 方向纠正", "本机没有中文字体")
else:
    tilted = ocr._load_image(make_invoice_image("JPEG", exif_rotate=True))
    ok("EXIF 方向被纠正（长边转竖）", tilted.shape[0] > tilted.shape[1])

_saved = ocr.MAX_PIXELS
ocr.MAX_PIXELS = 1000
try:
    shrunk = ocr._load_image(make_invoice_image())
    ok("超像素上限时等比缩小", shrunk.shape[0] * shrunk.shape[1] <= 1000)
finally:
    ocr.MAX_PIXELS = _saved

print("\n== 边界与健壮性 ==")
check("空字节返回 None", ocr.ocr_bytes(b"", "a.png"), None)
check("文件名与内容都不像发票", ocr.ocr_bytes(b"hello world", "notes.txt"), None)
check("损坏图片不抛异常", ocr.ocr_image(b"\xff\xd8\xff\xe0garbage"), None)
check("损坏 PDF 不抛异常", ocr.pdf_extract_text(b"%PDF-1.7 broken"), "")
check("损坏 PDF 栅格化不抛异常", ocr.pdf_to_images(b"%PDF-1.7 broken"), [])
check("空输入不触发模型", ocr.ocr_image(b""), None)
check("OcrResult 空行时布尔为假", bool(ocr.OcrResult(backend="x", lines=[])), False)
check("OcrResult 有行时布尔为真", bool(ocr.OcrResult(backend="x", lines=["a"])), True)
check("OcrResult.text 逐行拼接", ocr.OcrResult(backend="x", lines=["a", "b"]).text, "a\nb")

print(f"\n结果：{PASS} 通过 / {FAIL} 失败" + (f" / {SKIP} 跳过" if SKIP else ""))
if SKIP:
    print("（跳过项需要 OCR 引擎或中文字体；部署镜像若刻意不带这些依赖，属预期降级）")
sys.exit(1 if FAIL else 0)
