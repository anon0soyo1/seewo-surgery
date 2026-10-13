"""Block-sparse image container: carry *some* blocks of a big image, not all.

Round 2 (#5 delete 32 packages, #4 install the stub APK) needs a Linux kernel
with root and e2fsprogs, which this Windows droplet cannot provide. The plan
(PROJECT.md section 16) is to let a GitHub Actions ubuntu runner do the ext4
surgery -- but the runner must never see the 4.25 GiB of vendor firmware.

It does not need to. `rm -rf` and `cp` only touch *metadata*; the kernel never
reads the data blocks of a file it is deleting. So the traffic in both
directions is a small set of 4096-byte blocks:

    local  --> runner   the "skeleton": metadata blocks only, file data zeroed
    runner --> local    the "delta": the blocks the surgery actually changed

This module is the container for both. It is deliberately dependency-free
(stdlib only) and Python-3.8-compatible, because the exact same file is copied
to ci/bimg.py and runs on the runner.

On-disk format
--------------
128-byte plain header (little-endian), then one LZMA stream:

    off  size  field
      0     4  magic b"BIMG"
      4     4  version (1)
      8     4  block_size
     12     8  total_blocks    blocks in the *full* image, not in this file
     20     8  n_blocks        blocks actually carried here
     28     4  flags
     32    32  label           free-form ASCII, e.g. "system_a.skel"
     64    32  base_sha256     sha256 of the image this was derived from
     96    32  result_sha256   sha256 of the image you get by applying it

    LZMA stream body:
        n_blocks * u32   block numbers, strictly ascending
        n_blocks * block_size   block payloads, in that same order

Numbers and payloads are stored in separate runs rather than interleaved
because it compresses noticeably better.

The two digests are what make the pipeline auditable end to end:

    skeleton.bimg   base=sha(real image)      result=sha(skeleton image)
    delta.bimg      base=sha(skeleton image)  result=sha(post-surgery image)

so `delta.base_sha256 == skeleton.result_sha256` proves the runner started from
exactly the skeleton we shipped. tools/apply_round2.py checks that chain.

Note that a delta is applied locally to the *real* image, whose digest is
naturally different from `base_sha256` (the skeleton's). So `apply` does not
enforce base_sha256 by default; use --require-base when the digests genuinely
should match, which is the case when rolling back with a .rollback.bimg.

Usage
-----
    py tools/bimg.py info    FILE.bimg
    py tools/bimg.py pack    IMG OUT.bimg [--blocks 1,2,9-12 | --nonzero]
    py tools/bimg.py unpack  IN.bimg OUT.img
    py tools/bimg.py diff    OLD.img NEW.img OUT.bimg [--label L]
    py tools/bimg.py apply   IN.bimg TARGET.img [--no-backup] [--require-base]
    py tools/bimg.py verify  IN.bimg IMG.img [--against base|result]
"""

import argparse
import hashlib
import lzma
import os
import struct
import sys

MAGIC = b"BIMG"
VERSION = 1
HEADER_SIZE = 128
HDR_FMT = "<4sIIQQI"          # 4+4+4+8+8+4 = 32 bytes, then 3 x 32-byte fields
ZERO_SHA = b"\x00" * 32
DEFAULT_PRESET = 6
# Refuse to buffer more than this while building a container. Every real
# workflow here carries a few thousand blocks at most; blowing past this means
# something is wrong (e.g. somebody tried to pack a whole 3 GiB image).
MAX_BUFFER = 512 * 1024 * 1024
READ_CHUNK = 1024             # blocks per read when scanning an image


class BimgError(Exception):
    pass


# --------------------------------------------------------------------------
# header
# --------------------------------------------------------------------------

