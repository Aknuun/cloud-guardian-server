"""Jalali/Gregorian conversion (canonical jalaali algorithm, no deps)."""
from __future__ import annotations

import datetime
import re


def _div(a: int, b: int) -> int:
    return int(a / b) if a * b >= 0 else -int(abs(a) / abs(b))


def _mod(a: int, b: int) -> int:
    return a - _div(a, b) * b


_BREAKS = [-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210,
           1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178]


def _jal_cal(jy: int):
    bl = len(_BREAKS)
    gy = jy + 621
    leap_j = -14
    jp = _BREAKS[0]
    jm = jump = leap = leap_g = march = n = 0
    if jy < jp or jy >= _BREAKS[bl - 1]:
        raise ValueError("bad jalali year")
    for i in range(1, bl):
        jm = _BREAKS[i]
        jump = jm - jp
        if jy < jm:
            break
        leap_j = leap_j + _div(jump, 33) * 8 + _div(_mod(jump, 33), 4)
        jp = jm
    n = jy - jp
    leap_j = leap_j + _div(n, 33) * 8 + _div(_mod(n, 33) + 3, 4)
    if _mod(jump, 33) == 4 and jump - n == 4:
        leap_j += 1
    leap_g = _div(gy, 4) - _div((_div(gy, 100) + 1) * 3, 4) - 150
    march = 20 + leap_j - leap_g
    if jump - n < 6:
        n = n - jump + _div(jump + 4, 33) * 33
    leap = _mod(_mod(n + 1, 33) - 1, 4)
    if leap == -1:
        leap = 4
    return leap, gy, march


def _g2d(gy: int, gm: int, gd: int) -> int:
    return datetime.date(gy, gm, gd).toordinal() + 1721425


def _d2g(jdn: int):
    g = datetime.date.fromordinal(jdn - 1721425)
    return g.year, g.month, g.day


def _j2d(jy: int, jm: int, jd: int) -> int:
    _, gy, march = _jal_cal(jy)
    return _g2d(gy, 3, march) + (jm - 1) * 31 - _div(jm, 7) * (jm - 7) + jd - 1


def _d2j(jdn: int):
    gy = _d2g(jdn)[0]
    jy = gy - 621
    jdn2 = 1 + jdn - _j2d(jy, 1, 1)
    if jdn2 < 1:
        # مرز نوروز: در سال‌هایی که ۱ فروردین ۲۱ مارس است (مثل ۱۴۰۴)،
        # روزهای آخر اسفند به سال قبل تعلق دارند.
        jy -= 1
        jdn2 = 1 + jdn - _j2d(jy, 1, 1)
    if jdn2 <= 186:
        jm = 1 + _div(jdn2 - 1, 31)
        jd = _mod(jdn2 - 1, 31) + 1
    else:
        jm = 7 + _div(jdn2 - 187, 30)
        jd = _mod(jdn2 - 187, 30) + 1
    return jy, jm, jd


def to_gregorian(jy: int, jm: int, jd: int):
    gy, gm, gd = _d2g(_j2d(jy, jm, jd))
    # round-trip guard (e.g. Esfand 30 in non-leap years)
    if _d2j(_j2d(jy, jm, jd)) != (jy, jm, jd):
        raise ValueError("bad jalali day")
    return gy, gm, gd


def to_jalali(gy: int, gm: int, gd: int):
    return _d2j(_g2d(gy, gm, gd))


def parse_jalali_datetime(s: str) -> int:
    """'1404/06/20 14:30' -> epoch ms. Raises ValueError."""
    m = re.match(r"^(\d{4})[\/\-.](\d{1,2})[\/\-.](\d{1,2})(?:[\s\-T]+(\d{1,2}):(\d{2}))$",
                 s.strip())
    if not m:
        raise ValueError("bad format")
    jy, jm, jd, hh, mm = (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                          int(m.group(4)), int(m.group(5)))
    if not (1 <= jm <= 12 and 1 <= jd <= 31 and hh <= 23 and mm <= 59):
        raise ValueError("bad range")
    gy, gm, gd = to_gregorian(jy, jm, jd)
    return int(datetime.datetime(gy, gm, gd, hh, mm).timestamp() * 1000)


def fmt_jalali(ms: int) -> str:
    g = datetime.datetime.fromtimestamp(ms / 1000)
    jy, jm, jd = to_jalali(g.year, g.month, g.day)
    return f"{jy}/{jm:02d}/{jd:02d} - {g.hour:02d}:{g.minute:02d}"
