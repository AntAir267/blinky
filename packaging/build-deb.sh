#!/bin/sh
# Build blinky_<version>_all.deb from the source tree. No root needed.
set -e

here=$(cd "$(dirname "$0")" && pwd)
src=$(dirname "$here")
version=1.0
revision=1
pkg="blinky_${version}-${revision}_all"
build="$here/build/$pkg"

rm -rf "$here/build"
mkdir -p "$build/DEBIAN" \
         "$build/usr/bin" \
         "$build/usr/lib/udev/rules.d" \
         "$build/usr/share/man/man1" \
         "$build/usr/share/applications" \
         "$build/usr/share/bash-completion/completions" \
         "$build/usr/share/doc/blinky"

# Debian policy wants a concrete interpreter, not /usr/bin/env.
sed '1s|^#!/usr/bin/env python3$|#!/usr/bin/python3|' "$src/blinky.py" \
    > "$build/usr/bin/blinky"
chmod 755 "$build/usr/bin/blinky"
head -1 "$build/usr/bin/blinky" | grep -qx '#!/usr/bin/python3' \
    || { echo "shebang rewrite failed" >&2; exit 1; }

install -m 644 "$here/70-blinky-sipix.rules" "$build/usr/lib/udev/rules.d/"
install -m 644 "$here/blinky.desktop"        "$build/usr/share/applications/"
install -m 644 "$here/blinky.bash-completion" \
               "$build/usr/share/bash-completion/completions/blinky"
install -m 644 "$here/copyright"             "$build/usr/share/doc/blinky/"
gzip -9nc "$here/blinky.1"   > "$build/usr/share/man/man1/blinky.1.gz"
gzip -9nc "$here/changelog"  > "$build/usr/share/doc/blinky/changelog.Debian.gz"
gzip -9nc "$src/README.md"   > "$build/usr/share/doc/blinky/README.md.gz"
chmod 644 "$build/usr/share/man/man1/blinky.1.gz" \
          "$build/usr/share/doc/blinky/changelog.Debian.gz" \
          "$build/usr/share/doc/blinky/README.md.gz"

install -m 755 "$here/postinst" "$build/DEBIAN/postinst"
install -m 755 "$here/postrm"   "$build/DEBIAN/postrm"

size=$(du -ks "$build" | cut -f1)
cat > "$build/DEBIAN/control" <<EOF
Package: blinky
Version: ${version}-${revision}
Section: graphics
Priority: optional
Architecture: all
Depends: python3 (>= 3.8), python3-usb, python3-pil
Recommends: usbutils
Suggests: gphoto2
Installed-Size: ${size}
Maintainer: antair <antairdo@gmail.com>
Description: download and diagnose a SiPix StyleCam Blink II camera
 blinky talks directly to a SiPix StyleCam Blink II (USB 0c77:1011) over its
 vendor protocol, reproducing what libgphoto2's sipix camera library does, and
 adds the things a fragile twenty-year-old USB link needs.
 .
 Raw camera bytes are written to disk before anything tries to decode them, so
 a failed decode never costs a transfer. Each image is retried as a whole with
 backoff, and the bulk endpoint is drained between attempts so a half-finished
 transfer cannot desynchronise the next one. Stills are saved as PNG written
 directly from the decoded pixels; video clips are left as AVI.
 .
 A built-in doctor runs five ordered checks, stopping at the first failure and
 explaining it in plain language: whether the camera is on the bus, whether it
 opens without root, whether another process holds it, whether the handshake
 works, and whether the directory table is consistent. When the link is at
 fault it correlates the kernel log and surveys the machine for free USB ports
 rather than offering generic advice.
 .
 This package installs a udev rule, so the camera is usable without root.
EOF

# md5sums over everything except the control area.
( cd "$build" && find . -type f ! -path './DEBIAN/*' -printf '%P\0' \
    | sort -z | xargs -0 md5sum > DEBIAN/md5sums )
chmod 644 "$build/DEBIAN/md5sums" "$build/DEBIAN/control"

dpkg-deb --root-owner-group --build "$build" "$here/${pkg}.deb" >/dev/null
echo "$here/${pkg}.deb"
