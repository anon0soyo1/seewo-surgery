"""Minimal resources.arsc reader - enough to list what an RRO overlay overrides."""

import struct

RES_STRING_POOL_TYPE = 0x0001
RES_TABLE_TYPE = 0x0002
RES_TABLE_PACKAGE_TYPE = 0x0200
RES_TABLE_TYPE_TYPE = 0x0201
RES_TABLE_TYPE_SPEC_TYPE = 0x0202
RES_TABLE_LIBRARY_TYPE = 0x0203

FLAG_COMPLEX = 0x0001
FLAG_SPARSE = 0x01

TYPES = {
    0x00: "null", 0x01: "ref", 0x02: "attr", 0x03: "string", 0x04: "float",
    0x05: "dimen", 0x06: "fraction", 0x10: "int", 0x11: "hex",
    0x12: "bool", 0x1C: "argb8", 0x1D: "rgb8", 0x1E: "argb4", 0x1F: "rgb4",
}


def read_string_pool(buf, off):
    _t, header_size, size = struct.unpack_from("<HHI", buf, off)
    string_count, style_count, flags, strings_start, _styles_start = \
        struct.unpack_from("<IIIII", buf, off + 8)
    utf8 = bool(flags & (1 << 8))
    if string_count == 0:
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


def config_str(buf, off):
    """Very rough ResTable_config summary: '' for default config."""
    size = struct.unpack_from("<I", buf, off)[0]
    blob = buf[off:off + size]
    if all(b == 0 for b in blob[4:]):
        return ""
    parts = []
    # locale: language[2] country[2] at offset 8
    lang = blob[8:10]
    ctry = blob[10:12]
    if lang != b"\x00\x00":
        loc = "".join(chr(c & 0x7F) for c in lang if c)
        if ctry != b"\x00\x00":
            loc += "-r" + "".join(chr(c & 0x7F) for c in ctry if c)
        parts.append(loc)
    if size >= 16:
        density = struct.unpack_from("<H", blob, 14)[0]
        if density:
            parts.append("dpi%d" % density)
    if size >= 32:
        sw = struct.unpack_from("<H", blob, 24)[0]
        if sw:
            parts.append("sw%ddp" % sw)
    return ",".join(parts) or "cfg"


def value_str(dtype, data, global_strings):
    name = TYPES.get(dtype, "t%02x" % dtype)
    if dtype == 0x03:
        s = global_strings[data] if 0 <= data < len(global_strings) else "?"
        return "string %r" % s
    if dtype == 0x12:
        return "bool %s" % ("true" if data else "false")
    if dtype == 0x10:
        return "int %d" % struct.unpack("<i", struct.pack("<I", data))[0]
    if dtype == 0x01:
        return "ref @0x%08x" % data
    if dtype == 0x02:
        return "attr @0x%08x" % data
    if dtype in (0x1C, 0x1D, 0x1E, 0x1F):
        return "color #%08x" % data
    if dtype == 0x05:
        return "dimen 0x%08x" % data
    if dtype == 0x04:
        return "float %g" % struct.unpack("<f", struct.pack("<I", data))[0]
    return "%s 0x%08x" % (name, data)


def parse(buf):
    """Return (package_name, {(type, entryname, config): valuestring})."""
    _t, header_size, _size, package_count = struct.unpack_from("<HHII", buf, 0)
    off = header_size
    global_strings = []
    results = {}
    pkgname = None
    while off + 8 <= len(buf):
        ctype, hsize, csize = struct.unpack_from("<HHI", buf, off)
        if csize == 0:
            break
        if ctype == RES_STRING_POOL_TYPE and not global_strings:
            global_strings = read_string_pool(buf, off)
        elif ctype == RES_TABLE_PACKAGE_TYPE:
            pkg_start = off
            pkg_id = struct.unpack_from("<I", buf, off + 8)[0]
            raw_name = buf[off + 12:off + 12 + 256]
            pkgname = raw_name.decode("utf-16-le", "replace").split("\x00")[0]
            type_strings_off, _lastType, key_strings_off, _lastKey = \
                struct.unpack_from("<IIII", buf, off + 268)
            type_strings = read_string_pool(buf, pkg_start + type_strings_off) \
                if type_strings_off else []
            key_strings = read_string_pool(buf, pkg_start + key_strings_off) \
                if key_strings_off else []

            # iterate inner chunks
            inner = off + hsize
            end = off + csize
            while inner + 8 <= end:
                it, ih, ic = struct.unpack_from("<HHI", buf, inner)
                if ic == 0:
                    break
                if it == RES_TABLE_TYPE_TYPE:
                    tid = buf[inner + 8]
                    tflags = buf[inner + 9]
                    entry_count, entries_start = struct.unpack_from(
                        "<II", buf, inner + 12)
                    cfg = config_str(buf, inner + 20)
                    tname = type_strings[tid - 1] if 0 < tid <= len(type_strings) \
                        else "type%d" % tid
                    idx_off = inner + ih
                    for i in range(entry_count):
                        if tflags & FLAG_SPARSE:
                            rec = struct.unpack_from("<I", buf, idx_off + i * 4)[0]
                            eidx = rec & 0xFFFF
                            eoff = (rec >> 16) * 4
                        else:
                            eidx = i
                            eoff = struct.unpack_from("<I", buf, idx_off + i * 4)[0]
                            if eoff == 0xFFFFFFFF:
                                continue
                        ep = inner + entries_start + eoff
                        if ep + 8 > len(buf):
                            continue
                        esize, eflags, ekey = struct.unpack_from("<HHI", buf, ep)
                        kname = key_strings[ekey] if 0 <= ekey < len(key_strings) \
                            else "key%d" % ekey
                        if eflags & FLAG_COMPLEX:
                            _parent, count = struct.unpack_from("<II", buf, ep + 8)
                            val = "<complex %d items>" % count
                        else:
                            vp = ep + esize
                            _vs, _r0, dtype = struct.unpack_from("<HBB", buf, vp)
                            data = struct.unpack_from("<I", buf, vp + 4)[0]
                            val = value_str(dtype, data, global_strings)
                        results[(tname, kname, cfg)] = val
                inner += ic
        off += csize
    return pkgname, results
