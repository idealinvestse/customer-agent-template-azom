"""Pure-Python PNG codec, 5x7 bitmap font, and side-by-side comparison compositor."""

from __future__ import annotations

import os
import secrets
import struct
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) from a PNG IHDR header, or None if invalid."""
    if len(data) < 24 or not data.startswith(PNG_MAGIC) or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


_MINI_FONT_5X7: dict[str, tuple[int, ...]] = {
    " ": (0, 0, 0, 0, 0, 0, 0),
    "-": (0, 0, 0, 0b11111, 0, 0, 0),
    ".": (0, 0, 0, 0, 0, 0b01100, 0b01100),
    ":": (0, 0b01100, 0b01100, 0, 0b01100, 0b01100, 0),
    "(": (0b00100, 0b01000, 0b10000, 0b10000, 0b10000, 0b01000, 0b00100),
    ")": (0b00100, 0b00010, 0b00001, 0b00001, 0b00001, 0b00010, 0b00100),
    "/": (0b00001, 0b00010, 0b00100, 0b00100, 0b01000, 0b10000, 0),
    "0": (0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110),
    "1": (0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "2": (0b01110, 0b10001, 0b00001, 0b00110, 0b01000, 0b10000, 0b11111),
    "3": (0b11110, 0b00001, 0b00001, 0b01110, 0b00001, 0b00001, 0b11110),
    "4": (0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010),
    "5": (0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110),
    "6": (0b01110, 0b10000, 0b11110, 0b10001, 0b10001, 0b10001, 0b01110),
    "7": (0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000),
    "8": (0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110),
    "9": (0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00001, 0b01110),
    "A": (0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001),
    "B": (0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110),
    "C": (0b01110, 0b10001, 0b10000, 0b10000, 0b10000, 0b10001, 0b01110),
    "D": (0b11100, 0b10010, 0b10001, 0b10001, 0b10001, 0b10010, 0b11100),
    "E": (0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111),
    "F": (0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000),
    "G": (0b01110, 0b10001, 0b10000, 0b10111, 0b10001, 0b10001, 0b01110),
    "H": (0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001),
    "I": (0b01110, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "J": (0b00111, 0b00010, 0b00010, 0b00010, 0b10010, 0b10010, 0b01100),
    "K": (0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001),
    "L": (0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111),
    "M": (0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001),
    "N": (0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001),
    "O": (0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "P": (0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000),
    "Q": (0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101),
    "R": (0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001),
    "S": (0b01111, 0b10000, 0b10000, 0b01110, 0b00001, 0b00001, 0b11110),
    "T": (0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100),
    "U": (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110),
    "V": (0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100),
    "W": (0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b11011, 0b10001),
    "X": (0b10001, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b10001),
    "Y": (0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100),
    "Z": (0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b11111),
}


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter_scanline(ftype: int, row: bytearray, prev_row: bytearray, bpp: int, stride: int) -> bool:
    if ftype == 0:
        return True
    if ftype == 1:
        for i in range(bpp, stride):
            row[i] = (row[i] + row[i - bpp]) & 0xFF
        return True
    if ftype == 2:
        for i in range(stride):
            row[i] = (row[i] + prev_row[i]) & 0xFF
        return True
    if ftype == 3:
        for i in range(stride):
            left = row[i - bpp] if i >= bpp else 0
            row[i] = (row[i] + ((left + prev_row[i]) >> 1)) & 0xFF
        return True
    if ftype == 4:
        for i in range(stride):
            a = row[i - bpp] if i >= bpp else 0
            b = prev_row[i]
            c = prev_row[i - bpp] if i >= bpp else 0
            row[i] = (row[i] + _paeth_predictor(a, b, c)) & 0xFF
        return True
    return False


def _row_to_rgb(rgb: bytearray, dst_base: int, row: bytearray, width: int, color_type: int) -> None:
    if color_type == 2:
        rgb[dst_base : dst_base + width * 3] = row
        return
    if color_type == 6:
        for x in range(width):
            si = x * 4
            di = dst_base + x * 3
            alpha = row[si + 3]
            if alpha == 255:
                rgb[di] = row[si]
                rgb[di + 1] = row[si + 1]
                rgb[di + 2] = row[si + 2]
            else:
                inv = 255 - alpha
                rgb[di] = (row[si] * alpha + 255 * inv) // 255
                rgb[di + 1] = (row[si + 1] * alpha + 255 * inv) // 255
                rgb[di + 2] = (row[si + 2] * alpha + 255 * inv) // 255
        return
    for x in range(width):
        g = row[x]
        di = dst_base + x * 3
        rgb[di] = g
        rgb[di + 1] = g
        rgb[di + 2] = g


def _decode_png_rgb(raw: bytes) -> tuple[int, int, bytearray] | None:
    """Decode standard 8-bit non-interlaced RGB/RGBA/Grayscale PNG into flat RGB bytearray."""
    if len(raw) < 33 or raw[:8] != PNG_MAGIC:
        return None
    pos = 8
    width = 0
    height = 0
    color_type = -1
    idat_chunks: list[bytes] = []
    while pos + 12 <= len(raw):
        chunk_len = struct.unpack(">I", raw[pos : pos + 4])[0]
        chunk_type = raw[pos + 4 : pos + 8]
        data_start = pos + 8
        data_end = data_start + chunk_len
        if data_end + 4 > len(raw):
            return None
        chunk_data = raw[data_start:data_end]
        if chunk_type == b"IHDR":
            if chunk_len != 13:
                return None
            width, height, bit_depth, color_type, comp, flt, interlace = struct.unpack(">IIBBBBB", chunk_data)
            if width <= 0 or height <= 0 or width > 8192 or height > 12000:
                return None
            if bit_depth != 8 or comp != 0 or flt != 0 or interlace != 0:
                return None
        elif chunk_type == b"IDAT":
            idat_chunks.append(chunk_data)
        elif chunk_type == b"IEND":
            break
        pos = data_end + 4

    bpp = {0: 1, 2: 3, 6: 4}.get(color_type)
    if bpp is None or width <= 0 or height <= 0 or not idat_chunks:
        return None
    try:
        decompressed = zlib.decompress(b"".join(idat_chunks))
    except zlib.error:
        return None
    stride = width * bpp
    if len(decompressed) < height * (stride + 1):
        return None

    prev_row = bytearray(stride)
    rgb = bytearray(width * height * 3)
    src_off = 0
    for y in range(height):
        ftype = decompressed[src_off]
        src_off += 1
        row = bytearray(decompressed[src_off : src_off + stride])
        src_off += stride
        if not _unfilter_scanline(ftype, row, prev_row, bpp, stride):
            return None
        prev_row = row
        _row_to_rgb(rgb, y * width * 3, row, width, color_type)
    return width, height, rgb


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)


def _encode_png_rgb(width: int, height: int, rgb: bytearray) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_rows = bytearray()
    row_bytes = width * 3
    for y in range(height):
        raw_rows.append(0)
        start = y * row_bytes
        raw_rows.extend(rgb[start : start + row_bytes])
    compressed = zlib.compress(bytes(raw_rows), level=6)
    return PNG_MAGIC + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", compressed) + _png_chunk(b"IEND", b"")


def _resize_and_crop_rgb(
    width: int,
    height: int,
    rgb: bytearray,
    *,
    max_width: int,
    max_height: int,
) -> tuple[int, int, bytearray]:
    new_w = width
    new_h = height
    if width > max_width:
        scale = max_width / float(width)
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))
    cropped_h = min(new_h, max_height)
    if new_w == width and cropped_h == height:
        return width, height, rgb
    out = bytearray(new_w * cropped_h * 3)
    for dy in range(cropped_h):
        sy = min(height - 1, int(dy * height / new_h))
        src_row = sy * width * 3
        dst_row = dy * new_w * 3
        for dx in range(new_w):
            sx = min(width - 1, int(dx * width / new_w))
            si = src_row + sx * 3
            di = dst_row + dx * 3
            out[di] = rgb[si]
            out[di + 1] = rgb[si + 1]
            out[di + 2] = rgb[si + 2]
    return new_w, cropped_h, out


def _draw_mini_text_rgb(
    canvas: bytearray,
    canvas_w: int,
    canvas_h: int,
    x0: int,
    y0: int,
    text: str,
    color: tuple[int, int, int],
    *,
    scale: int = 2,
    max_x: int | None = None,
) -> None:
    translit = (
        str(text or "")
        .replace("Å", "A")
        .replace("å", "A")
        .replace("Ä", "A")
        .replace("ä", "A")
        .replace("Ö", "O")
        .replace("ö", "O")
        .replace("Ø", "O")
        .replace("ø", "O")
        .replace("Æ", "AE")
        .replace("æ", "AE")
        .upper()
    )
    limit_x = canvas_w if max_x is None else min(canvas_w, max_x)
    cx = x0
    r, g, b = color
    for ch in translit:
        glyph = _MINI_FONT_5X7.get(ch, _MINI_FONT_5X7[" "])
        if cx + 5 * scale >= limit_x:
            break
        for row_idx, bits in enumerate(glyph):
            for col_idx in range(5):
                if bits & (1 << (4 - col_idx)):
                    for sy in range(scale):
                        py = y0 + row_idx * scale + sy
                        if py < 0 or py >= canvas_h:
                            continue
                        row_off = py * canvas_w * 3
                        for sx in range(scale):
                            px = cx + col_idx * scale + sx
                            if 0 <= px < limit_x:
                                di = row_off + px * 3
                                canvas[di] = r
                                canvas[di + 1] = g
                                canvas[di + 2] = b
        cx += 6 * scale


def _atomic_write_png(out_path: Path, png_bytes: bytes) -> Path | None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(f"{out_path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_bytes(png_bytes)
        tmp.replace(out_path)
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return None
    return out_path


def _compose_with_stdlib_png(
    valid_panels: list[tuple[str, Path]],
    out_path: Path,
    *,
    max_panel_width: int,
    max_height: int,
) -> Path | None:
    decoded: list[tuple[str, int, int, bytearray]] = []
    for label, path in valid_panels:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        parsed = _decode_png_rgb(raw)
        if parsed is None:
            continue
        w, h, rgb = _resize_and_crop_rgb(
            parsed[0], parsed[1], parsed[2], max_width=max_panel_width, max_height=max_height
        )
        decoded.append((label, w, h, rgb))
    if len(decoded) < 2:
        return None

    header_h = 38
    divider_w = 2
    total_w = sum(item[1] for item in decoded) + divider_w * (len(decoded) - 1)
    body_h = max(item[2] for item in decoded)
    total_h = header_h + body_h

    canvas = bytearray([15, 23, 42] * (total_w * total_h))

    for y in range(min(2, total_h)):
        off = y * total_w * 3
        canvas[off : off + total_w * 3] = bytes([34, 197, 94]) * total_w

    if header_h - 1 < total_h:
        off = (header_h - 1) * total_w * 3
        canvas[off : off + total_w * 3] = bytes([51, 65, 85]) * total_w

    cursor_x = 0
    for idx, (label, pw, ph, prgb) in enumerate(decoded):
        _draw_mini_text_rgb(
            canvas,
            total_w,
            total_h,
            cursor_x + 10,
            12,
            label,
            (248, 250, 252),
            scale=2,
            max_x=cursor_x + pw - 6,
        )
        for y in range(ph):
            src_start = y * pw * 3
            dst_start = ((header_h + y) * total_w + cursor_x) * 3
            canvas[dst_start : dst_start + pw * 3] = prgb[src_start : src_start + pw * 3]
        cursor_x += pw
        if idx < len(decoded) - 1:
            for y in range(total_h):
                for dx in range(divider_w):
                    di = (y * total_w + cursor_x + dx) * 3
                    canvas[di] = 51
                    canvas[di + 1] = 65
                    canvas[di + 2] = 85
            cursor_x += divider_w

    return _atomic_write_png(out_path, _encode_png_rgb(total_w, total_h, canvas))


def build_comparison_composite(
    panels: Sequence[tuple[str, Path]],
    out_path: Path,
    *,
    max_panel_width: int = 720,
    max_height: int = 1200,
) -> Path | None:
    """Compose 2–4 preview PNGs side-by-side with a crisp dark header bar and divider borders.

    Uses Pillow when available and falls back to a zero-dependency pure-Python PNG compositor
    so side-by-side ``compare-desktop.png`` artifacts work reliably in every environment.
    """
    valid_panels: list[tuple[str, Path]] = []
    for raw_label, raw_path in list(panels)[:4]:
        p = Path(raw_path)
        if p.is_symlink() or not p.is_file():
            continue
        label = " ".join(str(raw_label or "").split()).strip()[:48] or "Panel"
        valid_panels.append((label, p))
    if len(valid_panels) < 2:
        return None

    cap_w = max(64, min(1440, int(max_panel_width)))
    cap_h = max(64, min(2400, int(max_height)))

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return _compose_with_stdlib_png(
            valid_panels,
            Path(out_path),
            max_panel_width=cap_w,
            max_height=cap_h,
        )

    loaded: list[tuple[str, Any]] = []
    for label, path in valid_panels:
        try:
            with Image.open(path) as img:
                rgb = img.convert("RGB")
                if rgb.width > cap_w:
                    new_h = max(1, int(round(rgb.height * (cap_w / float(rgb.width)))))
                    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
                    rgb = rgb.resize((cap_w, new_h), resample)
                if rgb.height > cap_h:
                    rgb = rgb.crop((0, 0, rgb.width, cap_h))
                loaded.append((label, rgb))
        except Exception:
            continue
    if len(loaded) < 2:
        return None

    header_h = 38
    divider_w = 2
    total_w = sum(im.width for _, im in loaded) + divider_w * (len(loaded) - 1)
    body_h = max(im.height for _, im in loaded)
    total_h = header_h + body_h

    canvas = Image.new("RGB", (total_w, total_h), (15, 23, 42))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([(0, 0), (total_w - 1, 1)], fill=(34, 197, 94))
    draw.line([(0, header_h - 1), (total_w - 1, header_h - 1)], fill=(51, 65, 85), width=1)
    font = ImageFont.load_default()

    cursor_x = 0
    for idx, (label, im) in enumerate(loaded):
        draw.text((cursor_x + 10, 12), label, fill=(248, 250, 252), font=font)
        canvas.paste(im, (cursor_x, header_h))
        cursor_x += im.width
        if idx < len(loaded) - 1:
            draw.rectangle([(cursor_x, 0), (cursor_x + divider_w - 1, total_h - 1)], fill=(51, 65, 85))
            cursor_x += divider_w

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        canvas.save(tmp, format="PNG", optimize=True)
        tmp.replace(target)
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return None
    return target
