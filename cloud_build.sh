#!/usr/bin/env bash
#
# Ubuntu Cloud Build Script for Seewo XPT21 (MT6771)
# Fully automated native Linux surgery:
# - Native mount -o loop
# - Physical rm -rf of Seewo private JARs & 32 bloatware packages
# - Safe prop & manifest patching (GmsCore, Launcher3, prop.default, RRO navbar)
# - Official /sbin/e2fsck filesystem integrity validation
# - zerofree + super.img packaging + AOSP sparse compression
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="/mnt/work"
OUT="$WORK/out"
MNT="$WORK/mnt"
PY="${PY:-python3}"

log()  { printf '\n=== %s\n' "$*"; }
step() { printf '  -> %s\n' "$*"; }
die()  { printf '\nFATAL: %s\n' "$*" >&2; exit 1; }

mkdir -p "$WORK" "$OUT" "$MNT/system" "$MNT/product" "$MNT/vendor"

log "1. Decompressing input partition images..."
cd "$WORK"
gzip -d -f system_a.img.gz product_a.img.gz vendor_a.img.gz
ls -lh system_a.img product_a.img vendor_a.img super_meta.bin

log "2. Mounting partitions via native Linux loop device..."
cleanup() {
    sudo umount "$MNT/system" 2>/dev/null || true
    sudo umount "$MNT/product" 2>/dev/null || true
    sudo umount "$MNT/vendor" 2>/dev/null || true
}
trap cleanup EXIT

