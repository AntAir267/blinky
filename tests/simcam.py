"""A simulated Blink II, so the protocol, retry, decode and download
paths can be tested with no camera attached."""
import array, io, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import usb.core
import blinky
from blinky import Blink2, Log

class FakeEP:
    bEndpointAddress = 0x82
    wMaxPacketSize = 64
    bmAttributes = 2
    def __init__(self, cam): self.cam = cam
    def read(self, size, timeout=None):
        c = self.cam
        if c.fail_reads:
            c.fail_reads -= 1
            raise usb.core.USBTimeoutError("simulated timeout", None, None)
        if not c.pending:
            if timeout is not None and timeout <= 200:      # drain() probe
                raise usb.core.USBTimeoutError("empty", None, None)
            return array.array('B', b'')
        n = min(size, len(c.pending))
        if c.truncate_at is not None and c.served + n > c.truncate_at:
            n = max(0, c.truncate_at - c.served)
            c.pending = b''                                  # camera gives up
        out, c.pending = c.pending[:n], c.pending[n:]
        c.served += len(out)
        return array.array('B', out)

class FakeDev:
    bus, address = 3, 9
    def __init__(self, cam): self.cam = cam
    def ctrl_transfer(self, rt, req, val, idx, data, timeout=None):
        c = self.cam
        if rt == 0x80 and req == 0x06:  return array.array('B', bytes(18))
        if rt == blinky.CTRL_IN:
            if req == blinky.REQ_GET_FIRMWARE_ID: return array.array('B', b'\x01\x02\x03\x04\x05\x06')
            if req == blinky.REQ_INIT_STILL:      return array.array('B', b'\x00')
            if req == blinky.REQ_GET_NUMPICS:
                n = len(c.blobs); return array.array('B', bytes([n >> 8, n & 0xff]))
            if req == blinky.REQ_GET_DIR:
                c.pending, c.served = c.table, 0; return array.array('B', b'\x00')
            if req == blinky.REQ_DELETE_ALL:
                c.blobs = []; c.rebuild(); return array.array('B', b'\x01')
            if req == blinky.REQ_DELETE_LAST:
                if c.blobs: c.blobs = c.blobs[:-1]
                c.rebuild(); return array.array('B', b'\x01')
            raise AssertionError("unexpected control read 0x%02x" % req)
        if rt == blinky.CTRL_OUT and req == blinky.REQ_GET_MEMORY:
            b = bytes(data)
            start = int.from_bytes(b[0:4], 'big'); ln = int.from_bytes(b[4:8], 'big')
            idx = c.starts.index(start)
            full = c.blobs[idx][0]
            # A short length asks for a prefix, exactly as the hardware does.
            want = ln * 8
            assert want <= len(full), "over-long request for image %d" % idx
            c.pending, c.served = full[:want], 0
            return 8
        raise AssertionError("unexpected control transfer")

class SimCam:
    """blobs: list of (data, is_movie). Each blob must be a multiple of 8 bytes."""
    def __init__(self, blobs):
        self.blobs = blobs
        self.fail_reads = 0
        self.truncate_at = None
        self.pending, self.served = b'', 0
        self.rebuild()

    def rebuild(self):
        """Recompute the directory table, e.g. after a delete."""
        addr, self.starts = 0x001000, []
        ends = []
        for data, _ in self.blobs:
            assert len(data) % 8 == 0
            self.starts.append(addr); addr += len(data) // 2; ends.append(addr)
        n = len(self.blobs)
        t = bytearray(8 * (n + 1))
        for i in range(n):
            t[8*i+5:8*i+8] = self.starts[i].to_bytes(3, 'big')
            t[8*(i+1)+5:8*(i+1)+8] = ends[i].to_bytes(3, 'big')
            t[8*(i+1)] = 1 if self.blobs[i][1] else 0
        self.table = bytes(t)

def make_cam(blobs, log=None, **kw):
    log = log or Log(quiet=True)
    sim = SimCam(blobs)
    cam = Blink2(log, **kw)
    cam.dev = FakeDev(sim); cam.in_ep = FakeEP(sim); cam.max_packet = 64
    cam.sim = sim
    return cam, sim

def make_jpeg(w=640, h=240, seed=7):
    from PIL import Image
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 7 + seed) % 256, (y * 5 + seed) % 256, (x ^ y) % 256)
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=90)
    d = buf.getvalue()
    return d + bytes((-len(d)) % 8)          # pad to a multiple of 8
