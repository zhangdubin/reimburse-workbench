"""OFD 版式文件 → SVG 渲染（纯标准库，零外部依赖）。

GB/T 33190-2016 版式文档本质是 zip + XML。这里只实现打印/预览需要的子集：

- Document.xml：CommonData（页面尺寸）、Pages（页清单）、TemplatePage、Annotations
- Page/Tpl/Annots 的 Content.xml：TextObject / PathObject / ImageObject（Layer、CTM、
  FillColor/StrokeColor/Alpha/LineWidth、Boundary、AbbreviatedData 路径）
- Res：MultiMedias 图片（转 data URI 内嵌）、Fonts（映射系统字体名）

坐标单位 mm（1mm ≈ 3.7795px @96dpi）。输出每页一个独立 SVG 字符串，前端用
`<img>`/canvas 直接消费。DeltaX 等逐字微调忽略（版面有 Boundary 锚点，偏差可接受）。
解析失败抛 `OfdError`，调用方按「无法预览」处理。
"""

from __future__ import annotations

import base64
import io
import re
import zipfile
import xml.etree.ElementTree as ET

MM_TO_PX = 96.0 / 25.4
_OFD_NS = "http://www.ofdspec.org/2016"

_FONT_MAP = {
    "楷体": "'Kaiti SC','STKaiti','KaiTi',serif",
    "楷体_GB2312": "'Kaiti SC','STKaiti','KaiTi',serif",
    "宋体": "'SimSun','Songti SC',serif",
    "仿宋": "'FangSong','Fangsong SC',serif",
    "黑体": "'SimHei',sans-serif",
    "小标宋": "'SimSun','Songti SC',serif",
}

_MAX_PAGE = 20
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


class OfdError(Exception):
    """OFD 解析/渲染失败。"""


def _ln(tag: str) -> str:
    """取 {ns}Name 的本地名。"""
    return tag.rsplit("}", 1)[-1]


def _children(el, name: str):
    return [c for c in el if _ln(c.tag) == name]


def _first(el, name: str):
    for c in el:
        if _ln(c.tag) == name:
            return c
    return None


def _texts(el, name: str) -> list[str]:
    """收集直接子元素 <name> 的文本（含无文本的跳过）。"""
    out = []
    for c in _children(el, name):
        if c.text and c.text.strip():
            out.append(c.text.strip())
    return out


def _attr_float(el, key: str, default: float = 0.0) -> float:
    try:
        return float(el.get(key, "").strip())
    except (ValueError, AttributeError):
        return default


def _parse_nums(s: str | None) -> list[float]:
    if not s:
        return []
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", s)]