class Header(object):
    def __init__(self, block_size, total_blocks, n_blocks, label="",
                 flags=0, base_sha=ZERO_SHA, result_sha=ZERO_SHA):
        self.block_size = block_size
        self.total_blocks = total_blocks
        self.n_blocks = n_blocks
        self.flags = flags
        self.label = label
        self.base_sha = base_sha
        self.result_sha = result_sha

    def pack(self):
        blob = struct.pack(HDR_FMT, MAGIC, VERSION, self.block_size,
                           self.total_blocks, self.n_blocks, self.flags)
        label = self.label.encode("ascii", "replace")[:32]
        blob += label + b"\x00" * (32 - len(label))
        blob += self.base_sha + self.result_sha
        assert len(blob) == HEADER_SIZE, len(blob)
        return blob

    @classmethod
    def parse(cls, blob):
        if len(blob) < HEADER_SIZE:
            raise BimgError("short header (%d bytes)" % len(blob))
        magic, version, bs, total, n, flags = struct.unpack_from(HDR_FMT, blob, 0)
        if magic != MAGIC:
            raise BimgError("not a bimg file (magic %r)" % magic)
        if version != VERSION:
            raise BimgError("unsupported bimg version %d" % version)
        label = blob[32:64].split(b"\x00", 1)[0].decode("ascii", "replace")
        return cls(bs, total, n, label, flags, blob[64:96], blob[96:128])

    @property
    def image_size(self):
        return self.total_blocks * self.block_size


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def sha256_file(path, block_size=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(block_size)
            if not buf:
                break
            h.update(buf)
    return h.digest()


def hexs(digest):
    if digest == ZERO_SHA:
        return "(none)"
    return digest.hex()


def image_blocks(path, block_size):
    size = os.path.getsize(path)
    if size % block_size:
        raise BimgError("%s is %d bytes, not a multiple of block size %d"
                        % (path, size, block_size))
    return size // block_size


def parse_block_spec(spec):
    """'1,2,9-12' -> sorted unique [1, 2, 9, 10, 11, 12]"""
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return sorted(out)


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def write_bimg(out_path, image_path, blocks, block_size, total_blocks,
               label="", base_sha=ZERO_SHA, result_sha=ZERO_SHA,
               skip_zero=False, preset=DEFAULT_PRESET):
    """Build a container carrying `blocks` (block numbers) of `image_path`.

    skip_zero drops blocks that are all 0x00 -- unpack materialises absent
    blocks as zeros anyway, so carrying them would be pure waste.

    Returns (n_blocks_written, output_size).
    """
    blocks = sorted(set(blocks))
    if blocks and blocks[-1] >= total_blocks:
        raise BimgError("block %d is past the end of the image (%d blocks)"
                        % (blocks[-1], total_blocks))

    zero = b"\x00" * block_size
    kept = []
    payload = []
    buffered = 0
    with open(image_path, "rb") as fh:
        for bno in blocks:
            fh.seek(bno * block_size)
            buf = fh.read(block_size)
            if len(buf) < block_size:                 # tolerate a short tail
                buf = buf + b"\x00" * (block_size - len(buf))
            if skip_zero and buf == zero:
                continue
            kept.append(bno)
            payload.append(buf)
            buffered += block_size
            if buffered > MAX_BUFFER:
                raise BimgError(
                    "refusing to buffer more than %d MiB; this container would "
                    "carry too much of the image to be worth compressing"
                    % (MAX_BUFFER // 1048576))

    hdr = Header(block_size, total_blocks, len(kept), label, 0,
                 base_sha, result_sha)
    comp = lzma.LZMACompressor(preset=preset)
    with open(out_path, "wb") as out:
        out.write(hdr.pack())
        out.write(comp.compress(
            struct.pack("<%dI" % len(kept), *kept) if kept else b""))
        for buf in payload:
            out.write(comp.compress(buf))
        out.write(comp.flush())
    return len(kept), os.path.getsize(out_path)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def read_header(path):
    with open(path, "rb") as fh:
        return Header.parse(fh.read(HEADER_SIZE))


def iter_blocks(path):
    """Yield (block_number, payload) pairs. Streams; does not hold the lot."""
    with open(path, "rb") as fh:
        hdr = Header.parse(fh.read(HEADER_SIZE))
        dec = lzma.LZMADecompressor()
        pending = bytearray()

        def pull(want):
            """Decompress until `pending` holds >= want bytes."""
            while len(pending) < want:
                if dec.eof:
                    raise BimgError("bimg body ended early (wanted %d more)"
                                    % (want - len(pending)))
                raw = fh.read(1024 * 1024)
                if not raw:
                    raise BimgError("truncated bimg body")
                pending.extend(dec.decompress(raw))

        need = hdr.n_blocks * 4
        pull(need)
        numbers = struct.unpack("<%dI" % hdr.n_blocks, bytes(pending[:need]))
        del pending[:need]
        for bno in numbers:
            pull(hdr.block_size)
            yield bno, bytes(pending[:hdr.block_size])
            del pending[:hdr.block_size]


def unpack(path, out_path):
    """Materialise the full image: carried blocks written, the rest zeros."""
    hdr = read_header(path)
    with open(out_path, "wb") as out:
        out.truncate(hdr.image_size)          # sparse where the OS allows it
        n = 0
        for bno, buf in iter_blocks(path):
            out.seek(bno * hdr.block_size)
            out.write(buf)
            n += 1
    if os.path.getsize(out_path) != hdr.image_size:
        raise BimgError("unpacked size %d != expected %d"
                        % (os.path.getsize(out_path), hdr.image_size))
    return hdr, n


def diff(old_path, new_path, out_path, block_size=4096, label="",
         preset=DEFAULT_PRESET):
    """Container holding every block of NEW that differs from OLD."""
    n_old = image_blocks(old_path, block_size)
    n_new = image_blocks(new_path, block_size)
    if n_old != n_new:
        raise BimgError("size mismatch: %s has %d blocks, %s has %d"
                        % (old_path, n_old, new_path, n_new))

    changed = []
    with open(old_path, "rb") as a, open(new_path, "rb") as b:
        bno = 0
        while bno < n_new:
            want = min(READ_CHUNK, n_new - bno) * block_size
            ba = a.read(want)
            bb = b.read(want)
            if len(ba) < want or len(bb) < want:
                raise BimgError("unexpected EOF while diffing")
            if ba != bb:
                for i in range(want // block_size):
                    o = i * block_size
                    if ba[o:o + block_size] != bb[o:o + block_size]:
                        changed.append(bno + i)
            bno += want // block_size

    return write_bimg(out_path, new_path, changed, block_size, n_new,
                      label=label or (os.path.basename(new_path) + ".delta"),
                      base_sha=sha256_file(old_path),
                      result_sha=sha256_file(new_path),
                      preset=preset)


def apply(path, target_path, backup_path=None, require_base=False,
          preset=DEFAULT_PRESET):
    """Write the carried blocks into an existing image, in place.

    Guardrails, in order:
      * the target must have exactly the block count the container expects
      * with --require-base, the target's digest must equal base_sha256
      * the original content of exactly those blocks is saved to a rollback
        container before anything is written
      * only blocks listed in the container are ever written
    """
    hdr = read_header(path)
    n_target = image_blocks(target_path, hdr.block_size)
    if n_target != hdr.total_blocks:
        raise BimgError("%s has %d blocks, container expects %d -- wrong image?"
                        % (target_path, n_target, hdr.total_blocks))

    before = sha256_file(target_path)
    if require_base:
        if hdr.base_sha == ZERO_SHA:
            raise BimgError("--require-base given but container has no base digest")
        if before != hdr.base_sha:
            raise BimgError("target digest %s does not match container base %s"
                            % (hexs(before), hexs(hdr.base_sha)))

    blocks = [bno for bno, _ in iter_blocks(path)]
    if backup_path:
        write_bimg(backup_path, target_path, blocks, hdr.block_size,
                   hdr.total_blocks,
                   label=(os.path.basename(target_path) + ".rollback")[:32],
                   result_sha=before, preset=preset)

    written = 0
    allowed = set(blocks)
    with open(target_path, "r+b") as out:
        for bno, buf in iter_blocks(path):
            if bno not in allowed:                    # cannot happen; belt+braces
                raise BimgError("block %d not in the allow list" % bno)
            out.seek(bno * hdr.block_size)
            out.write(buf)
            written += 1
        out.flush()
        os.fsync(out.fileno())

    after = sha256_file(target_path)
    if backup_path:                                   # finish the rollback chain
        with open(backup_path, "r+b") as fh:
            b = Header.parse(fh.read(HEADER_SIZE))
            b.base_sha = after
            fh.seek(0)
            fh.write(b.pack())
    return hdr, written, before, after


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_info(args):
    hdr = read_header(args.file)
    print("file          %s (%d bytes)" % (args.file, os.path.getsize(args.file)))
    print("label         %s" % (hdr.label or "(none)"))
    print("block_size    %d" % hdr.block_size)
    print("total_blocks  %d  -> image is %d bytes (%.2f GiB)"
          % (hdr.total_blocks, hdr.image_size, hdr.image_size / 1073741824.0))
    print("n_blocks      %d  (%.4f%% of the image, %d bytes raw)"
          % (hdr.n_blocks,
             100.0 * hdr.n_blocks / hdr.total_blocks if hdr.total_blocks else 0.0,
             hdr.n_blocks * hdr.block_size))
    print("base_sha256   %s" % hexs(hdr.base_sha))
    print("result_sha256 %s" % hexs(hdr.result_sha))
    if args.list:
        for bno, _ in iter_blocks(args.file):
            print("  block %d" % bno)
    return 0


def cmd_pack(args):
    bs = args.block_size
    total = image_blocks(args.image, bs)
    if args.blocks:
        blocks = parse_block_spec(args.blocks)
    elif args.nonzero:
        blocks = range(total)
    else:
        raise BimgError("give --blocks or --nonzero")
    n, size = write_bimg(args.out, args.image, blocks, bs, total,
                         label=args.label or os.path.basename(args.image),
                         base_sha=sha256_file(args.image),
                         skip_zero=args.nonzero or args.skip_zero,
                         preset=args.preset)
    print("packed %d blocks of %s -> %s (%d bytes)"
          % (n, args.image, args.out, size))
    return 0


def cmd_unpack(args):
    hdr, n = unpack(args.file, args.out)
    print("wrote %s (%d bytes) from %d carried blocks"
          % (args.out, hdr.image_size, n))
    if hdr.result_sha != ZERO_SHA:
        got = sha256_file(args.out)
        if got == hdr.result_sha:
            print("digest matches result_sha256 %s" % hexs(got))
        else:
            print("DIGEST MISMATCH: got %s, header says %s"
                  % (hexs(got), hexs(hdr.result_sha)))
            return 1
    return 0


def cmd_diff(args):
    n, size = diff(args.old, args.new, args.out, args.block_size,
                   label=args.label, preset=args.preset)
    print("%d blocks differ -> %s (%d bytes)" % (n, args.out, size))
    return 0


def cmd_apply(args):
    backup = None
    if not args.no_backup:
        backup = args.backup or (args.target + ".rollback.bimg")
    hdr, n, before, after = apply(args.file, args.target, backup,
                                  args.require_base, args.preset)
    print("target   %s" % args.target)
    print("label    %s" % (hdr.label or "(none)"))
    print("before   %s" % hexs(before))
    print("wrote    %d blocks" % n)
    print("after    %s" % hexs(after))
    if backup:
        print("rollback %s   (py tools/bimg.py apply %s %s --require-base)"
              % (backup, backup, args.target))
    if hdr.result_sha != ZERO_SHA and after == hdr.result_sha:
        print("NOTE: result matches the container's result_sha256")
    return 0


def cmd_verify(args):
    hdr = read_header(args.file)
    want = hdr.base_sha if args.against == "base" else hdr.result_sha
    if want == ZERO_SHA:
        print("container carries no %s digest" % args.against)
        return 1
    got = sha256_file(args.image)
    print("%s digest %s" % (args.against, hexs(want)))
    print("image    digest %s" % hexs(got))
    if got == want:
        print("MATCH")
        return 0
    print("MISMATCH")
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="block-sparse image container")
    ap.add_argument("--preset", type=int, default=DEFAULT_PRESET,
                    help="LZMA preset 0-9 (default %d)" % DEFAULT_PRESET)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("info")
    p.add_argument("file")
    p.add_argument("--list", action="store_true", help="print every block number")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("pack")
    p.add_argument("image")
    p.add_argument("out")
    p.add_argument("--blocks", help="e.g. 1,2,9-12")
    p.add_argument("--nonzero", action="store_true",
                   help="every non-zero block in the image")
    p.add_argument("--skip-zero", action="store_true")
    p.add_argument("--block-size", type=int, default=4096)
    p.add_argument("--label", default="")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("unpack")
    p.add_argument("file")
    p.add_argument("out")
    p.set_defaults(func=cmd_unpack)

    p = sub.add_parser("diff")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("out")
    p.add_argument("--block-size", type=int, default=4096)
    p.add_argument("--label", default="")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("apply")
    p.add_argument("file")
    p.add_argument("target")
    p.add_argument("--backup")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("--require-base", action="store_true")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("verify")
    p.add_argument("file")
    p.add_argument("image")
    p.add_argument("--against", choices=("base", "result"), default="result")
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    try:
        return args.func(args)
    except BimgError as exc:
        sys.stderr.write("bimg: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
