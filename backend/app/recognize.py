"""电子发票自动识别（本地优先，可选 OCR，零外部服务）。

识别来源按可靠性从高到低排序，命中即停：

1. **结构化解包**——OFD / XML / XLSX 版式或数据文件里带有发票元数据，
   解 zip + 扫 XML 文本节点即可拿到准确字段（数电票首选，准确率最高）。
2. **PDF 文字层**——装了 pypdfium2 就用它直读（正确处理嵌入字体子集）；
   没装则退回标准库实现的「解 ToUnicode CMap + 跟踪字体解码」。
3. **OCR**——扫描件 / 拍照件 / 图片型 PDF 交给 `ocr.py`（RapidOCR 或 Tesseract）。
   没有可用 OCR 后端时这一层自动跳过。
4. **文本规则**——邮件主题 / 正文 / 文件名里的号码、金额、日期、购销方。

提取策略是**两级**的，这是识别准确率的关键：

- **行级（优先）**：先把文本切行，逐行找「标签：值」配对。同一行里出现多组
  标签时按标签位置切开（真实发票经常把「购买方名称 + 统一社会信用代码」印在一行）。
  无主标签（如连续出现的两个「统一社会信用代码」）按**上文最近的购销方**归属，
  避免购销双方税号对调。
- **段级（兜底）**：整段跑正则，补齐行级没拿到的字段。

对外只有一个入口 `recognize()`，收票、手工上传、批量导入都走它。
每个字段还带 `field_sources`，标明它究竟来自哪一层，便于排查「为什么这个字段是空的」。
"""

from __future__ import annotations

import re
import zipfile
import zlib
from datetime import date
from io import BytesIO
from xml.etree import ElementTree as ET

# OCR 层是可选依赖，但它自身只 import 标准库（重依赖在函数里懒加载），
# 所以这里直接导入是安全的；后端是否真的可用由 ocr.probe() 在运行时判断。
from . import ocr as _ocr

# ---------------------------------------------------------------- 常量

MAX_SCAN_BYTES = 12 * 1024 * 1024  # 单个文件参与解析的上限，避免超大 PDF 拖垮进程

# 不同来源的置信度基准，最后再按字段完整度加权
SOURCE_WEIGHT = {
    "ofd": 0.92,
    "xml": 0.9,
    "xlsx": 0.85,
    "pdf-text": 0.86,  # PDF 文字层直读，几乎等同结构化
    "pdf": 0.7,        # 标准库兜底解析，可能丢中文
    "ocr": 0.78,       # OCR 文本，数字很准、个别汉字会错
    "text": 0.55,
    "filename": 0.4,
}

# 允许进入解析的文件后缀（与影像白名单保持一致，另加电子发票结构格式）
PARSE_EXT = {".pdf", ".ofd", ".xml", ".xlsx", ".xls", ".jpg", ".jpeg", ".png", ".webp", ".txt"}

MONEY = r"([0-9][0-9,]*\.?[0-9]{0,2})"

# 发票号码：数电票 20 位，老式 8 位；带上下文关键词时优先
_RE_NO_CTX = re.compile(r"(?:发票号码|发票No|票据号码|InvoiceN(?:o|umber))[^0-9]{0,8}(\d{8,25})", re.I)
_RE_NO_LONG = re.compile(r"(?<!\d)(\d{20})(?!\d)")
_RE_NO_MID = re.compile(r"(?<!\d)(\d{12})(?!\d)")

# 发票代码：老式 10/12 位
_RE_CODE_CTX = re.compile(r"(?:发票代码|票据代码|InvoiceCode)[^0-9]{0,8}(\d{10,12})", re.I)

# 金额 / 税额 / 税率
_RE_AMOUNT_CTX = re.compile(
    r"(?:价税合计|合计金额|金额合计|价税总计|小写|含税金额|TotalAmtWithTax|TotalTax-includedAmount|AmountWithTax)"
    r"[^0-9¥￥]{0,10}[¥￥]?\s*" + MONEY,
    re.I,
)
_RE_AMOUNT_YEN = re.compile(r"[¥￥]\s*" + MONEY)
_RE_TAX_CTX = re.compile(
    r"(?:税额合计|合计税额|税额|TotalTax|TaxAmount)[^0-9¥￥]{0,10}[¥￥]?\s*" + MONEY, re.I
)
_RE_RATE = re.compile(r"(?:税率|征收率|TaxRate)[^0-9]{0,6}(\d{1,2}(?:\.\d{1,2})?)\s*%?", re.I)
_RE_RATE_LOOSE = re.compile(r"(?<!\d)(\d{1,2})\s*%(?!\d)")

# 开票日期
_RE_DATE_CTX = re.compile(
    r"(?:开票日期|开具日期|填开日期|制票日期|开票时间|IssueDate|IssueTime|BillingDate)"
    r"[^0-9]{0,8}(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})",
    re.I,
)
_RE_DATE_ANY = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")

# 购销方
_RE_SELLER_CTX = re.compile(
    r"(?:销售方|销方|销\s*方|SellerName)[^\n]{0,40}?"
    r"(?:名称|Name)?[：:\s]{0,4}([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{4,60}?(?:公司|厂|店|中心|部|社|行|院|所|集团|商行|工作室))",
)
_RE_BUYER_CTX = re.compile(
    r"(?:购买方|购方|购\s*方|BuyerName)[^\n]{0,40}?"
    r"(?:名称|Name)?[：:\s]{0,4}([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{4,60}?(?:公司|厂|店|中心|部|社|行|院|所|集团|商行|工作室))",
)
_RE_NAME_ANY = re.compile(r"(?:名\s*称|CompanyName)[：:\s]{0,4}([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{4,60}?(?:公司|厂|店|中心|部|社|行|院|所|集团|商行|工作室))")

# 统一社会信用代码
_USCC = r"([0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10})"
_RE_USCC = re.compile(r"(?<![0-9A-Z])" + _USCC + r"(?![0-9A-Z])")
# 带上下文的税号：OFD/XML 里标签名是 SellerIdNum / BuyerIdNum，
# 纯文本里是「销售方名称：xxx 纳税人识别号：xxx」。按上下文取才不会把两者搞反。
_RE_SELLER_TAX = re.compile(
    r"(?:销售方|销方|销\s*方|Seller)[^\n]{0,80}?(?:纳税人识别号|统一社会信用代码|IdNum|TaxNo)[^0-9A-Z]{0,8}" + _USCC,
    re.I,
)
_RE_BUYER_TAX = re.compile(
    r"(?:购买方|购方|购\s*方|Buyer)[^\n]{0,80}?(?:纳税人识别号|统一社会信用代码|IdNum|TaxNo)[^0-9A-Z]{0,8}" + _USCC,
    re.I,
)

