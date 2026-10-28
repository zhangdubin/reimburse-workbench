"""OFD 渲染测试（纯合成样本，不依赖外部数据）。

覆盖：多页、TextObject/PathObject/ImageObject、模板层、DrawParam 颜色继承、
zip 条目大小写不敏感、CTM 旋转、坏文件报 OfdError。
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app import ofdrender  # noqa: E402

OK: list[str] = []
BAD: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    (OK if cond else BAD).append(name)
    print(("  ✔ " if cond else "  ✘ ") + name + ("" if cond else f"  [{detail}]"))


PNG_1PX = base64_bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8ffff3f0300050001a5f645400000000049454e44ae426082"
)


def build_ofd(**kw) -> bytes:
    """最小可用 OFD：1 页 + 模板 + 文本 + 红线 + 图片（可按需覆盖）。"""
    pages = kw.get("pages", 1)
    doc_pages = "".join(
        f'<ofd:Page ID="{10 + i}" BaseLoc="Pages/Page_{i}/Content.xml"/>' for i in range(pages)
    )
    doc = (
        '<ofd:Document xmlns:ofd="http://www.ofdspec.org/2016"><ofd:CommonData>'
        '<ofd:MaxUnitID>99</ofd:MaxUnitID>'
        '<ofd:PublicRes>PublicRes.xml</ofd:PublicRes>'
        '<ofd:DocumentRes>DocumentRes.xml</ofd:DocumentRes>'
        '<ofd:TemplatePage ID="1" BaseLoc="Tpls/Tpl_0/Content.xml"/>'
        "</ofd:CommonData>"
        f"<ofd:Pages>{doc_pages}</ofd:Pages></ofd:Document>"
    )
    ofd_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ofd:OFD xmlns:ofd="http://www.ofdspec.org/2016" Version="1.0" DocType="OFD">'
        "<ofd:DocBody><ofd:DocInfo/><ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot></ofd:DocBody></ofd:OFD>"
    )
    pres = (
        '<ofd:Res xmlns:ofd="http://www.ofdspec.org/2016">'
        '<ofd:DrawParam ID="3" LineWidth="0.35"><ofd:StrokeColor Value="255 0 0"/></ofd:DrawParam>'
        '<ofd:DrawParam ID="4" Relative="3"><ofd:FillColor Value="0 0 255"/></ofd:DrawParam>'
        '<ofd:Font ID="7" FontName="宋体"/>'
        "</ofd:Res>"
    )
    dres = (
        '<ofd:Res xmlns:ofd="http://www.ofdspec.org/2016" BaseLoc="Res">'
        '<ofd:MultiMedias><ofd:MultiMedia ID="5" Type="Image" Format="PNG">'
        "<ofd:MediaFile>qr.png</ofd:MediaFile></ofd:MultiMedia></ofd:MultiMedias></ofd:Res>"
    )
    tpl = (
        '<ofd:Page xmlns:ofd="http://www.ofdspec.org/2016"><ofd:Content><ofd:Layer ID="1" DrawParam="3">'
        '<ofd:PathObject ID="2" Boundary="10 10 190 0.4">'
        "<ofd:AbbreviatedData>M 0 0.2 L 190 0.2</ofd:AbbreviatedData></ofd:PathObject>"
        '<ofd:TextObject ID="3" Boundary="60 4 90 8" Font="7" Size="4.2">'
        "<ofd:TextCode X=\"0\" Y=\"3.4\">电子发票（测试模板）</ofd:TextCode></ofd:TextObject>"
        "</ofd:Layer></ofd:Content></ofd:Page>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("OFD.xml", ofd_xml)
        z.writestr("Doc_0/Document.xml", doc)
        z.writestr("Doc_0/PublicRes.xml", pres)
        z.writestr("Doc_0/DocumentRes.xml", dres)
        z.writestr("Doc_0/Tpls/Tpl_0/Content.xml", tpl)
        z.writestr("DOC_0/res/QR.PNG", PNG_1PX)  # 故意大小写不一致
        for i in range(pages):
            body = (
                '<ofd:Page xmlns:ofd="http://www.ofdspec.org/2016">'
                "<ofd:Area><ofd:PhysicalBox>0 0 210 140</ofd:PhysicalBox></ofd:Area>"
                '<ofd:Template TemplateID="1" ZOrder="Background"/>'
                "<ofd:Content><ofd:Layer ID=\"20\">"
                f'<ofd:TextObject ID="21" Boundary="20 {30 + i * 10} 100 6" Font="7" Size="3.5" DrawParam="4">'
                f"<ofd:TextCode X=\"0\" Y=\"2.9\">测试发票 第{i + 1}页 ¥100.00</ofd:TextCode></ofd:TextObject>"
                '<ofd:ImageObject ID="22" Boundary="160 100 30 30" ResourceID="5"/>'
                '<ofd:TextObject ID="23" Boundary="150 20 40 6" Font="7" Size="3.5" CTM="0 1 -1 0 3.5 0">'
                "<ofd:TextCode>竖排</ofd:TextCode></ofd:TextObject>"
                "</ofd:Layer></ofd:Content></ofd:Page>"
            )
            z.writestr(f"Doc_0/Pages/Page_{i}/Content.xml", body)
    return buf.getvalue()


def main() -> None:
    print("== OFD 渲染 ==")
    data = build_ofd(pages=3)
    try:
        pages = ofdrender.render_pages(data)
        check("多页渲染出 3 页", len(pages) == 3, len(pages))
        s1 = pages[0]
        check("SVG 是合法开头", s1.startswith("<svg"), s1[:40])
        check("页面尺寸 210x140mm → 793.7x529.1px", "793.7" in s1 and "529.1" in s1, s1[:120])
        check("模板红线（DrawParam 继承）", "rgb(255,0,0)" in s1, "缺红")
        check("模板文本在底图", "测试模板" in s1, "缺模板文本")
        check("页内值文本（按页错位）", "第1页" in s1 and "第2页" in pages[1] and "第3页" in pages[2])
        check("图片内嵌 dataURI", "data:image/png;base64," in s1)
        check("大小写不一致的资源能解析", "QR" not in s1 and "data:image/png" in s1)
        check("旋转文本有矩阵", "matrix(0,1,-1,0," in s1 or "matrix(0,1,-1,0" in s1)
        check("金额文本保留", "¥100.00" in s1)
    except ofdrender.OfdError as exc:
        check("正常样本渲染", False, exc)

    print("== 异常输入 ==")
    for name, blob in [("空字节", b""), ("非 zip", b"hello world" * 10)]:
        try:
            ofdrender.render_pages(blob)
            check(f"{name} 报 OfdError", False, "没抛")
        except ofdrender.OfdError:
            check(f"{name} 报 OfdError", True)
        except Exception as exc:
            check(f"{name} 报 OfdError", False, f"抛了 {type(exc).__name__}")

    print("== page_count ==")
    check("页数统计", ofdrender.page_count(data) == 3, ofdrender.page_count(data))

    print(f"\n结果：{len(OK)} 通过 / {len(BAD)} 失败")
    if BAD:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
