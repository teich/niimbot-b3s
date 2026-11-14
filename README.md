# nimbot

A small local CLI for the NIIMBOT B3S label printer over USB, so you don't need the NIIMBOT app.

The B3S shows up on macOS as a USB serial device (`/dev/cu.usbmodem…`, GigaDevice `0483:5740`). The protocol
implementation follows [niimbluelib](https://github.com/MultiMote/niimbluelib) (MIT), the most complete
open-source NIIMBOT implementation.

## Setup

```bash
git clone https://github.com/teich/niimbot-b3s && cd niimbot-b3s
uv venv && uv pip install -r requirements.txt
```

Without uv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

## Usage

```bash
.venv/bin/python nimbot.py info
.venv/bin/python nimbot.py text "Spices"                      # 15x30mm label, text auto-sized
.venv/bin/python nimbot.py text Chords Strums Blues Songs     # a matched set: one label each, same text size
.venv/bin/python nimbot.py text "Pantry\nRice" --copies 3
.venv/bin/python nimbot.py text "Big" --font-size 30          # exact size in points
.venv/bin/python nimbot.py image logo.png
.venv/bin/python nimbot.py text "Big" --size 50x30            # other label sizes
.venv/bin/python nimbot.py text "Test" --preview out.png      # render only, don't print
```

### Sheet-set labels

`sheet` prints 70x40mm linen labels: the brand in spaced serif capitals, a short rule, then `KING · SHEET SET`.
Each argument is one label, `Brand, Size[, Type[, Material]]`; the type defaults to Sheet Set, and an optional
material prints as an italic line under the brand. All labels in one run share the
brand text size, set by the longest name. It prints at density 4 by default, which shortens the faint fade-in at
the tops of large letters.

```bash
.venv/bin/python nimbot.py sheet "Frette, King" "Parachute, King" "Brooklinen, Queen" "Frette, King, Duvet Cover"
.venv/bin/python nimbot.py sheet "Parachute, King, Duvet Cover, Cotton & Linen"
.venv/bin/python nimbot.py sheet "Frette, King" --preview frette.png   # render only
```

Options: `--size WxH` in mm, where W runs across the 72mm printhead and H along the feed (default `15x30`).
On labels longer than they are wide, text and landscape images are laid out lengthwise automatically.
Several texts in one `text` command share the largest size that fits all of them, and they share a baseline.
Also `--density 1-5`, `--copies N`, `--rotate 90`, `--margin MM` (default 2),
`--label-type gaps|black|continuous|transparent`, `--font path.ttf`, `-v` for packet dumps.

If prints come out skewed or clipped at the edges, reseat the roll so it sits snugly in the guides. Alignment is
mechanical: the image itself is always square to the label.

The port is auto-detected. If you get "Resource busy", another program, usually a browser tab using
Web Serial, has the printer open. Disconnect it there first.

## Other open-source options

- [NiimBlue](https://niim.blue) ([source](https://github.com/MultiMote/niimblue)) is a full label designer that runs
  in Chrome and connects over Web Serial (USB) or Bluetooth. It supports the B3S, and you can self-host it.
- [niimprint](https://github.com/AndBondStyle/niimprint) is a Python CLI for the B1, B21, D11 and D110. It doesn't
  support the B3S.

## Printer details (B3S)

- 203 dpi, 576 px printhead (72mm), density 1–5 (default 3)
- Model IDs 256/260/262; this unit reports protocol v0 (connect result `1`)
- Uses niimbluelib's "B1" print sequence: density → label type → PrintStart(7b) → PageStart →
  SetPageSize(6b) → bitmap rows → PageEnd → poll PrintStatus → PrintEnd
- Firmware 10.29 doesn't support PrintStatus (`0xA3`), so completion is detected by polling PrintEnd until it returns 1
- Tested on macOS with 15×30mm labels

## License

MIT. The protocol implementation follows [niimbluelib](https://github.com/MultiMote/niimbluelib), which is also MIT.