def _fmt(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s else "0"


class _Ofd:
    def __init__(self, data: bytes):
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise OfdError("不是有效的 OFD（zip）文件") from exc
        self.names = set(self.zf.namelist())
        self._lower = {n.lower(): n for n in self.names if not n.endswith("/")}
        self.doc_root = self._find_doc_root()
        self.doc = self._read_xml(self.doc_root)
        doc_dir = self.doc_root.rsplit("/", 1)[0] if "/" in self.doc_root else ""
        self.doc_dir = doc_dir

    # ---- zip 基础 ----
    def _read(self, name: str) -> bytes:
        if name in self.names:
            return self.zf.read(name)
        real = self._lower.get(name.lower())
        if real:
            return self.zf.read(real)
        raise OfdError(f"OFD 内缺少文件：{name}")

    def _read_xml(self, name: str):
        try:
            return ET.fromstring(self._read(name))
        except ET.ParseError as exc:
            raise OfdError(f"OFD 内 XML 解析失败：{name}") from exc

    def _find_doc_root(self) -> str:
        for cand in ("OFD.xml", "ofd.xml"):
            if cand in self.names:
                root = self._read_xml(cand)
                for el in root.iter():
                    if _ln(el.tag) == "DocRoot" and el.text:
                        base = el.text.strip().lstrip("/").rstrip("/")
                        if base in self.names or base.lower() in self._lower:
                            return base
                        if f"{base}/Document.xml" in self.names:
                            return f"{base}/Document.xml"
                        lower = self._lower.get(f"{base}/document.xml".lower())
                        if lower:
                            return lower
                return cand
        raise OfdError("OFD 缺少 OFD.xml")

    # ---- 相对路径解析 ----
    def _has(self, name: str) -> bool:
        return name in self.names or name.lower() in self._lower

    def _rel(self, base_dir: str, loc: str) -> str:
        """把 OFD 内相对路径解析成真实条目名（大小写不敏感、逐级向上找）。"""
        loc = loc.strip().lstrip("/")
        if self._has(loc):
            return loc
        parts = [p for p in base_dir.split("/") if p]
        while True:
            cand = "/".join(parts + [loc]) if parts else loc
            if self._has(cand):
                return cand
            if not parts:
                break
            parts = parts[:-1]
        return loc

    # ---- 资源：字体与图片 ----
    def _load_res_images(self) -> dict[str, str]:
        """ResourceID → data URI。扫 PublicRes/DocumentRes（也可能被 Page 内引用）。"""
        images: dict[str, str] = {}
        res_files: list[str] = []
        for el in self.doc.iter():
            if _ln(el.tag) in ("PublicRes", "DocumentRes") and el.text:
                res_files.append(self._rel(self.doc_dir, el.text.strip()))
        for rf in res_files:
            try:
                root = self._read_xml(rf)
            except OfdError:
                continue
            base_loc = root.get("BaseLoc", "").strip()
            for mm in root.iter():
                if _ln(mm.tag) != "MultiMedia" or mm.get("Type") != "Image":
                    continue
                rid = mm.get("ID")
                if not rid or rid in images:
                    continue
                for mf in _children(mm, "MediaFile"):
                    if not mf.text:
                        continue
                    loc = self._rel(self.doc_dir or self.doc_root,
                                    self._join_res(base_loc, mf.text.strip()))
                    if not self._has(loc):
                        continue
                    raw = self._read(loc)
                    if not raw or len(raw) > _MAX_IMAGE_BYTES:
                        continue
                    mime = "image/png"
                    low = loc.lower()
                    if low.endswith((".jpg", ".jpeg")) or raw[:3] == b"\xff\xd8\xff":
                        mime = "image/jpeg"
                    elif low.endswith(".gif"):
                        mime = "image/gif"
                    elif raw[:8] != b"\x89PNG\r\n\x1a\n" and raw[:3] != b"\xff\xd8\xff":
                        continue  # 未知格式（如 jbig2）渲染不了，跳过
                    images[rid] = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
                    break
        return images

    @staticmethod
    def _join_res(base_loc: str, fname: str) -> str:
        if not base_loc:
            return fname
        return f"{base_loc.rstrip('/')}/{fname}"

    def _load_fonts(self) -> dict[str, str]:
        """Font ID → font-family。"""
        fonts: dict[str, str] = {}
        for rf in self._res_files():
            try:
                root = self._read_xml(rf)
            except OfdError:
                continue
            for f in root.iter():
                if _ln(f.tag) != "Font":
                    continue
                fid = f.get("ID")
                if not fid:
                    continue
                name = (f.get("FontName") or f.get("FamilyName") or "").strip()
                fonts[fid] = _FONT_MAP.get(name, "'SimSun','Songti SC',serif")
        return fonts

    def _res_files(self) -> list[str]:
        out = []
        for el in self.doc.iter():
            if _ln(el.tag) in ("PublicRes", "DocumentRes") and el.text:
                out.append(self._rel(self.doc_dir, el.text.strip()))
        return out

    def _load_drawparams(self) -> dict[str, dict]:
        """DrawParam ID → {stroke, fill, lw}（沿 Relative 链继承）。"""
        params: dict[str, dict] = {}
        for rf in self._res_files():
            try:
                root = self._read_xml(rf)
            except OfdError:
                continue
            for dp in root.iter():
                if _ln(dp.tag) != "DrawParam" or not dp.get("ID"):
                    continue
                st = _first(dp, "StrokeColor")
                fi = _first(dp, "FillColor")
                params[dp.get("ID")] = {
                    "stroke": (st.get("Value") if st is not None else None),
                    "fill": (fi.get("Value") if fi is not None else None),
                    "lw": _attr_float(dp, "LineWidth", 0.0),
                    "relative": dp.get("Relative"),
                }
        # 展开 Relative 继承
        def resolve(pid: str, seen: set | None = None) -> dict:
            seen = seen or set()
            if pid in seen or pid not in params:
                return {}
            seen.add(pid)
            p = params[pid]
            base = resolve(p.get("relative"), seen) if p.get("relative") else {}
            return {
                "stroke": p.get("stroke") or base.get("stroke"),
                "fill": p.get("fill") or base.get("fill"),
                "lw": p.get("lw") or base.get("lw") or 0.0,
            }
        return {k: resolve(k) for k in params}

    # ---- 页面 ----
    def page_boxes(self) -> list[tuple[float, float, float, float]]:
        """每页 PhysicalBox（x y w h，mm）。优先 Page/Area/PhysicalBox。"""
        boxes = []
        common = _first(self.doc, "CommonData")
        default = None
        if common is not None:
            pb = _first(common, "PageBox")
            if pb is None:
                area = _first(common, "PageArea")
                if area is not None:
                    pb = _first(area, "PhysicalBox")
            if pb is not None and pb.text:
                nums = _parse_nums(pb.text)
                if len(nums) == 4:
                    default = tuple(nums)
        files = self.page_files()
        for i, pf in enumerate(files):
            box = default
            try:
                root = self._read_xml(pf)
                for area in root.iter():
                    if _ln(area.tag) == "PhysicalBox" and area.text:
                        nums = _parse_nums(area.text)
                        if len(nums) == 4:
                            box = tuple(nums)
                        break
            except OfdError:
                pass
            boxes.append(box or (0.0, 0.0, 210.0, 297.0))
        # page_files 为空时至少给默认尺寸
        if not boxes and default:
            boxes.append(default)
        return boxes

    def page_files(self) -> list[str]:
        pages = _first(self.doc, "Pages")
        page_els = _children(pages, "Page") if pages is not None else []
        files = []
        for pe in page_els:
            loc = pe.get("BaseLoc") or ""
            files.append(self._rel(self.doc_dir, loc) if loc else "")
        return files

    def templates(self) -> dict[str, str]:
        """TemplateID → Tpl Content.xml 路径。"""
        out: dict[str, str] = {}
        common = _first(self.doc, "CommonData")
        if common is None:
            return out
        for tp in _children(common, "TemplatePage"):
            tid, loc = tp.get("ID"), tp.get("BaseLoc")
            if tid and loc:
                out[tid] = self._rel(self.doc_dir, loc)
        return out

    def annotations(self) -> dict[int, list[str]]:
        """页序号（0 起）→ Annots XML 文件列表。"""
        out: dict[int, list[str]] = {}
        ann_root_el = None
        for el in self.doc.iter():
            if _ln(el.tag) == "Annotations" and el.text:
                ann_root_el = el
                break
        if ann_root_el is None:
            return out
        ann_root = self._rel(self.doc_dir, ann_root_el.text.strip())
        try:
            root = self._read_xml(ann_root)
        except OfdError:
            return out
        base = ann_root.rsplit("/", 1)[0] if "/" in ann_root else ""
        for page in root.iter():
            if _ln(page.tag) != "PageAnnot":
                continue
            pid = page.get("PageID")  # 结构不统一：PageID / 父级 Page 序号两种流派
            idx = int(pid) if pid and pid.isdigit() else None
            for an in _children(page, "Annot"):
                pass
            if idx is not None:
                out.setdefault(idx, [])
        # 更通用的流派：Annotations.xml → <Page PageID="0"><Annot File="..."/></Page>
        for page in root.iter():
            if _ln(page.tag) != "Page":
                continue
            pid = page.get("PageID")
            files = []
            for an in page.iter():
                if _ln(an.tag) == "Annot" and an.get("File"):
                    files.append(self._rel(base, an.get("File").strip()))
            if files and pid is not None and pid.isdigit():
                out.setdefault(int(pid), files)
        return out


class _PageCtx:
    def __init__(self, ofd: _Ofd, images: dict[str, str], fonts: dict[str, str], dps=None):
        self.ofd = ofd
        self.images = images
        self.fonts = fonts
        self.dps = dps or {}
        self.body: list[str] = []

    # ---- 对象渲染 ----
    def _color(self, el, kind: str) -> tuple[str | None, float]:
        """取 FillColor/StrokeColor：子元素或属性。返回 (css 颜色, alpha 0-1)。"""
        val = None
        alpha = 1.0
        c = _first(el, kind)
        if c is None and el.get(kind):
            val = el.get(kind)
            try:
                alpha = int(el.get("Alpha", "255")) / 255.0
            except ValueError:
                pass
        elif c is not None:
            val = c.get("Value")
            try:
                alpha = int(c.get("Alpha", "255") or "255") / 255.0
            except ValueError:
                pass
        if not val:
            return (None, 1.0)
        nums = _parse_nums(val)
        if len(nums) >= 3:
            rgb = ",".join(str(min(255, max(0, int(v)))) for v in nums[:3])
            if len(nums) >= 4:
                alpha = min(alpha, nums[3] / 255.0)
            return (f"rgb({rgb})", alpha)
        if val.startswith("#"):
            return (val, alpha)
        return (None, 1.0)

    _WRAPPERS = {"Page", "Layer", "Content", "Area", "Template"}

    def _draw_objects(self, container, dp_id=None, depth=0):
        if depth > 12:
            return
        for obj in container:
            name = _ln(obj.tag)
            if name in self._WRAPPERS:
                child_dp = obj.get("DrawParam") or dp_id
                ctm = obj.get("CTM")
                g_open = f'<g transform="matrix({_fmt_ctm(ctm)})">' if ctm else "<g>"
                self.body.append(g_open)
                self._draw_objects(obj, child_dp, depth + 1)
                self.body.append("</g>")
                continue
            if name == "TextObject":
                self._text(obj, dp_id)
            elif name == "ImageObject":
                self._image(obj)
            elif name == "PathObject":
                self._path(obj, dp_id)

    def _tf(self, obj, extra: str = "") -> str:
        b = _parse_nums(obj.get("Boundary"))
        if len(b) != 4:
            return ""
        bx, by = b[0] * MM_TO_PX, b[1] * MM_TO_PX
        tf = f"translate({_fmt(bx)},{_fmt(by)})"
        ctm = obj.get("CTM")
        if ctm:
            tf += f" matrix({_fmt_ctm(ctm)})"
        return f' transform="{tf}{extra}"'

    def _dp(self, dp_id, kind):
        p = self.dps.get(dp_id or "") or {}
        v = p.get(kind)
        if not v:
            return None
        nums = _parse_nums(v)
        if len(nums) >= 3:
            return "rgb(%s)" % ",".join(str(min(255, max(0, int(x)))) for x in nums[:3])
        return None

    def _text(self, obj, dp_id=None):
        b = _parse_nums(obj.get("Boundary"))
        if len(b) != 4:
            return
        size_mm = _attr_float(obj, "Size", 3.0)
        size = size_mm * MM_TO_PX
        fill, alpha = self._color(obj, "FillColor")
        if not fill:
            fill = self._dp(dp_id, "fill") or "#000"
        font = self.fonts.get(obj.get("Font", ""), "'SimSun','Songti SC',serif")
        tf = self._tf(obj)
        for tc in obj.iter():
            if _ln(tc.tag) != "TextCode":
                continue
            txt = (tc.text or "").strip()
            if not txt:
                continue
            x = _attr_float(tc, "X") * MM_TO_PX
            y = _attr_float(tc, "Y", size_mm * 0.8) * MM_TO_PX
            esc = txt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            op = f' fill-opacity="{alpha:.2f}"' if alpha < 0.999 else ""
            self.body.append(
                f'<text x="{_fmt(x)}" y="{_fmt(y)}" font-family="{font}" '
                f'font-size="{_fmt(size)}" fill="{fill}"{op}{tf}>{esc}</text>'
            )

    def _image(self, obj, prefix: str = ""):
        b = _parse_nums(obj.get("Boundary"))
        uri = self.images.get(obj.get("ResourceID", ""))
        if len(b) != 4 or not uri:
            return
        w_mm, h_mm = b[2], b[3]
        w, h = w_mm * MM_TO_PX, h_mm * MM_TO_PX
        tf = ""
        ctm = obj.get("CTM")
        if ctm:
            # OFD 图片的局部空间是「单位方阵」：CTM 缩放分量 ≈ Boundary 宽高。
            # 归一化成纯旋转/平移，避免把图放大 Boundary 倍（实测会变巨型二维码）。
            nums = _parse_nums(ctm)
            while len(nums) < 6:
                nums.append(0.0)
            a, bb, c, d, e, f = nums[:6]
            a_n = a / w_mm if w_mm else a
            d_n = d / h_mm if h_mm else d
            b_n = bb / h_mm if h_mm else bb
            c_n = c / w_mm if w_mm else c
            tf = f' transform="translate({_fmt(b[0] * MM_TO_PX)},{_fmt(b[1] * MM_TO_PX)}) '
            tf += f'matrix({_fmt(a_n)},{_fmt(b_n)},{_fmt(c_n)},{_fmt(d_n)},{_fmt(e * MM_TO_PX)},{_fmt(f * MM_TO_PX)})"'
        else:
            tf = self._tf(obj)
        self.body.append(
            f'<image x="0" y="0" width="{_fmt(w)}" height="{_fmt(h)}" '
            f'preserveAspectRatio="none"{tf} xlink:href="{uri}"/>'
        )

    def _path(self, obj, dp_id=None):
        d_el = _first(obj, "AbbreviatedData")
        d = (d_el.text if d_el is not None else obj.get("d")) or ""
        if not d.strip():
            return
        d = re.sub(r"\bB\b", "C", d)  # OFD 的 B=贝塞尔，SVG 用 C
        fill, f_alpha = self._color(obj, "FillColor")
        stroke, s_alpha = self._color(obj, "StrokeColor")
        if not fill and obj.get("Fill", "").lower() != "true":
            fill = self._dp(dp_id, "fill")
        if not stroke and obj.get("Stroke", "").lower() != "true":
            stroke = self._dp(dp_id, "stroke")
        lw_dp = (self.dps.get(dp_id or "") or {}).get("lw") or 0.0
        is_fill = obj.get("Fill", "").lower() == "true"
        is_stroke = obj.get("Stroke", "").lower() == "true"
        if not is_fill and not is_stroke:
            is_fill = fill is not None
            is_stroke = stroke is not None
        lw = _attr_float(obj, "LineWidth", lw_dp or 0.25) * MM_TO_PX
        attrs = []
        if is_fill and fill:
            attrs.append(f'fill="{fill}"')
            if f_alpha < 0.999:
                attrs.append(f'fill-opacity="{f_alpha:.2f}"')
        else:
            attrs.append('fill="none"')
        if is_stroke and stroke:
            attrs.append(f'stroke="{stroke}" stroke-width="{_fmt(lw)}"')
            if s_alpha < 0.999:
                attrs.append(f'stroke-opacity="{s_alpha:.2f}"')
        self.body.append(f'<path d="{d.strip()}"{" " + " ".join(attrs) if attrs else ""}{self._tf(obj)}/>')

    def svg(self) -> str:
        return "".join(self.body)


def _fmt_ctm(ctm: str | None) -> str:
    nums = _parse_nums(ctm)
    while len(nums) < 6:
        nums.append(0.0 if len(nums) < 4 else (0.0 if len(nums) == 4 else 0.0))
    a, b, c, d, e, f = nums[:6]
    return f"{_fmt(a)},{_fmt(b)},{_fmt(c)},{_fmt(d)},{_fmt(e * MM_TO_PX)},{_fmt(f * MM_TO_PX)}"


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;")


def render_page(ofd: _Ofd, page_idx: int, box: tuple[float, float, float, float],
                images: dict, fonts: dict, templates: dict, ann_files: list[str],
                dps: dict | None = None) -> str:
    x0, y0, w_mm, h_mm = box
    w_px, h_px = w_mm * MM_TO_PX, h_mm * MM_TO_PX
    ctx = _PageCtx(ofd, images, fonts, dps)

    def emit_xml(xml_bytes: bytes, note: str):
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise OfdError(f"{note} XML 解析失败") from exc
        # Page/Layer/Template 都可能包对象
        ctx._draw_objects(root)

    # 1) 模板层（背景）
    page_el_file = ofd.page_files()[page_idx] if page_idx < len(ofd.page_files()) else ""
    page_root = ofd._read_xml(page_el_file) if page_el_file else None
    tpl_ids: list[str] = []
    if page_root is not None:
        for tpl in page_root.iter():
            if _ln(tpl.tag) == "Template" and tpl.get("TemplateID"):
                tpl_ids.append(tpl.get("TemplateID"))
    for tid in tpl_ids:
        f = templates.get(tid)
        if f and f in ofd.names:
            emit_xml(ofd.zf.read(f), "模板")

    # 2) 页面内容
    if page_root is not None:
        content = _first(page_root, "Content")
        src = content if content is not None else page_root
        ctx._draw_objects(src)

    # 3) 注释层（水印/签章）
    for af in ann_files:
        try:
            root = ofd._read_xml(af)
        except OfdError:
            continue
        for an in root.iter():
            if _ln(an.tag) != "Annot":
                continue
            app = _first(an, "Appearance")
            if app is not None:
                ctx._draw_objects(app)

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {_fmt(w_px)} {_fmt(h_px)}" width="{_fmt(w_px)}" height="{_fmt(h_px)}" '
        f'preserveAspectRatio="xMidYMid meet">'
        f'<rect x="0" y="0" width="{_fmt(w_px)}" height="{_fmt(h_px)}" fill="#ffffff"/>'
        f'{ctx.svg()}</svg>'
    )


def render_pages(data: bytes, max_pages: int = _MAX_PAGE) -> list[str]:
    """渲染 OFD 全部页面，返回每页 SVG 字符串列表。"""
    ofd = _Ofd(data)
    images = ofd._load_res_images()
    fonts = ofd._load_fonts()
    dps = ofd._load_drawparams()
    templates = ofd.templates()
    anns = ofd.annotations()
    boxes = ofd.page_boxes()
    files = ofd.page_files()
    n = min(len(files) or len(boxes), max_pages)
    if n <= 0:
        raise OfdError("OFD 没有页面")
    out = []
    for i in range(n):
        box = boxes[i] if i < len(boxes) else (0.0, 0.0, 210.0, 297.0)
        out.append(render_page(ofd, i, box, images, fonts, templates, anns.get(i, []), dps))
    return out


def page_count(data: bytes) -> int:
    ofd = _Ofd(data)
    return max(len(ofd.page_files()), len(ofd.page_boxes()))
