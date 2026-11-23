"""VENDRIX 브랜드 이미지(엠블럼/배너) 생성.

    python tools/make_assets.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BASE = Path(__file__).resolve().parents[1]
STATIC = BASE / "app" / "static"

FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/nanum",
]

VIOLET = (124, 92, 255)
INDIGO = (86, 108, 255)
CYAN = (72, 224, 255)
WHITE = (247, 245, 255)

HEX = [(50.0, 3.0), (90.7, 26.5), (90.7, 73.5), (50.0, 97.0), (9.3, 73.5), (9.3, 26.5)]

# V 모노그램 (0~100 좌표계) - 단일 폴리곤으로 날카롭게
V_SHAPE = [
    (17.0, 21.0),
    (35.0, 21.0),
    (50.0, 55.0),
    (65.0, 21.0),
    (83.0, 21.0),
    (58.0, 85.0),
    (42.0, 85.0),
]


def font_path(bold: bool = True, mono: bool = False) -> str | None:
    names = []
    if mono:
        names = ["DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf"]
    elif bold:
        names = ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "NanumSquareRoundB.ttf"]
    else:
        names = ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "NanumSquareRoundR.ttf"]
    for directory in FONT_DIRS:
        for name in names:
            candidate = Path(directory) / name
            if candidate.exists():
                return str(candidate)
    return None


def font(size: int, bold: bool = True, mono: bool = False) -> ImageFont.FreeTypeFont:
    path = font_path(bold, mono)
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def linear_gradient(size: tuple[int, int], stops: list[tuple[float, tuple[int, int, int]]], angle: float = 135.0):
    """각도 기반 선형 그라데이션."""
    width, height = size
    image = Image.new("RGBA", size)
    pixels = image.load()
    radians = math.radians(angle)
    dx, dy = math.cos(radians), math.sin(radians)
    # 투영 범위 정규화
    corners = [(0, 0), (width, 0), (0, height), (width, height)]
    projections = [x * dx + y * dy for x, y in corners]
    low, high = min(projections), max(projections)
    span = max(1e-6, high - low)
    for y in range(height):
        for x in range(width):
            t = ((x * dx + y * dy) - low) / span
            color = _sample(stops, t)
            pixels[x, y] = (*color, 255)
    return image


def _sample(stops: list[tuple[float, tuple[int, int, int]]], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    for index in range(len(stops) - 1):
        pos_a, col_a = stops[index]
        pos_b, col_b = stops[index + 1]
        if pos_a <= t <= pos_b:
            local = (t - pos_a) / max(1e-6, pos_b - pos_a)
            return tuple(int(col_a[i] + (col_b[i] - col_a[i]) * local) for i in range(3))
    return stops[-1][1]


def to_px(points, size: tuple[int, int], box: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    """0~100 좌표계를 box 영역(px)으로 매핑."""
    left, top, width, height = box
    return [(left + x / 100 * width, top + y / 100 * height) for x, y in points]


def hex_points(size: int, box: tuple[float, float, float, float], scale: float = 1.0) -> list[tuple[float, float]]:
    cx = box[0] + box[2] / 2
    cy = box[1] + box[3] / 2
    radius = min(box[2], box[3]) / 2 * scale
    return [
        (cx + radius * math.cos(math.radians(angle - 90)), cy + radius * math.sin(math.radians(angle - 90)))
        for angle in (0, 60, 120, 180, 240, 300)
    ]


def emblem(size: int, pad_ratio: float = 0.06) -> Image.Image:
    """육각 배지 + V 모노그램 엠블럼(투명 배경)."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pad = size * pad_ratio
    box = (pad, pad, size - 2 * pad, size - 2 * pad)

    outer = hex_points(size, box, 0.96)
    inner = hex_points(size, box, 0.855)
    stroke = max(2, int(size * 0.015))

    # 배지 내부 채움
    fill = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    fdraw = ImageDraw.Draw(fill)
    fdraw.polygon(inner, fill=(11, 10, 28, 242))
    canvas.alpha_composite(fill)

    # 외곽 스트로크 (그라데이션)
    stroke_mask = Image.new("L", (size, size), 0)
    sdraw = ImageDraw.Draw(stroke_mask)
    sdraw.polygon(outer, outline=255, width=stroke)
    sdraw.polygon(inner, outline=120, width=max(1, int(size * 0.005)))
    gradient = linear_gradient((size, size), [(0.0, (214, 200, 255)), (0.42, VIOLET), (1.0, CYAN)], 118)
    canvas.paste(gradient, (0, 0), stroke_mask)

    # 스트로크 글로우(은은하게)
    glow = stroke_mask.filter(ImageFilter.GaussianBlur(size * 0.009))
    glow_layer = Image.new("RGBA", (size, size), (124, 100, 255, 0))
    glow_layer.putalpha(glow.point(lambda value: int(value * 0.45)))
    canvas.alpha_composite(glow_layer)

    # V 모노그램
    vbox = (pad + (size - 2 * pad) * 0.20, pad + (size - 2 * pad) * 0.18, (size - 2 * pad) * 0.60, (size - 2 * pad) * 0.64)
    v_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(v_mask).polygon(to_px(V_SHAPE, (size, size), vbox), fill=255)

    v_grad = linear_gradient((size, size), [(0.0, (255, 255, 255)), (0.45, (176, 158, 255)), (1.0, CYAN)], 112)
    v_glow = v_mask.filter(ImageFilter.GaussianBlur(size * 0.016))
    v_glow_layer = Image.new("RGBA", (size, size), (116, 104, 255, 0))
    v_glow_layer.putalpha(v_glow.point(lambda value: int(value * 0.5)))
    canvas.alpha_composite(v_glow_layer)
    canvas.paste(v_grad, (0, 0), v_mask)

    # 상단 코인 슬롯
    slot_w = size * 0.13
    slot_h = max(2.0, size * 0.016)
    slot_x = size / 2 - slot_w / 2
    slot_y = pad + (size - 2 * pad) * 0.155
    slot_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(slot_mask).rounded_rectangle(
        [slot_x, slot_y, slot_x + slot_w, slot_y + slot_h], radius=slot_h / 2, fill=255
    )
    canvas.paste(Image.new("RGBA", (size, size), (*CYAN, 255)), (0, 0), slot_mask)

    # 우상단 스파크 액센트 (배지 바깥)
    cx = pad + (size - 2 * pad) * 0.93
    cy = pad + (size - 2 * pad) * 0.075
    r = size * 0.034
    spark = [
        (cx, cy - r),
        (cx + r * 0.18, cy - r * 0.18),
        (cx + r, cy),
        (cx + r * 0.18, cy + r * 0.18),
        (cx, cy + r),
        (cx - r * 0.18, cy + r * 0.18),
        (cx - r, cy),
        (cx - r * 0.18, cy - r * 0.18),
    ]
    smask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(smask).polygon(spark, fill=255)
    sglow = smask.filter(ImageFilter.GaussianBlur(size * 0.013))
    sglow_layer = Image.new("RGBA", (size, size), (110, 210, 255, 0))
    sglow_layer.putalpha(sglow.point(lambda value: int(value * 0.3)))
    canvas.alpha_composite(sglow_layer)
    canvas.paste(Image.new("RGBA", (size, size), (*WHITE, 245)), (0, 0), smask)

    return canvas


