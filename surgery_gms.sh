#!/usr/bin/env bash
#
# Full Google Suite & Navigation Surgery for seewo XPT21
# Run on GitHub Actions Ubuntu runner.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WORK:-$HERE/work}"
MNT="${MNT:-/tmp/mnt}"
OUT="${OUT:-$HERE/out}"
SKEL="$HERE/skel"
PY="${PY:-python3}"

CTX_SYSTEM_FILE_HEX="0x753a6f626a6563745f723a73797374656d5f66696c653a733000"
CTX_SYSTEM_FILE_STR="u:object_r:system_file:s0"

log()  { printf '\n=== %s\n' "$*"; }
step() { printf '  -> %s\n' "$*"; }
die()  { printf '\nFATAL: %s\n' "$*" >&2; exit 1; }

mkdir -p "$WORK" "$OUT" "$MNT/system" "$MNT/product"

cleanup() {
  sudo umount "$MNT/system" 2>/dev/null || true
  sudo umount "$MNT/product" 2>/dev/null || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
log "0. environment"
# ---------------------------------------------------------------------------
uname -a
$PY --version
/sbin/e2fsck -V 2>&1 | head -1 || true
sudo -n true && step "passwordless sudo: yes" || die "need passwordless sudo"

# ---------------------------------------------------------------------------
log "1. reassemble Google Suite payload"
# ---------------------------------------------------------------------------
GMS_ZIP="$WORK/gms.zip"
if compgen -G "$HERE/gms.zip.*" > /dev/null; then
  step "reassembling gms.zip from chunks"
  cat "$HERE"/gms.zip.* > "$GMS_ZIP"
elif [ -f "$HERE/gms.zip" ]; then
  cp "$HERE/gms.zip" "$GMS_ZIP"
else
  die "missing gms.zip or gms.zip.* in $HERE"
fi
step "payload size: $(stat -c%s "$GMS_ZIP") bytes"

mkdir -p "$WORK/gms_payload"
step "unzipping payload"
unzip -q "$GMS_ZIP" -d "$WORK/gms_payload"
find "$WORK/gms_payload" -type f | wc -l | sed 's/^/  extracted file count: /'

# ---------------------------------------------------------------------------
log "2. unpack skeletons"
# ---------------------------------------------------------------------------
for p in system product; do
  skel_file="$SKEL/${p}_a.skel.bimg"
  [ -f "$skel_file" ] || die "missing $skel_file"
  step "unpacking $(basename "$skel_file")"
  $PY "$HERE/bimg.py" unpack "$skel_file" "$WORK/${p}_a.img"
  step "keeping pristine copy for diff"
  cp --reflink=auto "$WORK/${p}_a.img" "$WORK/${p}_a.pristine.img"
  step "e2fsck pre-surgery ${p}_a"
  sudo e2fsck -fn "$WORK/${p}_a.img" || die "${p}_a skeleton is dirty before surgery"
done

# ---------------------------------------------------------------------------
log "3. perform system_a surgery"
# ---------------------------------------------------------------------------
sudo mount -o loop,rw "$WORK/system_a.img" "$MNT/system"
step "system free space before install:"
df -h "$MNT/system"

step "copying files into /system"
sudo cp -r "$WORK/gms_payload/system/"* "$MNT/system/system/"

step "setting permissions (0755 for dirs, 0644 for files, owner 0:0)"
while IFS= read -r rel; do
  target="$MNT/system/system/$rel"
  if [ -d "$target" ]; then
    sudo chown 0:0 "$target"
    sudo chmod 0755 "$target"
  elif [ -f "$target" ]; then
    sudo chown 0:0 "$target"
    sudo chmod 0644 "$target"
  fi
done < <(cd "$WORK/gms_payload/system" && find . -mindepth 1 | sed 's|^\./||')

step "applying SELinux context u:object_r:system_file:s0"
label_failures=0
while IFS= read -r rel; do
  target="$MNT/system/system/$rel"
  if ! sudo setfattr -n security.selinux -v "$CTX_SYSTEM_FILE_HEX" "$target" 2>/dev/null; then
    label_failures=$((label_failures + 1))
  fi
done < <(cd "$WORK/gms_payload/system" && find . -mindepth 1 | sed 's|^\./||')

if [ "$label_failures" -gt 0 ]; then
  step "setfattr reported $label_failures failures, will verify and fallback if needed"
fi

sudo umount "$MNT/system"

# Verify labels on remount
sudo mount -o loop,ro "$WORK/system_a.img" "$MNT/system"
check_ctx() {
  local tgt="$1" got
  got=$(sudo getfattr --absolute-names -n security.selinux --only-values "$tgt" 2>/dev/null | tr -d '\0' || true)
  if [ "$got" = "$CTX_SYSTEM_FILE_STR" ]; then
    return 0
  fi
  printf '  !! label mismatch on %s: got %q, wanted %q\n' "$tgt" "$got" "$CTX_SYSTEM_FILE_STR"
  return 1
}

ctx_errs=0
check_ctx "$MNT/system/system/priv-app/PrebuiltGmsCore" || ctx_errs=$((ctx_errs + 1))
check_ctx "$MNT/system/system/app/Chrome" || ctx_errs=$((ctx_errs + 1))
check_ctx "$MNT/system/system/app/WebViewGoogle" || ctx_errs=$((ctx_errs + 1))
check_ctx "$MNT/system/system/app/LatinIMEGooglePrebuilt" || ctx_errs=$((ctx_errs + 1))
check_ctx "$MNT/system/system/overlay/NavigationBarMode3ButtonOverlay.apk" || ctx_errs=$((ctx_errs + 1))
check_ctx "$MNT/system/system/overlay/NavigationBarModeGesturalOverlay.apk" || ctx_errs=$((ctx_errs + 1))
sudo umount "$MNT/system"

if [ "$ctx_errs" -gt 0 ]; then
  log "fallback: writing SELinux xattrs with debugfs"
  printf 'u:object_r:system_file:s0\0' > "$WORK/ctx.bin"
  while IFS= read -r rel; do
    fs_path="/system/$rel"
    sudo debugfs -w -R "ea_set -f $WORK/ctx.bin $fs_path security.selinux" "$WORK/system_a.img" || true
  done < <(cd "$WORK/gms_payload/system" && find . -mindepth 1 | sed 's|^\./||')
fi

step "e2fsck post-surgery system_a"
sudo e2fsck -fn "$WORK/system_a.img" || die "e2fsck reported errors on system_a.img"
step "system_a: 100% CLEAN"

# ---------------------------------------------------------------------------
log "4. perform product_a surgery"
# ---------------------------------------------------------------------------
sudo mount -o loop,rw "$WORK/product_a.img" "$MNT/product"
step "product free space before cleanup:"
df -h "$MNT/product"

if [ -d "$MNT/product/overlay/NavigationBarModeGesturalSW" ]; then
  step "removing broken seewo NavigationBarModeGesturalSW"
  sudo rm -rf "$MNT/product/overlay/NavigationBarModeGesturalSW"
fi

if [ -d "$MNT/product/app/LatinIME" ]; then
  step "removing broken AOSP LatinIME (superseded by Google Gboard)"
  sudo rm -rf "$MNT/product/app/LatinIME"
fi

sudo umount "$MNT/product"

step "e2fsck post-surgery product_a"
sudo e2fsck -fn "$WORK/product_a.img" || die "e2fsck reported errors on product_a.img"
step "product_a: 100% CLEAN"

# ---------------------------------------------------------------------------
log "5. generate deltas"
# ---------------------------------------------------------------------------
for p in system product; do
  step "diffing ${p}_a"
  $PY "$HERE/bimg.py" diff \
      "$WORK/${p}_a.pristine.img" "$WORK/${p}_a.img" \
      "$OUT/${p}_a.delta.bimg" --label "${p}_a.full_suite"
  $PY "$HERE/bimg.py" info "$OUT/${p}_a.delta.bimg"
done

ls -lh "$OUT"
log "ALL DONE! Deltas generated successfully in $OUT/"
