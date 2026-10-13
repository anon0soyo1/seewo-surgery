# seewo-t2 round 2 CI

This directory is a **self-contained git repository root**. Push it to GitHub as
its own repo and the workflow in `.github/workflows/surgery.yml` does round 2 of
the seewo XPT21 slimming project.

It is separate from the `stub/` repo (`moewneko/seewo-stub`), which already built
and verified the stub APK. That repo is left alone; the verified binary is
committed here as `app/EClassSettingsStub.apk`.

## Why this exists

The workstation driving the project is a non-admin Windows droplet. Round 1
(#1 #2 #3 #6) was done there in pure Python, because those changes were
*content* replacements: every patched file was padded back to its original byte
length, so not one byte of ext4 metadata changed.

Round 2 is different. Deleting a directory and creating a file rewrites
directory entries, the inode and block bitmaps, group descriptors and their
crc16, parent link counts and the superblock counters. A Linux kernel already
implements that correctly and `e2fsck` can prove the result is sound. The
droplet has no Linux, no root and no e2fsprogs. This runner has all three.

## What crosses the wire

Not the firmware. The runner only receives **skeletons**: images of the same
size with byte-identical ext4 metadata and every regular file's data blocks
zeroed. `rm -rf` and `cp` never read the contents of a file, so zeroed data is
all the kernel needs.

| container | real image | shipped |
|---|---|---|
| `skel/system_a.skel.bimg` | 3,240,542,208 B | ~62 KB |
| `skel/product_a.skel.bimg` | 408,035,328 B | ~13 KB |
| `skel/vendor_a.skel.bimg` | 661,389,312 B | ~25 KB |

Roughly 43000:1. The runner sends back only the blocks its surgery changed, and
`tools/apply_round2.py` replays those onto the real images at home.

A skeleton is **not** a usable Android image: every file in it reads as zeros.
It cannot be flashed and it will not boot. It exists so a kernel can rewrite its
metadata.

Nothing device-identifying is here. SN, MAC addresses and sensor calibration
live in `proinfo` / `nvram` / `nvdata`, which are different partitions entirely
and are not part of this repo. The `analysis/` directory of the main workspace
is deliberately **not** published, because `identity.txt` and `nv.txt` do
contain those values.

## Layout

```
.
├─ .github/workflows/surgery.yml   the pipeline
├─ surgery.sh                      the 11 steps that run on the runner
├─ bimg.py                         block-sparse container (copy of tools/bimg.py)
├─ removelist.txt                  32 directories to delete, generated
├─ app/EClassSettingsStub.apk      #4, already verified (14/14 checks)
└─ skel/*.skel.bimg                the three skeletons
```

`bimg.py` is a byte-identical copy of `tools/bimg.py` from the main workspace so
that both ends of the pipeline use the same container implementation. Keep them
in sync; `tools/apply_round2.py` refuses to run if they diverge.

`removelist.txt` is generated, never hand-edited:

```
py tools/removelist.py --emit ci/removelist.txt
```

The authoritative list is the `REMOVE` table in `tools/removelist.py`, which
`py tools/removelist.py` cross-checks in both directions against
`analysis/apps.txt` (scanned from the real images): every listed directory must
exist, and nothing seewo/cvte/third-party may be missing from the list.

## Order of operations, and why it is not negotiable

`surgery.sh` deletes before it adds. `/system` has only **9.19 MiB** of free
blocks, so nothing large fits until the 32 packages are gone. That is also why a
replacement input method (Fcitx5, ~44 MiB) can only be added after the deletes.

The stub must be installed **before** EClassSettings is deleted, in the same
run. SystemUI reaches for `com.seewo.eclass.settings/SettingsActivity` from four
call sites with no `try`/`catch` -- the WiFi, Bluetooth and volume long-presses
and the settings button. Without the stub in place those become
`ActivityNotFoundException` and take SystemUI down.

## The SELinux label risk

This is the one thing most likely to go wrong, and the workflow checks it rather
than assuming.

The runner runs AppArmor. SELinux is not loaded, so there is no SELinux hook to
claim the `security.selinux` attribute name. Writing it may work (stored as an
ordinary attribute in the `security` namespace) or may be refused.

`tools/ctxprobe.py` read the real images to find out exactly what the label
looks like on disk (`analysis/ctx.txt`):

```
u:object_r:system_file:s0\0     26 bytes, trailing NUL, stored in-inode
```

The trailing NUL matters. With SELinux loaded the kernel appends it; here
nothing would, so `surgery.sh` writes the value as hex
(`0x...733000`) rather than as a `-v` string.

Then it unmounts, mounts again read-only and reads the label back. If it did not
stick, it falls back to `debugfs -w -R 'ea_set -f ctx.bin ...'`, which edits the
image offline and never involves the mount path or the LSM layer at all. An
unlabelled file under `/system` shows up as `unlabeled` on the device and PMS
will not read it, which would defeat the whole point of shipping the stub.

## Running it

Actions tab, "round2-surgery", Run workflow. It also runs on pushes that touch
the inputs.

Two artifacts come back:

- **round2-deltas** — `{system,product,vendor}_a.delta.bimg` plus the removelist
  that was actually used
- **round2-logs** — the full `surgery.log`, a post-surgery listing and `df`

Downloading artifacts needs authentication **even on a public repo**
(`GET /actions/artifacts/{id}/zip` returns 401 anonymously).
`tools/ghartifact.ps1` handles it by reusing the github.com credential git
already stored; the token stays in memory. Run status queries are anonymous, so
`tools/ghrun.ps1` can report step-level results without any of that.

Then, at home:

```
py tools/apply_round2.py
```

which verifies the digest chain (`delta.base_sha256` must equal the skeleton's
`result_sha256`, proving the runner started from exactly the skeleton shipped
here), applies the blocks to the real images, and re-verifies.

## If the run fails

The deltas are only produced at step 11, and `surgery.sh` uses `set -euo
pipefail` with explicit `die` calls, so a failed run produces no delta rather
than a bad one. Things it refuses to continue past:

- a skeleton that does not `e2fsck` clean *before* surgery (means the block
  classifier dropped something it should have kept)
- any `removelist.txt` entry not found, or a count other than 32
- `e2fsck` complaining *after* surgery
- `debugfs ea_set` failing when the mounted label route already failed
