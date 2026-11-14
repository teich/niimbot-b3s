#!/usr/bin/env python3
"""Minimal local driver for the NIIMBOT B3S label printer over USB serial.

Protocol details come from MultiMote's niimbluelib (MIT):
https://github.com/MultiMote/niimbluelib
"""

import argparse
import glob
import sys
import time

import serial
from PIL import Image, ImageDraw, ImageFont, ImageOps

DPI = 203
PX_PER_MM = DPI / 25.4
PRINTHEAD_PX = 576  # 72mm at 203dpi

LABEL_TYPES = {"gaps": 1, "black": 2, "continuous": 3, "perforated": 4, "transparent": 5}

MODELS = {256: "B3S", 260: "B3S", 262: "B3S", 275: "B3S_A"}

# request cmd -> expected response cmd(s)
RESPONSES = {
    0xC1: {0xC2},  # connect
    0x40: None,  # printer info: response id varies by key
    0x21: {0x31},  # set density
    0x23: {0x33},  # set label type
    0x01: {0x02},  # print start
    0x03: {0x04},  # page start
    0x13: {0x14},  # set page size
    0xE3: {0xE4},  # page end
    0xA3: {0xB3},  # print status
    0xF3: {0xF4},  # print end
    0xDA: {0xD0},  # cancel print
    0xDC: {0xDD, 0xDE, 0xDF, 0xD9},  # heartbeat
}
PRINT_ERRORS = {
    1: "cover open",
    2: "no paper",
    3: "low battery",
    5: "cancelled on printer",
    6: "data error",
    7: "overheated",
    8: "paper out",
    9: "printer busy",
    0x10: "wrong paper",
    0x11: "label type rejected",
    0x13: "density rejected",
}
ERR_PRINT = 0xDB
ERR_NOT_SUPPORTED = 0x00


def u16(n):
    return [(n >> 8) & 0xFF, n & 0xFF]


def build_packet(cmd, data=(1,)):
    data = bytes(data)
    cs = cmd ^ len(data)
    for b in data:
        cs ^= b
    raw = b"\x55\x55" + bytes([cmd, len(data)]) + data + bytes([cs]) + b"\xaa\xaa"
    return b"\x03" + raw if cmd == 0xC1 else raw


class PrinterError(Exception):
    pass


