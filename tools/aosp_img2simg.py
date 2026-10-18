"""Standard AOSP-compliant Android Sparse Image (simg) generator.
Generates 100% compliant Android Sparse Image (v1.0) according to
AOSP system/core/libsparse/sparse_format.h specification.

Compatible with MediaTek SP Flash Tool, fastboot, and simg2img.
"""

import os
import struct
import sys
import time

SPARSE_HEADER_MAGIC = 0xED26FF3A
SPARSE_HEADER_MAJOR_VER = 1
SPARSE_HEADER_MINOR_VER = 0
FILE_HDR_SZ = 28
CHUNK_HDR_SZ = 12
BLOCK_SZ = 4096

CHUNK_TYPE_RAW = 0xCAC1
CHUNK_TYPE_FILL = 0xCAC2
CHUNK_TYPE_DONT_CARE = 0xCAC3
CHUNK_TYPE_CRC32 = 0xCAC4

def img2simg(raw_path, simg_path, use_dont_care_for_zero=True):
    raw_size = os.path.getsize(raw_path)
    if raw_size % BLOCK_SZ != 0:
        raise ValueError(f"File size {raw_size} is not a multiple of {BLOCK_SZ}")
    total_blks = raw_size // BLOCK_SZ

    print(f"[*] Packaging {raw_path} ({raw_size / (1024**3):.2f} GiB) -> {simg_path} (AOSP Sparse)...")
    t0 = time.time()

    zero_block = b'\x00' * BLOCK_SZ
    # Buffer in 16MB batches for high throughput
    batch_blocks = 4096
    batch_bytes = batch_blocks * BLOCK_SZ

    total_chunks = 0
    raw_chunks_count = 0
    fill_chunks_count = 0
    skip_chunks_count = 0

    cur_type = None
    cur_val = None
    cur_blks = 0
    raw_pending = []

    with open(raw_path, 'rb') as f_in, open(simg_path, 'wb') as f_out:
        # Reserve space for 28-byte sparse header
        f_out.write(b'\x00' * FILE_HDR_SZ)

        def flush():
            nonlocal cur_type, cur_val, cur_blks, raw_pending, total_chunks
            nonlocal raw_chunks_count, fill_chunks_count, skip_chunks_count
            if cur_blks == 0:
                return

            total_chunks += 1
            if cur_type == CHUNK_TYPE_DONT_CARE:
                skip_chunks_count += 1
                # 12 bytes header, 0 bytes data
                chdr = struct.pack('<2H2I', CHUNK_TYPE_DONT_CARE, 0, cur_blks, CHUNK_HDR_SZ)
                f_out.write(chdr)
            elif cur_type == CHUNK_TYPE_FILL:
                fill_chunks_count += 1
                # 12 bytes header, 4 bytes fill value
                chdr = struct.pack('<2H2I', CHUNK_TYPE_FILL, 0, cur_blks, CHUNK_HDR_SZ + 4)
                f_out.write(chdr)
                f_out.write(struct.pack('<I', cur_val))
            elif cur_type == CHUNK_TYPE_RAW:
                raw_chunks_count += 1
                total_sz = CHUNK_HDR_SZ + cur_blks * BLOCK_SZ
                chdr = struct.pack('<2H2I', CHUNK_TYPE_RAW, 0, cur_blks, total_sz)
                f_out.write(chdr)
                for blk_data in raw_pending:
                    f_out.write(blk_data)
                raw_pending.clear()

            cur_type = None
            cur_val = None
            cur_blks = 0

        blks_processed = 0
        while blks_processed < total_blks:
            buf = f_in.read(batch_bytes)
            if not buf:
                break
            n_blks = len(buf) // BLOCK_SZ
            for i in range(n_blks):
                blk = buf[i * BLOCK_SZ : (i + 1) * BLOCK_SZ]

                # Strict MTK / SP Flash Tool compatibility: ONLY RAW (0xCAC1) and DONT_CARE (0xCAC3)
                if blk == zero_block:
                    btype = CHUNK_TYPE_DONT_CARE
                else:
                    btype = CHUNK_TYPE_RAW

                # Merge with current chunk if type matches, capped at 4096 blocks (16MB)
                # to guarantee 100% buffer compatibility with MTK DA and SP Flash Tool
                if btype == cur_type and cur_blks < 4096:
                    cur_blks += 1
                    if btype == CHUNK_TYPE_RAW:
                        raw_pending.append(blk)
                else:
                    flush()
                    cur_type = btype
                    cur_blks = 1
                    if btype == CHUNK_TYPE_RAW:
                        raw_pending.append(blk)

            blks_processed += n_blks

        flush()

        # Write final standard header
        f_out.seek(0)
        hdr = struct.pack(
            '<I4H4I',
            SPARSE_HEADER_MAGIC,
            SPARSE_HEADER_MAJOR_VER,
            SPARSE_HEADER_MINOR_VER,
            FILE_HDR_SZ,
            CHUNK_HDR_SZ,
            BLOCK_SZ,
            total_blks,
            total_chunks,
            0  # checksum
        )
        f_out.write(hdr)

    out_sz = os.path.getsize(simg_path)
    elapsed = time.time() - t0
    print(f"[OK] Sparse conversion completed in {elapsed:.1f}s:")
    print(f"     Output size:  {out_sz:,} bytes ({out_sz / (1024**3):.2f} GiB)")
    print(f"     4GB limit:    4,294,967,296 bytes (Margin: {4294967296 - out_sz:,} bytes / {(4294967296 - out_sz)/(1024**2):.1f} MB under limit)")
    print(f"     Total chunks: {total_chunks} (RAW: {raw_chunks_count}, FILL: {fill_chunks_count}, DONT_CARE: {skip_chunks_count})")
    print(f"     Total blks:   {total_blks}")

if __name__ == '__main__':
    raw = sys.argv[1] if len(sys.argv) > 1 else 'signed/super.img'
    simg = sys.argv[2] if len(sys.argv) > 2 else 'signed/super.simg'
    img2simg(raw, simg)
