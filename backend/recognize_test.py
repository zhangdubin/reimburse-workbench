"""发票识别引擎自测：构造各类型样本，断言字段抽取正确。

这些样本都是**合成数据**（号码/公司名均为虚构），不依赖外部文件，
可以在任何环境直接跑：`python recognize_test.py`
"""

from __future__ import annotations

import io
import sys
import zipfile
import zlib

sys.path.insert(0, ".")

from app import recognize as R  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
        print(f"  PASS {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} -> got={got!r} want={want!r}")


OFD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice>
  <InvoiceNumber>24417000000012345678</InvoiceNumber>
  <InvoiceCode>044001900111</InvoiceCode>
  <IssueTime>2026-08-14</IssueTime>
  <SellerName>深圳市云图科技有限公司</SellerName>
  <SellerIdNum>91440300MA5F1234XA</SellerIdNum>
  <BuyerName>深圳市恒信商贸有限公司</BuyerName>
  <BuyerIdNum>91440300MA5G5678XB</BuyerIdNum>
  <TotalAmtWithTax>1280.00</TotalAmtWithTax>
  <TotalTax>147.61</TotalTax>
  <TaxRate>13</TaxRate>
</Invoice>"""


def make_ofd() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("OFD.xml", "<OFD/>")
        z.writestr("Doc_0/OriginalInvoice.xml", OFD_XML)
    return buf.getvalue()


def make_xlsx() -> bytes:
    strings = (
        "<sst>"
        "<si><t>发票号码</t></si><si><t>24992000000087654321</t></si>"
        "<si><t>价税合计</t></si><si><t>3560.50</t></si>"
        "<si><t>开票日期</t></si><si><t>2026-07-02</t></si>"
        "<si><t>销方名称</t></si><si><t>广州市启明电子有限公司</t></si>"
        "</sst>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", strings)
        z.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
    return buf.getvalue()


def make_pdf() -> bytes:
    content = (
        b"BT /F1 10 Tf 50 700 Td (Fa Piao Hao Ma: 24417000000099998888) Tj "
        b"0 -14 Td (Jia Shui He Ji: 520.00) Tj ET"
    )
    comp = zlib.compress(content)
    head = b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(comp)).encode() + b" /Filter /FlateDecode >>\nstream\n"
    return head + comp + b"\nendstream\nendobj\ntrailer\n%%EOF"


# 真实数电票 PDF 的结构：中文用 Identity-H 子集字体，内容是字形码，
# 必须靠字体的 ToUnicode CMap 才能还原。早先的实现只扫 (…) 字面量，
# 这类票只能识别出号码和日期，金额/购销方/税号全丢——这是「识别不全」的主因。
def make_pdf_with_cmap() -> bytes:
    texts = ["发票号码", "开票日期", "销售方名称", "购买方名称", "统一社会信用代码",
             "价税合计", "小写", "税额", "年月日",
             "深圳市云图科技有限公司", "深圳市恒信商贸有限公司"]
    chars: list[str] = []
    for t in texts:
        for ch in t:
            if ch not in chars:
                chars.append(ch)
    cid = {ch: i + 1 for i, ch in enumerate(chars)}

    def h4(n: int) -> str:
        return f"{n:04X}"

    bfchar = "\n".join(f"<{h4(cid[c])}> <{h4(ord(c))}>" for c in chars)
    cmap = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CMapName /AAAAAA+SimSun def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{len(chars)} beginbfchar\n{bfchar}\nendbfchar\n"
        "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
    ).encode("latin-1")

    def cn(text: str) -> str:
        return "<" + "".join(h4(cid[c]) for c in text) + ">"

    ops = [
        f"BT /F1 10 Tf 30 700 Td {cn('发票号码')} Tj ET",
        "BT /F2 10 Tf 120 700 Td (24417000000012345678) Tj ET",
        f"BT /F1 10 Tf 30 680 Td {cn('开票日期')} Tj ET",
        "BT /F2 10 Tf 120 680 Td (2026) Tj ET",
        f"BT /F1 10 Tf 150 680 Td {cn('年')} Tj ET",
        "BT /F2 10 Tf 170 680 Td (08) Tj ET",
        f"BT /F1 10 Tf 195 680 Td {cn('月')} Tj ET",
        "BT /F2 10 Tf 215 680 Td (14) Tj ET",
        f"BT /F1 10 Tf 235 680 Td {cn('日')} Tj ET",
        f"BT /F1 10 Tf 30 660 Td {cn('销售方名称')} Tj ET",
        f"BT /F1 10 Tf 120 660 Td {cn('深圳市云图科技有限公司')} Tj ET",
        f"BT /F1 10 Tf 30 645 Td {cn('统一社会信用代码')} Tj ET",
        "BT /F2 10 Tf 160 645 Td (91440300MA5F1234XA) Tj ET",
        f"BT /F1 10 Tf 30 625 Td {cn('购买方名称')} Tj ET",
        f"BT /F1 10 Tf 120 625 Td {cn('深圳市恒信商贸有限公司')} Tj ET",
        f"BT /F1 10 Tf 30 610 Td {cn('统一社会信用代码')} Tj ET",
        "BT /F2 10 Tf 160 610 Td (91440300MA5G5678XB) Tj ET",
        f"BT /F1 10 Tf 30 590 Td {cn('价税合计')} Tj ET",
        f"BT /F1 10 Tf 90 590 Td {cn('小写')} Tj ET",
        "BT /F2 10 Tf 140 590 Td (1280.00) Tj ET",
        f"BT /F1 10 Tf 30 575 Td {cn('税额')} Tj ET",
        "BT /F2 10 Tf 90 575 Td (147.61) Tj ET",
    ]
    comp = zlib.compress("\n".join(ops).encode("latin-1"))
    cmap_comp = zlib.compress(cmap)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(comp)).encode() + b" /Filter /FlateDecode >>\nstream\n" + comp + b"\nendstream",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /AAAAAA+SimSun /Encoding /Identity-H /ToUnicode 7 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(cmap_comp)).encode() + b" /Filter /FlateDecode >>\nstream\n" + cmap_comp + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.7\n")
    for i, body in enumerate(objs, start=1):
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    return bytes(out) + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"


print("== OFD 数电票 ==")
r = R.recognize("dzfp.ofd", make_ofd())
check("发票号码", r["invoice_no"], "24417000000012345678")
check("发票代码", r["invoice_code"], "044001900111")
check("价税合计", r["amount"], 1280.0)
check("税额", r["tax_amount"], 147.61)
check("税率", r["tax_rate"], 13.0)
check("开票日期", r["invoice_date"], "2026-08-14")
check("销售方", r["seller_name"], "深圳市云图科技有限公司")
check("购买方", r["buyer_name"], "深圳市恒信商贸有限公司")
check("销售方税号（不得与购买方对调）", r["seller_tax_no"], "91440300MA5F1234XA")
check("购买方税号", r["buyer_tax_no"], "91440300MA5G5678XB")
check("来源", r["source"], "ofd")
check("置信度不低于 0.9", r["confidence"] >= 0.9, True)

print("\n== XML 电子发票 ==")
r = R.recognize("24417000000012345678.xml", OFD_XML.encode())
check("发票号码", r["invoice_no"], "24417000000012345678")
check("价税合计", r["amount"], 1280.0)
check("来源", r["source"], "xml")

print("\n== XLSX 发票数据 ==")
r = R.recognize("invoice.xlsx", make_xlsx())
check("发票号码", r["invoice_no"], "24992000000087654321")
check("价税合计", r["amount"], 3560.5)
check("开票日期", r["invoice_date"], "2026-07-02")
check("销售方", r["seller_name"], "广州市启明电子有限公司")
check("来源", r["source"], "xlsx")

print("\n== PDF（FlateDecode 文本流）==")
r = R.recognize("scan.pdf", make_pdf())
check("发票号码", r["invoice_no"], "24417000000099998888")
check("来源", r["source"], "pdf")

print("\n== PDF（Identity-H 子集字体 + ToUnicode CMap，数电票实际形态）==")
r = R.recognize("24417000000012345678.pdf", make_pdf_with_cmap())
check("发票号码", r["invoice_no"], "24417000000012345678")
check("价税合计（不能只拿到号码）", r["amount"], 1280.0)
check("税额", r["tax_amount"], 147.61)
check("开票日期（年月日分属不同字体）", r["invoice_date"], "2026-08-14")
check("销售方", r["seller_name"], "深圳市云图科技有限公司")
check("购买方", r["buyer_name"], "深圳市恒信商贸有限公司")
check("销售方税号", r["seller_tax_no"], "91440300MA5F1234XA")
check("购买方税号", r["buyer_tax_no"], "91440300MA5G5678XB")
check("来源", r["source"], "pdf")
check("置信度不低于 0.9", r["confidence"] >= 0.9, True)

print("\n== 图片/扫描件：不能凭空造字段 ==")
jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"
r = R.recognize("电子发票.jpg", jpg)
check("拍照件不谎报号码", r["invoice_no"], None)
check("拍照件不谎报金额", r["amount"], None)
r = R.recognize("发票扫描件.pdf", b"%PDF-1.7\n1 0 obj\n<< /Type /XObject /Subtype /Image >>\nstream\n\xff\xd8\xff\xd9\nendstream\nendobj\n%%EOF")
check("扫描版 PDF 不谎报号码", r["invoice_no"], None)

print("\n== 邮件正文兜底 ==")
r = R.recognize(
    "发票.pdf",
    b"",
    subject="【发票】深圳市云图科技有限公司 电子发票",
    body=(
        "发票号码：24417000000012345678 开票日期：2026年08月14日 "
        "价税合计：￥1280.00 税额：147.61 销售方名称：深圳市云图科技有限公司 "
        "购买方名称：深圳市恒信商贸有限公司"
    ),
)
check("发票号码", r["invoice_no"], "24417000000012345678")
check("价税合计", r["amount"], 1280.0)
check("税额", r["tax_amount"], 147.61)
check("开票日期", r["invoice_date"], "2026-08-14")
check("销售方", r["seller_name"], "深圳市云图科技有限公司")
check("购买方", r["buyer_name"], "深圳市恒信商贸有限公司")

print("\n== 文件名兜底 ==")
r = R.recognize("发票_24417000000012345678_1280.00.pdf")
check("发票号码", r["invoice_no"], "24417000000012345678")
check("来源", r["source"], "filename")

print("\n== 边界 ==")
check("空输入不报错", R.recognize("", b"")["confidence"], 0.0)
check("噪声文件被识别为噪声", R.looks_like_invoice_name("电子发票使用说明.pdf"), False)
check("号码文件名被接受", R.looks_like_invoice_name("24417000000012345678.pdf"), True)
check("金额不会取到号码", R.extract_fields("发票号码 24417000000012345678").get("amount"), None)
check("离谱日期被拒绝", R.extract_fields("开票日期 1899-01-01").get("invoice_date"), None)
r = R.recognize("x.ofd", b"PK\x03\x04broken")
check("损坏 zip 不抛异常", r["invoice_no"], None)

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
