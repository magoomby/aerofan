"""
The tray icon, drawn in Python. No image files, no Pillow.

A 16-pixel square has to say two things at a glance: this is aerofan, and this
is which profile is running. Shape carries the first - a three-blade pinwheel
is unmistakable at any size once you know it - and colour carries the second,
which is why the profiles are ordered cool to hot rather than given arbitrary
palette entries:

    auto        slate    the EC is driving; aerofan is not touching anything
    quiet       blue
    balanced    green
    aggressive  amber
    max         red
    fixed:NN    violet   a duty you asked for by hand
    offline     grey     no service to talk to

Windows takes an icon as a DIB with the AND mask stapled to the bottom, which
is why the height in the header is doubled. The mask is all zeros because the
32-bit pixels carry their own alpha; it has to be there anyway.

The pinwheel is supersampled 4x4 and the alpha comes out of the coverage, so
the curves stay smooth at 16 pixels instead of turning into a grey blob.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import struct

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.CreateIconFromResourceEx.restype = wt.HICON
user32.CreateIconFromResourceEx.argtypes = [
    ctypes.c_char_p, wt.DWORD, wt.BOOL, wt.DWORD, ctypes.c_int, ctypes.c_int,
    wt.UINT]
user32.DestroyIcon.argtypes = [wt.HICON]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]

SM_CXSMICON = 49
SM_CYSMICON = 50
LR_DEFAULTCOLOR = 0x0000

PROFILE_COLOURS = {
    "auto": (122, 132, 145),
    "quiet": (58, 132, 214),
    "balanced": (46, 164, 100),
    "aggressive": (232, 140, 40),
    "max": (216, 68, 56),
    "offline": (128, 128, 128),
    "error": (196, 64, 64),
}

FIXED_COLOUR = (150, 108, 206)
BLADE = (250, 250, 252)

# How many sub-samples per pixel per axis. 4 is plenty at 16px and costs
# nothing - the icon is redrawn only when the profile changes.
SUPERSAMPLE = 4


def colour_for(profile: str | None, online: bool = True,
               degraded: bool = False) -> tuple[int, int, int]:
    if not online:
        return PROFILE_COLOURS["offline"]
    if degraded:
        return PROFILE_COLOURS["error"]
    if profile and profile.startswith("fixed:"):
        return FIXED_COLOUR
    return PROFILE_COLOURS.get(profile or "auto", PROFILE_COLOURS["auto"])


def _pinwheel(size: int, rgb: tuple[int, int, int]) -> bytes:
    """
    BGRA pixels, bottom-up, straight (not premultiplied) alpha.

    Every sub-sample answers two questions - am I inside the disc, and am I in
    a blade - and the pixel is the average. Doing it this way rather than
    drawing shapes means there is no drawing library involved at all.
    """
    radius = size / 2.0 - 0.5
    centre = (size - 1) / 2.0
    sector = 2.0 * math.pi / 3.0
    step = 1.0 / SUPERSAMPLE
    samples = SUPERSAMPLE * SUPERSAMPLE

    rows = []
    for y in range(size - 1, -1, -1):  # bottom-up
        row = bytearray()
        for x in range(size):
            inside = 0
            blade_hits = 0
            for sy in range(SUPERSAMPLE):
                dy = (y + (sy + 0.5) * step) - centre
                for sx in range(SUPERSAMPLE):
                    dx = (x + (sx + 0.5) * step) - centre
                    distance = math.hypot(dx, dy)
                    if distance > radius:
                        continue
                    inside += 1
                    unit = distance / radius if radius else 0.0
                    # The 2.3 is the sweep: it curves the blades back as they
                    # go out, which is what reads as "fan" rather than "star".
                    angle = (math.atan2(dy, dx) + 2.3 * unit) % sector
                    # Thin blades on purpose: colour is what tells you which
                    # profile is running, so the disc has to stay mostly
                    # colour. The centre stays clear of blades and reads as
                    # the hub.
                    if unit > 0.24 and angle < sector * 0.28:
                        blade_hits += 1
            if not inside:
                row += b"\x00\x00\x00\x00"
                continue
            alpha = round(255 * inside / samples)
            mix = blade_hits / inside
            red = round(rgb[0] * (1 - mix) + BLADE[0] * mix)
            green = round(rgb[1] * (1 - mix) + BLADE[1] * mix)
            blue = round(rgb[2] * (1 - mix) + BLADE[2] * mix)
            row += bytes((blue, green, red, alpha))
        rows.append(bytes(row))
    return b"".join(rows)


def icon_dib(size: int, rgb: tuple[int, int, int]) -> bytes:
    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        size,        # biWidth
        size * 2,    # biHeight - colour rows plus the mask rows
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression = BI_RGB
        0, 0, 0, 0, 0,
    )
    mask_stride = ((size + 31) // 32) * 4  # 1bpp rows pad to 4 bytes
    mask = b"\x00" * (mask_stride * size)
    return header + _pinwheel(size, rgb) + mask


def make_icon(rgb: tuple[int, int, int], size: int | None = None) -> int:
    """An HICON the caller owns and must DestroyIcon."""
    if size is None:
        size = user32.GetSystemMetrics(SM_CXSMICON) or 16
    blob = icon_dib(size, rgb)
    handle = user32.CreateIconFromResourceEx(
        blob, len(blob), True, 0x00030000, size, size, LR_DEFAULTCOLOR)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def destroy_icon(handle: int) -> None:
    if handle:
        user32.DestroyIcon(handle)