# 发票类型（按关键词判定，数电票优先）
TYPE_RULES = [
    (("数电票", "电子发票（增值税专用发票）", "全电发票"), "数电票"),
    (("增值税专用发票", "专用发票", "专票"), "增值税电子专用发票"),
    (("增值税普通发票", "普通发票", "普票"), "增值税电子普通发票"),
    (("电子发票",), "电子发票"),
    (("机动车",), "机动车销售统一发票"),
    (("火车票", "铁路电子客票"), "铁路电子客票"),
    (("机票", "航空运输电子客票"), "航空运输电子客票行程单"),
    (("出租车", "滴滴", "行程单"), "出租车电子发票"),
    (("定额发票", "卷式发票"), "定额发票"),
]
DEFAULT_TYPE = "增值税电子普通发票"

# 明显不是发票的附件名（收票时直接跳过）
NOISE_HINT = re.compile(
    r"(签名|说明|使用手册|README|通知|回执|对账单|账单|合同|报价|明细表清单)", re.I
)


# ---------------------------------------------------------------- 行级标签解析
#
# 真实发票（尤其是 OCR 出来的）几乎都是「标签 值」一行一条。按行解析比整段正则
# 准得多：不会把销售方的税号读成购买方的，也不会把公司名后面的字吃进金额里。

# OCR 常见形近字混淆。只作用于标签词，不碰数值；且仅对 OCR 来源生效，
# 避免污染 OFD/XML 这种本来就准确的文本。
OCR_LABEL_FIXES = [
    (re.compile(r"购[实买買天]方"), "购买方"),
    (re.compile(r"销[售贷隹管]方"), "销售方"),
    (re.compile(r"价税[台谷含]计"), "价税合计"),
    (re.compile(r"发票号[码吗馬鸟]"), "发票号码"),
    (re.compile(r"开票日[期其朝]"), "开票日期"),
    (re.compile(r"税额合[计汁]"), "税额合计"),
    (re.compile(r"税[率车辛]"), "税率"),
    (re.compile(r"统一社会信用代[码吗馬]"), "统一社会信用代码"),
    (re.compile(r"纳税人识[别另]号"), "纳税人识别号"),
    (re.compile(r"开[具其]日期"), "开具日期"),
]

# 标签 -> 字段。同一个字段可以有多个写法，长的写在前（前缀匹配时长的优先）。
LABEL_FIELDS: dict[str, str] = {
    "发票号码": "invoice_no",
    "票据号码": "invoice_no",
    "发票号": "invoice_no",
    "发票代码": "invoice_code",
    "票据代码": "invoice_code",
    "发票类型": "invoice_type",
    "票种": "invoice_type",
    "开票日期": "invoice_date",
    "开具日期": "invoice_date",
    "填开日期": "invoice_date",
    "制票日期": "invoice_date",
    "开票时间": "invoice_date",
    "价税合计": "amount",
    "合计金额": "amount",
    "金额合计": "amount",
    "价税总计": "amount",
    "含税金额": "amount",
    "小写": "amount",
    "税额合计": "tax_amount",
    "合计税额": "tax_amount",
    "税额": "tax_amount",
    "税率": "tax_rate",
    "征收率": "tax_rate",
    "销售方名称": "seller_name",
    "销方名称": "seller_name",
    "销售方": "seller_name",
    "销方": "seller_name",
    "购买方名称": "buyer_name",
    "购方名称": "buyer_name",
    "购买方": "buyer_name",
    "购方": "buyer_name",
    # 「名称」单独出现时不知道该归谁，靠上文最近的购销方判断（见 _assign_party_name）
    "名称": "party_name",
    # 税号同理：数电票里购销双方都用同一个标签名，必须靠上文归属
    "统一社会信用代码": "party_tax_no",
    "纳税人识别号": "party_tax_no",
    "纳税人识别号/统一社会信用代码": "party_tax_no",
    "销售方纳税人识别号": "seller_tax_no",
    "购买方纳税人识别号": "buyer_tax_no",
    "销售方统一社会信用代码": "seller_tax_no",
    "购买方统一社会信用代码": "buyer_tax_no",
}

# 按长度倒序拼成正则，保证「销售方名称」不会被「销售方」抢先匹配
_LABEL_ALT = re.compile(
    "(?<![\u4e00-\u9fa5A-Za-z0-9])("
    + "|".join(re.escape(k) for k in sorted(LABEL_FIELDS, key=len, reverse=True))
    + ")"
)
# 值里可能带的前缀噪声，如「（小写）」「(大写)」「：」「￥」
_VALUE_PREFIX = re.compile(r"^[\s：:，,、]*(?:[（(]\s*(?:小写|大写|含税|不含税)\s*[)）])?[\s：:￥¥]*")

# 中文大写金额（发票上「价税合计（大写）」那一栏）
_CN_DIGIT = {
    "零": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9,
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "两": 2,
}
_CN_UNIT = {"拾": 10, "十": 10, "佰": 100, "百": 100, "仟": 1000, "千": 1000}
_CN_SECTION = {"万": 10000, "亿": 100000000}
_CN_AMOUNT_RE = re.compile(
    r"^([零壹贰叁肆伍陆柒捌玖拾佰仟万亿两一二三四五六七八九十百千]+)[元圆](.*)$"
)


def fix_ocr_labels(text: str) -> str:
    """纠正 OCR 在标签词上的形近字错认（购实方 → 购买方）。"""
    for pattern, repl in OCR_LABEL_FIXES:
        text = pattern.sub(repl, text)
    return text


def _cn_int(chunk: str) -> int | None:
    """中文数词 -> 整数。支持「壹仟贰佰捌拾」这种带单位的写法。"""
    total = 0
    section = 0
    number = 0
    for ch in chunk:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            # 「拾」前面没有数字时按 1 算（如「拾伍」= 15）——发票里少见但得兼容
            section += (number or 1) * _CN_UNIT[ch]
            number = 0
        elif ch in _CN_SECTION:
            section = (section + number) * _CN_SECTION[ch]
            total += section
            section = 0
            number = 0
        else:
            return None
    return total + section + number