def make_logo(path: Path) -> None:
    emblem(512).save(path, "PNG")


def make_banner(path: Path) -> None:
    width, height = 1200, 420
    canvas = linear_gradient((width, height), [(0.0, (5, 5, 13)), (0.55, (11, 9, 26)), (1.0, (17, 12, 42))], 120)
    draw = ImageDraw.Draw(canvas)

    # 바닥 글로우
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse([560, height - 150, 1180, height + 190], fill=(104, 74, 255, 60))
    gdraw.ellipse([120, -190, 620, 190], fill=(44, 196, 255, 34))
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(70)))

    # 헥스 패턴
    pattern = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(pattern)
    step = 54
    for row in range(-1, height // step + 2):
        for col in range(-1, width // step + 2):
            cx = col * step + (step // 2 if row % 2 else 0)
            cy = row * step * 0.87
            radius = 15
            hexagon = [
                (cx + radius * math.cos(math.radians(a - 90)), cy + radius * math.sin(math.radians(a - 90)))
                for a in (0, 60, 120, 180, 240, 300)
            ]
            pdraw.polygon(hexagon, outline=(255, 255, 255, 9))
    canvas.alpha_composite(pattern)
    draw = ImageDraw.Draw(canvas)

    # 좌측 엠블럼
    mark_size = 268
    mark = emblem(mark_size, pad_ratio=0.02)
    canvas.alpha_composite(mark, (68, (height - mark_size) // 2 - 4))

    # 구분선
    draw.line([(372, 96), (372, height - 96)], fill=(120, 110, 200, 60), width=1)

    # 워드마크 (그라데이션 텍스트)
    title = "VENDRIX"
    title_font = font(104)
    tracking = 12
    text_y = 118
    text_mask = Image.new("L", (width, height), 0)
    tdraw = ImageDraw.Draw(text_mask)
    x = 410
    for char in title:
        tdraw.text((x, text_y), char, font=title_font, fill=255)
        x += tdraw.textlength(char, font=title_font) + tracking
    bbox = text_mask.getbbox()
    if bbox:
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        word_grad = linear_gradient((tw, th), [(0.0, (255, 255, 255)), (0.55, (188, 168, 255)), (1.0, CYAN)], 12)
        canvas.paste(word_grad, (bbox[0], bbox[1]), text_mask.crop(bbox))

    # 서브 타이틀
    sub = "AUTOMATED VENDING ENGINE"
    sub_font = font(19, bold=True)
    sub_mask = Image.new("L", (width, height), 0)
    sdraw2 = ImageDraw.Draw(sub_mask)
    sx = 414
    for char in sub:
        sdraw2.text((sx, 250), char, font=sub_font, fill=255)
        sx += sdraw2.textlength(char, font=sub_font) + 5
    canvas.paste(Image.new("RGBA", (width, height), (126, 226, 255, 255)), (0, 0), sub_mask)

    # 태그 칩
    chip_font = font(15, bold=True)
    labels = ["DISCORD", "WEB PANEL", "AUTO CHARGE"]
    cx = 414
    for label in labels:
        text_w = draw.textlength(label, font=chip_font)
        chip_w = text_w + 30
        draw.rounded_rectangle(
            [cx, 296, cx + chip_w, 332], radius=18, fill=(124, 92, 255, 24), outline=(158, 140, 255, 120)
        )
        draw.text((cx + 15, 306), label, font=chip_font, fill=(206, 196, 250, 255))
        cx += chip_w + 10

    # 코너 브래킷
    bracket = (150, 132, 255, 120)
    for (bx, by, dx, dy) in ((36, 34, 1, 1), (width - 36, 34, -1, 1), (36, height - 34, 1, -1), (width - 36, height - 34, -1, -1)):
        draw.line([(bx, by), (bx + 26 * dx, by)], fill=bracket, width=2)
        draw.line([(bx, by), (bx, by + 26 * dy)], fill=bracket, width=2)

    canvas.convert("RGB").save(path, "PNG", quality=95)


def make_avatar(path: Path, size: int = 512) -> None:
    """디스코드 봇 아이콘용 아바타(원형 크롭 안전 영역)."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    center = size / 2

    # 배경 원판 (원형 크롭 경계까지 채움)
    disc = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ddraw = ImageDraw.Draw(disc)
    ddraw.ellipse([0, 0, size - 1, size - 1], fill=(10, 8, 24, 255))
    canvas.alpha_composite(disc)

    # 원판 안쪽 그라데이션 글로우
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse(
        [size * 0.12, size * -0.10, size * 0.98, size * 0.72],
        fill=(104, 74, 255, 90),
    )
    gdraw.ellipse(
        [size * -0.16, size * 0.34, size * 0.62, size * 1.06],
        fill=(34, 168, 255, 64),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.09))
    # 원형 마스크로 잘라내기
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    glow.putalpha(Image.composite(glow.getchannel("A"), Image.new("L", (size, size), 0), mask))
    canvas.alpha_composite(glow)

    # 엠블럼 (원형 안전 영역: 0.66 크기)
    mark_size = int(size * 0.73)
    mark = emblem(mark_size, pad_ratio=0.02)
    offset = (size - mark_size) // 2
    canvas.alpha_composite(mark, (offset, offset))

    # 은은한 링
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rdraw = ImageDraw.Draw(ring)
    rdraw.ellipse([1, 1, size - 2, size - 2], outline=(150, 132, 255, 70), width=max(2, size // 170))
    canvas.alpha_composite(ring)

    canvas.save(path, "PNG")



if __name__ == "__main__":
    STATIC.mkdir(parents=True, exist_ok=True)
    make_logo(STATIC / "logo.png")
    make_banner(STATIC / "banner.png")
    make_avatar(STATIC / "avatar.png")
    print("generated:", STATIC / "logo.png", STATIC / "banner.png", STATIC / "avatar.png")