class Printer:
    def __init__(self, port, verbose=False):
        self.ser = serial.Serial(port, 115200, timeout=0.1)
        self.verbose = verbose
        self.buf = b""

    def close(self):
        self.ser.close()

    def _read_packet(self, deadline):
        while time.monotonic() < deadline:
            start = self.buf.find(b"\x55\x55")
            if start >= 0 and len(self.buf) >= start + 4:
                length = self.buf[start + 3]
                end = start + 4 + length + 3
                if len(self.buf) >= end:
                    pkt = self.buf[start:end]
                    self.buf = self.buf[end:]
                    if pkt[-2:] == b"\xaa\xaa":
                        if self.verbose:
                            print(f"  <- {pkt.hex(' ')}", file=sys.stderr)
                        return pkt[2], pkt[4 : 4 + length]
                    continue
            self.buf += self.ser.read(256)
        return None

    def send(self, cmd, data=(1,), expect=None, timeout=2.0):
        """Send a packet; wait for a response unless the command is one-way."""
        pkt = build_packet(cmd, data)
        if self.verbose and cmd not in (0x83, 0x84, 0x85):
            print(f"  -> {pkt.hex(' ')}", file=sys.stderr)
        self.ser.write(pkt)
        if cmd in (0x83, 0x84, 0x85):  # bitmap rows: no response
            return None
        expect = expect or RESPONSES.get(cmd)
        deadline = time.monotonic() + timeout
        while True:
            resp = self._read_packet(deadline)
            if resp is None:
                raise PrinterError(f"timeout waiting for response to 0x{cmd:02x}")
            rcmd, rdata = resp
            if rcmd == ERR_PRINT:
                code = rdata[0] if rdata else 0
                raise PrinterError(f"printer error {code}: {PRINT_ERRORS.get(code, 'unknown')}")
            if rcmd == ERR_NOT_SUPPORTED:
                raise PrinterError(f"command 0x{cmd:02x} not supported")
            if expect is None or rcmd in expect:
                return rdata

    def connect(self):
        self.ser.reset_input_buffer()
        return self.send(0xC1)[0]

    def info(self, key):
        return self.send(0x40, [key])

    def model_id(self):
        d = self.info(8)
        return d[0] << 8 if len(d) == 1 else (d[0] << 8) | d[1]

    # --- printing (niimbluelib "B1" print task) ---

    def print_image(self, img, copies=1, density=3, label_type=1):
        rows = encode(img)
        width, height = img.size
        self.send(0x21, [density])
        self.send(0x23, [label_type])
        self.send(0x01, [*u16(copies), 0, 0, 0, 0, 0])
        self.send(0x03)
        self.send(0x13, [*u16(height), *u16(width), *u16(copies)])
        for kind, rownum, repeat, data in rows:
            if kind == "void":
                self.send(0x84, [*u16(rownum), repeat])
            else:
                self.send(*bitmap_row_packet(rownum, repeat, data))
        self.send(0xE3, timeout=10)
        self._wait_done(copies)

    def _wait_done(self, copies, timeout=60):
        """Poll PrintStatus; firmware without it (B3S 10.x) is polled via PrintEnd instead."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                d = self.send(0xA3, timeout=5)
            except PrinterError as e:
                if "not supported" not in str(e):
                    raise
                return self._wait_print_end(deadline)
            page = (d[0] << 8) | d[1]
            if len(d) == 10 and d[6]:
                raise PrinterError(f"printer error {d[6]}: {PRINT_ERRORS.get(d[6], 'unknown')}")
            if page >= copies:
                self.send(0xF3, timeout=5)
                return
            time.sleep(0.3)
        raise PrinterError("timed out waiting for print to finish")

    def _wait_print_end(self, deadline):
        while time.monotonic() < deadline:
            if self.send(0xF3, timeout=5)[0] == 1:
                return
            time.sleep(0.5)
        raise PrinterError("timed out waiting for print to finish")


def encode(img):
    """1-bit PIL image -> list of (kind, row, repeat, bytes), run-length merged."""
    width, height = img.size
    px = img.load()
    nbytes = (width + 7) // 8
    out = []
    for y in range(height):
        row = bytearray(nbytes)
        for x in range(width):
            if px[x, y] == 0:  # black
                row[x >> 3] |= 0x80 >> (x & 7)
        kind = "pixels" if any(row) else "void"
        row = bytes(row)
        if out and out[-1][0] == kind and out[-1][3] == row and out[-1][2] < 255:
            out[-1][2] += 1
        else:
            out.append([kind, y, 1, row])
    return out


def bitmap_row_packet(rownum, repeat, data):
    black = sum(bin(b).count("1") for b in data)
    chunk = PRINTHEAD_PX // 8 // 3
    parts = [0, 0, 0]
    for i, b in enumerate(data):
        parts[min(i // chunk, 2)] += bin(b).count("1")
    if black <= 6:  # indexed packet: list of black pixel positions
        idx = []
        for i, b in enumerate(data):
            for bit in range(8):
                if b & (0x80 >> bit):
                    idx += u16(i * 8 + bit)
        return 0x83, [*u16(rownum), *parts, repeat, *idx]
    return 0x85, [*u16(rownum), *parts, repeat, *data]


# --- rendering ---

FONT_PATHS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/Library/Fonts/Arial.ttf",
]


def load_font(path, size):
    for p in [path] if path else FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def parse_size(s):
    w, h = s.lower().split("x")
    return round(float(w) * PX_PER_MM), round(float(h) * PX_PER_MM)


def text_box(size_px, margin_mm):
    margin = round(margin_mm * PX_PER_MM)
    return margin, size_px[0] - 2 * margin, size_px[1] - 2 * margin


def fit_font_px(text, size_px, font_path=None, margin_mm=2.0, align="center"):
    """Largest font size in px at which text fits inside the label margins."""
    _, box_w, box_h = text_box(size_px, margin_mm)
    text = text.replace("\\n", "\n")
    draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    lo, hi = 6, box_h
    while lo < hi:
        mid = (lo + hi + 1) // 2
        l, t, r, b = draw.multiline_textbbox((0, 0), text, font=load_font(font_path, mid), align=align)
        if r - l <= box_w and b - t <= box_h:
            lo = mid
        else:
            hi = mid - 1
    return lo


def render_text(text, size_px, font_path=None, margin_mm=2.0, align="center", font_px=None):
    w, h = size_px
    margin, box_w, box_h = text_box(size_px, margin_mm)
    font_px = font_px or fit_font_px(text, size_px, font_path, margin_mm, align)
    text = text.replace("\\n", "\n")
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    font = load_font(font_path, font_px)
    l, t, r, b = draw.multiline_textbbox((0, 0), text, font=font, align=align)
    if r - l > box_w or b - t > box_h:
        print(f"warning: {text!r} overflows the label margins at this font size", file=sys.stderr)
    # center vertically on a fixed "Hg" height so labels in a set share a baseline
    ref = "\n".join("Hg" for _ in text.split("\n"))
    _, t, _, b = draw.multiline_textbbox((0, 0), ref, font=font, align=align)
    x = margin + (box_w - (r - l)) / 2 - l
    y = margin + (box_h - (b - t)) / 2 - t
    draw.multiline_text((x, y), text, font=font, fill=0, align=align)
    return img.point(lambda v: 0 if v < 128 else 255, "1")


# --- sheet-set labels: brand, italic description, short rule, "KING · SHEET SET" ---

SHEET_FONTS = {
    # Georgia was drawn for low-res screens: its serifs and joins are thick enough to survive a
    # 203dpi thermal head, where Baskerville's fine serifs printed ragged.
    "brand": [
        ("/System/Library/Fonts/Supplemental/Georgia Bold.ttf", 0),
        ("/System/Library/Fonts/Supplemental/Baskerville.ttc", 1),  # Bold
    ],
    "description": [
        ("/System/Library/Fonts/Supplemental/Georgia Bold Italic.ttf", 0),
        ("/System/Library/Fonts/Supplemental/Baskerville.ttc", 3),  # Bold Italic
    ],
    "detail": [("/System/Library/Fonts/Avenir Next.ttc", 2)],  # Demi Bold
}
BRAND_TRACK = 0.08  # letter spacing, in em
DETAIL_TRACK = 0.18


def sheet_font(role, px):
    for path, index in SHEET_FONTS[role]:
        try:
            return ImageFont.truetype(path, px, index=index)
        except OSError:
            continue
    return load_font(None, px)


def parse_sheet(spec):
    """'Brand, Size[, Type[, Description]]' -> (brand, size, kind, description).

    Type defaults to Sheet Set (leave it empty to keep the default: 'Frette, King, , White Cotton').
    """
    parts = [p.strip() for p in spec.split(",")]
    if not 2 <= len(parts) <= 4 or not parts[0] or not parts[1]:
        sys.exit(f"expected 'Brand, Size[, Type[, Description]]', got {spec!r}")
    brand, size, kind, description = parts + [""] * (4 - len(parts))
    return brand, size, kind or "Sheet Set", description or None


def tracked_width(draw, text, font, track_px):
    return sum(draw.textlength(c, font=font) for c in text) + track_px * (len(text) - 1)


def draw_tracked(draw, x, baseline, text, font, track_px):
    for c in text:
        draw.text((x, baseline), c, font=font, fill=0, anchor="ls")
        x += draw.textlength(c, font=font) + track_px


def cap_height(font):
    _, top, _, bottom = font.getbbox("H", anchor="ls")
    return bottom - top


BRAND_REF = "BROOKLINEN"  # every label uses the brand size that fits this name
BRAND_MAX_CAP_MM = 7.5
DESC_CAP_MM = 4.4
DETAIL_CAP_MM = 2.4
# fixed baselines (mm from the label top) so every label lines up the same
BRAND_BASE_MM = 15.0
DESC_BASE_MM = 23.0
RULE_MM = 27.0
DETAIL_BASE_MM = 32.6


def fit_brand_px(brand, size_px, margin_mm=5.0, max_cap_mm=BRAND_MAX_CAP_MM):
    """Largest brand font size that fits the width and keeps caps under max_cap_mm."""
    draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    box_w = size_px[0] - 2 * round(margin_mm * PX_PER_MM)
    px = round(max_cap_mm * PX_PER_MM * 1.6)
    while px > 8:
        font = sheet_font("brand", px)
        if (
            cap_height(font) <= max_cap_mm * PX_PER_MM
            and tracked_width(draw, brand.upper(), font, BRAND_TRACK * px) <= box_w
        ):
            return px
        px -= 1
    return px


def px_for_cap(role, cap_mm):
    """Font size in px whose capital H is cap_mm tall."""
    ref = 100
    return round(ref * cap_mm * PX_PER_MM / cap_height(sheet_font(role, ref)))


def shrink_to_width(text, role, px, max_w, track_em=0.0):
    """Shrink px until text fits max_w; warn when it had to shrink."""
    draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    start = px
    while px > 8 and tracked_width(draw, text, sheet_font(role, px), track_em * px) > max_w:
        px -= 1
    if px < start:
        print(f"note: {text!r} set smaller ({px * 100 // start}%) to fit the label", file=sys.stderr)
    return px


def render_sheet(brand, size, kind, size_px, description=None):
    """Brand in spaced caps, italic description, short rule, small 'KING · SHEET SET'.

    Sizes and positions are fixed, so labels printed on different days still match.
    """
    w, h = size_px
    mm = PX_PER_MM
    brand = brand.upper()
    detail = f"{size.upper()}  ·  {kind.upper()}"

    brand_px = shrink_to_width(brand, "brand", fit_brand_px(BRAND_REF, size_px), w - 10 * mm, BRAND_TRACK)
    desc_px = px_for_cap("description", DESC_CAP_MM)
    if description:
        desc_px = shrink_to_width(description, "description", desc_px, w - 8 * mm)
    detail_px = px_for_cap("detail", DETAIL_CAP_MM)
    fb, fm, fd = sheet_font("brand", brand_px), sheet_font("description", desc_px), sheet_font("detail", detail_px)

    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    bw = tracked_width(draw, brand, fb, BRAND_TRACK * brand_px)
    draw_tracked(draw, (w - bw) / 2, BRAND_BASE_MM * mm, brand, fb, BRAND_TRACK * brand_px)
    if description:
        dw = draw.textlength(description, font=fm)
        draw.text(((w - dw) / 2, DESC_BASE_MM * mm), description, font=fm, fill=0, anchor="ls")
    rule_w, rule_h = round(12 * mm), max(2, round(0.25 * mm))
    rule_y = round(RULE_MM * mm)
    draw.rectangle([(w - rule_w) / 2, rule_y, (w + rule_w) / 2, rule_y + rule_h - 1], fill=0)
    tw = tracked_width(draw, detail, fd, DETAIL_TRACK * detail_px)
    draw_tracked(draw, (w - tw) / 2, DETAIL_BASE_MM * mm, detail, fd, DETAIL_TRACK * detail_px)
    return img.point(lambda v: 0 if v < 128 else 255, "1")


def render_image(path, size_px=None, dither=True, invert=False):
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, "white")
        img = Image.alpha_composite(bg, img)
    img = img.convert("L")
    if invert:
        img = ImageOps.invert(img)
    if size_px:
        canvas = Image.new("L", size_px, 255)
        img = ImageOps.contain(img, size_px, Image.LANCZOS)
        canvas.paste(img, ((size_px[0] - img.width) // 2, (size_px[1] - img.height) // 2))
        img = canvas
    elif img.width > PRINTHEAD_PX:
        img = ImageOps.contain(img, (PRINTHEAD_PX, 10_000), Image.LANCZOS)
    if dither:
        return img.convert("1")
    return img.point(lambda v: 0 if v < 128 else 255, "1")


def finalize(img, rotate):
    if rotate:
        img = img.rotate(rotate, expand=True)
    if img.width > PRINTHEAD_PX:
        sys.exit(f"image is {img.width}px wide; B3S printhead is {PRINTHEAD_PX}px (72mm). Try --rotate 90.")
    return img


def find_port():
    ports = sorted(glob.glob("/dev/cu.usbmodem*")) + sorted(glob.glob("/dev/ttyACM*"))
    if not ports:
        sys.exit("no printer found (looked for /dev/cu.usbmodem*, /dev/ttyACM*). Pass --port.")
    return ports[0]


def main():
    ap = argparse.ArgumentParser(description="Print to a NIIMBOT B3S over USB")
    ap.add_argument("--port", help="serial port (auto-detected)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="show printer info")

    def common(p, default_size="15x30", default_density=3):
        p.add_argument(
            "--size",
            default=default_size,
            help=f"label size in mm: across the printhead x along the feed (default {default_size})",
        )
        p.add_argument("--copies", type=int, default=1)
        p.add_argument("--density", type=int, default=default_density, choices=range(1, 6), help=f"print darkness 1-5 (default {default_density})")
        p.add_argument("--label-type", default="gaps", choices=LABEL_TYPES)
        p.add_argument("--rotate", type=int, default=0, choices=(0, 90, 180, 270))
        p.add_argument("--preview", metavar="PNG", help="save rendered bitmap instead of printing")

    t = sub.add_parser("text", help="print text, auto-sized to fit the label")
    t.add_argument(
        "text", nargs="+", help=r"one label per argument (use \n for new lines); several labels share one text size"
    )
    t.add_argument("--font", help="path to a .ttf/.ttc/.otf font")
    t.add_argument("--font-size", type=float, metavar="PT", help="fixed text size in points instead of auto-fit")
    t.add_argument("--margin", type=float, default=2.0, help="margin in mm")
    t.add_argument("--align", default="center", choices=("left", "center", "right"))
    common(t)

    s = sub.add_parser("sheet", help="sheet-set labels: brand, rule, 'KING · SHEET SET'")
    s.add_argument(
        "labels",
        nargs="+",
        metavar="'BRAND, SIZE[, TYPE[, DESCRIPTION]]'",
        help="one label per argument, e.g. 'Parachute, King, , White Linen' or 'Frette, King, Duvet Cover, Grey'"
        " (type defaults to Sheet Set; description is an italic line under the brand)",
    )
    common(s, default_size="70x40", default_density=4)  # 4 shortens the fade-in on solid tops

    i = sub.add_parser("image", help="print an image file")
    i.add_argument("path")
    i.add_argument("--no-dither", action="store_true", help="hard threshold instead of dithering")
    i.add_argument("--invert", action="store_true")
    common(i)

    args = ap.parse_args()

    if args.cmd in ("text", "image", "sheet"):
        w, h = parse_size(args.size)
        # On labels longer than they are wide, lay content out lengthwise and turn it to feed.
        lengthwise = h > w
        if args.cmd == "image" and lengthwise:
            with Image.open(args.path) as src:
                lengthwise = src.width > src.height
        size = (h, w) if lengthwise else (w, h)
        rotate = (args.rotate + (90 if lengthwise else 0)) % 360
        if args.cmd == "text":
            if args.font_size:
                font_px = round(args.font_size * DPI / 72)
            else:
                # one shared size: the largest at which every label fits
                font_px = min(fit_font_px(s, size, args.font, args.margin, args.align) for s in args.text)
            if len(args.text) > 1:
                print(f"text size: {font_px * 72 / DPI:.0f}pt")
            imgs = [
                finalize(render_text(s, size, args.font, args.margin, args.align, font_px), rotate) for s in args.text
            ]
        elif args.cmd == "sheet":
            specs = [parse_sheet(s) for s in args.labels]
            imgs = [finalize(render_sheet(b, sz, k, size, d), rotate) for b, sz, k, d in specs]
        else:
            imgs = [finalize(render_image(args.path, size, not args.no_dither, args.invert), rotate)]
        if args.preview:
            stem, dot, ext = args.preview.rpartition(".")
            for n, img in enumerate(imgs, 1):
                path = args.preview if len(imgs) == 1 else f"{stem}-{n}{dot}{ext}"
                img.save(path)
                print(f"saved {img.width}x{img.height}px preview to {path}")
            return

    printer = Printer(args.port or find_port(), args.verbose)
    try:
        printer.connect()
        if args.cmd == "info":
            mid = printer.model_id()
            sw = printer.info(9)
            hw = printer.info(12)
            print(f"model:    {MODELS.get(mid, 'unknown')} (id {mid})")
            print(f"firmware: {sw[0]}.{sw[1]}" if len(sw) == 2 else f"firmware: {sw.hex()}")
            print(f"hardware: {hw[0]}.{hw[1]}" if len(hw) == 2 else f"hardware: {hw.hex()}")
            print(f"density:  {printer.info(1)[0]}")
            print(f"label:    {printer.info(3)[0]}")
            return
        for n, img in enumerate(imgs, 1):
            label = f" ({n}/{len(imgs)})" if len(imgs) > 1 else ""
            print(f"printing {img.width}x{img.height}px x{args.copies}{label}...")
            # One job per copy: B3S 10.x has no PrintStatus, and ending a multi-copy job via
            # PrintEnd stops it after the first label.
            for _ in range(args.copies):
                printer.print_image(img, 1, args.density, LABEL_TYPES[args.label_type])
        print("done")
    except PrinterError as e:
        try:
            printer.send(0xF3, timeout=1)
        except PrinterError:
            pass
        sys.exit(f"error: {e}")
    finally:
        printer.close()


if __name__ == "__main__":
    main()