def cn_amount(raw: str) -> float | None:
    """把「壹仟贰佰捌拾元整」「贰佰元伍角」这类大写金额换成数字。"""
    text = re.sub(r"[\s，,]", "", raw or "")
    text = text.replace("人民币", "").replace("￥", "").replace("¥", "")
    match = _CN_AMOUNT_RE.match(text)
    if not match:
        return None
    whole = _cn_int(match.group(1))
    if whole is None:
        return None
    frac_text = match.group(2)
    cents = 0.0
    jiao = re.search(r"([零壹贰叁肆伍陆柒捌玖一二三四五六七八九])角", frac_text)
    fen = re.search(r"([零壹贰叁肆伍陆柒捌玖一二三四五六七八九])分", frac_text)
    if jiao:
        cents += _CN_DIGIT[jiao.group(1)] / 10
    if fen:
        cents += _CN_DIGIT[fen.group(1)] / 100
    value = round(whole + cents, 2)
    return value if 0 < value <= 100_000_000 else None


def _iter_label_pairs(line: str):
    """把一行拆成若干 (标签, 值)。

    真实版式经常一行挤多组，例如：
        「购买方名称：深圳市恒信商贸有限公司 统一社会信用代码：91440300MA5G5678XB」
    所以不能只按第一个冒号切——要找出所有标签的位置，相邻标签之间才是值。
    """
    hits = list(_LABEL_ALT.finditer(line))
    for idx, mo in enumerate(hits):
        end = hits[idx + 1].start() if idx + 1 < len(hits) else len(line)
        raw = line[mo.end() : end]
        value = _VALUE_PREFIX.sub("", raw).strip()
        # 去掉值尾部残留的分隔符
        value = value.rstrip(" 　\t:：,，;；")
        yield mo.group(1), value


def extract_by_lines(text: str) -> dict:
    """行级提取。返回 {字段: 值}，只包含确实解析出来的项。"""
    fields: dict[str, object] = {}
    deferred_amount: float | None = None
    # 记「上文最近的购销方」，用于归属无主的「名称」「统一社会信用代码」
    last_party: str | None = None

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or len(line) > 400:
            continue
        for label, value in _iter_label_pairs(line):
            key = LABEL_FIELDS.get(label)
            if not key or not value:
                continue
            # ---- 归属类标签：靠上文判断是购买方还是销售方 ----
            if key == "party_name":
                if "购" in line[: line.find(label) + 8]:
                    last_party = "buyer"
                elif "销" in line[: line.find(label) + 8]:
                    last_party = "seller"
                if last_party:
                    fields.setdefault(f"{last_party}_name", _clean_name(value))
                continue
            if key == "party_tax_no":
                if last_party:
                    fields.setdefault(f"{last_party}_tax_no", _clean_tax_no(value))
                continue

            if key in ("buyer_name", "seller_name"):
                last_party = key.split("_")[0]
                got = _clean_name(value)
                if got:
                    fields.setdefault(key, got)
                continue

            if key == "amount":
                # 「价税合计（大写）」交给中文数词解析，且优先级低于小写金额
                if re.search(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿]", value):
                    if deferred_amount is None:
                        deferred_amount = cn_amount(value)
                    continue
                money = _clean_money(_first_number(value))
                if money is not None and money not in fields.values():
                    fields.setdefault("amount", money)
                continue

            if key == "tax_amount":
                money = _clean_money(_first_number(value))
                if money is not None:
                    fields.setdefault("tax_amount", money)
                continue

            if key == "tax_rate":
                got = _clean_rate(value)
                if got is not None:
                    fields.setdefault("tax_rate", got)
                continue

            if key == "invoice_date":
                got = _date_from_text(value)
                if got:
                    fields.setdefault("invoice_date", got)
                continue

            if key == "invoice_no":
                digits = re.sub(r"\D", "", value)
                if 8 <= len(digits) <= 25:
                    fields.setdefault("invoice_no", digits)
                continue

            if key == "invoice_code":
                digits = re.sub(r"\D", "", value)
                # 数电票没有发票代码，别把号码抄成代码
                if 10 <= len(digits) <= 12 and digits != fields.get("invoice_no"):
                    fields.setdefault("invoice_code", digits)
                continue

            if key == "invoice_type":
                got = _type_from_text(value) or _type_from_text(line)
                if got:
                    fields.setdefault("invoice_type", got)

    if deferred_amount is not None and not fields.get("amount"):
        fields["amount"] = deferred_amount
    return fields


def _first_number(value: str) -> str | None:
    """取值里第一个像金额的数字串（跳过「大写」那种中文串）。"""
    if re.search(r"[零壹贰叁肆伍陆柒捌玖]", value):
        return None
    found = re.search(r"\d[\d,]*\.?\d{0,2}", value)
    return found.group(0) if found else None


def _clean_rate(value: str) -> float | None:
    found = re.search(r"(\d{1,2}(?:\.\d{1,2})?)\s*%?", value)
    if not found:
        return None
    try:
        rate = float(found.group(1))
    except ValueError:
        return None
    return rate if 0 < rate <= 20 else None


def _date_from_text(value: str) -> str | None:
    found = re.search(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", value)
    if not found:
        return None
    return _clean_date(found.group(1), found.group(2), found.group(3))


# 公司名后缀。发票上的购销方名称几乎都带这些，用来判断「这个值像不像公司名」
_NAME_TAIL = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,60}?"
    r"(?:公司|厂|店|中心|部|社|行|院|所|集团|商行|工作室|事业部|分公司|超市|酒店|宾馆|事务所)$"
)
_NOISE_IN_NAME = re.compile(r"(纳税人识别号|统一社会信用代码|税号|开户行|地址|电话|名称[:：])")


def _clean_name(value: str) -> str | None:
    """从可能夹带相邻标签的值里抠出公司名。"""
    text = _NOISE_IN_NAME.split(value)[0].strip()
    text = text.strip("：: ,，、")
    if not text:
        return None
    found = _NAME_TAIL.search(text)
    if found:
        return found.group(0)
    # 没有公司后缀时，可能是「个人」或未写全，长度合理就先用着
    return text if 2 <= len(text) <= 60 else None


def _clean_tax_no(value: str) -> str | None:
    found = _RE_USCC.search(value.replace(" ", ""))
    return found.group(1) if found else None


