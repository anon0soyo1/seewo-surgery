"""Surgical APK (zip) editing that keeps Android happy.

Replacing one entry shifts every later offset, so the archive has to be
rewritten. Two things matter for a system APK:

  * STORED entries (resources.arsc, *.so) must stay 4-byte aligned, i.e. the
    file data offset must be a multiple of 4. We achieve that with local-header
    extra-field padding, same as zipalign.
  * Already-DEFLATEd entries are copied as raw compressed bytes so we never
    recompress and never change unrelated content.

The APK Signing Block (v2/v3) is dropped: it covers the whole archive, so it is
invalid the moment anything changes. That is fine here - Android 10 scans
system-partition APKs with skipVerify and reads certificates from the v1 (JAR)
block in META-INF, which we preserve untouched.

`pad_to` grows the finished archive to an exact byte length using the EOCD
comment field. That matters for in-place ext4 replacement: if the patched APK is
byte-for-byte the same length as the original, the file's inode needs no change
at all (same i_size, same extents), so the image edit touches only data blocks
and no filesystem metadata whatsoever.
"""

import struct
import zipfile

ALIGN = 4
EOCD_SIG = b"PK\x05\x06"
EOCD_LEN = 22
MAX_COMMENT = 0xFFFF

# Marker placed at the start of the padding so a hexdump explains itself.
# Must not contain the EOCD signature - see _pad_to_length().
PAD_MARKER = b"seewo-t2 pad: length matched to original for in-place ext4 write"


def _read_local(fp, zi):
    """Return the raw (possibly compressed) bytes of one entry."""
    fp.seek(zi.header_offset)
    sig, _ver, _flag, _meth, _t, _d, _crc, csize, _usize, nlen, elen = \
        struct.unpack("<IHHHHHIIIHH", fp.read(30))
    if sig != 0x04034B50:
        raise ValueError("bad local header at %d" % zi.header_offset)
    fp.seek(zi.header_offset + 30 + nlen + elen)
    n = csize if csize else zi.compress_size
    return fp.read(n)


def _pad_to_length(out, pad_to, verbose=True):
    """Grow `out` to exactly `pad_to` bytes using the EOCD comment field.

    `out` must already end with a complete EOCD record carrying comment_len=0.

    Android's libziparchive locates the EOCD by scanning *backwards* for its
    signature, so the padding must never contain `PK\\x05\\x06` - otherwise the
    scan would latch onto a fake record inside our own comment. We fill with
    0x00 after an ASCII marker (neither can produce the signature) and then
    assert the invariant before returning.
    """
    base = len(out)
    if pad_to is None or pad_to == base:
        return out
    if pad_to < base:
        raise ValueError(
            "cannot pad to %d: archive is already %d bytes (pad only grows)"
            % (pad_to, base))
    need = pad_to - base
    if need > MAX_COMMENT:
        raise ValueError(
            "need %d padding bytes but the zip comment field caps at %d"
            % (need, MAX_COMMENT))

    if out[base - EOCD_LEN:base - EOCD_LEN + 4] != EOCD_SIG:
        raise ValueError("no EOCD record at the end of the archive")

    comment = PAD_MARKER[:need] + b"\x00" * max(0, need - len(PAD_MARKER))
    assert len(comment) == need
    if EOCD_SIG in comment:
        raise ValueError("padding would contain an EOCD signature")

    # patch comment_len (last 2 bytes of the EOCD) then append the comment
    struct.pack_into("<H", out, base - 2, need)
    out += comment

    eocd_at = out.rfind(EOCD_SIG)
    if eocd_at != base - EOCD_LEN:
        raise ValueError(
            "EOCD signature is no longer the last one in the file "
            "(found at %d, expected %d)" % (eocd_at, base - EOCD_LEN))
    if len(out) != pad_to:
        raise ValueError("padding arithmetic is wrong: %d != %d"
                         % (len(out), pad_to))
    if verbose:
        print("  = padded   %d -> %d bytes (%d-byte EOCD comment)"
              % (base, pad_to, need))
    return out


