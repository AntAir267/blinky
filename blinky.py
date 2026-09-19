#!/usr/bin/env python3
"""blinky - standalone driver for the SiPix StyleCam Blink II (USB 0c77:1011).

Speaks the vendor protocol directly over pyusb, reproducing what
libgphoto2's camlibs/sipix/blink2.c does, with retries, raw-data
preservation, PNG output and a built-in troubleshooter.

Subcommands:
    doctor    run the diagnostic checks, stopping at the first failure
    list      list the photos on the camera
    download  download photos: raw bytes first, then decode to PNG
    decode    decode already-saved .raw files to PNG (no camera needed)
    convert   batch-convert existing .pnm files to PNG (no camera needed)

Protocol notes (blink2.c / blink2.txt):
    control read  = bmRequestType 0xC0 (vendor|device|in),  wValue 0x03
    control write = bmRequestType 0x40 (vendor|device|out), wValue 0x03
    bulk data     = first bulk IN endpoint of config 1 / interface 0 / alt 0
"""

import argparse
import datetime
import errno
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

__version__ = "1.5"

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

VENDOR_ID = 0x0C77
PRODUCT_ID = 0x1011

REQ_INIT_STILL = 0x04           # -> 1 byte   (camera_init, after firmware id)
REQ_GET_NUMPICS = 0x08          # -> 2 bytes  big-endian
REQ_GET_MEMORY = 0x0A           # <- 8 bytes  (start BE32, len BE32), then bulk
REQ_GET_DIR = 0x0D              # -> 1 byte, then bulk
REQ_DELETE_LAST = 0x11          # -> 1 byte   destroys the newest image
REQ_DELETE_ALL = 0x12           # -> 1 byte   destroys everything
REQ_GET_FIRMWARE_ID = 0x18      # -> 6 bytes

# How many bytes of an image to read when fingerprinting it. The camera
# honours a short length on GET_MEMORY, so this costs about 30ms instead of
# the seconds a whole image takes.
FINGERPRINT_BYTES = 4096

WVALUE = 0x03
WINDEX = 0x00

CTRL_IN = 0xC0                  # LIBUSB_REQUEST_TYPE_VENDOR|RECIPIENT_DEVICE|ENDPOINT_IN
CTRL_OUT = 0x40                 # LIBUSB_REQUEST_TYPE_VENDOR|RECIPIENT_DEVICE

DEFAULT_TIMEOUT_MS = 5000
DEFAULT_CHUNK = 4096
DEFAULT_RETRIES = 3
RAW_SUBDIR = "raw"

# Extensions a clip might end up with. blink2.c names anything with the
# directory's movie flag set ".avi", but that is a guess from one byte: the
# driver never inspects the data, and its own protocol notes about video are
# hedged ("I suspect", "Unclear"). So check the bytes before claiming a format.
MOVIE_EXTS = (".avi", ".bin")


# SiPix's own manual, on making AVIs from these clips: "The camera records
# approximately 3-4 frames per second. Setting the frame rate to 3 or 4 will
# make the video speed match." The Blink II spec sheet lists 5 fps for stored
# clips (15 fps is the live PC-camera mode, which is a different thing). So 5
# plays a clip at the speed it actually happened.
DEFAULT_CLIP_FPS = 5


def split_clip_frames(data):
    """A clip is Motion JPEG: whole JPEG frames laid end to end.

    Measured on this camera: each frame is a complete JFIF image, 320x120,
    carrying the same two-field interleave as a still, and frames are padded
    to an 8-byte boundary between them.
    """
    frames = []
    i = 0
    while True:
        soi = data.find(b"\xff\xd8", i)
        if soi < 0:
            break
        eoi = data.find(b"\xff\xd9", soi + 2)
        if eoi < 0:
            # A truncated trailing frame is still worth keeping.
            frames.append(data[soi:])
            break
        frames.append(data[soi:eoi + 2])
        i = eoi + 2
    return frames


def _avi_chunk(fourcc, payload):
    pad = b"\x00" if len(payload) & 1 else b""
    return fourcc + len(payload).to_bytes(4, "little") + payload + pad


def write_mjpeg_avi(path, jpeg_frames, width, height, fps=DEFAULT_CLIP_FPS):
    """Write a minimal but valid MJPEG AVI. No dependencies, no ffmpeg."""
    import struct
    if not jpeg_frames:
        raise CameraError("no frames to write")
    biggest = max(len(f) for f in jpeg_frames)
    rate = max(1, int(round(fps)))

    avih = struct.pack("<IIIIIIIIII16x",
                       int(1000000 / rate),   # microseconds per frame
                       biggest * rate,        # max bytes per second
                       0,                     # padding granularity
                       0x10,                  # AVIF_HASINDEX
                       len(jpeg_frames),      # total frames
                       0,                     # initial frames
                       1,                     # streams
                       biggest,               # suggested buffer
                       width, height)
    # fccType, fccHandler, dwFlags, wPriority, wLanguage, dwInitialFrames,
    # dwScale, dwRate, dwStart, dwLength, dwSuggestedBufferSize, dwQuality,
    # dwSampleSize, rcFrame -- 56 bytes.
    strh = struct.pack("<4s4sIHHIIIIIIiIhhhh",
                       b"vids", b"MJPG", 0, 0, 0, 0,
                       1, rate,               # scale, rate -> fps
                       0, len(jpeg_frames),
                       biggest, -1, 0,
                       0, 0, width, height)
    strf = struct.pack("<IiiHH4sIiiII", 40, width, height, 1, 24,
                       b"MJPG", width * height * 3, 0, 0, 0, 0)
    hdrl = _avi_chunk(b"LIST", b"hdrl" + _avi_chunk(b"avih", avih)
                      + _avi_chunk(b"LIST", b"strl" + _avi_chunk(b"strh", strh)
                                   + _avi_chunk(b"strf", strf)))

    movi_body = bytearray(b"movi")
    index = bytearray()
    for frame in jpeg_frames:
        # Index offsets are measured from the start of the 'movi' payload.
        index += (b"00dc" + (0x10).to_bytes(4, "little")
                  + len(movi_body).to_bytes(4, "little")
                  + len(frame).to_bytes(4, "little"))
        movi_body += _avi_chunk(b"00dc", frame)
    movi = _avi_chunk(b"LIST", bytes(movi_body))
    idx1 = _avi_chunk(b"idx1", bytes(index))

    body = b"AVI " + hdrl + movi + idx1
    write_file_atomically(path, b"RIFF"
                          + len(body).to_bytes(4, "little") + body)
    return len(jpeg_frames)


def decode_clip(data, log, quality=95):
    """Clip bytes -> (width, height, de-interleaved JPEG frames, partials).

    Each frame goes through the same de-interleave as a still, then back to
    JPEG for the container. That is a re-encode of already-lossy data, which
    is why the camera's untouched bytes are kept alongside.
    """
    import io
    Image = _import_pil()
    raw_frames = split_clip_frames(data)
    if not raw_frames:
        raise CameraError("no JPEG frames found in the clip data")
    out, partials, size = [], 0, None
    for n, frame in enumerate(raw_frames):
        try:
            width, height, raster, partial = decode_still(frame, log)
        except CameraError as exc:
            log.warn("clip frame %d could not be decoded (%s); skipping it"
                     % (n, exc))
            continue
        partials += 1 if partial else 0
        if size is None:
            size = (width, height)
        elif (width, height) != size:
            log.warn("clip frame %d is %dx%d, not %dx%d; skipping it"
                     % (n, width, height, size[0], size[1]))
            continue
        buf = io.BytesIO()
        Image.frombytes("RGB", (width, height), raster).save(
            buf, "JPEG", quality=quality, subsampling=0)
        out.append(buf.getvalue())
    if not out:
        raise CameraError("none of the clip's %d frame(s) could be decoded"
                          % len(raw_frames))
    return size[0], size[1], out, partials


def build_clip(data, avi_path, log, fps=DEFAULT_CLIP_FPS, quality=95):
    """Turn a clip's raw bytes into a playable AVI. Returns (frames, w, h)."""
    _, is_avi = movie_extension(data)
    if is_avi:
        # Already a container: pass it through untouched rather than
        # re-encoding something that needs no work.
        log.info("    the camera supplied a complete AVI; copying it as-is")
        write_file_atomically(avi_path, data)
        return 0, 0, 0
    width, height, frames, partials = decode_clip(data, log, quality)
    if partials:
        log.warn("%d clip frame(s) were damaged and decoded only partially"
                 % partials)
    write_mjpeg_avi(avi_path, frames, width, height, fps)
    return len(frames), width, height


def movie_extension(data):
    """(extension, recognised) for a clip's raw bytes.

    An AVI file begins 'RIFF....AVI '. If that is what the camera stored,
    say so; if not, do not put a container's extension on bytes that are not
    in that container.
    """
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return ".avi", True
    return ".bin", False


def _pictures_dir():
    """The user's Pictures folder, honouring XDG (and its localised names)."""
    env = os.environ.get("XDG_PICTURES_DIR")
    if env:
        return os.path.expandvars(os.path.expanduser(env))
    config = os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "user-dirs.dirs")
    try:
        with open(config) as fh:
            for line in fh:
                if line.startswith("XDG_PICTURES_DIR"):
                    value = line.split("=", 1)[1].strip().strip('"')
                    value = value.replace("$HOME", os.path.expanduser("~"))
                    if value:
                        return os.path.expanduser(value)
    except OSError:
        pass
    return os.path.expanduser("~/Pictures")


DEFAULT_OUTDIR = os.path.join(_pictures_dir(), "blink-pics")


def raw_dir(outdir, use_subfolder=True):
    """Where the camera's untouched bytes go."""
    return os.path.join(outdir, RAW_SUBDIR) if use_subfolder else outdir


def scan_dirs(outdir):
    """The folder and its raw/ subfolder: both hold files we may already have."""
    dirs = [outdir]
    sub = os.path.join(outdir, RAW_SUBDIR)
    if os.path.isdir(sub):
        dirs.append(sub)
    return dirs
# Debian convention: package-shipped udev rules live under /usr/lib, and
# /etc/udev/rules.d is reserved for local overrides. Look in both, because the
# rule may have come from the blinky package or been written by hand.
# Where updates come from. Pinned, so nothing in a downloaded file can
# redirect a later check somewhere else.
GITHUB_REPO = "AntAir267/blinky"
RELEASES_API = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO
RELEASES_PAGE = "https://github.com/%s/releases" % GITHUB_REPO

UDEV_RULE_PATHS = (
    "/usr/lib/udev/rules.d/70-blinky-sipix.rules",
    "/lib/udev/rules.d/70-blinky-sipix.rules",
    "/etc/udev/rules.d/70-sipix-blink2.rules",
    "/etc/udev/rules.d/70-blinky-sipix.rules",
)
UDEV_RULE_PATH = UDEV_RULE_PATHS[2]      # where to tell people to write one
UDEV_RULE_TEXT = (
    'SUBSYSTEM=="usb", ATTR{idVendor}=="0c77", ATTR{idProduct}=="1011", TAG+="uaccess"'
)

# Kernel-log patterns that mean "the physical link is the problem, not the software".
KERNEL_TROUBLE_PATTERNS = [
    (re.compile(r"error -71|-71\b"),
     "-71 (EPROTO) is a protocol error on the wire: the host and the device "
     "could not agree on a transfer. It is almost always electrical."),
    (re.compile(r"disabled by hub \(EMI\?\)", re.I),
     "The hub itself shut the port down after repeated errors. The kernel is "
     "guessing at electrical interference."),
    (re.compile(r"device descriptor read|unable to enumerate", re.I),
     "The device could not even be enumerated, so no driver ever got a chance "
     "to talk to it."),
    (re.compile(r"not accepting address", re.I),
     "The device stopped answering during address assignment, which points at "
     "power or signal integrity."),
    (re.compile(r"over-?current", re.I),
     "The port cut power because it drew too much current."),
]

SYSFS_USB = "/sys/bus/usb/devices"

# "usb 3-2: ..." or "usb usb3-port2: ..." -> the bus the failure happened on.
KERNEL_BUS_RE = re.compile(r"usb (?:usb)?(\d+)[-\s]")