def _type_from_text(text: str) -> str | None:
    for keywords, name in TYPE_RULES:
        if any(k in text for k in keywords):
            return name
    return None


# ---------------------------------------------------------------- 工具


def _clean_money(raw: str | None) -> float | None:
    if not raw:
        return None
    text = raw.replace(",", "").strip()
    if not text or text in (".", "-"):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    # 发票金额不可能超过 1 亿，超过基本是把日期/号码误当金额了
    if value < 0 or value > 100_000_000:
        return None
    return round(value, 2)


def _clean_date(y: str, mth: str, d: str) -> str | None:
    try:
        y_i, m_i, d_i = int(y), int(mth), int(d)
        got = date(y_i, m_i, d_i)
    except (TypeError, ValueError):
        return None
    # 拒绝明显离谱的日期（发票不可能开在 2000 年前，也不该晚于今天太多）
    if got.year < 2000 or got.year > date.today().year + 1:
        return None
    return got.isoformat()


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def _squeeze(text: str) -> str:
    """把换行与连续空白压成单空格，让「跨行」的键值对能被正则抓到。"""
    return re.sub(r"[\s\u3000]+", " ", text or "")


def _squeeze_lines(text: str) -> str:
    """行内空白压成单空格，但**保留换行**。

    行级解析依赖换行来界定「一条标签」；若像 _squeeze 那样把换行也吃掉，
    「销售方名称」后面就会连上下一行的内容，归属判断直接失效。
    """
    out = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = re.sub(r"[\t\u3000 ]+", " ", line).strip()
        out.append(cleaned)
    return "\n".join(out)


# ---------------------------------------------------------------- 解包


def _zip_texts(data: bytes, want_suffix: tuple[str, ...] | None = None) -> str:
    """把 zip 容器里的 XML 文本节点全部抽出来拼成一段文本。

    OFD 与 XLSX 本质都是 zip + XML，用同一条路径处理，命中即可用。
    """
    chunks: list[str] = []
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.lower()
                if want_suffix and not name.endswith(want_suffix):
                    continue
                if not name.endswith((".xml", ".rels")) and "/xml" not in name:
                    continue
                if info.file_size > MAX_SCAN_BYTES:
                    continue
                try:
                    raw = zf.read(info)
                except (KeyError, RuntimeError, zipfile.BadZipFile):
                    continue
                chunks.append(_xml_to_text(raw))
    except (zipfile.BadZipFile, OSError):
        return ""
    return _squeeze(" ".join(chunks))


def _xml_to_text(raw: bytes) -> str:
    """XML -> 纯文本。ElementTree 负责结构，失败则退回去标签。"""
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            root = ET.fromstring(raw.decode(encoding, errors="ignore"))
        except Exception:  # noqa: BLE001
            continue
        parts: list[str] = []
        for node in root.iter():
            text = (node.text or "").strip()
            if text:
                # 同时保留「标签名 文本」的组合，方便按关键词定位
                tag = node.tag.split("}")[-1]
                parts.append(f"{tag} {text}" if len(tag) <= 32 else text)
        if parts:
            return " ".join(parts)
    try:
        return _strip_tags(raw.decode("utf-8", errors="ignore"))
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- PDF 抽取

# PDF 里文本片段的拼接规则很关键：国内电子发票的版式是「标签 值 标签 值」，
# 正文被拆成很多个 Tj。中文标签常被拆成单字（「价」「税」「合」「计」），
# 若一律用空格连接就再也匹配不到「价税合计」；而金额被拆成「1280」「.00」
# 时用空格连接又会把它读成 1280。所以按字符类型决定连不连：
#   中文↔中文    直接拼（价税合计）
#   ASCII↔ASCII  直接拼（1280.00）
#   其余         加空格（价税合计 1280.00 / 1280.00 元）
_PDF_TEXT_TOKEN = re.compile(
    r"/([A-Za-z0-9#+._-]+)\s+[-\d.]+\s+Tf"           # 切换字体 /F1 10 Tf
    r"|\((?P<lit>(?:\\.|[^\\()])*)\)\s*(?:Tj|TJ|')"  # (字面量) Tj
    r"|<(?P<hex>[0-9A-Fa-f\s]+)>\s*(?:Tj|TJ|')"      # <hex> Tj（CID 字形码）
    r"|\[(?P<arr>[^\[\]]*)\]\s*TJ"                   # [] TJ 数组
    # 文本定位操作符：用来判断「换行了」，见 _pdf_decode_content
    r"|(?P<pos>-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?\s+(?:Td|TD))"
    r"|(?P<tm>[-.\d]+\s+[-.\d]+\s+[-.\d]+\s+[-.\d]+\s+-?[\d.]+\s+(?P<tmy>-?[\d.]+)\s+Tm)"
    r"|(?P<tstar>T\*)",
    re.S,
)
_PDF_HEX_INLINE = re.compile(r"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>")
_OBJ_RE = re.compile(rb"(\d+)\s+\d+\s+obj\b(.*?)\bendobj", re.S)
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
_FONT_DICT_RE = re.compile(rb"/Font\s*<<(.*?)>>", re.S)
_FONT_ENTRY_RE = re.compile(rb"/([A-Za-z0-9#+._-]+)\s+(\d+)\s+\d+\s+R")


def _inflate(raw: bytes) -> bytes:
    """尽力解开 FlateDecode 流；不是压缩流或解不开时原样返回。"""
    if not raw:
        return raw
    for candidate in (raw, raw.strip(b"\r\n")):
        try:
            return zlib.decompress(candidate)
        except zlib.error:
            continue
    try:
        partial = zlib.decompressobj().decompress(raw)
        if partial:
            return partial
    except zlib.error:
        pass
    return raw


def _hex_to_unicode(chunk: str) -> str:
    """把 CMap 里的目标串（UTF-16BE 十六进制）还原成文本。"""
    if len(chunk) % 2:
        chunk = chunk[:-1]
    if not chunk:
        return ""
    try:
        raw = bytes.fromhex(chunk)
    except ValueError:
        return ""
    try:
        text = raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return ""
    return text.replace("\x00", "")


