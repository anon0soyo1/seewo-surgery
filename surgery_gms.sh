#!/usr/bin/env bash
#
# GMS Integration Surgery for seewo XPT21 (system partition)
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

mkdir -p "$WORK" "$OUT" "$MNT/system"

# ---------------------------------------------------------------------------
log "0. environment"
# ---------------------------------------------------------------------------
uname -a
$PY --version
/sbin/e2fsck -V 2>&1 | head -1 || true
sudo -n true && step "passwordless sudo: yes" || die "need passwordless sudo"

# ---------------------------------------------------------------------------
log "1. unpack system_a skeleton"
# ---------------------------------------------------------------------------
SKEL_SRC="$SKEL/system_a.skel.bimg"
[ -f "$SKEL_SRC" ] || die "missing $SKEL_SRC"
step "unpacking $(basename "$SKEL_SRC")"
$PY "$HERE/bimg.py" unpack "$SKEL_SRC" "$WORK/system_a.img"
step "keeping pristine copy for diff"
cp --reflink=auto "$WORK/system_a.img" "$WORK/system_a.pristine.img"

# ---------------------------------------------------------------------------
log "2. fsck skeleton before surgery"
# ---------------------------------------------------------------------------
sudo e2fsck -fn "$WORK/system_a.img" || die "system_a skeleton is dirty before surgery"

# ---------------------------------------------------------------------------
log "3. extract GMS payload"
# ---------------------------------------------------------------------------
GMS_ZIP="$HERE/gms.zip"
[ -f "$GMS_ZIP" ] || die "missing $GMS_ZIP"
mkdir -p "$WORK/gms_payload"
step "unzipping $GMS_ZIP ($(stat -c%s "$GMS_ZIP") bytes)"
unzip -q "$GMS_ZIP" -d "$WORK/gms_payload"
find "$WORK/gms_payload" -type f | wc -l | sed 's/^/  extracted file count: /'

# ---------------------------------------------------------------------------
log "4. mount system_a and install GMS"
# ---------------------------------------------------------------------------
sudo mount -o loop,rw "$WORK/system_a.img" "$MNT/system"

cleanup() {
  sudo umount "$MNT/system" 2>/dev/null || true
}
trap cleanup EXIT

step "pre-install free space:"
df -h "$MNT/system"

# Copy GMS payload into mounted filesystem
# The zip root contains system/... so copying into $MNT/system/ merges with /system
step "copying files into /system"
sudo cp -r "$WORK/gms_payload/system/"* "$MNT/system/system/"

# Permissions and ownership on all new files
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

# ---------------------------------------------------------------------------
log "5. SELinux contexts"
# ---------------------------------------------------------------------------
step "applying u:object_r:system_file:s0 to all installed files and directories"
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

# ---------------------------------------------------------------------------
log "6. verify labels"
# ---------------------------------------------------------------------------
sudo umount "$MNT/system"
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
check_ctx "$MNT/system/system/priv-app/PrebuiltGmsCore/PrebuiltGmsCoreQt.apk" || ctx_errs=$((ctx_errs + 1))
check_ctx "$MNT/system/system/priv-app/Phonesky/Phonesky.apk" || ctx_errs=$((ctx_errs + 1))
check_ctx "$MNT/system/system/priv-app/GoogleServicesFramework/GoogleServicesFramework.apk" || ctx_errs=$((ctx_errs + 1))
check_ctx "$MNT/system/system/framework/com.google.android.maps.jar" || ctx_errs=$((ctx_errs + 1))
check_ctx "$MNT/system/system/etc/permissions/privapp-permissions-google-ps.xml" || ctx_errs=$((ctx_errs + 1))

sudo umount "$MNT/system"

if [ "$ctx_errs" -gt 0 ]; then
  log "fallback: writing SELinux xattrs with debugfs"
  printf 'u:object_r:system_file:s0\0' > "$WORK/ctx.bin"
  while IFS= read -r rel; do
    fs_path="/system/$rel"
    sudo debugfs -w -R "ea_set -f $WORK/ctx.bin $fs_path security.selinux" "$WORK/system_a.img" || true
  done < <(cd "$WORK/gms_payload/system" && find . -mindepth 1 | sed 's|^\./||')
fi

# ---------------------------------------------------------------------------
log "7. e2fsck post-surgery"
# ---------------------------------------------------------------------------
step "e2fsck -fn system_a.img"
sudo e2fsck -fn "$WORK/system_a.img" || die "e2fsck reported errors on system_a.img"
step "e2fsck: 100% CLEAN"

# ---------------------------------------------------------------------------
log "8. generate delta"
# ---------------------------------------------------------------------------
step "computing system_a.delta.bimg"
$PY "$HERE/bimg.py" diff \
    "$WORK/system_a.pristine.img" "$WORK/system_a.img" \
    "$OUT/system_a.delta.bimg" --label "system_a.gms"

$PY "$HERE/bimg.py" info "$OUT/system_a.delta.bimg"

ls -lh "$OUT"
log "DONE! Delta ready in $OUT/system_a.delta.bimg"
