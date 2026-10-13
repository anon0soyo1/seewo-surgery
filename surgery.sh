#!/usr/bin/env bash
#
# Round 2 ext4 surgery, run on a GitHub Actions ubuntu runner.
#
# What this does, and why it is here instead of on the workstation: deleting a
# directory from an ext4 image touches directory entries, the inode bitmap, the
# block bitmap, group descriptors (including their crc16, because gdt_csum is
# set), the parent's link count and the superblock counters. Creating a file
# does all of that in reverse, plus xattrs. Hand-writing that allocator is how
# you get an image that will not mount. A Linux kernel already implements it
# correctly, and e2fsck can prove the result is sound. This droplet has no
# Linux, no root and no e2fsprogs; the runner has all three (its sudo is
# passwordless, so mount -o loop works).
#
# The runner never sees the real 4.25 GiB of firmware. It works on "skeletons":
# same size, byte-identical ext4 metadata, every file's data blocks zeroed. See
# PROJECT.md section 16 and tools/skeleton.py. Afterwards we ship back only the
# blocks that changed, and tools/apply_round2.py replays them onto the real
# images at home.
#
# Ordering matters and is not arbitrary:
#   deletes come first, additions second. /system has only 9.19 MiB of free
#   blocks (analysis/writecheck.txt, confirmed by skelsize's FREE count), so
#   anything bigger than that does not fit until the 32 packages are gone.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WORK:-$HERE/work}"
MNT="${MNT:-/tmp/mnt}"          # not /mnt: that is the runner's own temp disk
OUT="${OUT:-$HERE/out}"
SKEL="$HERE/skel"
APPDIR="$HERE/app"
REMOVELIST="$HERE/removelist.txt"
PY="${PY:-python3}"

# label bytes verified against the real images by tools/ctxprobe.py
# (analysis/ctx.txt). The trailing 00 is part of the stored value; setfattr
# with a plain -v string would omit it, because the runner has no SELinux to
# append it for us.
CTX_SYSTEM_FILE_HEX="0x753a6f626a6563745f723a73797374656d5f66696c653a733000"
CTX_SYSTEM_FILE_STR="u:object_r:system_file:s0"

PARTS=(system product vendor)

log()  { printf '\n=== %s\n' "$*"; }
step() { printf '  -> %s\n' "$*"; }
die()  { printf '\nFATAL: %s\n' "$*" >&2; exit 1; }

mkdir -p "$WORK" "$OUT" "$MNT"

# ---------------------------------------------------------------------------
log "0. environment"
# ---------------------------------------------------------------------------
uname -a
$PY --version
/sbin/e2fsck -V 2>&1 | head -1 || true
sudo -n true && step "passwordless sudo: yes" || die "need passwordless sudo"
step "SELinux on the runner: $(command -v getenforce >/dev/null && getenforce || echo 'not present (expected)')"

# ---------------------------------------------------------------------------
log "1. rebuild the skeleton images from their containers"
# ---------------------------------------------------------------------------
# bimg unpack re-hashes the result against result_sha256 and fails if it does
# not match, so this also proves the transfer was faithful.
for p in "${PARTS[@]}"; do
  src="$SKEL/${p}_a.skel.bimg"
  [ -f "$src" ] || die "missing $src"
  step "unpacking $(basename "$src")"
  $PY "$HERE/bimg.py" unpack "$src" "$WORK/${p}_a.img"
  step "keeping a pristine copy to diff against later"
  cp --reflink=auto "$WORK/${p}_a.img" "$WORK/${p}_a.pristine.img"
done

# ---------------------------------------------------------------------------
log "2. fsck the skeletons before touching them"
# ---------------------------------------------------------------------------
# A skeleton must already be a clean filesystem. If it is not, the block
# classifier dropped something it should have kept and everything downstream is
# suspect -- stop here rather than produce a delta nobody can trust.
for p in "${PARTS[@]}"; do
  step "e2fsck -fn ${p}_a.img"
  if ! sudo e2fsck -fn "$WORK/${p}_a.img"; then
    die "skeleton ${p}_a.img is not a clean filesystem before surgery"
  fi