def _parse_cmap(text: str) -> dict[int, str]:
    """解析 ToUnicode CMap：{字形码: 文本}。

    数电票 PDF 的正文多是 Identity-H 编码的子集字体，内容流里写的是字形码，
    必须靠这张映射表才能还原成中文——这也是「PDF 只识别出号码不识别出
    购销方」的根因。
    """
    out: dict[int, str] = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            try:
                out[int(src, 16)] = _hex_to_unicode(dst)
            except ValueError:
                continue
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        # 数组形式：<lo> <hi> [<d1> <d2> …]（先处理，避免被普通形式误吃）
        for lo, hi, arr in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", block, re.S
        ):
            try:
                start, end = int(lo, 16), int(hi, 16)
            except ValueError:
                continue
            targets = re.findall(r"<([0-9A-Fa-f]*)>", arr)
            for offset, dst in enumerate(targets):
                if start + offset > end:
                    break
                out[start + offset] = _hex_to_unicode(dst)
        # 普通形式：<lo> <hi> <dst>，目标是连续码位
        for lo, hi, dst in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block
        ):
            try:
                start, end = int(lo, 16), int(hi, 16)
                base = bytes.fromhex(dst if len(dst) % 2 == 0 else dst[:-1])
                first = int.from_bytes(base[-2:], "big") if len(base) >= 2 else int(dst, 16)
                head = base[:-2] if len(base) >= 2 else b""
            except ValueError:
                continue
            for offset in range(max(end - start + 1, 0)):
                if start + offset > 0xFFFF:
                    break
                tail = (first + offset).to_bytes(2, "big")
                out[start + offset] = (head + tail).decode("utf-16-be", errors="ignore")
    return out


def _pdf_font_cmaps(data: bytes) -> dict[str, dict[int, str]]:
    """收集 {资源名 → CMap}，资源名即内容流里 /F1 这类名字。"""
    objs: dict[int, bytes] = {}
    for mo in _OBJ_RE.finditer(data):
        objs[int(mo.group(1))] = mo.group(2)
    if not objs:
        return {}

    cmaps: dict[str, dict[int, str]] = {}
    for dict_mo in _FONT_DICT_RE.finditer(data):
        for name_b, obj_b in _FONT_ENTRY_RE.findall(dict_mo.group(1)):
            name = name_b.decode("latin-1")
            if name in cmaps:
                continue
            body = objs.get(int(obj_b), b"")
            if b"/Type" in body and b"/Font" not in body:
                continue
            ref = re.search(rb"/ToUnicode\s+(\d+)\s+\d+\s+R", body)
            if not ref:
                continue
            target = objs.get(int(ref.group(1)))
            if not target:
                continue
            stream = _STREAM_RE.search(target)
            if not stream:
                continue
            cmap = _parse_cmap(_inflate(stream.group(1)).decode("latin-1", errors="ignore"))
            if cmap:
                cmaps[name] = cmap
    return cmaps


