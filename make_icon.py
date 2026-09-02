"""生成 Windows 多尺寸 icon.ico，供 Nuitka 打包用。

自带一个「蓝色玻璃 + 金色流沙」的沙漏图标（与 UI 的 iOS 玻璃浅蓝风呼应），
无需任何外部素材即可构建。若想换成自定义图案，把 1024x1024 的 PNG 放到
assets/icon-1024.png 即可覆盖内置图案。

关键：ICO 的每一帧都写成 BMP/DIB（BGRA + AND 掩码），不用 PNG 帧。
Windows 的 UpdateResource（Nuitka 打包时用来把图标塞进 app.dll/exe）对 PNG
编码的图标帧会以 error code 22 失败；DIB 帧才稳。Pillow 新版会把所有帧存成
PNG，故这里自己拼 ICO 容器。
"""

import os
import struct

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
LOCAL_PNG = os.path.join(ASSETS, "icon-1024.png")
ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]

SIZE = 1024
TOP_BLUE = (142, 197, 255)
BOTTOM_BLUE = (58, 123, 255)
GLASS = (255, 255, 255, 236)
SAND = (255, 199, 102, 255)


def _vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    column = Image.new("RGBA", (1, size))
    for y in range(size):
        t = y / (size - 1)
        column.putpixel(
            (0, y),
            (
                round(top[0] + (bottom[0] - top[0]) * t),
                round(top[1] + (bottom[1] - top[1]) * t),
                round(top[2] + (bottom[2] - top[2]) * t),
                255,
            ),
        )
    return column.resize((size, size))


def _render_icon() -> Image.Image:
    gradient = _vertical_gradient(SIZE, TOP_BLUE, BOTTOM_BLUE)
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=224, fill=255)
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    img.paste(gradient, (0, 0), mask)

    draw = ImageDraw.Draw(img)
    cx, cy = SIZE // 2, SIZE // 2
    cap_half, cap_h = 168, 48
    top_cap_y, bottom_cap_y = 250, SIZE - 250 - cap_h

    draw.rounded_rectangle(
        [cx - cap_half, top_cap_y, cx + cap_half, top_cap_y + cap_h], radius=22, fill=GLASS
    )
    draw.rounded_rectangle(
        [cx - cap_half, bottom_cap_y, cx + cap_half, bottom_cap_y + cap_h], radius=22, fill=GLASS
    )

    glass_half = 138
    draw.polygon(
        [(cx - glass_half, top_cap_y + cap_h), (cx + glass_half, top_cap_y + cap_h), (cx, cy)],
        fill=GLASS,
    )
    draw.polygon(
        [(cx - glass_half, bottom_cap_y), (cx + glass_half, bottom_cap_y), (cx, cy)],
        fill=GLASS,
    )

    sand_half = 104
    draw.polygon(
        [(cx - sand_half, top_cap_y + cap_h + 6), (cx + sand_half, top_cap_y + cap_h + 6), (cx, cy - 26)],
        fill=SAND,
    )
    draw.polygon(
        [(cx - sand_half, bottom_cap_y - 6), (cx + sand_half, bottom_cap_y - 6), (cx, cy + 40)],
        fill=SAND,
    )
    draw.rectangle([cx - 7, cy - 26, cx + 7, cy + 40], fill=SAND)
    return img


def load_source() -> Image.Image:
    if os.path.exists(LOCAL_PNG):
        print("使用自定义图标：", LOCAL_PNG)
        return Image.open(LOCAL_PNG).convert("RGBA")
    print("未找到 assets/icon-1024.png，生成内置沙漏图标")
    img = _render_icon()
    os.makedirs(ASSETS, exist_ok=True)
    img.save(LOCAL_PNG)
    print("已缓存源图：", LOCAL_PNG)
    return img


def _dib_frame(im: Image.Image) -> bytes:
    """单帧 BMP/DIB：BITMAPINFOHEADER(高度翻倍) + BGRA 像素(自下而上) + 全 0 AND 掩码。"""
    w, h = im.size
    flipped = im.transpose(Image.FLIP_TOP_BOTTOM)
    color = flipped.tobytes("raw", "BGRA")
    mask_row = ((w + 31) // 32) * 4
    mask = b"\x00" * (mask_row * h)
    header = struct.pack(
        "<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0
    )
    return header + color + mask


def write_ico(src: Image.Image, out_path: str, sizes) -> None:
    frames = []
    for s in sizes:
        frame = src.resize((s, s), Image.LANCZOS)
        frames.append((s, _dib_frame(frame)))

    entries = bytearray()
    blobs = bytearray()
    offset = 6 + 16 * len(frames)
    for s, data in frames:
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)

    with open(out_path, "wb") as handle:
        handle.write(struct.pack("<HHH", 0, 1, len(frames)))
        handle.write(bytes(entries))
        handle.write(bytes(blobs))


def main() -> None:
    src = load_source()
    out = os.path.join(HERE, "icon.ico")
    write_ico(src, out, ICON_SIZES)
    print("icon.ico created (BMP frames) ->", out)


if __name__ == "__main__":
    main()
