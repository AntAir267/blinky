#!/usr/bin/env python3
"""Everything that can be checked without a camera attached.

Run with the project venv:   .venv/bin/python tests/test_blinky.py
"""
import argparse
import array
import contextlib
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from simcam import make_cam, make_jpeg           # noqa: E402
import usb.core                                  # noqa: E402
import blinky                                    # noqa: E402
from blinky import Log, CameraError, DeviceBusy  # noqa: E402

FAILURES = []
CURRENT = [""]


def section(name):
    CURRENT[0] = name
    print("\n%s" % name)


def check(name, cond, extra=""):
    print("  %-54s %s%s" % (name, "ok" if cond else "FAIL",
                            (" -- " + str(extra)) if extra and not cond else ""))
    if not cond:
        FAILURES.append("%s / %s" % (CURRENT[0], name))


def quiet(fn, *a, **kw):
    """Run fn capturing stdout, returning (result, stdout)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()


# ---------------------------------------------------------------------------
# Decoder, checked against real camera output
# ---------------------------------------------------------------------------

def test_decoder_against_real_pnm(sample):
    section("decoder vs. a PNM that gphoto2 produced from the real camera")
    w, h, raster = blinky.read_pnm(sample)
    check("sample is 640x480", (w, h) == (640, 480), (w, h))
    rows = [raster[y * w * 3:(y + 1) * w * 3] for y in range(h)]

    bad = sum(1 for row in rows for i in range(0, w, 2)
              if row[i * 3:i * 3 + 3] != row[(i + 1) * 3:(i + 1) * 3 + 3])
    check("every horizontal pixel pair is identical (2x upscale)", bad == 0, bad)

    # Invert the field mapping to rebuild the JPEG scanline the camera must
    # have sent, then check the decoder turns it back into the same raster.
    jw, jh = 640, 240
    pitch = (jw * 3 + 3) & ~3
    nb = jw // 16
    undouble = lambda row: b"".join(row[i * 3:i * 3 + 3] for i in range(0, w, 2))
    rebuilt = bytearray()
    for y in range(jh):
        fa, fb = undouble(rows[2 * y]), undouble(rows[2 * y + 1])
        src = bytearray(jw * 3)
        for m in range(nb // 2):
            src[(32 * m + 8) * 3:(32 * m + 24) * 3] = fa[(16 * m) * 3:(16 * m + 16) * 3]
        src[0:24] = fb[0:24]
        for m in range(nb // 2 - 1):
            src[(32 * m + 24) * 3:(32 * m + 40) * 3] = fb[(16 * m + 8) * 3:(16 * m + 24) * 3]
        src[(jw - 8) * 3:jw * 3] = fb[(jw // 2 - 8) * 3:(jw // 2) * 3]
        rebuilt += blinky.deinterleave_scanline(bytes(src), jw, pitch)
    check("decoder reproduces all %d raster bytes" % len(raster),
          bytes(rebuilt) == raster)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

def test_protocol(blobs):
    section("handshake, directory parsing and image fetch")
    cam, sim = make_cam(blobs)
    check("firmware ID is 6 bytes", len(cam.firmware_id()) == 6)
    check("photo count", cam.get_numpics() == len(blobs))
    entries, table, expected = cam.get_directory()
    check("table is 8*(numpics+1) bytes",
          len(table) == expected == 8 * (len(blobs) + 1), len(table))
    check("byte counts match", [e.data_bytes for e in entries] ==
          [len(b[0]) for b in blobs], [e.data_bytes for e in entries])
    check("movie flags", [e.is_movie for e in entries] == [b[1] for b in blobs])
    check("every end is at or after its start",
          all(e.end >= e.start for e in entries))
    for i, e in enumerate(entries):
        check("image %d round-trips byte for byte" % i,
              cam.read_image(e) == blobs[i][0])
    return entries


def test_retries(blobs, entries):
    section("retry, truncation and malformed-directory handling")
    log = Log(quiet=True)
    cam, sim = make_cam(blobs, log=log, retries=3)
    sim.fail_reads = 3
    check("recovers after a mid-transfer timeout",
          cam.read_image_with_retries(entries[0]) == blobs[0][0])
    t = log.transcript()
    check("the timeout was logged", "timeout after" in t)
    check("the retry was logged", "retry 1/3" in t, t[-200:])

    cam, sim = make_cam(blobs, log=Log(quiet=True), retries=0)
    sim.truncate_at = 1000
    try:
        cam.read_image(entries[0])
        check("a truncated transfer raises", False, "no exception")
    except CameraError as exc:
        check("a truncated transfer raises", "short image read" in str(exc), exc)

    cam, sim = make_cam(blobs)
    t = bytearray(sim.table)
    t[8 + 5:8 + 8] = (0x000100).to_bytes(3, "big")      # end before start
    sim.table = bytes(t)
    try:
        cam.get_directory()
        check("a backwards entry raises", False, "no exception")
    except CameraError as exc:
        check("a backwards entry raises", "before start" in str(exc), exc)

    cam, sim = make_cam(blobs)
    sim.table = sim.table[:16]
    try:
        cam.get_directory()
        check("a short directory table raises", False, "no exception")
    except CameraError as exc:
        check("a short directory table raises",
              "directory table is" in str(exc), exc)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def dl_args(out, **kw):
    base = dict(out=out, images=None, force=False, timeout=5000, retries=3,
                chunk=4096, verbose=False, quiet=True)
    base.update(kw)
    return argparse.Namespace(**base)


def test_download(blobs):
    section("download: raw first, then PNG; clips left as AVI")
    tmp = tempfile.mkdtemp()
    cam, sim = make_cam(blobs)
    blinky.open_camera = lambda a, l: cam
    rc, out = quiet(blinky.cmd_download, dl_args(tmp), Log(quiet=True),
                    blinky.RunState())
    got = sorted(os.listdir(tmp))
    check("exit 0", rc == 0, rc)
    check("raw + png per still, avi per clip",
          got == ["image0000.png", "image0000.raw", "image0001.avi"], got)
    check("the .raw is the camera's exact bytes",
          open(os.path.join(tmp, "image0000.raw"), "rb").read() == blobs[0][0])
    check("the .avi is the untouched raw data",
          open(os.path.join(tmp, "image0001.avi"), "rb").read() == blobs[1][0])
    from PIL import Image
    with Image.open(os.path.join(tmp, "image0000.png")) as im:
        check("PNG is 640x480 from a 640x240 JPEG", im.size == (640, 480), im.size)
    check("no .part files left behind", not any(f.endswith(".part") for f in got))
    check("-q leaves only the summary on stdout",
          "Done: 3 file(s) written" in out and "saved raw" not in out, repr(out))

    rc, _ = quiet(blinky.cmd_download, dl_args(tmp), Log(quiet=True),
                  blinky.RunState())
    check("re-running skips rather than overwriting",
          rc == 0 and len(os.listdir(tmp)) == 3, os.listdir(tmp))

    tmp2 = tempfile.mkdtemp()
    cam, sim = make_cam(blobs)
    blinky.open_camera = lambda a, l: cam
    quiet(blinky.cmd_download, dl_args(tmp2, images="1"), Log(quiet=True),
          blinky.RunState())
    check("--images selects", sorted(os.listdir(tmp2)) == ["image0001.avi"],
          sorted(os.listdir(tmp2)))
    try:
        blinky.parse_image_spec("9", 2)
        check("an out-of-range --images is rejected", False)
    except CameraError as exc:
        check("an out-of-range --images is rejected", "out of range" in str(exc))
    shutil.rmtree(tmp)
    shutil.rmtree(tmp2)


def test_list(blobs):
    section("list")
    cam, sim = make_cam(blobs)
    blinky.open_camera = lambda a, l: cam
    rc, out = quiet(blinky.cmd_list,
                    argparse.Namespace(timeout=5000, retries=3, chunk=4096),
                    Log(), blinky.RunState())
    check("exit 0", rc == 0)
    check("shows the firmware ID", "01 02 03 04 05 06" in out, out)
    check("shows the photo count", "photos      : 2" in out, out)
    check("shows the expected table size", "expected 8*(2+1) = 24" in out, out)
    check("maps still -> .png and clip -> .avi",
          "image0000.png" in out and "image0001.avi" in out, out)


def test_convert(sample):
    section("convert: verify, never overwrite, keep originals")
    tmp = tempfile.mkdtemp()
    shutil.copy(sample, os.path.join(tmp, "a.pnm"))
    open(os.path.join(tmp, "junk.pnm"), "wb").write(b"P6\n640 480 255\nshort")
    args = lambda **kw: argparse.Namespace(
        **{**dict(paths=[tmp], out=None, delete_originals=False), **kw})

    rc, _ = quiet(blinky.cmd_convert, args(), Log(quiet=True), blinky.RunState())
    check("exit 1 because the malformed file failed", rc == 1, rc)
    check("good file converted, bad one produced no PNG",
          sorted(os.listdir(tmp)) == ["a.png", "a.pnm", "junk.pnm"],
          sorted(os.listdir(tmp)))

    before = os.path.getmtime(os.path.join(tmp, "a.png"))
    os.remove(os.path.join(tmp, "junk.pnm"))
    rc, _ = quiet(blinky.cmd_convert, args(), Log(quiet=True), blinky.RunState())
    check("an existing PNG is never overwritten",
          rc == 0 and os.path.getmtime(os.path.join(tmp, "a.png")) == before)

    tmp2 = tempfile.mkdtemp()
    shutil.copy(sample, os.path.join(tmp2, "c.pnm"))
    real = blinky.write_png
    blinky.write_png = lambda p, w, h, r: real(p, w, h, bytes(len(r)))
    rc, _ = quiet(blinky.cmd_convert, args(paths=[tmp2]), Log(quiet=True),
                  blinky.RunState())
    blinky.write_png = real
    check("a PNG that is not pixel-identical is detected", rc == 1)
    check("the bad PNG is removed", not os.path.exists(os.path.join(tmp2, "c.png")),
          os.listdir(tmp2))
    check("the original survives", os.path.exists(os.path.join(tmp2, "c.pnm")))

    rc, _ = quiet(blinky.cmd_convert, args(paths=[tmp2], delete_originals=True),
                  Log(quiet=True), blinky.RunState())
    check("--delete-originals removes only a verified source",
          rc == 0 and sorted(os.listdir(tmp2)) == ["c.png"], os.listdir(tmp2))

    tmp3, out = tempfile.mkdtemp(), tempfile.mkdtemp()
    shutil.copy(sample, os.path.join(tmp3, "d.pnm"))
    rc, _ = quiet(blinky.cmd_convert, args(paths=[tmp3], out=out),
                  Log(quiet=True), blinky.RunState())
    check("--out redirects and leaves the source alone",
          rc == 0 and os.listdir(out) == ["d.png"] and os.listdir(tmp3) == ["d.pnm"],
          (os.listdir(out), os.listdir(tmp3)))
    for d in (tmp, tmp2, tmp3, out):
        shutil.rmtree(d)


def test_decode_command():
    section("decode")
    tmp = tempfile.mkdtemp()
    raw = os.path.join(tmp, "image0009.raw")
    open(raw, "wb").write(make_jpeg())
    a = argparse.Namespace(files=[raw], out=tmp, force=False)
    rc, _ = quiet(blinky.cmd_decode, a, Log(quiet=True), blinky.RunState())
    check("exit 0", rc == 0)
    from PIL import Image
    with Image.open(os.path.join(tmp, "image0009.png")) as im:
        check("PNG is 640x480", im.size == (640, 480), im.size)
    rc, out = quiet(blinky.cmd_decode, a, Log(quiet=True), blinky.RunState())
    check("skips an existing PNG", rc == 0 and "skipped" in out, out)
    bogus = os.path.join(tmp, "bogus.raw")
    open(bogus, "wb").write(os.urandom(500))
    rc, _ = quiet(blinky.cmd_decode,
                  argparse.Namespace(files=[bogus], out=tmp, force=False),
                  Log(quiet=True), blinky.RunState())
    check("non-JPEG data fails cleanly", rc == 1)
    shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def test_doctor(blobs):
    real_find, real_access = usb.core.find, os.access
    real_open, real_holding = blinky.Blink2.open, blinky.processes_holding

    def install(open_behaviour=None, holders=None, access=True):
        cam, sim = make_cam(blobs)
        usb.core.find = lambda **kw: cam.dev
        os.access = (lambda p, m: access if str(p).startswith("/dev/bus/usb")
                     else real_access(p, m))

        def fake_open(self, handshake=True):
            if open_behaviour:
                raise open_behaviour
            self.dev, self.in_ep, self.max_packet = cam.dev, cam.in_ep, 64
            return self
        blinky.Blink2.open = fake_open
        blinky.processes_holding = lambda node: (holders or [], 0)
        return cam, sim

    def restore():
        usb.core.find, os.access = real_find, real_access
        blinky.Blink2.open, blinky.processes_holding = real_open, real_holding

    try:
        section("doctor: all five checks pass")
        install()
        results, cam = blinky.run_checks(Log(quiet=True))
        check("five checks ran", len(results) == 5, len(results))
        check("all passed", all(r.ok for r in results),
              [(r.number, r.status) for r in results])
        check("check 4 is the handshake",
              "firmware ID reads 6 bytes" in results[3].title, results[3].title)
        check("check 5 is the directory table",
              "8*(numpics+1)" in results[4].title, results[4].title)
        check("check 5 lists both entries",
              sum(1 for d in results[4].detail if "image000" in d) == 2,
              results[4].detail)

        section("doctor: check 3 names the process holding the device")
        install(holders=[(4242, "gvfsd-gphoto2", "/usr/libexec/gvfsd-gphoto2")])
        results, cam = blinky.run_checks(Log(quiet=True))
        r = results[2]
        check("stops at check 3", len(results) == 3 and not r.ok, len(results))
        check("names the process", any("gvfsd-gphoto2" in d for d in r.detail))
        check("names the pid", any("4242" in d for d in r.detail))
        check("fix mentions gio mount -u", "gio mount -u" in (r.fix or ""), r.fix)

        section("doctor: check 3 on EBUSY with nothing visible in /proc")
        install(open_behaviour=DeviceBusy("busy: another process has it"))
        results, cam = blinky.run_checks(Log(quiet=True))
        r = results[2]
        check("stops at check 3", len(results) == 3 and not r.ok)
        check("falls back to fuser", "fuser -v" in (r.fix or ""), r.fix)
        check("does not invent a suspect from a command line",
              "bash" not in "".join(r.detail), r.detail)

        section("doctor: check 2 points at the udev rule")
        install(access=False)
        results, cam = blinky.run_checks(Log(quiet=True))
        r = results[1]
        check("stops at check 2", len(results) == 2 and not r.ok, len(results))
        check("names the rule file", blinky.UDEV_RULE_PATH in (r.fix or ""))

        section("doctor: check 4 on a short handshake")
        cam, sim = install()
        orig = cam.dev.ctrl_transfer

        def short_fw(rt, req, val, idx, data, timeout=None):
            if rt == blinky.CTRL_IN and req == blinky.REQ_GET_FIRMWARE_ID:
                return array.array("B", b"\x01\x02")
            return orig(rt, req, val, idx, data, timeout)
        cam.dev.ctrl_transfer = short_fw
        results, c = blinky.run_checks(Log(quiet=True))
        check("stops at check 4", len(results) == 4 and not results[3].ok,
              len(results))
        check("says how many bytes came back",
              any("2 byte" in d for d in results[3].detail), results[3].detail)
        check("fix points at journalctl", "journalctl -k" in (results[3].fix or ""))

        section("doctor: check 5 on a truncated directory table")
        cam, sim = install()
        sim.table = sim.table[:8]
        results, c = blinky.run_checks(Log(quiet=True))
        check("stops at check 5", len(results) == 5 and not results[4].ok,
              len(results))
        check("explains the 8*(numpics+1) expectation",
              "8*(numpics+1)" in (results[4].explanation or ""))

        section("--report")
        install()
        log = Log(quiet=True)
        log.info("a log line from the run")
        state = blinky.RunState()
        state.results, cam = blinky.run_checks(log)
        path = os.path.join(tempfile.mkdtemp(), "report.txt")
        blinky.write_report(path, log, state)
        body = open(path).read()
        for want in ["CHECK RESULTS", "KERNEL LOG", "lsusb -v -d 0c77:1011",
                     "TOOL LOG", "a log line from the run", "pyusb", "Pillow",
                     "[PASS] 1."]:
            check("report contains %r" % want, want in body)
        check("report is substantial", len(body) > 2000, len(body))
        shutil.rmtree(os.path.dirname(path))
    finally:
        restore()


def test_misc():
    section("miscellaneous")
    real = blinky._run
    blinky._run = lambda cmd, timeout=20: (False, "journalctl: No journal files.\n")
    ok, msg = blinky.kernel_log()
    blinky._run = real
    check("an unreadable kernel log yields a plain string",
          ok is False and "['" not in msg, msg)
    check("it quotes the reason", "No journal files." in msg, msg)

    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "x.bin")
    try:
        blinky.write_file_atomically(p, b"hello")
        check("atomic write lands", open(p, "rb").read() == b"hello")
        check("no .part left", not os.path.exists(p + ".part"))
    finally:
        shutil.rmtree(tmp)

    check("--images parses ranges", blinky.parse_image_spec("0,2-4", 5) == [0, 2, 3, 4])
    check("--images dedupes", blinky.parse_image_spec("1,1,1", 5) == [1])
    check("--images None means all", blinky.parse_image_spec(None, 3) == [0, 1, 2])


def main():
    sample = os.path.expanduser("~/blink-pics/image0000.pnm")
    blobs = [(make_jpeg(), False), (bytes(2048), True)]

    if os.path.exists(sample):
        test_decoder_against_real_pnm(sample)
        test_convert(sample)
    else:
        print("\n(skipping the real-PNM tests: %s is not there)" % sample)

    entries = test_protocol(blobs)
    test_retries(blobs, entries)
    test_download(blobs)
    test_list(blobs)
    test_decode_command()
    test_doctor(blobs)
    test_misc()

    print("\n%s" % ("-" * 60))
    if FAILURES:
        print("%d FAILURE(S):" % len(FAILURES))
        for f in FAILURES:
            print("  %s" % f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
