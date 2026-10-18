"""In-place resources.arsc value patching.

A simple (non-complex) resource entry ends with a fixed 8-byte Res_value:

    struct Res_value { uint16_t size; uint8_t res0; uint8_t dataType; uint32_t data; }

so overwriting `data` changes nothing about the file layout - no offsets move,
no chunk sizes change. That makes it a safe surgical edit, unlike string-pool
work.

Complex dimension encoding (android.util.TypedValue):

    data = (mantissa << 8) | (radix << 4) | unit
    unit: 0=PX 1=DIP 2=SP 3=PT 4=IN 5=MM
    radix: 0 = 23p0 (plain integer)

So 48dp -> (48 << 8) | (0 << 4) | 1 == 0x00003001.
"""

import re
import struct

RES_STRING_POOL_TYPE = 0x0001
RES_TABLE_PACKAGE_TYPE = 0x0200
RES_TABLE_TYPE_TYPE = 0x0201
FLAG_COMPLEX = 0x0001
FLAG_SPARSE = 0x01

UNITS = {0: "px", 1: "dp", 2: "sp", 3: "pt", 4: "in", 5: "mm"}


def dim(value, unit=1):
    """Build a complex-dimension Res_value data word (radix 0 / plain int)."""
    if not (0 <= value <= 0xFFFFFF):
        raise ValueError("mantissa out of range")
    return (value << 8) | (0 << 4) | unit


def dim_str(data):
    unit = data & 0xF
    radix = (data >> 4) & 0x3
    mant = data >> 8
    if radix != 0:
        return "0x%08x (radix %d)" % (data, radix)
    return "%d%s" % (mant, UNITS.get(unit, "?%d" % unit))


def _read_string_pool(buf, off):
    _t, header_size, _size = struct.unpack_from("<HHI", buf, off)
    string_count, _sc, flags, strings_start, _ss = struct.unpack_from(
        "<IIIII", buf, off + 8)
    utf8 = bool(flags & (1 << 8))
    if not string_count:
        return []
    offsets = struct.unpack_from("<%dI" % string_count, buf, off + header_size)
    base = off + strings_start
    out = []
    for o in offsets:
        p = base + o
        try:
            if utf8:
                n = buf[p]; p += 1
                if n & 0x80:
                    p += 1
                ln = buf[p]; p += 1
                if ln & 0x80:
                    ln = ((ln & 0x7F) << 8) | buf[p]; p += 1
                out.append(buf[p:p + ln].decode("utf-8", "replace"))
            else:
                ln = struct.unpack_from("<H", buf, p)[0]; p += 2
                if ln & 0x8000:
                    ln = ((ln & 0x7FFF) << 16) | struct.unpack_from("<H", buf, p)[0]
                    p += 2
                out.append(buf[p:p + ln * 2].decode("utf-16-le", "replace"))
        except Exception:
            out.append("<bad>")
    return out


def find_entries(buf, type_re=None, name_re=None):
    """Yield dicts describing every simple entry matching the filters.

    Keys: type, name, cfg_default, value_off, dtype, data, resid, pkg
    """
    type_rx = re.compile(type_re) if type_re else None
    name_rx = re.compile(name_re) if name_re else None

    _t, header_size, _size, _pkgs = struct.unpack_from("<HHII", buf, 0)
    off = header_size
    results = []
    while off + 8 <= len(buf):
        ctype, hsize, csize = struct.unpack_from("<HHI", buf, off)
        if csize == 0:
            break
        if ctype == RES_TABLE_PACKAGE_TYPE:
            pkg_start = off
            pkg_id = struct.unpack_from("<I", buf, off + 8)[0]
            pkgname = buf[off + 12:off + 268].decode(
                "utf-16-le", "replace").split("\x00")[0]
            ts_off, _lt, ks_off, _lk = struct.unpack_from("<IIII", buf, off + 268)
            tstr = _read_string_pool(buf, pkg_start + ts_off) if ts_off else []
            kstr = _read_string_pool(buf, pkg_start + ks_off) if ks_off else []
            inner = off + hsize
            end = off + csize
            while inner + 8 <= end:
                it, ih, ic = struct.unpack_from("<HHI", buf, inner)
                if ic == 0:
                    break
                if it == RES_TABLE_TYPE_TYPE:
                    tid = buf[inner + 8]
                    tflags = buf[inner + 9]
                    ecount, estart = struct.unpack_from("<II", buf, inner + 12)
                    cfg_size = struct.unpack_from("<I", buf, inner + 20)[0]
                    cfg_blob = buf[inner + 20:inner + 20 + cfg_size]
                    cfg_default = all(b == 0 for b in cfg_blob[4:])
                    tname = tstr[tid - 1] if 0 < tid <= len(tstr) else "t%d" % tid
                    idx_off = inner + ih
                    for i in range(ecount):
                        if tflags & FLAG_SPARSE:
                            rec = struct.unpack_from("<I", buf, idx_off + i * 4)[0]
                            eidx = rec & 0xFFFF
                            eoff = (rec >> 16) * 4
                        else:
                            eidx = i
                            eoff = struct.unpack_from("<I", buf, idx_off + i * 4)[0]
                            if eoff == 0xFFFFFFFF:
                                continue
                        ep = inner + estart + eoff
                        if ep + 8 > len(buf):
                            continue
                        esize, eflags, ekey = struct.unpack_from("<HHI", buf, ep)
                        kname = kstr[ekey] if 0 <= ekey < len(kstr) else "k%d" % ekey
                        if eflags & FLAG_COMPLEX:
                            continue
                        if type_rx and not type_rx.search(tname):
                            continue
                        if name_rx and not name_rx.search(kname):
                            continue
                        vp = ep + esize
                        _vs, _r0, dtype = struct.unpack_from("<HBB", buf, vp)
                        data = struct.unpack_from("<I", buf, vp + 4)[0]
                        results.append({
                            "type": tname, "name": kname,
                            "cfg_default": cfg_default,
                            "value_off": vp, "dtype": dtype, "data": data,
                            "resid": (pkg_id << 24) | (tid << 16) | eidx,
                            "pkg": pkgname,
                        })
                inner += ic
        off += csize
    return results


def set_value(buf, value_off, data, dtype=None):
    """Overwrite the 4-byte data word (and optionally dataType) in place."""
    out = bytearray(buf)
    if dtype is not None:
        out[value_off + 3] = dtype
    struct.pack_into("<I", out, value_off + 4, data)
    return bytes(out)
