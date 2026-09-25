#!/usr/bin/env python3
"""生成手机版 / PWA 图标（frontend/img/）。

为什么要有这个脚本而不是直接提交几张 PNG：图标要跟品牌主色和首字保持一致，
换主色或改应用名时得能一键重出，而不是找设计要图。同时它把「iOS 不接受带透明
通道的 apple-touch-icon（会渲染成黑底）」这类约束固化下来——统一存成不透明 RGB。

用法：
    backend/.venv/bin/python tools/make_icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend" / "img"

# 与 app.css 的 --primary 一致。改主色时这里要跟着改。
TOP = (22, 119, 255)
BOTTOM = (9, 88, 214)
GLYPH = "销"

# 系统里可用的中文字体，按优先级排。找不到就退回纯几何图形，不让脚本硬崩。
FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]


def load_font(size: int):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001 - 字体损坏/索引不对，继续试下一个
                continue
    return None


def gradient(size: int) -> Image.Image:
    """竖直线性渐变。逐行画，几百行而已，不值得为它引 numpy。"""
    img = Image.new("RGB", (1, size))
    px = img.load()
    for y in range(size):
        t = y / max(1, size - 1)
        px[0, y] = tuple(round(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3))
    return img.resize((size, size), Image.BILINEAR)


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def draw_glyph(canvas: Image.Image, font_ratio: float = 0.56) -> None:
    """把首字居中画上去。找不到中文字体时退化成一条「单据」轮廓，仍然是个可用图标。"""
    size = canvas.width
    d = ImageDraw.Draw(canvas)
    font = load_font(int(size * font_ratio))
    if font is None:
        # 兜底图形：白色圆角单据 + 两条文字线
        w, h = size * 0.44, size * 0.54
        x0, y0 = (size - w) / 2, (size - h) / 2
        d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=size * 0.05, outline=(255, 255, 255), width=max(2, size // 32))
        for i in range(3):
            yy = y0 + h * (0.32 + i * 0.18)
            d.line((x0 + w * 0.2, yy, x0 + w * 0.8, yy), fill=(255, 255, 255), width=max(2, size // 48))
        return

    # textbbox 给出的是「实际墨迹」范围，用它对齐才是视觉居中；
    # 直接用 anchor='mm' 会因为汉字字形上下留白不一致而偏高。
    box = d.textbbox((0, 0), GLYPH, font=font)
    d.text(
        ((size - (box[2] - box[0])) / 2 - box[0], (size - (box[3] - box[1])) / 2 - box[1]),
        GLYPH,
        font=font,
        fill=(255, 255, 255),
    )


def make(size: int, *, maskable: bool = False) -> Image.Image:
    img = gradient(size)
    if maskable:
        # maskable 图标会被系统裁成圆形/胶囊：内容必须落在中间 80% 的安全区内，
        # 且底色要铺满整块（不能有圆角），否则裁切后四周会露白。
        pad = Image.new("RGB", (size, size))
        inner = gradient(int(size * 0.72))
        draw_glyph(inner, 0.62)
        pad.paste(inner, ((size - inner.width) // 2, (size - inner.height) // 2))
        return pad
    img.putalpha(rounded_mask(size, round(size * 0.22)))
    draw_glyph(img)
    return img


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable-512.png", 512, True),
        # iOS 的 apple-touch-icon 只认 180×180；给 192 它也会缩，但会糊一点
        ("apple-touch-icon.png", 180, False),
    ]
    for name, size, maskable in jobs:
        img = make(size, maskable=maskable)
        path = OUT / name
        if maskable:
            img.save(path, "PNG", optimize=True)
        else:
            # iOS 对带透明通道的 touch icon 处理得很糟（黑底），统一转不透明，
            # 圆角之外的像素填成主色深端
            flat = Image.new("RGB", img.size, BOTTOM)
            flat.paste(img, mask=(img.getchannel("A") if img.mode == "RGBA" else None))
            flat.save(path, "PNG", optimize=True)
        # 自检：全是纯色的图 = 字形没画上去，必须在这里就发现
        colors = img.convert("RGB").getcolors(maxcolors=1 << 20) or []
        if len(colors) < 50:
            print(f"FAIL {name}: 颜色数只有 {len(colors)}，疑似空白图", file=sys.stderr)
            return 1
        print(f"OK   {name}  {size}x{size}  {path.stat().st_size} bytes  colors={len(colors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