done

# ---------------------------------------------------------------------------
log "3. mount"
# ---------------------------------------------------------------------------
for p in "${PARTS[@]}"; do
  mkdir -p "$MNT/$p"
  sudo mount -o loop,rw "$WORK/${p}_a.img" "$MNT/$p"
  step "$(df -h --output=target,size,used,avail "$MNT/$p" | tail -1)"
done

cleanup() {
  for p in "${PARTS[@]}"; do
    sudo umount "$MNT/$p" 2>/dev/null || true
  done
}
trap cleanup EXIT

df -h "$MNT"/* || true

# ---------------------------------------------------------------------------
log "4. DELETE the 32 packages (must precede any addition)"
# ---------------------------------------------------------------------------
deleted=0
missing=0
while read -r part path; do
  case "$part" in ""|\#*) continue ;; esac
  target="$MNT/$part$path"
  if [ -d "$target" ]; then
    sz=$(sudo du -sm "$target" | cut -f1)
    sudo rm -rf "$target"
    [ -e "$target" ] && die "rm -rf left $target behind"
    step "removed $part$path (${sz} MB)"
    deleted=$((deleted + 1))
  else
    printf '  !! MISSING %s%s\n' "$part" "$path"
    missing=$((missing + 1))
  fi
done < "$REMOVELIST"

step "deleted $deleted directories, $missing missing"
[ "$missing" -eq 0 ] || die "$missing entries from removelist.txt were not found"
[ "$deleted" -eq 32 ] || die "expected to delete 32 directories, deleted $deleted"

log "free space after the deletions"
df -h "$MNT"/*

# ---------------------------------------------------------------------------
log "5. ADD the EClassSettings stub"
# ---------------------------------------------------------------------------
# Deleting EClassSettings without this in place makes SystemUI crash on the
# WiFi/Bluetooth/volume long-press and on the settings button -- those four
# call sites have no try/catch (PROJECT.md 5.6).
STUB_SRC="$APPDIR/EClassSettingsStub.apk"
[ -f "$STUB_SRC" ] || die "missing $STUB_SRC"
STUB_DIR="$MNT/system/system/app/EClassSettingsStub"
sudo mkdir -p "$STUB_DIR"
sudo cp "$STUB_SRC" "$STUB_DIR/EClassSettingsStub.apk"
sudo chown 0:0 "$STUB_DIR" "$STUB_DIR/EClassSettingsStub.apk"
sudo chmod 0755 "$STUB_DIR"
sudo chmod 0644 "$STUB_DIR/EClassSettingsStub.apk"
step "installed $(stat -c '%s bytes, mode %a, %u:%g' "$STUB_DIR/EClassSettingsStub.apk")"

# ---------------------------------------------------------------------------
log "6. SELinux labels on everything we created"
# ---------------------------------------------------------------------------
# Section 16.6 risk #1: the runner runs AppArmor, not SELinux, so the kernel
# has no SELinux hook to claim the security.selinux name. Writing it may work
# (stored as a plain attribute in the security namespace) or may be refused.
# Either way we verify by reading it back after a remount, and fall back to
# offline debugfs if the mounted route did not stick.
label_ok=1
for tgt in "$STUB_DIR" "$STUB_DIR/EClassSettingsStub.apk"; do
  if sudo setfattr -n security.selinux -v "$CTX_SYSTEM_FILE_HEX" "$tgt" 2>/dev/null; then
    step "setfattr ok on $tgt"
  else
    printf '  !! setfattr refused on %s\n' "$tgt"
    label_ok=0
  fi
done

# ---------------------------------------------------------------------------
log "7. unmount and re-mount, then read the labels back"
# ---------------------------------------------------------------------------
for p in "${PARTS[@]}"; do
  sudo umount "$MNT/$p"
done
sudo mount -o loop,ro "$WORK/system_a.img" "$MNT/system"

check_ctx() {
  local tgt="$1" got
  got=$(sudo getfattr --absolute-names -n security.selinux --only-values "$tgt" 2>/dev/null | tr -d '\0' || true)
  if [ "$got" = "$CTX_SYSTEM_FILE_STR" ]; then
    step "label verified on $tgt -> $got"
    return 0
  fi
  printf '  !! label on %s is %q, wanted %q\n' "$tgt" "$got" "$CTX_SYSTEM_FILE_STR"
  return 1
}

ctx_verified=1
check_ctx "$MNT/system/system/app/EClassSettingsStub" || ctx_verified=0
check_ctx "$MNT/system/system/app/EClassSettingsStub/EClassSettingsStub.apk" || ctx_verified=0
step "for reference, an untouched neighbour:"
sudo getfattr --absolute-names -n security.selinux \
  "$MNT/system/system/app/WallpaperCropper" 2>/dev/null || true
sudo umount "$MNT/system"

# ---------------------------------------------------------------------------
log "8. offline fallback for the labels, if the mounted route failed"
# ---------------------------------------------------------------------------
if [ "$ctx_verified" -ne 1 ] || [ "$label_ok" -ne 1 ]; then
  step "using debugfs ea_set (e2fsprogs, no mount, no SELinux involved)"
  printf 'u:object_r:system_file:s0\0' > "$WORK/ctx.bin"
  for rel in "/system/app/EClassSettingsStub" \
             "/system/app/EClassSettingsStub/EClassSettingsStub.apk"; do
    sudo debugfs -w -R "ea_set -f $WORK/ctx.bin $rel security.selinux" \
      "$WORK/system_a.img" || die "debugfs ea_set failed on $rel"
  done
  for rel in "/system/app/EClassSettingsStub" \
             "/system/app/EClassSettingsStub/EClassSettingsStub.apk"; do
    step "debugfs ea_get $rel:"
    sudo debugfs -R "ea_get $rel security.selinux" "$WORK/system_a.img" || true
  done
else
  step "labels already correct, no fallback needed"
fi

# ---------------------------------------------------------------------------
log "9. e2fsck the result"
# ---------------------------------------------------------------------------
fsck_ok=1
for p in "${PARTS[@]}"; do
  step "e2fsck -fn ${p}_a.img"
  sudo e2fsck -fn "$WORK/${p}_a.img" || { fsck_ok=0; printf '  !! e2fsck complained about %s\n' "$p"; }
done
[ "$fsck_ok" -eq 1 ] || die "e2fsck reported problems -- not shipping this delta"

# ---------------------------------------------------------------------------
log "10. inventory of what changed, for the record"
# ---------------------------------------------------------------------------
for p in "${PARTS[@]}"; do
  sudo mount -o loop,ro "$WORK/${p}_a.img" "$MNT/$p"
done
{
  echo "# post-surgery listing, generated on the runner"
  for p in "${PARTS[@]}"; do
    echo "### $p"
    sudo find "$MNT/$p" -maxdepth 3 \( -name '*.apk' -o -type d \) \
      -printf '%M %u:%g %10s %P\n' | sort -k4
  done
} > "$OUT/post_surgery.txt" 2>/dev/null || true
df -h "$MNT"/* | tee "$OUT/df_after.txt"
for p in "${PARTS[@]}"; do
  sudo umount "$MNT/$p"
done

# ---------------------------------------------------------------------------
log "11. build the block deltas to take home"
# ---------------------------------------------------------------------------
for p in "${PARTS[@]}"; do
  step "diffing ${p}_a"
  $PY "$HERE/bimg.py" diff \
      "$WORK/${p}_a.pristine.img" "$WORK/${p}_a.img" \
      "$OUT/${p}_a.delta.bimg" --label "${p}_a.delta"
  $PY "$HERE/bimg.py" info "$OUT/${p}_a.delta.bimg"
done

cp "$REMOVELIST" "$OUT/removelist.used.txt"
ls -l "$OUT"

log "DONE"
printf 'deltas are in %s -- apply them at home with:\n' "$OUT"
printf '  py tools/apply_round2.py\n'