def rewrite(src_path, dst_path, replacements=None, additions=None,
            removals=None, pad_to=None, verbose=True):
    """Rewrite an APK.

    replacements: {entry_name: uncompressed_bytes}
    additions:    {entry_name: uncompressed_bytes}
    removals:     iterable of entry names
    pad_to:       exact final byte length (via the EOCD comment), or None
    """
    replacements = replacements or {}
    additions = additions or {}
    removals = set(removals or ())

    src = zipfile.ZipFile(src_path)
    infos = src.infolist()

    out = bytearray()
    central = []

    with open(src_path, "rb") as raw:
        for zi in infos:
            if zi.filename in removals:
                if verbose:
                    print("  - removed  %s" % zi.filename)
                continue

            if zi.filename in replacements:
                data = replacements[zi.filename]
                method = zi.compress_type
                if method == zipfile.ZIP_DEFLATED:
                    import zlib
                    co = zlib.compressobj(9, zlib.DEFLATED, -15)
                    payload = co.compress(data) + co.flush()
                else:
                    payload = data
                crc = zipfile.crc32(data) & 0xFFFFFFFF
                usize, csize = len(data), len(payload)
                if verbose:
                    print("  * replaced %-40s %d -> %d bytes (%s)"
                          % (zi.filename, zi.file_size, usize,
                             "deflate" if method else "store"))
            else:
                payload = _read_local(raw, zi)
                method = zi.compress_type
                crc = zi.CRC
                usize, csize = zi.file_size, zi.compress_size

            name = zi.filename.encode("utf-8")
            # zipalign: pad the extra field so STORED data starts 4-aligned
            extra = b""
            if method == zipfile.ZIP_STORED:
                data_off = len(out) + 30 + len(name)
                pad = (ALIGN - (data_off % ALIGN)) % ALIGN
                extra = b"\x00" * pad

            local_off = len(out)
            dt = zi.date_time
            dostime = (dt[3] << 11) | (dt[4] << 5) | (dt[5] // 2)
            dosdate = ((dt[0] - 1980) << 9) | (dt[1] << 5) | dt[2]
            out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, method,
                               dostime, dosdate, crc, csize, usize,
                               len(name), len(extra))
            out += name + extra + payload
            central.append((zi, name, method, dostime, dosdate, crc,
                            csize, usize, local_off))

        for fname, data in additions.items():
            import zlib
            co = zlib.compressobj(9, zlib.DEFLATED, -15)
            payload = co.compress(data) + co.flush()
            name = fname.encode("utf-8")
            crc = zipfile.crc32(data) & 0xFFFFFFFF
            local_off = len(out)
            out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0,
                               zipfile.ZIP_DEFLATED, 0, 0x21, crc,
                               len(payload), len(data), len(name), 0)
            out += name + payload
            zi = zipfile.ZipInfo(fname)
            central.append((zi, name, zipfile.ZIP_DEFLATED, 0, 0x21, crc,
                            len(payload), len(data), local_off))
            if verbose:
                print("  + added    %s (%d bytes)" % (fname, len(data)))

    cd_off = len(out)
    for (zi, name, method, dostime, dosdate, crc, csize, usize,
         local_off) in central:
        out += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0,
                           method, dostime, dosdate, crc, csize, usize,
                           len(name), 0, 0, 0, 0,
                           zi.external_attr, local_off)
        out += name
    cd_size = len(out) - cd_off
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(central),
                       len(central), cd_size, cd_off, 0)

    out = _pad_to_length(out, pad_to, verbose=verbose)

    with open(dst_path, "wb") as fh:
        fh.write(out)
    src.close()
    if verbose:
        import os
        print("  wrote %s (%d bytes, %d entries)"
              % (dst_path, os.path.getsize(dst_path), len(central)))
    return dst_path