def _read_sysfs(path, default=""):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def usb_port_survey():
    """Free ports on every hub and root hub, so advice can name real options.

    "Try another port" is useless on a machine that has none. This finds the
    ports that are actually empty and flags self-powered hubs, which supply
    more current than a root port and sit behind a different host controller.
    """
    hubs = []
    try:
        names = sorted(os.listdir(SYSFS_USB))
    except OSError:
        return hubs
    for name in names:
        if ":" in name:                       # an interface, not a device
            continue
        base = os.path.join(SYSFS_USB, name)
        maxchild = _read_sysfs(os.path.join(base, "maxchild"))
        if not maxchild.isdigit() or int(maxchild) == 0:
            continue
        total = int(maxchild)
        is_root = name.startswith("usb")
        free = []
        for port in range(1, total + 1):
            child = ("%s-%d" % (name[3:], port) if is_root
                     else "%s.%d" % (name, port))
            if not os.path.exists(os.path.join(SYSFS_USB, child, "idVendor")):
                free.append(port)
        try:
            attrs = int(_read_sysfs(os.path.join(base, "bmAttributes"), "0"), 16)
        except ValueError:
            attrs = 0
        try:
            speed = float(_read_sysfs(os.path.join(base, "speed"), "0"))
        except ValueError:
            speed = 0.0
        hubs.append({
            "name": name,
            "bus": _read_sysfs(os.path.join(base, "busnum"), "?"),
            "product": _read_sysfs(os.path.join(base, "product"),
                                   "root hub" if is_root else "hub").strip(),
            "root": is_root,
            # Root hubs always report self-powered, so the flag only tells you
            # something useful about an external hub.
            "self_powered": bool(attrs & 0x40) and not is_root,
            "speed": speed,
            "free": free,
            "total": total,
        })
    return hubs


def bus_from_kernel_hits(hits):
    """The bus number the most recent logged USB fault happened on."""
    for line, _ in reversed(hits):
        m = KERNEL_BUS_RE.search(line)
        if m:
            return m.group(1)
    return None