sudo mount -o loop,rw "$WORK/system_a.img" "$MNT/system"
sudo mount -o loop,rw "$WORK/product_a.img" "$MNT/product"
sudo mount -o loop,rw "$WORK/vendor_a.img" "$MNT/vendor"
step "All partitions mounted successfully"
df -h "$MNT"/*

log "3. Physical removal of Seewo private JARs & intercept services..."
sudo rm -f "$MNT/system/system/framework/seewo_gesture_service.jar"
sudo rm -f "$MNT/system/system/framework/package_daemon_service.jar"
sudo rm -f "$MNT/system/system/framework/system_security_service.jar"
sudo rm -f "$MNT/system/system/framework/seewo_activity_service.jar"
step "Removed Seewo private JARs from /system/framework/"

log "4. Physical removal of 32 bloatware packages from removelist.txt..."
deleted=0
missing=0
while read -r part path; do
  case "$part" in ""|\#*) continue ;; esac
  target="$MNT/$part$path"
  if [ -e "$target" ]; then
    sz=$(sudo du -sm "$target" 2>/dev/null | cut -f1 || echo 0)
    sudo rm -rf "$target"
    step "Removed $part$path (${sz} MB)"
    deleted=$((deleted + 1))
  else
    step "Already absent or missing: $part$path"
    missing=$((missing + 1))
  fi
done < "$HERE/removelist.txt"
step "Deleted bloatware packages: $deleted removed, $missing absent"

log "5. Installing EClassSettings routing stub..."
sudo mkdir -p "$MNT/system/system/app/EClassSettings"
sudo cp "$HERE/app/EClassSettingsStub.apk" "$MNT/system/system/app/EClassSettings/EClassSettings.apk"
sudo chown 0:0 "$MNT/system/system/app/EClassSettings/EClassSettings.apk"
sudo chmod 0644 "$MNT/system/system/app/EClassSettings/EClassSettings.apk"
sudo setfattr -n security.selinux -v u:object_r:system_file:s0 "$MNT/system/system/app/EClassSettings/EClassSettings.apk" || true
step "EClassSettingsStub installed with proper permissions"

log "6. Patching /system/etc/prop.default (Disabling Seewo hooks)..."
sudo python3 - << 'EOF'
import os
prop_file = "/mnt/work/mnt/system/system/etc/prop.default"
with open(prop_file, "r", encoding="utf-8") as f:
    props = f.read()

props = props.replace("ro.config.enable_seewo_gesture=true", "ro.config.enable_seewo_gesture=false")
props = props.replace("ro.config.seewo.quickstep=true", "ro.config.seewo.quickstep=false")
props = props.replace("ro.seewo.customer.launcher=seewo", "ro.seewo.customer.launcher=")
props = props.replace("persist.seewo.config.filter.enable=true", "persist.seewo.config.filter.enable=false")
props = props.replace("ro.config.enable_package_daemon_service=true", "ro.config.enable_package_daemon_service=false")
props = props.replace("ro.config.enable_system_security=true", "ro.config.enable_system_security=false")
props = props.replace("ro.config.enable_seewo_activity=true", "ro.config.enable_seewo_activity=false")
props = props.replace("persist.sys.seewo.ui=true", "persist.sys.seewo.ui=false")

with open(prop_file, "w", encoding="utf-8") as f:
    f.write(props)
print("[OK] /system/etc/prop.default updated cleanly")
EOF

log "7. Eliminating GmsCore.apk RoleManager HOME conflict..."
sudo python3 - << 'EOF'
import sys, os, zipfile, io
sys.path.insert(0, "/mnt/work/repo/tools")
import axml, apkedit

gms_path = "/mnt/work/mnt/system/system/priv-app/GmsCore/GmsCore.apk"
with open(gms_path, "rb") as f:
    blob = f.read()

zf = zipfile.ZipFile(io.BytesIO(blob))
mf = zf.read("AndroidManifest.xml")

if (b"android.intent.category.HOME" in mf) or ("android.intent.category.HOME".encode("utf-16le") in mf):
    new_mf, cnt = axml.replace_string(mf, "android.intent.category.HOME", "android.intent.category.GMS_DUMMY")
    print(f"[OK] Replaced {cnt} category.HOME with GMS_DUMMY in GmsCore")
    temp_in = "/tmp/gms_in.apk"
    temp_out = "/tmp/gms_out.apk"
    with open(temp_in, "wb") as f: f.write(blob)
    apkedit.rewrite(temp_in, temp_out, replacements={"AndroidManifest.xml": new_mf})
    with open(temp_out, "rb") as f: patched_bytes = f.read()
    with open(gms_path, "wb") as f: f.write(patched_bytes)
    os.remove(temp_in)
    os.remove(temp_out)
    print("[OK] Patched GmsCore.apk written back")
else:
    print("[--] GmsCore already does not have category.HOME")
EOF

log "8. Patching MtkLauncher3QuickStep.apk (Restoring HOME category & invalidating vdex)..."
sudo python3 - << 'EOF'
import sys, os, zipfile, io
sys.path.insert(0, "/mnt/work/repo/tools")
import axml, apkedit

l3_path = "/mnt/work/mnt/product/priv-app/MtkLauncher3QuickStep/MtkLauncher3QuickStep.apk"
with open(l3_path, "rb") as f:
    blob = f.read()

zf = zipfile.ZipFile(io.BytesIO(blob))
mf = zf.read("AndroidManifest.xml")

if (b"android.intent.category.LAUNCHER_APP" in mf) or ("android.intent.category.LAUNCHER_APP".encode("utf-16le") in mf):
    new_mf, cnt = axml.replace_string(mf, "android.intent.category.LAUNCHER_APP", "android.intent.category.HOME")
    print(f"[OK] Replaced {cnt} category.LAUNCHER_APP with HOME in Launcher3")
    temp_in = "/tmp/l3_in.apk"
    temp_out = "/tmp/l3_out.apk"
    with open(temp_in, "wb") as f: f.write(blob)
    apkedit.rewrite(temp_in, temp_out, replacements={"AndroidManifest.xml": new_mf})
    with open(temp_out, "rb") as f: patched_bytes = f.read()
    with open(l3_path, "wb") as f: f.write(patched_bytes)
    os.remove(temp_in)
    os.remove(temp_out)
    print("[OK] Patched MtkLauncher3QuickStep.apk written back")

vdex_path = "/mnt/work/mnt/product/priv-app/MtkLauncher3QuickStep/oat/arm64/MtkLauncher3QuickStep.vdex"
if os.path.exists(vdex_path):
    with open(vdex_path, "r+b") as f:
        f.seek(0)
        f.write(b"\x00" * 16)
    print("[OK] Invalidated MtkLauncher3QuickStep.vdex (zeroed first 16 bytes)")
EOF

log "9. Patching framework-res RRO (Restoring navigation_bar_height to 48dp)..."
sudo python3 - << 'EOF'
import sys, os, zipfile, io
sys.path.insert(0, "/mnt/work/repo/tools")
import arspatch, apkedit

rro_path = "/mnt/work/mnt/product/overlay/framework-res_seewo_rro_t2.apk"
with open(rro_path, "rb") as f:
    blob = f.read()

zf = zipfile.ZipFile(io.BytesIO(blob))
arsc_bytes = zf.read("resources.arsc")

rows = arspatch.find_entries(arsc_bytes, r"^dimen$", r"^navigation_bar_height")
want_data = arspatch.dim(48, 1)
arsc_patched = arsc_bytes
for r in rows:
    if r["name"] in ["navigation_bar_height", "navigation_bar_height_landscape"]:
        arsc_patched = arspatch.set_value(arsc_patched, r["value_off"], want_data)
        print(f"[OK] Set {r['name']} to 48dp (0x{want_data:08x})")
temp_in = "/tmp/rro_in.apk"
temp_out = "/tmp/rro_out.apk"
with open(temp_in, "wb") as f: f.write(blob)
apkedit.rewrite(temp_in, temp_out, replacements={"resources.arsc": arsc_patched})
with open(temp_out, "rb") as f: patched_bytes = f.read()
with open(rro_path, "wb") as f: f.write(patched_bytes)
os.remove(temp_in)
os.remove(temp_out)
print("[OK] framework-res RRO navigation_bar_height patched to 48dp")
EOF

log "10. Unmounting cleanly..."
sudo umount "$MNT/system"
sudo umount "$MNT/product"
sudo umount "$MNT/vendor"
step "All partitions unmounted cleanly"

log "11. Official Linux /sbin/e2fsck filesystem integrity check..."
for p in system product vendor; do
    step "Running: sudo /sbin/e2fsck -f -y $WORK/${p}_a.img"
    set +e
    sudo /sbin/e2fsck -f -y "$WORK/${p}_a.img"
    rc=$?
    set -e
    # 0 = no errors, 1 = errors corrected (both are PASS for e2fsck)
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
        die "/sbin/e2fsck reported unrecoverable error on ${p}_a.img (exit code: $rc)"
    fi
    step "${p}_a.img PASSED official e2fsck (exit code $rc)"
done

log "12. Zerofree unused ext4 blocks for compact sparse packaging..."
for p in system product vendor; do
    step "Running zerofree on ${p}_a.img..."
    sudo zerofree "$WORK/${p}_a.img" || true
done

log "13. Assembling super.img.raw from verified partitions..."
python3 - << 'EOF'
import os

super_raw = "/mnt/work/super.img.raw"
total_size = 10737418240  # 10 GiB

print("[*] Creating 10 GiB raw super image...")
with open(super_raw, "wb") as f:
    f.truncate(total_size)

print("[*] Writing liblp metadata region [0:1048576)...")
with open("/mnt/work/super_meta.bin", "rb") as f_meta, open(super_raw, "r+b") as f_out:
    f_out.seek(0)
    f_out.write(f_meta.read())

def write_part(img_path, super_off):
    sz = os.path.getsize(img_path)
    print(f"[*] Writing {img_path} ({sz} bytes) -> offset {super_off}...")
    buf = bytearray(8 * 1024 * 1024)
    mv = memoryview(buf)
    with open(img_path, "rb") as src, open(super_raw, "r+b") as dst:
        dst.seek(super_off)
        while True:
            n = src.readinto(mv)
            if not n: break
            dst.write(mv[:n])

write_part("/mnt/work/product_a.img", 1048576)
write_part("/mnt/work/system_a.img", 409993216)
write_part("/mnt/work/vendor_a.img", 3651141632)
print("[OK] All partition extents mapped cleanly into super.img.raw")
EOF

log "14. Packaging super.img (AOSP Sparse Image)..."
python3 "/mnt/work/repo/tools/aosp_img2simg.py" "/mnt/work/super.img.raw" "$OUT/super.img"
simg_sz=$(stat -c%s "$OUT/super.img")
echo "Generated $OUT/super.img ($simg_sz bytes / $(echo "scale=2; $simg_sz/1073741824" | bc) GiB)"
rm -f "/mnt/work/super.img.raw"

log "15. Computing SHA256 checksums..."
cd "$OUT"
sha256sum super.img | tee sha256sums.txt

log "=== CLOUD BUILD COMPLETED SUCCESSFULLY! ==="
