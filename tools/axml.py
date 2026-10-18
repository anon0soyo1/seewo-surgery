"""Binary AndroidManifest.xml (AXML) string-pool editor.

Why this exists: seewo replaced Launcher3's `android.intent.category.HOME`
with a bogus `android.intent.category.LAUNCHER_APP`. The fix is a single string
substitution in the manifest's string pool - no structural change to the XML
tree at all. Everything outside the string pool chunk uses *indices*, never
byte offsets, so the rest of the file can be copied verbatim once the pool is
rebuilt and the two size fields are fixed up.

On-disk layout (frameworks/base/libs/androidfw/include/androidfw/ResourceTypes.h):

  ResChunk_header          type=0x0003 (RES_XML_TYPE), headerSize=8, size=<file>
    ResStringPool_header   type=0x0001, headerSize=28
        stringCount  u32
        styleCount   u32
        flags        u32   (0x100 = UTF8_FLAG, 0x001 = SORTED_FLAG)
        stringsStart u32   (from chunk start)
        stylesStart  u32   (from chunk start, 0 when styleCount == 0)
      u32 * stringCount    offsets, relative to stringsStart
      u32 * styleCount     offsets, relative to stylesStart
      <string data>
      <style data>
    RES_XML_RESOURCE_MAP_TYPE / START_ELEMENT / END_ELEMENT / ... (untouched)
"""

import struct

RES_XML_TYPE = 0x0003
RES_STRING_POOL_TYPE = 0x0001
UTF8_FLAG = 1 << 8


class AxmlError(Exception):
    pass


# --------------------------------------------------------------------------
# length prefixes
# --------------------------------------------------------------------------
def _dec_len8(buf, p):
    """UTF-8 pool length prefix: 1 or 2 bytes."""
    n = buf[p]
    p += 1
    if n & 0x80:
        n = ((n & 0x7F) << 8) | buf[p]
        p += 1
    return n, p


def _enc_len8(n):
    if n > 0x7FFF:
        raise AxmlError("utf8 length %d too large" % n)
    if n < 0x80:
        return bytes([n])
    return bytes([0x80 | (n >> 8), n & 0xFF])


def _dec_len16(buf, p):
    """UTF-16 pool length prefix: 1 or 2 u16 words."""
    n = struct.unpack_from("<H", buf, p)[0]
    p += 2
    if n & 0x8000:
        lo = struct.unpack_from("<H", buf, p)[0]
        p += 2
        n = ((n & 0x7FFF) << 16) | lo
    return n, p


def _enc_len16(n):
    if n > 0x7FFF:
        return struct.pack("<HH", 0x8000 | (n >> 16), n & 0xFFFF)
    return struct.pack("<H", n)


# --------------------------------------------------------------------------
class StringPool:
    def __init__(self, data, off):
        (self.type, self.header_size, self.size) = struct.unpack_from("<HHI", data, off)
        if self.type != RES_STRING_POOL_TYPE:
            raise AxmlError("chunk at %d is not a string pool (type 0x%x)"
                            % (off, self.type))
        (self.string_count, self.style_count, self.flags,
         self.strings_start, self.styles_start) = struct.unpack_from(
            "<IIIII", data, off + 8)
        self.utf8 = bool(self.flags & UTF8_FLAG)
        self.off = off
        self.raw = data[off:off + self.size]

        # offsets
        o = self.header_size
        self.str_offsets = list(struct.unpack_from(
            "<%dI" % self.string_count, self.raw, o)) if self.string_count else []
        o += 4 * self.string_count
        self.style_offsets = list(struct.unpack_from(
            "<%dI" % self.style_count, self.raw, o)) if self.style_count else []

        # decode strings
        self.strings = []
        base = self.strings_start
        for so in self.str_offsets:
            p = base + so
            if self.utf8:
                _u16len, p = _dec_len8(self.raw, p)
                blen, p = _dec_len8(self.raw, p)
                self.strings.append(self.raw[p:p + blen].decode("utf-8", "replace"))
            else:
                clen, p = _dec_len16(self.raw, p)
                self.strings.append(
                    self.raw[p:p + clen * 2].decode("utf-16-le", "replace"))

        # style data kept as an opaque blob (AndroidManifest.xml has none)
        if self.style_count:
            end = self.size
            self.style_blob = self.raw[self.styles_start:end]
        else:
            self.style_blob = b""

    # ----------------------------------------------------------------------
    def encode_string(self, s):
        if self.utf8:
            b = s.encode("utf-8")
            # the first prefix counts UTF-16 code units, the second counts bytes
            u16len = len(s.encode("utf-16-le")) // 2
            return _enc_len8(u16len) + _enc_len8(len(b)) + b + b"\x00"
        b = s.encode("utf-16-le")
        return _enc_len16(len(b) // 2) + b + b"\x00\x00"

    def rebuild(self, strings):
        """Serialise a fresh string pool chunk from `strings`."""
        if len(strings) != self.string_count:
            raise AxmlError("string count must stay the same (%d != %d)"
                            % (len(strings), self.string_count))

        blob = bytearray()
        offsets = []
        for s in strings:
            offsets.append(len(blob))
            blob += self.encode_string(s)
        # string data is padded to a 4-byte boundary
        while len(blob) % 4:
            blob += b"\x00"

        header_size = 28
        strings_start = header_size + 4 * len(offsets) + 4 * self.style_count
        styles_start = (strings_start + len(blob)) if self.style_count else 0

        total = strings_start + len(blob) + len(self.style_blob)
        while total % 4:
            total += 1

        out = bytearray()
        out += struct.pack("<HHI", RES_STRING_POOL_TYPE, header_size, total)
        out += struct.pack("<IIIII", len(offsets), self.style_count, self.flags,
                           strings_start, styles_start)
        for o in offsets:
            out += struct.pack("<I", o)
        for o in self.style_offsets:
            out += struct.pack("<I", o)
        out += blob
        out += self.style_blob
        while len(out) % 4:
            out += b"\x00"
        assert len(out) == total, (len(out), total)
        return bytes(out)


# --------------------------------------------------------------------------
def replace_string(data, old, new, expect_count=1):
    """Return (new_axml_bytes, n_replaced).

    Only touches the string pool; the XML tree references strings by index so
    it needs no changes.
    """
    ctype, header_size, fsize = struct.unpack_from("<HHI", data, 0)
    if ctype != RES_XML_TYPE:
        raise AxmlError("not an AXML file (type 0x%x)" % ctype)
    if fsize != len(data):
        # some builds pad; tolerate but keep the real length
        fsize = len(data)

    pool = StringPool(data, header_size)
    hits = [i for i, s in enumerate(pool.strings) if s == old]
    if expect_count is not None and len(hits) != expect_count:
        raise AxmlError("expected %d occurrence(s) of %r, found %d at %s"
                        % (expect_count, old, len(hits), hits))

    strings = list(pool.strings)
    for i in hits:
        strings[i] = new
    new_pool = pool.rebuild(strings)

    tail = data[header_size + pool.size:]
    out = bytearray()
    out += struct.pack("<HHI", RES_XML_TYPE, header_size,
                       header_size + len(new_pool) + len(tail))
    out += data[8:header_size]          # rest of the outer header, if any
    out += new_pool
    out += tail
    return bytes(out), len(hits)


def pool_info(data):
    ctype, header_size, _fsize = struct.unpack_from("<HHI", data, 0)
    if ctype != RES_XML_TYPE:
        raise AxmlError("not an AXML file")
    return StringPool(data, header_size)