def _unescape_pdf_literal(raw: str) -> str:
    """还原 PDF 字面量里的转义序列。"""
    out: list[str] = []
    i = 0
    simple = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "(": "(", ")": ")", "\\": "\\"}
    while i < len(raw):
        ch = raw[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(raw):
            break
        nxt = raw[i]
        if nxt in simple:
            out.append(simple[nxt])
            i += 1
        elif nxt.isdigit():  # 八进制 \053
            j = i
            while j < len(raw) and j < i + 3 and raw[j].isdigit():
                j += 1
            try:
                out.append(chr(int(raw[i:j], 8)))
            except ValueError:
                pass
            i = j
        else:
            out.append(nxt)
            i += 1
    return "".join(out)


def _decode_hex_text(chunk: str, cmap: dict[int, str] | None) -> str:
    """把内容流里的 <hex> 还原成文本：优先查 CMap，其次按 2 字节尝试常见编码。"""
    hexs = re.sub(r"\s+", "", chunk)
    if len(hexs) % 2:
        hexs = hexs[:-1]
    if not hexs:
        return ""
    try:
        raw = bytes.fromhex(hexs)
    except ValueError:
        return ""

    if cmap:
        codes = [int(hexs[i : i + 4], 16) for i in range(0, len(hexs) - len(hexs) % 4, 4)]
        if codes:
            mapped = [cmap.get(c, "") for c in codes]
            hit = sum(1 for v in mapped if v)
            # 命中率够高才认 CMap 的结果，否则宁可整串回退，避免解出半截乱码
            if hit and hit >= len(codes) * 0.6:
                return "".join(mapped)

    for encoding in ("gbk", "utf-16-be", "big5", "latin-1"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if text.strip():
            return text
    return ""


def _is_cjk(ch: str) -> bool:
    return "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef"


def _is_narrow(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch in ".,%-+*/:()")


def _join_pdf_pieces(pieces: list[str]) -> str:
    """按字符类型决定片段之间是否补空格（见文件顶部说明）。

    `"\\n"` 是一个特殊标记：表示这里发生了换行（文本定位 Y 变了）。
    保留换行很重要——行级标签解析靠它界定「一条标签」。
    """
    out = ""
    for piece in pieces:
        if not piece:
            continue
        if piece == "\n":
            if out and not out.endswith("\n"):
                out += "\n"
            continue
        if not out or out.endswith("\n"):
            out += piece
            continue
        a, b = out[-1], piece[0]
        if (_is_cjk(a) and _is_cjk(b)) or (_is_narrow(a) and _is_narrow(b)):
            out += piece
        else:
            out += " " + piece
    return out


def _pdf_decode_content(raw: bytes, cmaps: dict[str, dict[int, str]]) -> str:
    """解码一条内容流：跟踪当前字体与文本位置，把 Tj / TJ 里的片段按序取出。

    除了取文字，还要**还原换行**：内容流里没有行这个概念，只有一串定位操作符。
    当 Y 坐标明显变化时插入换行标记，下游的行级标签解析才能用得上。
    这也是「PDF 识别出来全挤在一行、标签对不上值」的根因。
    """
    text = raw.decode("latin-1", errors="ignore")
    pieces: list[str] = []
    cmap: dict[int, dict[int, str]] | None = None
    last_y: float | None = None
    for mo in _PDF_TEXT_TOKEN.finditer(text):
        if mo.group(1):
            cmap = cmaps.get(mo.group(1))
            continue
        # ---- 文本定位：Y 变了就认为换行 ----
        pos = mo.group("pos")
        if pos is not None:
            try:
                y = float(pos.split()[1])
            except (IndexError, ValueError):
                y = None
            if y is not None:
                if last_y is not None and abs(y - last_y) > 0.5:
                    pieces.append("\n")
                last_y = y
            continue
        if mo.group("tm") is not None:
            try:
                y = float(mo.group("tmy"))
            except (TypeError, ValueError):
                y = None
            if y is not None:
                if last_y is not None and abs(y - last_y) > 0.5:
                    pieces.append("\n")
                last_y = y
            continue
        if mo.group("tstar") is not None:
            pieces.append("\n")
            continue

        literal = mo.group("lit")
        if literal is not None:
            piece = _unescape_pdf_literal(literal)
            if piece.strip():
                pieces.append(piece)
            continue
        hexed = mo.group("hex")
        if hexed is not None:
            piece = _decode_hex_text(hexed, cmap)
            if piece.strip():
                pieces.append(piece)
            continue
        arr = mo.group("arr")
        if arr is not None:
            for sub in _PDF_HEX_INLINE.finditer(arr):
                token = sub.group(0)
                if token.startswith("("):
                    pieces.append(_unescape_pdf_literal(token[1:-1]))
                else:
                    pieces.append(_decode_hex_text(token[1:-1], cmap))
    return _join_pdf_pieces([p for p in pieces if p])


def _pdf_text(data: bytes) -> str:
    """从 PDF 里尽力抽取可见文本。

    三步走，能拿到多少算多少：
      1. 解析字体 ToUnicode CMap，按字体解码内容流里的 <hex> 字形码——这一步
         才能把中文（购销方名称、价税合计等标签）还原出来；
      2. 退回扫 (…) 字面量，数字类字段往往用简单字体，这一步有兜底价值；
      3. 两步都拿不到中文时，至少留下数字，给号码/日期用。
    """
    if not data.startswith(b"%PDF"):
        return ""

    cmaps = _pdf_font_cmaps(data)
    chunks: list[str] = []
    for match in _STREAM_RE.finditer(data):
        raw = match.group(1)
        if len(raw) > MAX_SCAN_BYTES:
            continue
        dec = _inflate(raw)
        if b"Tj" not in dec and b"TJ" not in dec:
            continue
        got = _pdf_decode_content(dec, cmaps)
        if got:
            chunks.append(got)

    decoded = "\n".join(chunks)
    literals = _pdf_literals(data)
    if not re.search(r"[\u4e00-\u9fff]", decoded):
        # CMap 路线没解出中文，说明字体没有 ToUnicode 或是图片版，
        # 此时用字面量扫描补齐数字类字段
        return _squeeze_lines((decoded + "\n" + literals).strip())
    return _squeeze_lines(decoded)


def _pdf_literals(buf: bytes) -> str:
    """抓 PDF 内容流里的 (…) 字面量并拼接成文本（不含压缩流解包）。"""
    out: list[str] = []
    for match in re.finditer(rb"\((?:\\.|[^\\()])*\)", buf):
        raw = match.group(0)[1:-1]
        try:
            text = raw.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        if len(text) < 2:
            continue
        if sum(ch.isprintable() or ch.isspace() for ch in text) < len(text) * 0.6:
            continue
        out.append(text)
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", buf, re.S):
        raw = match.group(1)
        if len(raw) > MAX_SCAN_BYTES:
            continue
        dec = _inflate(raw)
        if dec is raw:
            continue
        for inner in re.finditer(rb"\((?:\\.|[^\\()])*\)", dec):
            text = inner.group(0)[1:-1].decode("utf-8", errors="ignore")
            if len(text) >= 2 and sum(ch.isprintable() or ch.isspace() for ch in text) >= len(text) * 0.6:
                out.append(text)
    return " ".join(out)


def _eml_text_and_attachments(data: bytes) -> tuple[str, list[tuple[str, bytes, str]]]:
    """解析 .eml（原始邮件），返回 (正文文本, [(文件名, 内容, mime)])。"""
    import email
    from email import policy

    try:
        msg = email.message_from_bytes(data, policy=policy.default)
    except Exception:  # noqa: BLE001
        return "", []

    body_parts: list[str] = []
    heads = [str(msg.get("Subject") or ""), str(msg.get("From") or ""), str(msg.get("Date") or "")]
    atts: list[tuple[str, bytes, str]] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get_content_disposition() or "").lower()
            filename = part.get_filename()
            if filename or disp == "attachment":
                payload = part.get_payload(decode=True) or b""
                atts.append((str(filename or "attachment"), payload, ctype))
                continue
            if ctype in ("text/plain", "text/html"):
                try:
                    body_parts.append(part.get_content())
                except Exception:  # noqa: BLE001
                    payload = part.get_payload(decode=True) or b""
                    body_parts.append(_strip_tags(payload.decode("utf-8", errors="ignore")))
    else:
        try:
            body_parts.append(msg.get_content())
        except Exception:  # noqa: BLE001
            body_parts.append("")

    text = _squeeze(" ".join(heads + [_strip_tags(b) for b in body_parts]))
    return text, atts


# ---------------------------------------------------------------- 规则


def extract_fields(text: str) -> dict:
    """从一段文本里提取发票字段（行级优先，段级补齐）。

    单文本场景的便捷入口；多来源合并请用 `extract_by_lines` / `extract_by_patterns`
    分开跑，见 `recognize()` 里的两阶段合并。
    """
    text = _squeeze_lines(text)
    if not text:
        return {}
    fields = dict(extract_by_lines(text))
    for key, value in extract_by_patterns(text).items():
        fields.setdefault(key, value)
    return fields


def extract_by_patterns(text: str) -> dict:
    """段级正则提取：不依赖换行，能跨行匹配，但精度低于行级解析。

    定位是**兜底**——只有当行级解析没拿到某个字段时才会用到它的结果。
    """
    text = _squeeze_lines(text)
    if not text:
        return {}
    squeezed = _squeeze(text)  # 段级正则要的是「一整条」文本
    fields: dict[str, object] = {}

    # 发票号码
    for regex in (_RE_NO_CTX, _RE_NO_LONG, _RE_NO_MID):
        m = regex.search(squeezed)
        if m:
            fields["invoice_no"] = m.group(1)
            break

    # 发票代码（数电票没有代码，抓不到就是空）
    m = _RE_CODE_CTX.search(squeezed)
    if m and fields.get("invoice_no") != m.group(1):
        # 常见误判：把号码当代码。两个数字长度相近时以号码为准
        fields["invoice_code"] = m.group(1)

    # 金额：带上下文优先，其次 ¥ 符号，最后中文大写金额
    m = _RE_AMOUNT_CTX.search(squeezed) or _RE_AMOUNT_YEN.search(squeezed)
    if m:
        amount = _clean_money(m.group(1))
        if amount is not None:
            fields["amount"] = amount
    if not fields.get("amount"):
        big = re.search(
            r"(?:大写|价税合计)[^\n]{0,10}?"
            r"([零壹贰叁肆伍陆柒捌玖拾佰仟万亿两一二三四五六七八九十百千]+[元圆][^\n]{0,8})",
            squeezed,
        )
        if big:
            got = cn_amount(big.group(1))
            if got is not None:
                fields["amount"] = got

    # 税额
    m = _RE_TAX_CTX.search(squeezed)
    if m:
        tax = _clean_money(m.group(1))
        # 税额不该大于价税合计
        if tax is not None and (not fields.get("amount") or tax <= float(fields["amount"])):
            fields["tax_amount"] = tax

    # 税率
    m = _RE_RATE.search(squeezed)
    if not m:
        m = _RE_RATE_LOOSE.search(squeezed)
    if m:
        try:
            rate = float(m.group(1))
            if 0 < rate <= 20:
                fields["tax_rate"] = rate
        except ValueError:
            pass

    # 开票日期
    m = _RE_DATE_CTX.search(squeezed) or _RE_DATE_ANY.search(squeezed)
    if m:
        got = _clean_date(m.group(1), m.group(2), m.group(3))
        if got:
            fields["invoice_date"] = got

    # 购销方
    m = _RE_SELLER_CTX.search(squeezed)
    if m:
        fields["seller_name"] = m.group(1).strip()
    m = _RE_BUYER_CTX.search(squeezed)
    if m:
        fields["buyer_name"] = m.group(1).strip()
    if not fields.get("buyer_name"):
        all_names = [x.group(1).strip() for x in _RE_NAME_ANY.finditer(squeezed)]
        if all_names:
            # 名称出现两次以上时，第一个通常是购买方
            fields.setdefault("buyer_name", all_names[0])
            if len(all_names) > 1 and not fields.get("seller_name"):
                fields["seller_name"] = all_names[1]

    # 税号：优先按上下文定位，避免购销方对调
    m = _RE_SELLER_TAX.search(squeezed)
    if m:
        fields["seller_tax_no"] = m.group(1)
    m = _RE_BUYER_TAX.search(squeezed)
    if m:
        fields["buyer_tax_no"] = m.group(1)
    if not fields.get("seller_tax_no") and not fields.get("buyer_tax_no"):
        uscc = _RE_USCC.findall(squeezed)
        if uscc:
            uniq = list(dict.fromkeys(uscc))
            fields["buyer_tax_no"] = uniq[0]
            if len(uniq) > 1:
                fields["seller_tax_no"] = uniq[1]

    # 发票类型
    got = _type_from_text(squeezed)
    if got:
        fields["invoice_type"] = got

    return fields


def _confidence(source: str, fields: dict) -> float:
    """来源基准分 + 字段完整度加成，封顶 0.99。"""
    base = SOURCE_WEIGHT.get(source, 0.4)
    important = ("invoice_no", "amount", "invoice_date", "seller_name")
    hit = sum(1 for k in important if fields.get(k))
    bonus = 0.07 * hit
    if fields.get("invoice_no"):
        bonus += 0.05
    return round(min(base + bonus, 0.99), 2)


# ---------------------------------------------------------------- 入口


def _file_texts(data: bytes, name: str, use_ocr: bool) -> tuple[list[tuple[str, str]], dict]:
    """按文件类型选解析路径，返回 ([(来源, 文本)], OCR 层信息)。

    解析顺序体现优先级：结构化容器 > PDF 文字层 > OCR > 标准库兜底。
    每一层都**尽力而为**，拿不到就跳过，任何一层失败都不影响其它层——
    多一条路就多一分命中机会，这正是识别率的关键。
    """
    lower = name.lower()
    out: list[tuple[str, str]] = []
    ocr_info: dict = {}

    # 1) 结构化容器：OFD / XLSX 是 zip+XML，XML 直接读
    is_zip = data[:2] == b"PK"
    if is_zip:
        if lower.endswith(".ofd"):
            out.append(("ofd", _zip_texts(data)))
        elif lower.endswith((".xlsx", ".xls")):
            out.append(("xlsx", _zip_texts(data)))
        else:
            # 后缀不可信时按内容再试一次：电子发票容器基本都是 zip + XML
            sniffed = _zip_texts(data)
            if "Invoice" in sniffed or "发票" in sniffed:
                out.append(("ofd", sniffed))
    elif lower.endswith(".xml"):
        out.append(("xml", _xml_to_text(data)))

    # 2) PDF 与图片：统一走 OCR 层（PDF 有文字层时它会直读，不跑模型）
    looks_pdf = data[:4] == b"%PDF"
    looks_image = (
        _ocr.is_image_name(lower)
        or data[:3] == b"\xff\xd8\xff"
        or data[:8] == b"\x89PNG\r\n\x1a\n"
    )

    # 2) PDF：先文字层（准且快），没有文字层说明是扫描件，才栅格化跑 OCR
    if looks_pdf:
        layer = _ocr.pdf_extract_text(data) if use_ocr else ""
        if layer and len(layer.strip()) >= 20:
            out.append(("pdf-text", layer))
        elif use_ocr:
            pages = _ocr.pdf_to_images(data)
            lines: list[str] = []
            backend = _ocr.engine_name()
            for page in pages:
                got = _ocr.ocr_image(page, backend)
                if got:
                    lines.extend(got.lines)
            if lines:
                # OCR 在标签词上会认错字（购实方/销俢方），先纠正再进规则引擎
                out.append(("ocr", fix_ocr_labels("\n".join(lines))))
                ocr_info = {"backend": f"pdf+{backend}", "pages": len(pages), "lines": len(lines)}
        # 标准库兜底：合成字体 / 子集字体场景下 pdfium 可能解不出字，
        # 但内容流里的 ToUnicode 映射还能救回来。它排在最后，只补空缺。
        std = _pdf_text(data)
        if std:
            out.append(("pdf", std))

    # 3) 图片
    if looks_image and use_ocr:
        got = _ocr.ocr_image(data)
        if got:
            out.append(("ocr", fix_ocr_labels(got.text)))
            ocr_info = {
                "backend": got.backend, "pages": got.pages,
                "elapsed_ms": got.elapsed_ms, "avg_score": got.avg_score,
                "lines": len(got.lines),
            }

    return out, ocr_info


def recognize(
    filename: str = "",
    data: bytes | None = None,
    subject: str = "",
    body: str = "",
    *,
    use_ocr: bool = True,
    include_text: bool = False,
) -> dict:
    """识别一张电子发票，返回字段 + 来源 + 置信度 + 逐字段来源。

    参数全部可选：只有文件名时走文件名规则，只有邮件正文时走文本规则。
    `use_ocr=False` 可跳过 OCR（纯规则单测、或调用方明确不要花这个算力时用）。
    `include_text=True` 额外返回各层解析出的原文（截断），供大模型兜底时复用。
    """
    data = data or b""
    name = (filename or "").strip()
    lower = name.lower()
    parts: list[tuple[str, str]] = []  # (source, text)，顺序即优先级
    attachments: list[tuple[str, bytes, str]] = []
    ocr_info: dict = {}

    # 0) .eml：先拆邮件，再对每个真实附件递归识别（取置信度最高的一个）
    if lower.endswith(".eml") or data[:5] == b"From ":
        eml_text, atts = _eml_text_and_attachments(data)
        if eml_text:
            parts.append(("text", eml_text))
        attachments = atts

    # 1) 文件本体：结构化 / PDF / OCR
    got_parts, ocr_info = _file_texts(data, name, use_ocr)
    parts.extend(got_parts)

    # 2) 文本兜底：邮件主题、正文、文件名。
    #    排在文件本体之后——正文里的「价税合计：1280.00」远不如票面本身可靠。
    if subject or body:
        parts.append(("text", _squeeze(f"主题 {subject} 内容 {body}")))
    if name:
        # 文件名去掉扩展名与常见分隔符，避免「发票」二字干扰
        stem = re.sub(r"[_\-.]+", " ", re.sub(r"\.(pdf|ofd|xml|xlsx?|jpe?g|png|webp|eml)$", "", name, flags=re.I))
        parts.append(("filename", stem))

    if not parts and not attachments:
        return _empty("none")

    merged: dict = {}
    field_sources: dict[str, str] = {}

    def _absorb(source: str, got: dict) -> None:
        if not got:
            return
        for key, value in got.items():
            if key in merged:
                continue
            merged[key] = value
            field_sources[key] = source

    # 阶段一：所有来源的**行级**解析，精确解析跨来源优先。
    # 这样「A 来源只对了号码、B 来源只对了税号」时两者都能保住；
    # 若按来源逐个合并，先来的模糊结果会把后来的精确结果挡在门外。
    for source, text in parts:
        if text:
            _absorb(source, extract_by_lines(text))

    # 阶段二：所有来源的**段级**正则，只补阶段一没拿到的字段
    for source, text in parts:
        if text:
            _absorb(source, extract_by_patterns(text))

    # 主来源取「贡献字段最多」的那一层，同数量时取权重更高的。
    # 比「谁给出了发票号码」更贴近事实——号码可能只是某一层顺手扫到的。
    tally: dict[str, int] = {}
    for source in field_sources.values():
        tally[source] = tally.get(source, 0) + 1
    best_source = "none"
    if tally:
        best_source = max(tally, key=lambda s: (tally[s], SOURCE_WEIGHT.get(s, 0.3)))

    # 3) 邮件内嵌附件：递归识别，取置信度更高的结果合并
    for att_name, payload, _mime in attachments:
        if not payload:
            continue
        sub = recognize(att_name, payload, use_ocr=use_ocr)
        if sub["confidence"] > 0.0 and sub.get("invoice_no") and not merged.get("invoice_no"):
            for key, value in sub.items():
                if key in _NON_FIELD_KEYS:
                    continue
                merged.setdefault(key, value)
                field_sources.setdefault(key, f"附件/{sub['source']}")
            best_source = sub["source"]
            ocr_info = ocr_info or (sub.get("ocr") or {})

    if not merged:
        empty = _empty(best_source if best_source != "none" else "none")
        # 本地一条字段都没解析出来，但可能仍有可读原文（版式诡异的 OCR 结果等）——
        # 这时原文对「让大模型兜底」反而最有价值，不能丢。
        if include_text:
            chunks = [f"【{src}】\n{text}" for src, text in parts if src != "filename" and text]
            empty["text"] = "\n\n".join(chunks)[:8000]
            empty["ocr"] = ocr_info or None
            empty["layers"] = [src for src, text in parts if text]
        return empty

    merged.setdefault("invoice_type", DEFAULT_TYPE)
    result = _empty(best_source)
    result.update(merged)
    result["confidence"] = _confidence(best_source, merged)
    result["fields"] = sorted(merged.keys())
    result["field_sources"] = field_sources
    result["ocr"] = ocr_info or None
    result["layers"] = [src for src, text in parts if text]
    if include_text:
        # 给大模型兜底用：把各层原文拼起来（附来源标注），截断控制 token
        chunks = []
        for src, text in parts:
            if src in ("filename",) or not text:
                continue
            chunks.append(f"【{src}】\n{text}")
        result["text"] = "\n\n".join(chunks)[:8000]
    result["raw_name"] = name
    return result


# 结果里「不是发票字段」的键，合并 / 递归时不能当字段抄过去
_NON_FIELD_KEYS = {
    "confidence", "source", "fields", "raw_name",
    "field_sources", "ocr", "layers", "text",
}


def _empty(source: str) -> dict:
    return {
        "invoice_no": None,
        "invoice_code": None,
        "invoice_type": None,
        "amount": None,
        "tax_amount": None,
        "tax_rate": None,
        "invoice_date": None,
        "seller_name": None,
        "seller_tax_no": None,
        "buyer_name": None,
        "buyer_tax_no": None,
        "source": source,
        "confidence": 0.0,
        "fields": [],
        "field_sources": {},
        "ocr": None,
        "layers": [],
        "raw_name": "",
    }


def looks_like_invoice_name(filename: str) -> bool:
    """按文件名判断是否值得送解析（用于收票时过滤签名图、通知等噪声）。"""
    if not filename:
        return False
    if NOISE_HINT.search(filename):
        return False
    return bool(re.search(r"(发票|票据|invoice|fapiao|\d{8,})", filename, re.I))