def link_advice(failed_bus=None):
    """Advice for a link-layer fault, naming ports that actually exist here."""
    lines = [
        "These are link-layer faults, not software faults, but two of the "
        "remedies are free and need no different hardware.",
        "",
        "1. Move the camera to a port on a different host controller. A "
        "self-powered hub beats a root port: it supplies more current and "
        "isolates the device from the controller that is failing.",
    ]
    # This camera is a full-speed (12 Mbps) device, so a SuperSpeed root hub
    # is not somewhere it can ever attach; listing those would be noise.
    candidates = [h for h in usb_port_survey()
                  if h["free"] and not (h["root"] and h["speed"] >= 5000)]
    # External hubs first: they are the genuinely different electrical path.
    candidates.sort(key=lambda h: (h["bus"] == str(failed_bus)
                                   if failed_bus is not None else False,
                                   h["root"], not h["self_powered"]))
    if candidates:
        lines.append("")
        lines.append("   Free ports on this machine right now:")
        for h in candidates:
            tag = []
            if h["self_powered"]:
                tag.append("self-powered")
            if failed_bus is not None and h["bus"] == str(failed_bus):
                tag.append("same bus as the failure, so least likely to help")
            lines.append("     bus %s  %s  %s: port(s) %s%s"
                         % (h["bus"], h["name"], h["product"] or "hub",
                            ", ".join(str(p) for p in h["free"]),
                            ("  [%s]" % "; ".join(tag)) if tag else ""))
        lines.append("   Some of those may be internal headers rather than "
                     "sockets you can reach; sysfs cannot tell the "
                     "difference.")
    else:
        lines.append("")
        lines.append("   (no free ports found, so this one does need a hub)")

    lines += [
        "",
        "2. Old cameras often cannot survive the kernel's modern enumeration "
        "sequence, which reads the device descriptor before assigning an "
        "address; 'device descriptor read/all, error -71' is that failure. "
        "Make the kernel try the old sequence first, then replug:",
        "     echo 1 | sudo tee /sys/module/usbcore/parameters/old_scheme_first",
        "",
        "3. Give the device longer to answer, then replug:",
        "     echo '%04x:%04x:gn' | sudo tee /sys/module/usbcore/parameters/quirks"
        % (VENDOR_ID, PRODUCT_ID),
        "   (g is DELAY_INIT, n is DELAY_CTRL_MSG.)",
        "",
        "   Both knobs reset at reboot. If they help, make them permanent by "
        "adding usbcore.old_scheme_first=1 to the kernel command line in "
        "/etc/default/grub.",
        "",
        "4. Only if none of that works is it the cable or the camera itself. A "
        "shorter, shielded cable routed away from monitors, speakers and power "
        "bricks is the next thing to change.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class Log:
    """Prints to stderr and keeps a timestamped transcript for --report."""

    def __init__(self, verbose=False, quiet=False):
        self.verbose = verbose
        self.quiet = quiet
        self.lines = []
        self.start_wall = datetime.datetime.now()
        self.start_mono = time.monotonic()

    def _record(self, level, msg):
        stamp = "%8.3f" % (time.monotonic() - self.start_mono)
        for line in str(msg).splitlines() or [""]:
            self.lines.append("[%s] %-5s %s" % (stamp, level, line))

    def _emit(self, msg, stream=sys.stderr):
        if not self.quiet:
            print(msg, file=stream)

    def debug(self, msg):
        self._record("DEBUG", msg)
        if self.verbose:
            self._emit(msg)

    def info(self, msg):
        self._record("INFO", msg)
        self._emit(msg)

    def warn(self, msg):
        self._record("WARN", msg)
        self._emit("warning: %s" % msg)

    def error(self, msg):
        self._record("ERROR", msg)
        print("error: %s" % msg, file=sys.stderr)

    def out(self, msg=""):
        """User-facing result text: always goes to stdout, always recorded."""
        self._record("OUT", msg)
        # Flushed so stdout stays in step with the progress on stderr when
        # both are redirected to the same place.
        print(msg, flush=True)

    def transcript(self):
        return "\n".join(self.lines)


# ---------------------------------------------------------------------------
# Kernel log access
# ---------------------------------------------------------------------------

def _run(cmd, timeout=20):
    """Run a command, returning (ok, combined_output)."""
    if shutil.which(cmd[0]) is None:
        return False, "%s is not installed" % cmd[0]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "%s timed out after %ds" % (cmd[0], timeout)
    except OSError as exc:
        return False, "could not run %s: %s" % (cmd[0], exc)
    text = p.stdout.decode("utf-8", "replace")
    return p.returncode == 0, text


def kernel_log(since=None, lines=400):
    """Fetch recent kernel messages.  Returns (ok, text_or_reason)."""
    cmd = ["journalctl", "-k", "--no-pager"]
    if since is not None:
        cmd += ["--since", since]
    else:
        cmd += ["-n", str(lines)]
    ok, text = _run(cmd)
    if not ok:
        lines = text.strip().splitlines()
        why = lines[-1].strip() if lines else "journalctl produced no output"
        return False, ("kernel log is not readable (%s). Continuing without it; "
                       "add yourself to the 'systemd-journal' group or use sudo "
                       "to include it." % why)
    return True, text


def scan_kernel_trouble(text):
    """Return [(line, explanation)] for USB link faults found in `text`."""
    hits = []
    seen = set()
    for line in text.splitlines():
        if "usb" not in line.lower() and "-71" not in line:
            continue
        for pattern, explanation in KERNEL_TROUBLE_PATTERNS:
            if pattern.search(line):
                key = (pattern.pattern, line.strip()[-120:])
                if key not in seen:
                    seen.add(key)
                    hits.append((line.strip(), explanation))
                break
    return hits


def report_kernel_trouble(log, since=None):
    """Look at journalctl -k and print anything that explains a failed transfer."""
    ok, text = kernel_log(since=since)
    if not ok:
        log.warn(text)
        return []
    hits = scan_kernel_trouble(text)
    if not hits:
        log.info("  kernel log: no USB link errors recorded in that window.")
        return []
    log.warn("kernel log shows USB link errors:")
    for line, explanation in hits[-12:]:
        log.info("    %s" % line)
    explained = []
    for _, explanation in hits:
        if explanation not in explained:
            explained.append(explanation)
    for explanation in explained:
        log.info("  -> %s" % explanation)
    for line in _wrap(link_advice(bus_from_kernel_hits(hits))):
        log.info("  %s" % line)
    return hits


# ---------------------------------------------------------------------------
# USB layer
# ---------------------------------------------------------------------------

class CameraError(Exception):
    pass


class DeviceBusy(CameraError):
    pass


class PermissionDenied(CameraError):
    pass


class NotPresent(CameraError):
    pass


class DeviceStalled(CameraError):
    pass


STALL_ADVICE = (
    "Power-cycle the camera: unplug it (or take the batteries out) for a few "
    "seconds, then plug it back in. Clearing the USB halt, re-setting the "
    "configuration and resetting the device have all been tried against this "
    "camera in this state and none of them revive it -- only removing power "
    "does."
)


def _import_usb():
    try:
        import usb.core
        import usb.util
    except ImportError:
        raise CameraError(
            "pyusb is not available to this interpreter.\n"
            "  Fix: run the tool through the project venv, e.g.\n"
            "       ./blinky <command>\n"
            "  or install it system-wide with: sudo apt install python3-usb")
    return usb


class DirEntry:
    """One row of the camera's directory table."""

    def __init__(self, index, start, end, type_byte):
        self.index = index
        self.start = start
        self.end = end
        self.type_byte = type_byte

    @property
    def is_movie(self):
        return self.type_byte != 0

    @property
    def length_words(self):
        """The 'len' the camera wants in the GET_MEMORY request."""
        return (self.end - self.start) // 4

    @property
    def data_bytes(self):
        """How many bytes the bulk endpoint will hand back."""
        return self.length_words * 8

    @property
    def basename(self):
        return "image%04d" % self.index

    def describe(self):
        return "%s  %-5s  start=0x%06x end=0x%06x  %7d bytes" % (
            self.basename, "AVI" if self.is_movie else "still",
            self.start, self.end, self.data_bytes)


class Blink2:
    def __init__(self, log, timeout=DEFAULT_TIMEOUT_MS, chunk=DEFAULT_CHUNK,
                 retries=DEFAULT_RETRIES):
        self.log = log
        self.timeout = timeout
        self.chunk = chunk
        self.retries = retries
        self.usb = _import_usb()
        self.dev = None
        self.in_ep = None
        self.max_packet = 64
        self._claimed = False
        self._detached = False

    # -- discovery ---------------------------------------------------------

    def find(self):
        dev = self.usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
        if dev is None:
            raise NotPresent(
                "no SiPix Blink II (%04x:%04x) found on the USB bus"
                % (VENDOR_ID, PRODUCT_ID))
        self.dev = dev
        return dev

    @property
    def node_path(self):
        if self.dev is None:
            return None
        try:
            return "/dev/bus/usb/%03d/%03d" % (self.dev.bus, self.dev.address)
        except (AttributeError, TypeError):
            return None

    # -- open / close ------------------------------------------------------

    def open(self, handshake=True):
        if self.dev is None:
            self.find()
        self.log.debug("found device on bus %s address %s (%s)"
                       % (self.dev.bus, self.dev.address, self.node_path))

        # Some kernels bind usbfs-visible devices; hand it over if so.
        try:
            if self.dev.is_kernel_driver_active(0):
                self.log.debug("detaching kernel driver from interface 0")
                self.dev.detach_kernel_driver(0)
                self._detached = True
        except self.usb.core.USBError as exc:
            self.log.debug("kernel driver check skipped: %s" % exc)
        except NotImplementedError:
            pass

        # blink2.c camera_init: config 1, interface 0, altsetting 0.
        # Only call set_configuration if it is not already active: on a flaky
        # link a needless SET_CONFIGURATION is one more chance to fail.
        try:
            cfg = self.dev.get_active_configuration()
            active = cfg.bConfigurationValue
        except self.usb.core.USBError as exc:
            self._translate(exc, "reading the active configuration")
            active = None
        if active != 1:
            self.log.debug("setting configuration 1 (was %r)" % active)
            try:
                self.dev.set_configuration(1)
            except self.usb.core.USBError as exc:
                self._translate(exc, "setting configuration 1")
            cfg = self.dev.get_active_configuration()

        intf = cfg[(0, 0)]
        try:
            self.usb.util.claim_interface(self.dev, 0)
            self._claimed = True
        except self.usb.core.USBError as exc:
            self._translate(exc, "claiming interface 0")

        try:
            self.dev.set_interface_altsetting(interface=0, alternate_setting=0)
        except self.usb.core.USBError as exc:
            # Harmless when the interface has only one altsetting.
            self.log.debug("set_interface_altsetting(0,0) skipped: %s" % exc)

        self.in_ep = self.usb.util.find_descriptor(
            intf,
            custom_match=lambda e: (
                self.usb.util.endpoint_direction(e.bEndpointAddress)
                == self.usb.util.ENDPOINT_IN
                and self.usb.util.endpoint_type(e.bmAttributes)
                == self.usb.util.ENDPOINT_TYPE_BULK))
        if self.in_ep is None:
            raise CameraError(
                "the device has no bulk IN endpoint on interface 0 altsetting 0; "
                "it may have enumerated with a truncated descriptor set")
        self.max_packet = int(self.in_ep.wMaxPacketSize) or 64
        self.log.debug("bulk IN endpoint 0x%02x, wMaxPacketSize %d"
                       % (self.in_ep.bEndpointAddress, self.max_packet))

        if handshake:
            self.handshake()
        return self

    def close(self):
        if self.dev is None:
            return
        try:
            if self._claimed:
                self.usb.util.release_interface(self.dev, 0)
        except Exception:
            pass
        try:
            if self._detached:
                self.dev.attach_kernel_driver(0)
        except Exception:
            pass
        try:
            self.usb.util.dispose_resources(self.dev)
        except Exception:
            pass
        self._claimed = False
        self._detached = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _translate(self, exc, what):
        """Turn a pyusb USBError into one of our typed errors."""
        code = getattr(exc, "errno", None)
        if code == errno.EACCES:
            raise PermissionDenied(
                "permission denied while %s. The udev rule that grants your "
                "user access is missing or has not been applied yet." % what)
        if code == errno.EBUSY:
            raise DeviceBusy(
                "the device is busy while %s: another process already has it "
                "open." % what)
        if code == errno.ENODEV or code == errno.ENOENT:
            raise NotPresent(
                "the device disappeared from the bus while %s." % what)
        if code == errno.EPIPE:
            # The camera's firmware can wedge and STALL every vendor request
            # while still sitting happily on the bus. Measured on real
            # hardware: clear_halt, set_configuration and reset all fail to
            # revive it, so do not pretend otherwise -- it needs power removed.
            raise DeviceStalled(
                "the camera stalled while %s. It is still on the bus but its "
                "firmware has stopped answering vendor requests." % what)
        raise CameraError("USB error while %s: %s" % (what, exc))

    # -- primitives --------------------------------------------------------

    def ctrl_read(self, request, length, value=WVALUE, index=WINDEX):
        self.log.debug("ctrl_read  req=0x%02x val=0x%02x len=%d"
                       % (request, value, length))
        try:
            data = self.dev.ctrl_transfer(CTRL_IN, request, value, index,
                                          length, self.timeout)
        except self.usb.core.USBError as exc:
            self._translate(exc, "control-reading request 0x%02x" % request)
        data = bytes(data)
        if len(data) < length:
            self.log.warn("short control read on request 0x%02x: got %d of %d "
                          "bytes" % (request, len(data), length))
        return data

    def ctrl_write(self, request, payload, value=WVALUE, index=WINDEX):
        self.log.debug("ctrl_write req=0x%02x val=0x%02x len=%d data=%s"
                       % (request, value, len(payload), payload.hex()))
        try:
            n = self.dev.ctrl_transfer(CTRL_OUT, request, value, index,
                                       payload, self.timeout)
        except self.usb.core.USBError as exc:
            self._translate(exc, "control-writing request 0x%02x" % request)
        if n < len(payload):
            raise CameraError("short control write on request 0x%02x: sent %d "
                              "of %d bytes" % (request, n, len(payload)))
        return n

    def _round_up(self, n):
        mp = self.max_packet
        return ((n + mp - 1) // mp) * mp

    def bulk_read(self, expected, what="data"):
        """Read `expected` bytes from the bulk IN endpoint.

        Accepts a short final transfer (the camera ends the directory table
        with a short packet rather than padding to the rounded size) and logs
        every short read and timeout.
        """
        buf = bytearray()
        deadline_strikes = 0
        while len(buf) < expected:
            remaining = expected - len(buf)
            want = min(self.chunk, remaining)
            # Never ask for a non-multiple of the packet size: a full packet
            # arriving against a smaller request is an overflow, not a read.
            req = self._round_up(want)
            try:
                chunk = self.in_ep.read(req, self.timeout)
            except self.usb.core.USBTimeoutError:
                deadline_strikes += 1
                self.log.warn("timeout after %d ms reading %s at offset %d/%d "
                              "(strike %d)"
                              % (self.timeout, what, len(buf), expected,
                                 deadline_strikes))
                if deadline_strikes >= 2:
                    raise CameraError(
                        "bulk read of %s timed out at offset %d of %d bytes"
                        % (what, len(buf), expected))
                continue
            except self.usb.core.USBError as exc:
                self._translate(exc, "bulk-reading %s at offset %d of %d"
                                % (what, len(buf), expected))
            if not len(chunk):
                self.log.warn("zero-length bulk packet reading %s at offset "
                              "%d/%d; the camera ended the transfer early"
                              % (what, len(buf), expected))
                break
            buf += bytes(chunk)
            if len(chunk) < req:
                # A short packet terminates a USB bulk transfer. This is normal
                # at the end, and a truncation anywhere else.
                if len(buf) < expected:
                    self.log.warn("short bulk packet reading %s: got %d bytes "
                                  "for a %d-byte request at offset %d/%d"
                                  % (what, len(chunk), req,
                                     len(buf) - len(chunk), expected))
                    break
        if len(buf) > expected:
            self.log.debug("trimming %d byte(s) of packet padding from %s"
                           % (len(buf) - expected, what))
            del buf[expected:]
        return bytes(buf)

    def drain(self, timeout=150):
        """Discard anything still queued on the bulk endpoint.

        After an aborted transfer the camera may still be mid-stream; reading
        it dry resynchronises us before the next command.
        """
        total = 0
        while True:
            try:
                chunk = self.in_ep.read(self._round_up(self.chunk), timeout)
            except self.usb.core.USBTimeoutError:
                break
            except self.usb.core.USBError:
                break
            if not len(chunk):
                break
            total += len(chunk)
            if total > 8 * 1024 * 1024:
                break
        if total:
            self.log.warn("drained %d stale byte(s) from the bulk endpoint"
                          % total)
        return total

    # -- protocol ----------------------------------------------------------

    def firmware_id(self):
        data = self.ctrl_read(REQ_GET_FIRMWARE_ID, 6)
        if len(data) != 6:
            raise CameraError("firmware ID read returned %d bytes, expected 6"
                              % len(data))
        return data

    def init_still(self):
        data = self.ctrl_read(REQ_INIT_STILL, 1)
        if len(data) != 1:
            raise CameraError("init-still read returned %d bytes, expected 1"
                              % len(data))
        return data

    def handshake(self):
        fw = self.firmware_id()
        self.log.debug("firmware id: %s" % fw.hex(" "))
        self.init_still()
        return fw

    def get_numpics(self):
        data = self.ctrl_read(REQ_GET_NUMPICS, 2)
        if len(data) != 2:
            raise CameraError("photo count read returned %d bytes, expected 2"
                              % len(data))
        n = (data[0] << 8) | data[1]
        self.log.debug("numpics = %d" % n)
        return n

    def get_directory(self, numpics=None):
        """Return (entries, raw_table_bytes, expected_len)."""
        if numpics is None:
            numpics = self.get_numpics()
        if numpics == 0:
            return [], b"", 0

        expected = 8 * (1 + numpics)
        rounded = (expected + 0x3F) & ~0x3F
        self.ctrl_read(REQ_GET_DIR, 1)
        # blink2.c requests the 64-rounded size; the camera answers with
        # exactly `expected` bytes and ends the transfer with a short packet.
        table = self.bulk_read_directory(rounded, expected)
        if len(table) < expected:
            raise CameraError(
                "directory table is %d bytes, expected %d for %d photo(s)"
                % (len(table), expected, numpics))

        entries = []
        for i in range(numpics):
            start = (table[8 * i + 5] << 16) | (table[8 * i + 6] << 8) | table[8 * i + 7]
            end = (table[8 * i + 13] << 16) | (table[8 * i + 14] << 8) | table[8 * i + 15]
            if end < start:
                raise CameraError(
                    "directory entry %d is inconsistent: end 0x%06x is before "
                    "start 0x%06x" % (i, end, start))
            entries.append(DirEntry(i, start, end, table[8 * (i + 1)]))
        return entries, table[:expected], expected

    def bulk_read_directory(self, rounded, expected):
        """One bulk read for the directory, tolerating the short answer."""
        try:
            chunk = self.in_ep.read(rounded, self.timeout)
        except self.usb.core.USBTimeoutError:
            raise CameraError("timed out reading the directory table")
        except self.usb.core.USBError as exc:
            self._translate(exc, "bulk-reading the directory table")
        data = bytes(chunk)
        if len(data) != rounded:
            self.log.debug("directory table: camera returned %d bytes for a "
                           "%d-byte request (expecting %d)"
                           % (len(data), rounded, expected))
        return data

    def read_image(self, entry):
        """Fetch one image's raw bytes. Raises on failure; caller retries."""
        payload = (entry.start.to_bytes(4, "big")
                   + entry.length_words.to_bytes(4, "big"))
        self.ctrl_write(REQ_GET_MEMORY, payload)
        data = self.bulk_read(entry.data_bytes, what="%s data" % entry.basename)
        if len(data) < entry.data_bytes:
            raise CameraError(
                "short image read: got %d of %d bytes for %s"
                % (len(data), entry.data_bytes, entry.basename))
        return data

    def read_prefix(self, entry, nbytes=FINGERPRINT_BYTES):
        """Read only the first `nbytes` of an image.

        GET_MEMORY takes a length, so a short request returns a short answer;
        verified against hardware as byte-identical to the head of the full
        image, with nothing left queued on the endpoint afterwards.
        """
        nbytes = min(nbytes, entry.data_bytes)
        words = nbytes // 8
        if words == 0:
            return b""
        payload = entry.start.to_bytes(4, "big") + words.to_bytes(4, "big")
        self.ctrl_write(REQ_GET_MEMORY, payload)
        data = self.bulk_read(words * 8, what="%s prefix" % entry.basename)
        if len(data) < words * 8:
            raise CameraError(
                "short prefix read for %s: got %d of %d bytes"
                % (entry.basename, len(data), words * 8))
        return data

    def fingerprint(self, entry, nbytes=FINGERPRINT_BYTES):
        """(size, digest-of-prefix) identifying this image's content."""
        return entry.data_bytes, hashlib.sha256(
            self.read_prefix(entry, nbytes)).hexdigest()

    def delete_all(self):
        """Erase every image. There is no undo and no per-image delete."""
        data = self.ctrl_read(REQ_DELETE_ALL, 1)
        if len(data) != 1:
            raise CameraError("delete-all returned %d bytes, expected 1"
                              % len(data))
        return data

    def delete_last(self):
        """Erase the newest image only."""
        data = self.ctrl_read(REQ_DELETE_LAST, 1)
        if len(data) != 1:
            raise CameraError("delete-last returned %d bytes, expected 1"
                              % len(data))
        return data

    def read_image_with_retries(self, entry, retries=None):
        """Fetch one image, re-issuing the whole request on failure."""
        attempts = (self.retries if retries is None else retries) + 1
        last = None
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                backoff = 0.4 * (2 ** (attempt - 2))
                self.log.info("  retry %d/%d for %s in %.1fs"
                              % (attempt - 1, attempts - 1, entry.basename,
                                 backoff))
                time.sleep(backoff)
                self.drain()
            try:
                return self.read_image(entry)
            except (CameraError, self.usb.core.USBError) as exc:
                last = exc
                self.log.warn("attempt %d/%d for %s failed: %s"
                              % (attempt, attempts, entry.basename, exc))
                if isinstance(exc, (PermissionDenied, NotPresent)):
                    raise
        raise CameraError("gave up on %s after %d attempt(s): %s"
                          % (entry.basename, attempts, last))


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def _import_pil():
    try:
        from PIL import Image
    except ImportError:
        raise CameraError(
            "Pillow is required to write PNGs.\n"
            "  Fix: sudo apt install python3-pil   (or pip install Pillow)")
    return Image


def _double_pixels(convline, npix):
    """Duplicate every RGB pixel horizontally: ABC -> AABBCC."""
    src = memoryview(bytes(convline))[:npix * 3]
    out = bytearray(npix * 6)
    out[0::6] = src[0::3]
    out[1::6] = src[1::3]
    out[2::6] = src[2::3]
    out[3::6] = src[0::3]
    out[4::6] = src[1::3]
    out[5::6] = src[2::3]
    return out


def deinterleave_scanline(rawline, jw, pitch):
    """One JPEG scanline -> two output rows, per blink2.c:285-297.

    The camera packs two 320-pixel fields into each 640-pixel JPEG scanline,
    in 16-pixel blocks with an 8-pixel phase offset, and each field is then
    pixel-doubled back out to the full width.  This is a direct
    transliteration of the C so the output matches gphoto2 byte for byte.
    """
    convline = bytearray(jw * 2 * 3)
    nblocks = jw // 16
    half = pitch // 2

    # memcpy(convline+((w/16-1)*16+8)*3, rawline+((w/16-1)*16+8)*3, 8*3)
    if nblocks >= 1:
        off = ((nblocks - 1) * 16 + 8) * 3
        convline[off:off + 24] = rawline[off:off + 24]

    # memcpy(convline+pitch/2, rawline, 8*3)
    convline[half:half + 24] = rawline[0:24]

    # The alternating 16-pixel block copies.
    for j in range(nblocks - 1):
        src = (j * 16 + 8) * 3
        if (j & 1) == 0:
            dst = ((j // 2) * 16) * 3
        else:
            dst = half + ((j // 2) * 16 + 8) * 3
        convline[dst:dst + 48] = rawline[src:src + 48]

    doubled = _double_pixels(convline, jw * 2)
    return bytes(doubled[:pitch * 2])


def decode_still(jpeg_bytes, log):
    """Decode camera JPEG data to (width, height, packed RGB raster)."""
    import io
    Image = _import_pil()
    from PIL import ImageFile
    partial = False
    try:
        img = Image.open(io.BytesIO(jpeg_bytes))
        img.load()
    except Exception as exc:
        # libjpeg, and so gphoto2, renders whatever decoded before a damaged
        # entropy stream gave out. Pillow refuses by default, which would
        # throw away a recoverable photo, so ask it to do the same thing.
        was = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            img = Image.open(io.BytesIO(jpeg_bytes))
            img.load()
            partial = True
        except Exception:
            raise CameraError("the raw data is not a decodable JPEG: %s" % exc)
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = was
        log.warn("the JPEG data is damaged (%s); decoded as much as was "
                 "readable, so the lower part of this image will be blank"
                 % exc)
    img = img.convert("RGB")
    jw, jh = img.size
    pitch = (jw * 3 + 3) & ~3
    log.debug("jpeg is %dx%d, pitch %d -> output %dx%d"
              % (jw, jh, pitch, jw, jh * 2))
    if pitch != jw * 3:
        log.warn("jpeg width %d gives pitch %d != %d; the row stride does not "
                 "divide evenly and the output may be skewed"
                 % (jw, pitch, jw * 3))

    src = img.tobytes()
    row_bytes = jw * 3
    out = bytearray()
    for y in range(jh):
        rawline = src[y * row_bytes:(y + 1) * row_bytes]
        if len(rawline) < pitch:
            rawline = rawline + bytes(pitch - len(rawline))
        out += deinterleave_scanline(rawline, jw, pitch)

    width, height = jw, jh * 2
    want = width * height * 3
    if len(out) != want:
        log.warn("decoded raster is %d bytes, expected %d for %dx%d; "
                 "padding/truncating" % (len(out), want, width, height))
        out = (bytes(out) + bytes(want))[:want]
    return width, height, bytes(out), partial


def write_png(path, width, height, raster):
    Image = _import_pil()
    img = Image.frombytes("RGB", (width, height), raster)
    img.save(path, "PNG", optimize=True)


def write_jpeg(path, width, height, raster, quality=92):
    """Re-encode the decoded pixels as JPEG.

    Worth knowing: the camera's own data is already JPEG, so this is a second
    lossy pass over lossy data. PNG is the lossless default for that reason.
    The .raw file blinky keeps is the camera's original JPEG bitstream, and
    opens in any viewer as the 640x240 interleaved frame.
    """
    Image = _import_pil()
    img = Image.frombytes("RGB", (width, height), raster)
    img.save(path, "JPEG", quality=quality, subsampling=0, optimize=True)


def write_still(outdir, basename, width, height, raster, fmt, quality):
    """Write the decoded still in the requested format(s). Returns paths."""
    written = []
    if fmt in ("png", "both"):
        path = os.path.join(outdir, basename + ".png")
        write_png(path, width, height, raster)
        written.append(path)
    if fmt in ("jpeg", "both"):
        path = os.path.join(outdir, basename + ".jpg")
        write_jpeg(path, width, height, raster, quality)
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# PNM
# ---------------------------------------------------------------------------

def read_pnm(path):
    """Read a binary PPM (P6). Returns (width, height, raster RGB bytes)."""
    with open(path, "rb") as fh:
        data = fh.read()
    if not data.startswith(b"P6"):
        raise ValueError("not a binary PPM (P6) file")

    pos = 2
    values = []
    while len(values) < 3:
        # Skip whitespace, then comments, then read one decimal token.
        while pos < len(data) and data[pos:pos + 1].isspace():
            pos += 1
        if pos < len(data) and data[pos:pos + 1] == b"#":
            while pos < len(data) and data[pos:pos + 1] not in (b"\n", b"\r"):
                pos += 1
            continue
        start = pos
        while pos < len(data) and data[pos:pos + 1].isdigit():
            pos += 1
        if pos == start:
            raise ValueError("malformed PPM header")
        values.append(int(data[start:pos]))
    # Exactly one whitespace byte separates the header from the raster.
    pos += 1

    width, height, maxval = values
    if maxval != 255:
        raise ValueError("maxval %d is not supported (only 8-bit PPMs)" % maxval)
    want = width * height * 3
    raster = data[pos:pos + want]
    if len(raster) != want:
        raise ValueError("raster is %d bytes, expected %d for %dx%d"
                         % (len(raster), want, width, height))
    if len(data) > pos + want:
        # Trailing junk is not fatal, but the caller should know.
        raise ValueError("%d unexpected trailing byte(s) after the raster"
                         % (len(data) - pos - want))
    return width, height, raster


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

SUSPECT_PROCESSES = re.compile(
    r"gphoto2|gphotofs|gvfsd?-gphoto2|gvfs-gphoto2|gvfsd-gphoto2|"
    r"gvfs-gphoto2-volume-monitor|shotwell|digikam|rapid-photo|darktable",
    re.I)


def _proc_name(pid):
    try:
        with open("/proc/%s/comm" % pid) as fh:
            comm = fh.read().strip()
    except OSError:
        comm = "?"
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as fh:
            cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        cmdline = ""
    return comm, cmdline


def processes_holding(node_path, exclude_self=True):
    """Find processes with `node_path` open. Returns (holders, unreadable).

    Our own pid is excluded: by the time this runs, check 2 has already opened
    the device to read its descriptor, so we would otherwise report ourselves
    as the process holding it.
    """
    holders = []
    unreadable = 0
    if not node_path:
        return holders, unreadable
    me = os.getpid() if exclude_self else None
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        if me is not None and int(pid) == me:
            continue
        fddir = "/proc/%s/fd" % pid
        try:
            fds = os.listdir(fddir)
        except PermissionError:
            unreadable += 1
            continue
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fddir, fd))
            except OSError:
                continue
            if target == node_path:
                comm, cmdline = _proc_name(pid)
                holders.append((int(pid), comm, cmdline))
                break
    return holders, unreadable


def suspect_processes():
    """Processes whose *executable* suggests they grab cameras, open or not.

    Matched against the program name only, never the whole command line: a
    shell or editor that merely mentions gphoto2 in its arguments is not a
    process holding the camera, and naming it would send you chasing ghosts.
    """
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        comm, cmdline = _proc_name(pid)
        if comm == "?" and not cmdline:
            continue
        argv0 = os.path.basename(cmdline.split(" ", 1)[0]) if cmdline else ""
        if SUSPECT_PROCESSES.search(comm) or (
                argv0 and SUSPECT_PROCESSES.search(argv0)):
            found.append((int(pid), comm, cmdline))
    return found


class CheckResult:
    def __init__(self, number, title, status, detail=None, explanation=None,
                 fix=None):
        self.number = number
        self.title = title
        self.status = status            # "PASS", "FAIL" or "SKIP"
        self.detail = detail or []
        self.explanation = explanation
        self.fix = fix

    @property
    def ok(self):
        return self.status == "PASS"

    def render(self):
        lines = ["[%s] %d. %s" % (self.status, self.number, self.title)]
        for d in self.detail:
            lines.append("       %s" % d)
        if self.explanation:
            lines.append("")
            lines.append("  What this means:")
            for line in _wrap(self.explanation):
                lines.append("    %s" % line)
        if self.fix:
            lines.append("")
            lines.append("  Likely fix:")
            for line in _wrap(self.fix):
                lines.append("    %s" % line)
        return "\n".join(lines)


def _wrap(text, width=74):
    """Wrap prose, but leave indented lines (commands, samples) untouched."""
    import textwrap
    out = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        if para[0].isspace():
            out.append(para.rstrip())
            continue
        indent = "  " if para.lstrip().startswith("- ") else ""
        out.extend(textwrap.wrap(para, width, subsequent_indent=indent) or [""])
    return out


def run_checks(log, timeout=DEFAULT_TIMEOUT_MS, chunk=DEFAULT_CHUNK,
               retries=DEFAULT_RETRIES):
    """Run the five diagnostic checks in order, stopping at the first failure.

    Returns (results, cam_or_None).  The caller owns closing the camera.
    """
    results = []
    cam = None

    # -- 1. on the bus ----------------------------------------------------
    try:
        usb = _import_usb()
    except CameraError as exc:
        results.append(CheckResult(
            1, "Camera is on the USB bus (%04x:%04x)" % (VENDOR_ID, PRODUCT_ID),
            "FAIL", ["pyusb could not be imported"],
            "The tool cannot look at the USB bus at all without pyusb.",
            str(exc)))
        return results, None

    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        detail = ["no device with ID %04x:%04x is present" % (VENDOR_ID, PRODUCT_ID)]
        ok, text = kernel_log()
        explanation = ("The camera is not enumerated, so nothing in userspace "
                       "can reach it.")
        if not ok:
            detail.append(text)
            explanation += (" The kernel log could not be read, so there is no "
                            "evidence either way about why.")
            fix = ("Check that the camera is switched on and plugged in, then "
                   "re-run this check.")
        else:
            hits = scan_kernel_trouble(text)
            if hits:
                detail.append("recent kernel messages about USB trouble:")
                for line, _ in hits[-8:]:
                    detail.append("  %s" % line)
                seen = []
                for _, why in hits:
                    if why not in seen:
                        seen.append(why)
                explanation += (" The kernel log explains why:\n\n"
                                + "\n".join("- " + s for s in seen)
                                + "\n\nThose are all link-layer faults. The "
                                  "camera, cable, port or hub dropped the "
                                  "connection; no amount of software change "
                                  "will fix them.")
                fix = link_advice(bus_from_kernel_hits(hits))
            else:
                detail.append("no USB link errors in the recent kernel log")
                explanation += (" The kernel log shows no USB errors, so the "
                                "camera most likely was never plugged in or is "
                                "switched off.")
                fix = ("Plug the camera in, switch it on, and confirm it "
                       "appears in: lsusb -d 0c77:1011")
        results.append(CheckResult(
            1, "Camera is on the USB bus (%04x:%04x)" % (VENDOR_ID, PRODUCT_ID),
            "FAIL", detail, explanation, fix))
        return results, None

    node = None
    try:
        node = "/dev/bus/usb/%03d/%03d" % (dev.bus, dev.address)
    except (AttributeError, TypeError):
        pass
    results.append(CheckResult(
        1, "Camera is on the USB bus (%04x:%04x)" % (VENDOR_ID, PRODUCT_ID),
        "PASS", ["bus %s address %s%s" % (dev.bus, dev.address,
                                          " -> %s" % node if node else "")]))

    # -- 2. opens without root -------------------------------------------
    title2 = "Device opens without root"
    detail = []
    if node and not os.access(node, os.R_OK | os.W_OK):
        try:
            st = os.stat(node)
            import grp
            import pwd
            owner = pwd.getpwuid(st.st_uid).pw_name
            group = grp.getgrgid(st.st_gid).gr_name
            detail.append("%s is %s %s:%s -- your user cannot open it"
                          % (node, oct(st.st_mode & 0o777), owner, group))
        except (OSError, KeyError):
            detail.append("%s is not readable and writable by your user" % node)
        results.append(CheckResult(2, title2, "FAIL", detail,
                                   *_udev_advice()))
        return results, None

    # A real transfer is the only honest test: ask for the device descriptor.
    try:
        dev.ctrl_transfer(0x80, 0x06, 0x0100, 0, 18, timeout)
        detail.append("device descriptor read back as your own user (no sudo)")
    except usb.core.USBError as exc:
        code = getattr(exc, "errno", None)
        if code == errno.EACCES:
            detail.append("permission denied opening %s" % (node or "the device"))
            results.append(CheckResult(2, title2, "FAIL", detail,
                                       *_udev_advice()))
            return results, None
        if code == errno.EBUSY:
            detail.append("the device opened, but is busy (see the next check)")
        elif code in (errno.ENODEV, errno.ENOENT):
            results.append(CheckResult(
                2, title2, "FAIL",
                ["the device vanished from the bus between checks"],
                "The camera disappeared mid-diagnosis. That is the flaky-link "
                "symptom, not a permissions problem.",
                link_advice()))
            return results, None
        else:
            results.append(CheckResult(
                2, title2, "FAIL", ["USB error: %s" % exc],
                "The device is listed on the bus but will not answer a "
                "standard descriptor request.",
                "Unplug and replug the camera, then re-run doctor. If it "
                "persists, try another port or cable."))
            return results, None
    if os.geteuid() == 0:
        detail.append("note: you are running as root, so this check cannot "
                      "prove the udev rule works for your normal user")
    results.append(CheckResult(2, title2, "PASS", detail))

    # -- 3. nobody else holds it ------------------------------------------
    title3 = "No other process has the device open"
    holders, unreadable = processes_holding(node)
    if holders:
        detail = ["%s is held by:" % node]
        for pid, comm, cmdline in holders:
            detail.append("  pid %d  %s  %s" % (pid, comm, cmdline[:100]))
        names = ", ".join(sorted({h[1] for h in holders}))
        results.append(CheckResult(
            3, title3, "FAIL", detail,
            "Only one program at a time can claim the camera's interface. "
            "%s already has it, so this tool would get EBUSY as soon as it "
            "tried to read." % names,
            _holder_fix(holders)))
        return results, None

    # Nothing visible in /proc, but the claim itself is the real test.
    cam = Blink2(log, timeout=timeout, chunk=chunk, retries=retries)
    cam.dev = dev
    try:
        cam.open(handshake=False)
    except DeviceBusy as exc:
        detail = [str(exc)]
        suspects = suspect_processes()
        if suspects:
            detail.append("processes that commonly grab cameras are running:")
            for pid, comm, cmdline in suspects:
                detail.append("  pid %d  %s  %s" % (pid, comm, cmdline[:100]))
        if unreadable:
            detail.append("%d process(es) belong to other users and could not "
                          "be inspected" % unreadable)
        results.append(CheckResult(
            3, title3, "FAIL", detail,
            "The kernel refused to hand over interface 0 because another "
            "program already claimed it.",
            ((_holder_fix(suspects) + "\n\n") if suspects else "") + (
                "Find the holder for certain with:\n"
                "  sudo fuser -v %s\n"
                "then stop it. The usual culprits are gvfs's gphoto2 backend "
                "and a stray gphoto2 process." % (node or "/dev/bus/usb/*/*"))))
        cam.close()
        return results, None
    except PermissionDenied as exc:
        results.append(CheckResult(3, title3, "FAIL", [str(exc)],
                                   *_udev_advice()))
        cam.close()
        return results, None
    except CameraError as exc:
        results.append(CheckResult(
            3, title3, "FAIL", [str(exc)],
            "The interface could not be claimed, for a reason other than "
            "another process holding it.",
            "Unplug and replug the camera, then re-run doctor."))
        cam.close()
        return results, None

    detail = ["interface 0 claimed cleanly"]
    if unreadable:
        detail.append("(%d process(es) owned by other users were not "
                      "inspectable, but the claim succeeded, which is the "
                      "test that matters)" % unreadable)
    results.append(CheckResult(3, title3, "PASS", detail))

    # -- 4. handshake ------------------------------------------------------
    title4 = "Handshake: firmware ID reads 6 bytes, photo count reads 2"
    try:
        fw = cam.ctrl_read(REQ_GET_FIRMWARE_ID, 6)
        if len(fw) != 6:
            raise CameraError("firmware ID read returned %d byte(s), expected 6"
                              % len(fw))
        init = cam.ctrl_read(REQ_INIT_STILL, 1)
        if len(init) != 1:
            raise CameraError("init-still read returned %d byte(s), expected 1"
                              % len(init))
        raw_count = cam.ctrl_read(REQ_GET_NUMPICS, 2)
        if len(raw_count) != 2:
            raise CameraError("photo count read returned %d byte(s), expected 2"
                              % len(raw_count))
        numpics = (raw_count[0] << 8) | raw_count[1]
    except DeviceStalled as exc:
        results.append(CheckResult(
            4, title4, "FAIL", [str(exc)],
            "The camera is on the bus and will accept a connection, but its "
            "firmware has wedged: every vendor request comes back as a USB "
            "stall. This is a state the camera gets into on its own; it is not "
            "a cable, a permission or a driver problem.",
            STALL_ADVICE))
        return results, cam
    except (CameraError, usb.core.USBError) as exc:
        detail = [str(exc)]
        results.append(CheckResult(
            4, title4, "FAIL", detail,
            "The camera is reachable but is not answering the vendor control "
            "requests that every other operation depends on. A short or empty "
            "answer here usually means the link dropped mid-transfer rather "
            "than that the firmware disagrees with the protocol.",
            "Check the kernel log for -71 errors around now:\n"
            "  journalctl -k -n 50\n"
            "If it shows link errors, change the cable or port. If it is "
            "clean, power-cycle the camera (remove the batteries briefly) and "
            "re-run doctor."))
        return results, cam

    results.append(CheckResult(
        4, title4, "PASS",
        ["firmware ID: %s" % fw.hex(" "),
         "init-still:  %s" % init.hex(),
         "photo count: %d" % numpics]))

    # -- 5. directory table ------------------------------------------------
    title5 = "Directory table is 8*(numpics+1) bytes and internally consistent"
    expected = 8 * (1 + numpics)
    if numpics == 0:
        results.append(CheckResult(
            5, title5, "PASS",
            ["the camera reports 0 photos, so there is no table to read"]))
        return results, cam
    try:
        entries, table, expected = cam.get_directory(numpics)
    except (CameraError, usb.core.USBError) as exc:
        detail = [str(exc)]
        results.append(CheckResult(
            5, title5, "FAIL", detail,
            "The directory table is how the tool learns where each photo "
            "lives in the camera's memory. blink2.c asks for the size rounded "
            "up to a multiple of 64, but this camera answers with exactly "
            "8*(numpics+1) bytes and ends the transfer with a short packet -- "
            "the same thing your libgphoto2 patch accounts for. A table that "
            "is shorter than that, or whose entries run backwards, means the "
            "transfer was truncated.",
            "Check the kernel log for link errors:\n"
            "  journalctl -k -n 50\n"
            "Then power-cycle the camera and re-run doctor."))
        return results, cam

    detail = ["read %d bytes, expected %d for %d photo(s)"
              % (len(table), expected, numpics)]
    for entry in entries:
        detail.append("  %s" % entry.describe())
    results.append(CheckResult(5, title5, "PASS", detail))
    return results, cam


def find_udev_rule():
    """The udev rule granting access, wherever it came from. (path, body)."""
    for path in UDEV_RULE_PATHS:
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    return path, fh.read().strip()
            except OSError:
                return path, "(could not read it)"
    return None, None


def _udev_advice():
    """(explanation, fix) for a permissions failure."""
    explanation = ("Raw USB devices are root-only unless a udev rule grants "
                   "your login session access. Without it, every operation "
                   "fails with EACCES before a single byte moves.")
    path, body = find_udev_rule()
    if path:
        fix = ("The rule file %s exists:\n"
               "  %s\n"
               "but it has not taken effect for the device that is currently "
               "plugged in. udev only applies rules when a device appears, so:\n"
               "  sudo udevadm control --reload-rules && sudo udevadm trigger\n"
               "then unplug and replug the camera." % (path, body))
    else:
        fix = ("No udev rule for this camera was found in any of:\n"
               + "".join("  %s\n" % p for p in UDEV_RULE_PATHS) +
               "Installing the blinky package puts one in place for you. To do "
               "it by hand instead:\n"
               "  echo '%s' | sudo tee %s\n"
               "  sudo udevadm control --reload-rules && sudo udevadm trigger\n"
               "then unplug and replug the camera."
               % (UDEV_RULE_TEXT, UDEV_RULE_PATH))
    return explanation, fix


def _holder_fix(holders):
    names = sorted({h[1] for h in holders})
    lines = []
    if any("gvfs" in n or "gphoto" in n for n in names):
        lines.append("GVFS mounts cameras automatically. Unmount it:")
        lines.append("  gio mount -u gphoto2://")
        lines.append("and stop the backend:")
        lines.append("  pkill -f gvfsd-gphoto2")
    if any(n.startswith("gphoto2") for n in names):
        lines.append("Stop the gphoto2 process that is holding the camera:")
        lines.append("  pkill -x gphoto2")
    if not lines:
        lines.append("Stop the process(es) listed above, then re-run doctor:")
        for pid, comm, _ in holders:
            lines.append("  kill %d   # %s" % (pid, comm))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class RunState:
    def __init__(self):
        self.results = []
        self.checks_ran = False
        self.offline = False


def _section(title):
    return "\n" + "=" * 78 + "\n" + title + "\n" + "=" * 78 + "\n"


def write_report(path, log, state, timeout=DEFAULT_TIMEOUT_MS):
    """Write checks + kernel log + lsusb + the tool's own log to one file."""
    parts = []
    parts.append("blinky %s diagnostic report" % __version__)
    parts.append("generated: %s" % datetime.datetime.now().isoformat(" ", "seconds"))
    parts.append("command:   %s" % " ".join(sys.argv))
    parts.append("cwd:       %s" % os.getcwd())

    parts.append(_section("ENVIRONMENT"))
    parts.append("python:  %s" % sys.version.replace("\n", " "))
    for mod, label in (("usb", "pyusb"), ("PIL", "Pillow")):
        try:
            m = __import__(mod)
            parts.append("%-8s %s  (%s)" % (label + ":", getattr(m, "__version__", "?"),
                                            getattr(m, "__file__", "?")))
        except ImportError:
            parts.append("%-8s not installed" % (label + ":"))
    ok, uname = _run(["uname", "-a"])
    parts.append("uname:   %s" % (uname.strip() if ok else "unavailable"))

    parts.append(_section("CHECK RESULTS"))
    if not state.results and not state.offline:
        parts.append("(the command did not run the checks; running them now)\n")
        try:
            results, cam = run_checks(log, timeout=timeout)
            if cam is not None:
                cam.close()
            state.results = results
        except Exception as exc:
            parts.append("could not run the checks: %s" % exc)
    if state.offline and not state.results:
        parts.append("(no device checks were run: this command does not use "
                     "the camera)")
    for result in state.results:
        parts.append(result.render())
        parts.append("")

    parts.append(_section("KERNEL LOG"))
    ok, text = kernel_log(lines=600)
    if not ok:
        parts.append(text)
        parts.append("\n(continuing without kernel log data)")
    else:
        hits = scan_kernel_trouble(text)
        parts.append("-- USB link errors found: %d --\n" % len(hits))
        for line, _ in hits:
            parts.append(line)
        parts.append("\n-- last 200 kernel lines mentioning usb --\n")
        usb_lines = [l for l in text.splitlines() if "usb" in l.lower()]
        parts.extend(usb_lines[-200:])

    parts.append(_section("lsusb -v -d %04x:%04x" % (VENDOR_ID, PRODUCT_ID)))
    ok, text = _run(["lsusb", "-v", "-d", "%04x:%04x" % (VENDOR_ID, PRODUCT_ID)])
    parts.append(text.strip() or "(no output: the camera is probably not on the bus)")
    if not ok:
        parts.append("\n(lsusb exited non-zero; output above is whatever it "
                     "managed to print. Some descriptor fields need root.)")
    ok, text = _run(["lsusb"])
    parts.append("\n-- full bus listing --\n")
    parts.append(text.strip() if ok else "(unavailable)")

    parts.append(_section("TOOL LOG"))
    parts.append(log.transcript())

    body = "\n".join(parts) + "\n"
    with open(path, "w") as fh:
        fh.write(body)
    return path


# ---------------------------------------------------------------------------
# Helpers shared by the commands
# ---------------------------------------------------------------------------

def parse_image_spec(spec, count):
    """'0,2-4' -> [0, 2, 3, 4], validated against `count`."""
    if spec is None:
        return list(range(count))
    wanted = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                lo, hi = int(lo), int(hi)
            except ValueError:
                raise CameraError("bad --images range %r" % part)
            if lo > hi:
                raise CameraError("bad --images range %r: %d is above %d"
                                  % (part, lo, hi))
            wanted.extend(range(lo, hi + 1))
        else:
            try:
                wanted.append(int(part))
            except ValueError:
                raise CameraError("bad --images value %r" % part)
    out = []
    for i in wanted:
        if not 0 <= i < count:
            raise CameraError("image %d is out of range: the camera has %d "
                              "photo(s), numbered 0 to %d"
                              % (i, count, count - 1))
        if i not in out:
            out.append(i)
    return out


def local_fingerprints(outdir, nbytes=FINGERPRINT_BYTES):
    """Index what is already in a folder, by content rather than by name.

    Returns {(size, digest): path}. The .raw and .avi files blinky writes are
    the camera's exact bytes, so their prefixes hash to the same value as the
    camera's own -- which means a photo can be recognised after it has been
    renamed or moved, and a different photo that merely reuses a filename is
    not mistaken for it.
    """
    index = {}
    for folder in scan_dirs(outdir):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith((".raw",) + MOVIE_EXTS):
                continue
            path = os.path.join(folder, name)
            try:
                size = os.path.getsize(path)
                with open(path, "rb") as fh:
                    head = fh.read(nbytes)
            except OSError:
                continue
            index.setdefault((size, hashlib.sha256(head).hexdigest()), path)
    return index


IMAGE_NAME_RE = re.compile(r"^image(\d{4,})\b", re.I)


def next_free_index(outdir):
    """One past the highest imageNNNN already in the folder.

    The camera's own numbering restarts at zero every time it is erased, so
    it is not an identity: photo 0 from today is a different picture from
    photo 0 last week. Continuing the count instead of reusing it keeps names
    unique across sessions, keeps them sorting in the order they arrived, and
    avoids the confusing image0000-1 pile-up that suffixing produces.
    """
    highest = -1
    for folder in scan_dirs(outdir):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            m = IMAGE_NAME_RE.match(name)
            if m:
                try:
                    highest = max(highest, int(m.group(1)))
                except ValueError:
                    pass
    return highest + 1


def free_path(path):
    """A path that does not exist yet, by adding -1, -2 ... before the suffix."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists("%s-%d%s" % (stem, n, ext)):
        n += 1
    return "%s-%d%s" % (stem, n, ext)


def write_file_atomically(path, data):
    tmp = path + ".part"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def open_camera(args, log):
    cam = Blink2(log, timeout=args.timeout, chunk=args.chunk,
                 retries=args.retries)
    cam.open()
    return cam


def explain_camera_error(log, exc, since):
    """Print the failure plus whatever the kernel log says about it."""
    log.error(str(exc))
    if isinstance(exc, DeviceStalled):
        log.info("")
        for line in _wrap(STALL_ADVICE):
            log.info("  %s" % line)
        return
    if isinstance(exc, PermissionDenied):
        explanation, fix = _udev_advice()
        log.info("")
        log.info("  %s" % "\n  ".join(_wrap(explanation)))
        log.info("  Likely fix:")
        for line in _wrap(fix):
            log.info("    %s" % line)
        return
    report_kernel_trouble(log, since=since)
    log.info("  Run 'blinky doctor' for a full check of the link.")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_doctor(args, log, state):
    log.out("blinky doctor -- checking the SiPix Blink II link, in order.")
    log.out("")
    results, cam = run_checks(log, timeout=args.timeout, chunk=args.chunk,
                              retries=args.retries)
    state.results = results
    state.checks_ran = True
    try:
        for result in results:
            log.out(result.render())
            log.out("")
        failed = [r for r in results if not r.ok]
        if failed:
            log.out("Stopped at check %d of 5. Fix that, then run doctor again."
                    % failed[0].number)
            return 1
        log.out("All 5 checks passed. The camera is ready to talk to.")
        return 0
    finally:
        if cam is not None:
            cam.close()


def cmd_list(args, log, state):
    since = log.start_wall.strftime("%Y-%m-%d %H:%M:%S")
    cam = None
    try:
        cam = open_camera(args, log)
        fw = cam.firmware_id()
        numpics = cam.get_numpics()
        log.out("firmware ID : %s" % fw.hex(" "))
        log.out("photos      : %d" % numpics)
        if numpics == 0:
            log.out("")
            log.out("The camera is empty.")
            return 0
        entries, table, expected = cam.get_directory(numpics)
        log.out("directory   : %d bytes (expected 8*(%d+1) = %d)"
                % (len(table), numpics, expected))
        log.out("")
        total = 0
        for entry in entries:
            total += entry.data_bytes
            log.out("  %s -> %s" % (entry.describe(),
                                    entry.basename + (".avi" if entry.is_movie
                                                      else ".png")))
        log.out("")
        log.out("total: %d photo(s), %d bytes to transfer" % (len(entries), total))
        return 0
    except CameraError as exc:
        explain_camera_error(log, exc, since)
        return 1
    finally:
        if cam is not None:
            cam.close()


def verify_saved(path, expected_bytes, expected_digest):
    """Re-read a file from disk and confirm it is what we just transferred."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
    except OSError as exc:
        return False, "could not read it back: %s" % exc
    if size != expected_bytes:
        return False, "on disk it is %d bytes, expected %d" % (size, expected_bytes)
    if digest != expected_digest:
        return False, "its contents do not match what was transferred"
    return True, ""


def cmd_download(args, log, state):
    since = log.start_wall.strftime("%Y-%m-%d %H:%M:%S")
    outdir = os.path.expanduser(args.out)
    os.makedirs(outdir, exist_ok=True)
    rawdir = raw_dir(outdir, not args.no_raw_subfolder)
    cam = None
    failures = []
    partials = []
    saved = []
    skipped = []
    verified = {}          # entry index -> (path, bytes, digest)
    try:
        cam = open_camera(args, log)
        numpics = cam.get_numpics()
        if numpics == 0:
            log.out("The camera is empty; nothing to download.")
            return 0
        entries, table, expected = cam.get_directory(numpics)
        log.debug("directory table: %d bytes, expected %d"
                  % (len(table), expected))
        wanted = parse_image_spec(args.images, len(entries))
        log.out("Downloading %d of %d photo(s) to %s"
                % (len(wanted), len(entries), outdir))
        log.out("")

        index = {} if args.no_skip_duplicates else local_fingerprints(outdir)
        if index:
            log.debug("%d file(s) already in %s are indexed by content"
                      % (len(index), outdir))
        sizes_present = {size for size, _ in index}

        # The camera renumbers from zero after an erase, so its index cannot
        # name the file. Carry on from whatever is already in the folder.
        counter = next_free_index(outdir) if args.naming == "continue" else None
        if counter:
            log.info("  continuing numbering from image%04d" % counter)

        for i in wanted:
            entry = entries[i]

            # Content-based skip: only pay for the prefix read when some local
            # file is the right size to be this photo in the first place.
            if index and entry.data_bytes in sizes_present and not args.force:
                try:
                    key = cam.fingerprint(entry)
                except CameraError as exc:
                    log.warn("could not fingerprint %s (%s); downloading it"
                             % (entry.basename, exc))
                    key = None
                if key in index:
                    log.info("  %s: already saved as %s"
                             % (entry.basename,
                                os.path.basename(index[key])))
                    skipped.append(entry.basename)
                    continue

            if counter is None:
                stem = entry.basename
            else:
                stem = "image%04d" % counter
                counter += 1
            exts = ((".png",) if args.format == "png"
                    else (".jpg",) if args.format == "jpeg"
                    else (".png", ".jpg"))
            if entry.is_movie:
                # A clip is Motion JPEG, so like a still it has raw bytes to
                # keep and a playable file to build from them.
                raw_path = os.path.join(rawdir, stem + ".raw")
                targets = [raw_path, os.path.join(outdir, stem + ".avi")]
            else:
                raw_path = os.path.join(rawdir, stem + ".raw")
                targets = [raw_path] + [os.path.join(outdir, stem + e)
                                        for e in exts]

            existing = [t for t in targets if os.path.exists(t)]
            if existing and not args.force:
                if args.no_skip_duplicates:
                    log.info("  %s: skipped, %s already exists (use --force "
                             "to re-download)"
                             % (entry.basename, os.path.basename(existing[0])))
                    skipped.append(entry.basename)
                    continue
                # Content said this is a photo we do not have, but the name
                # is taken by a different one. Never clobber it.
                raw_path = free_path(raw_path)
                log.info("  %s: %s is a different photo, saving as %s"
                         % (entry.basename, os.path.basename(existing[0]),
                            os.path.basename(raw_path)))
            elif stem != entry.basename:
                # Say what happened, not why we guess it happened: the folder
                # simply numbers on from what it already holds.
                log.info("  %s: saving as %s"
                         % (entry.basename, os.path.basename(raw_path)))

            log.info("  %s: %s, %d bytes"
                     % (entry.basename, "AVI clip" if entry.is_movie else "still",
                        entry.data_bytes))
            t0 = time.monotonic()
            try:
                data = cam.read_image_with_retries(entry)
            except (PermissionDenied, NotPresent, DeviceStalled) as exc:
                explain_camera_error(log, exc, since)
                failures.append((entry.basename, str(exc)))
                log.error("aborting: the device is no longer usable")
                break
            except CameraError as exc:
                log.error("%s: %s" % (entry.basename, exc))
                report_kernel_trouble(log, since=since)
                failures.append((entry.basename, str(exc)))
                continue
            elapsed = time.monotonic() - t0
            log.info("    read %d bytes in %.1fs (%.1f kB/s)"
                     % (len(data), elapsed,
                        len(data) / 1024.0 / elapsed if elapsed else 0.0))

            # Raw bytes hit the disk before anything tries to interpret them.
            os.makedirs(os.path.dirname(raw_path) or ".", exist_ok=True)
            write_file_atomically(raw_path, data)
            log.info("    saved raw -> %s" % raw_path)
            saved.append(raw_path)
            verified[i] = (raw_path, len(data),
                           hashlib.sha256(data).hexdigest())

            stem = os.path.splitext(os.path.basename(raw_path))[0]
            if entry.is_movie:
                avi_path = os.path.join(outdir, stem + ".avi")
                try:
                    n, w, h = build_clip(data, avi_path, log, args.fps)
                    log.info("    %d frame%s at %dx%d -> %s"
                             % (n, "" if n == 1 else "s", w, h, avi_path))
                    saved.append(avi_path)
                except CameraError as exc:
                    log.error("%s: could not build a video: %s"
                              % (entry.basename, exc))
                    log.info("    the camera's bytes are kept at %s" % raw_path)
                    failures.append((entry.basename, "clip: %s" % exc))
                continue
            try:
                width, height, raster, partial = decode_still(data, log)
                for path in write_still(outdir, stem, width, height, raster,
                                        args.format, args.jpeg_quality):
                    log.info("    decoded %dx%d%s -> %s"
                             % (width, height, " (partial)" if partial else "",
                                path))
                    saved.append(path)
                if partial:
                    partials.append(entry.basename)
            except CameraError as exc:
                log.error("%s: decode failed: %s" % (entry.basename, exc))
                log.info("    the raw data is kept at %s; re-run "
                         "'blinky decode %s' once the cause is known"
                         % (raw_path, raw_path))
                failures.append((entry.basename, "decode: %s" % exc))

        if args.delete_after:
            rc = _delete_after_download(cam, log, entries, wanted, verified,
                                        failures, skipped)
            if rc:
                failures.append(("delete", "refused; see above"))
    except CameraError as exc:
        explain_camera_error(log, exc, since)
        return 1
    finally:
        if cam is not None:
            cam.close()

    log.out("")
    if saved and rawdir != outdir:
        log.out("Raw camera data is in %s" % rawdir)
    log.out("Done: %d file(s) written, %d skipped, %d failed%s."
            % (len(saved), len(skipped), len(failures),
               ", %d partial" % len(partials) if partials else ""))
    for name, why in failures:
        log.out("  failed: %s -- %s" % (name, why))
    for name in partials:
        log.out("  partial: %s -- the JPEG in the camera is damaged; the "
                "picture holds the rows that decoded, and the .raw holds the "
                "exact bytes" % name)
    return 1 if failures else 0


def _delete_after_download(cam, log, entries, wanted, verified, failures,
                           skipped):
    """Erase the camera, but only when that is provably safe.

    The camera can only erase *everything*: there is no per-image delete in
    the protocol. So anything short of "every photo on the camera is now
    safely on disk" means refusing, because the alternative is destroying a
    photo that was never copied.
    """
    log.out("")
    log.out("--delete-after: checking it is safe to erase the camera")

    def refuse(reason):
        log.error("refusing to erase the camera: %s" % reason)
        log.info("  The camera has no per-image delete; erasing removes "
                 "everything, so blinky only does it when every photo is "
                 "accounted for on disk.")
        return 1

    if failures:
        return refuse("%d image(s) failed during this run" % len(failures))
    if len(wanted) != len(entries):
        return refuse("only %d of %d photos were selected (--images), and "
                      "erasing would destroy the other %d"
                      % (len(wanted), len(entries), len(entries) - len(wanted)))

    # Every photo must be on disk: either saved now, or skipped because an
    # identical copy was already there.
    unaccounted = [entries[i].basename for i in wanted
                   if i not in verified and entries[i].basename not in skipped]
    if unaccounted:
        return refuse("no verified copy of %s" % ", ".join(unaccounted[:5]))

    # Re-read from disk what we just wrote and check it byte for byte.
    for i, (path, size, digest) in sorted(verified.items()):
        ok, why = verify_saved(path, size, digest)
        if not ok:
            return refuse("%s did not verify: %s" % (os.path.basename(path), why))
    log.out("  %d file(s) re-read from disk and verified byte for byte"
            % len(verified))
    if skipped:
        log.out("  %d photo(s) were already saved from a previous run"
                % len(skipped))

    log.out("  erasing the camera")
    cam.delete_all()
    time.sleep(0.6)
    left = cam.get_numpics()
    if left:
        log.warn("the camera still reports %d photo(s) after the erase" % left)
        return 1
    log.out("  the camera is now empty")
    return 0


def version_tuple(text):
    """'1.10-2' -> (1, 10, 2), so 1.10 sorts above 1.9."""
    parts = re.findall(r"\d+", text or "")
    return tuple(int(x) for x in parts) or (0,)


def _http_get(url, log, accept=None, timeout=20, allow_404=False):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "blinky/%s" % __version__,
        "Accept": accept or "application/vnd.github+json",
    })
    log.debug("GET %s" % url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        # GitHub answers 404 for a repository that simply has no releases,
        # which is a normal state, not a failure.
        if exc.code == 404 and allow_404:
            return None
        raise CameraError("%s returned HTTP %s" % (url, exc.code))
    except urllib.error.URLError as exc:
        raise CameraError("could not reach %s: %s" % (url, exc.reason))
    except OSError as exc:
        raise CameraError("could not reach %s: %s" % (url, exc))


def cmd_update(args, log, state):
    """Check GitHub for a newer release, fetch it, and verify it."""
    state.offline = True
    import json

    log.out("Installed: blinky %s" % __version__)
    try:
        raw = _http_get(RELEASES_API, log, allow_404=True)
        info = json.loads(raw.decode("utf-8")) if raw else {}
    except (CameraError, ValueError) as exc:
        log.error(str(exc))
        log.info("  Releases are listed at %s" % RELEASES_PAGE)
        return 1

    tag = str(info.get("tag_name") or "").strip()
    if not tag:
        log.out("")
        log.out("No releases have been published yet.")
        log.info("  %s" % RELEASES_PAGE)
        return 0
    latest = tag.lstrip("v")
    log.out("Latest:    blinky %s" % latest)

    if version_tuple(latest) <= version_tuple(__version__):
        log.out("")
        log.out("You are up to date.")
        return 0

    log.out("")
    body = (info.get("body") or "").strip()
    if body:
        log.out("What is new in %s:" % latest)
        for line in body.splitlines()[:20]:
            log.out("  %s" % line)
        log.out("")
    if args.check:
        log.out("Run 'blinky update' to fetch it.")
        return 0

    assets = {a.get("name"): a for a in info.get("assets") or []}
    deb = next((n for n in assets if n.endswith(".deb")), None)
    if deb is None:
        log.error("release %s has no .deb attached" % tag)
        log.info("  See %s" % RELEASES_PAGE)
        return 1

    outdir = os.path.expanduser(args.out) if args.out else tempfile.mkdtemp(
        prefix="blinky-update-")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, deb)

    log.out("Downloading %s (%s KB)"
            % (deb, "{:,}".format((assets[deb].get("size") or 0) // 1024)))
    data = _http_get(assets[deb]["browser_download_url"], log,
                     accept="application/octet-stream", timeout=120)

    # Verify against the checksums published with the release. Installing a
    # package is root-level trust, so a truncated or swapped download should
    # stop here rather than reach dpkg.
    if "SHA256SUMS" in assets:
        sums = _http_get(assets["SHA256SUMS"]["browser_download_url"], log,
                         accept="application/octet-stream").decode("utf-8")
        want = None
        for line in sums.splitlines():
            bits = line.split()
            if len(bits) == 2 and bits[1].lstrip("*") == deb:
                want = bits[0]
        got = hashlib.sha256(data).hexdigest()
        if want is None:
            log.error("SHA256SUMS does not mention %s; refusing" % deb)
            return 1
        if want != got:
            log.error("checksum mismatch for %s" % deb)
            log.info("  published: %s" % want)
            log.info("  downloaded: %s" % got)
            log.info("  Nothing was installed. Try again, and if it persists "
                     "report it at %s" % RELEASES_PAGE)
            return 1
        log.out("  checksum verified against the release's SHA256SUMS")
    else:
        log.warn("this release publishes no SHA256SUMS, so the download "
                 "could not be verified")

    write_file_atomically(path, data)
    log.out("  saved to %s" % path)

    cmd = ["sudo", "apt", "install", "-y" if args.yes else "", path]
    cmd = [c for c in cmd if c]
    if not args.install:
        log.out("")
        log.out("To install it:")
        log.out("  sudo apt install %s" % path)
        log.out("Then close and reopen the Blinky window if it is running.")
        return 0

    log.out("")
    log.out("Installing (this needs sudo)")
    try:
        rc = subprocess.call(cmd)
    except OSError as exc:
        log.error("could not run apt: %s" % exc)
        return 1
    if rc:
        log.error("apt exited %d; nothing was changed" % rc)
        return 1
    log.out("Installed blinky %s." % latest)
    log.out("Close and reopen the Blinky window if it is running.")
    return 0


def cmd_delete(args, log, state):
    """Erase the camera. Destructive, irreversible, and always confirmed."""
    since = log.start_wall.strftime("%Y-%m-%d %H:%M:%S")
    cam = None
    try:
        cam = open_camera(args, log)
        numpics = cam.get_numpics()
        if numpics == 0:
            log.out("The camera is already empty.")
            return 0
        entries, _, _ = cam.get_directory(numpics)

        if args.last:
            doomed = entries[-args.last:]
            what = ("the newest photo" if args.last == 1
                    else "the %d newest photos" % args.last)
        else:
            doomed = entries
            what = "all %d photos" % numpics

        log.out("This will erase %s from the camera:" % what)
        for e in doomed:
            log.out("    %s  %s  %d bytes"
                    % (e.basename, "AVI" if e.is_movie else "still",
                       e.data_bytes))
        log.out("")
        log.out("The camera has no undo. Anything not already downloaded is "
                "gone for good.")

        if not args.yes:
            if not sys.stdin.isatty():
                log.error("refusing to erase without confirmation; pass --yes "
                          "if you really mean it")
                return 1
            try:
                reply = input("Type 'erase' to go ahead: ").strip()
            except (EOFError, KeyboardInterrupt):
                log.out("")
                log.out("Cancelled; nothing was erased.")
                return 1
            if reply != "erase":
                log.out("Cancelled; nothing was erased.")
                return 1

        if args.last:
            # The protocol can only pop the newest image, one at a time.
            for n in range(args.last):
                cam.delete_last()
                time.sleep(0.4)
            left = cam.get_numpics()
            log.out("Erased %d; %d photo(s) left on the camera."
                    % (args.last, left))
            if left != numpics - args.last:
                log.warn("expected %d left, the camera reports %d"
                         % (numpics - args.last, left))
                return 1
        else:
            cam.delete_all()
            time.sleep(0.6)
            left = cam.get_numpics()
            if left:
                log.warn("the camera still reports %d photo(s)" % left)
                return 1
            log.out("The camera is now empty.")
        return 0
    except CameraError as exc:
        explain_camera_error(log, exc, since)
        return 1
    finally:
        if cam is not None:
            cam.close()


def cmd_decode(args, log, state):
    state.offline = True
    paths = [os.path.expanduser(p) for p in args.files]
    outdir = os.path.expanduser(args.out) if args.out else None
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    done = failed = skipped = 0
    for path in paths:
        if not os.path.isfile(path):
            log.error("%s: not a file" % path)
            failed += 1
            continue
        base = os.path.splitext(os.path.basename(path))[0]
        target_dir = outdir or os.path.dirname(os.path.abspath(path))
        exts = ((".png",) if args.format == "png"
                else (".jpg",) if args.format == "jpeg" else (".png", ".jpg"))
        existing = [os.path.join(target_dir, base + e) for e in exts
                    if os.path.exists(os.path.join(target_dir, base + e))]
        if existing and not args.force:
            log.out("%s: skipped, %s already exists" % (path, existing[0]))
            skipped += 1
            continue
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            # A clip's raw file holds many frames; decoding only the first
            # would quietly turn a video into a photograph.
            if len(split_clip_frames(data)) > 1:
                avi_path = os.path.join(target_dir, base + ".avi")
                if os.path.exists(avi_path) and not args.force:
                    log.out("%s: skipped, %s already exists" % (path, avi_path))
                    skipped += 1
                    continue
                n, w, h = build_clip(data, avi_path, log, args.fps)
                log.out("%s -> %s  (%d frames at %dx%d)"
                        % (path, avi_path, n, w, h))
                done += 1
                continue
            width, height, raster, partial = decode_still(data, log)
            written = write_still(target_dir, base, width, height, raster,
                                  args.format, args.jpeg_quality)
            log.out("%s -> %s  (%dx%d%s)"
                    % (path, ", ".join(written), width, height,
                       ", partial: the JPEG is damaged" if partial else ""))
            done += 1
        except (CameraError, OSError) as exc:
            log.error("%s: %s" % (path, exc))
            failed += 1
    log.out("")
    log.out("Decoded %d, skipped %d, failed %d." % (done, skipped, failed))
    return 1 if failed else 0


def collect_pnm(paths):
    """Expand files and directories into a sorted list of .pnm/.ppm files."""
    found = []
    for path in paths:
        path = os.path.expanduser(path)
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if name.lower().endswith((".pnm", ".ppm")):
                    found.append(os.path.join(path, name))
        elif os.path.isfile(path):
            found.append(path)
        else:
            raise CameraError("%s does not exist" % path)
    return found


def cmd_convert(args, log, state):
    state.offline = True
    try:
        files = collect_pnm(args.paths)
    except CameraError as exc:
        log.error(str(exc))
        return 1
    if not files:
        log.out("No .pnm files found in: %s" % ", ".join(args.paths))
        return 0

    outdir = os.path.expanduser(args.out) if args.out else None
    if outdir:
        os.makedirs(outdir, exist_ok=True)

    converted = skipped = failed = deleted = 0
    for path in files:
        base = os.path.splitext(os.path.basename(path))[0]
        target_dir = outdir or os.path.dirname(os.path.abspath(path))
        png_path = os.path.join(target_dir, base + ".png")

        if os.path.exists(png_path):
            log.out("%s: skipped, %s already exists (never overwritten)"
                    % (os.path.basename(path), os.path.basename(png_path)))
            skipped += 1
            continue
        try:
            width, height, raster = read_pnm(path)
        except (ValueError, OSError) as exc:
            log.error("%s: %s" % (path, exc))
            failed += 1
            continue
        try:
            write_png(png_path, width, height, raster)
        except Exception as exc:
            log.error("%s: could not write %s: %s" % (path, png_path, exc))
            failed += 1
            continue

        # Verify before we trust it, and certainly before we delete anything.
        try:
            Image = _import_pil()
            with Image.open(png_path) as check:
                check.load()
                check_size = check.size
                check_bytes = check.convert("RGB").tobytes()
        except Exception as exc:
            log.error("%s: wrote %s but could not read it back: %s"
                      % (path, png_path, exc))
            failed += 1
            continue

        if check_size != (width, height) or check_bytes != raster:
            log.error("%s: %s is NOT pixel-identical to the source; removing it"
                      % (path, png_path))
            try:
                os.unlink(png_path)
            except OSError:
                pass
            failed += 1
            continue

        log.out("%s -> %s  (%dx%d, verified pixel-identical)"
                % (os.path.basename(path), os.path.basename(png_path),
                   width, height))
        converted += 1

        if args.delete_originals:
            try:
                os.unlink(path)
                log.out("    deleted %s" % path)
                deleted += 1
            except OSError as exc:
                log.warn("could not delete %s: %s" % (path, exc))

    log.out("")
    log.out("Converted %d, skipped %d, failed %d%s."
            % (converted, skipped, failed,
               ", deleted %d original(s)" % deleted if deleted else ""))
    if not args.delete_originals and converted:
        log.out("Originals kept. Pass --delete-originals to remove verified "
                "sources.")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

COMMON_DEFAULTS = {
    "verbose": False,
    "quiet": False,
    "timeout": DEFAULT_TIMEOUT_MS,
    "retries": DEFAULT_RETRIES,
    "chunk": DEFAULT_CHUNK,
    "report": None,
}


def add_common_options(parser):
    """Options accepted both before and after the subcommand.

    Every default is SUPPRESS so an option left off a subcommand does not
    appear in the subparser's namespace and therefore cannot clobber a value
    the top-level parser already set. main() fills the real defaults in.
    """
    g = parser.add_argument_group("common options")
    g.add_argument("-v", "--verbose", action="store_true",
                   default=argparse.SUPPRESS, help="log every transfer")
    g.add_argument("-q", "--quiet", action="store_true",
                   default=argparse.SUPPRESS,
                   help="suppress progress output (results and errors still "
                        "print)")
    g.add_argument("--timeout", type=int, default=argparse.SUPPRESS,
                   metavar="MS",
                   help="USB transfer timeout in ms (default %d)"
                        % DEFAULT_TIMEOUT_MS)
    g.add_argument("--retries", type=int, default=argparse.SUPPRESS,
                   metavar="N",
                   help="retries per image after the first attempt "
                        "(default %d)" % DEFAULT_RETRIES)
    g.add_argument("--chunk", type=int, default=argparse.SUPPRESS,
                   metavar="BYTES",
                   help="bulk read chunk size (default %d)" % DEFAULT_CHUNK)
    g.add_argument("--report", nargs="?", const="", default=argparse.SUPPRESS,
                   metavar="FILE",
                   help="write checks, kernel log, lsusb -v and the tool log "
                        "to FILE (auto-named if FILE is omitted)")
    return parser


def add_format_options(parser):
    parser.add_argument("--format", choices=("png", "jpeg", "both"),
                        default="png",
                        help="picture format for stills (default png). The "
                             "camera's data is already JPEG, so png is "
                             "lossless from the decoded pixels while jpeg "
                             "re-encodes them a second time")
    parser.add_argument("--jpeg-quality", type=int, default=92, metavar="N",
                        help="JPEG quality 1-100 (default 92)")
    parser.add_argument("--fps", type=float, default=DEFAULT_CLIP_FPS,
                        metavar="N",
                        help="frame rate to record in a clip's AVI (default "
                             "%g, the speed the camera actually records at). "
                             "Raise it for smoother but faster-than-real "
                             "playback" % DEFAULT_CLIP_FPS)
    return parser


def build_parser():
    parser = argparse.ArgumentParser(
        prog="blinky",
        description="Talk to a SiPix StyleCam Blink II (0c77:1011) directly.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  blinky doctor                     run the five link checks, in order
  blinky doctor --report            ... and write a shareable report
  blinky list                       show what is on the camera
  blinky download                   fetch everything to ~/blink-pics
  blinky download --images 0,2-3    fetch selected photos
  blinky download --format both     save PNG and JPEG
  blinky download --delete-after    fetch everything, verify, then erase
  blinky delete --last 1            erase just the newest photo
  blinky decode ~/blink-pics/image0000.raw
  blinky convert ~/blink-pics       batch .pnm -> .png, originals kept
  blinky update                    check GitHub for a newer release
  blinky update --install          ... and install it

Stills are saved twice: the exact bytes from the camera as imageNNNN.raw,
and the decoded picture as imageNNNN.png. Video clips need no decoding, so
imageNNNN.avi is itself the untouched raw data.
""")
    parser.add_argument("--version", action="version",
                        version="blinky %s" % __version__)
    add_common_options(parser)

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = add_common_options(sub.add_parser("doctor",
                                          help="diagnose the camera link"))
    p.set_defaults(func=cmd_doctor)

    p = add_common_options(sub.add_parser(
        "list", help="list the photos on the camera"))
    p.set_defaults(func=cmd_list)

    p = add_common_options(sub.add_parser("download", help="download photos"))
    p.add_argument("--out", default=DEFAULT_OUTDIR, metavar="DIR",
                   help="output directory (default %s)" % DEFAULT_OUTDIR)
    p.add_argument("--images", metavar="SPEC",
                   help="which photos, e.g. 0,2-4 (default: all)")
    p.add_argument("--force", action="store_true",
                   help="re-download and overwrite existing files")
    add_format_options(p)
    p.add_argument("--no-raw-subfolder", action="store_true",
                   help="keep the .raw files beside the pictures instead of "
                        "in a raw/ subfolder")
    p.add_argument("--naming", choices=("continue", "camera"),
                   default="continue",
                   help="'continue' (default) numbers new photos on from the "
                        "highest already in the output folder, because the "
                        "camera restarts at zero after an erase; 'camera' "
                        "uses the camera's own index and suffixes on a clash")
    p.add_argument("--no-skip-duplicates", action="store_true",
                   help="do not check whether a photo is already in the "
                        "output folder by content; fall back to matching "
                        "filenames only")
    p.add_argument("--delete-after", action="store_true",
                   help="erase the camera once every photo is downloaded and "
                        "verified on disk (refused if anything failed, or if "
                        "--images selected only some of them)")
    p.set_defaults(func=cmd_download)

    p = add_common_options(sub.add_parser(
        "update", help="check GitHub for a newer release"))
    p.add_argument("--check", action="store_true",
                   help="only report whether one is available")
    p.add_argument("--install", action="store_true",
                   help="install it once downloaded (runs sudo apt)")
    p.add_argument("--yes", action="store_true",
                   help="with --install, do not prompt")
    p.add_argument("--out", metavar="DIR",
                   help="where to put the download (default: a temp folder)")
    p.set_defaults(func=cmd_update)

    p = add_common_options(sub.add_parser(
        "delete", help="erase photos from the camera"))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true",
                   help="erase every photo (the default)")
    g.add_argument("--last", type=int, metavar="N",
                   help="erase only the N newest photos")
    p.add_argument("--yes", action="store_true",
                   help="skip the confirmation prompt")
    p.set_defaults(func=cmd_delete)

    p = add_common_options(sub.add_parser(
        "decode", help="decode saved .raw files to PNG or JPEG"))
    p.add_argument("files", nargs="+", metavar="RAW")
    p.add_argument("--out", metavar="DIR", help="output directory")
    p.add_argument("--force", action="store_true",
                   help="overwrite existing pictures")
    add_format_options(p)
    p.set_defaults(func=cmd_decode)

    p = add_common_options(sub.add_parser(
        "convert", help="batch-convert .pnm files to PNG"))
    p.add_argument("paths", nargs="*", default=[DEFAULT_OUTDIR],
                   metavar="PATH",
                   help="files or directories (default %s)" % DEFAULT_OUTDIR)
    p.add_argument("--out", metavar="DIR",
                   help="write PNGs here instead of beside the sources")
    p.add_argument("--delete-originals", action="store_true",
                   help="delete each .pnm after its PNG is verified identical")
    p.set_defaults(func=cmd_convert)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    for key, value in COMMON_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    if not args.command:
        parser.print_help()
        return 2
    if args.timeout <= 0 or args.chunk <= 0 or args.retries < 0:
        parser.error("--timeout and --chunk must be positive, --retries >= 0")
    if getattr(args, "jpeg_quality", 92) not in range(1, 101):
        parser.error("--jpeg-quality must be between 1 and 100")
    if not 0 < getattr(args, "fps", 1) <= 120:
        parser.error("--fps must be above 0 and at most 120")
    if getattr(args, "last", None) is not None and args.last < 1:
        parser.error("--last must be 1 or more")

    log = Log(verbose=args.verbose, quiet=args.quiet)
    state = RunState()
    log.debug("blinky %s, argv: %s" % (__version__, " ".join(sys.argv)))

    try:
        rc = args.func(args, log, state)
    except KeyboardInterrupt:
        log.error("interrupted")
        rc = 130
    except CameraError as exc:
        log.error(str(exc))
        rc = 1

    if args.report is not None:
        path = args.report or ("blinky-report-%s.txt"
                               % log.start_wall.strftime("%Y%m%d-%H%M%S"))
        path = os.path.expanduser(path)
        try:
            write_report(path, log, state, timeout=args.timeout)
            print("\nReport written to %s" % path)
        except OSError as exc:
            print("\nerror: could not write the report to %s: %s"
                  % (path, exc), file=sys.stderr)
            rc = rc or 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
