"""Direct CXLMemSim TCP client. Real bytes and server CAS; no file locks.

The server reports modeled nanoseconds but does not delay each request by that
amount. Those counters are diagnostics, never a predicted application runtime.
Timestamps use wall-clock ns (host clocks need not be synchronized); comparisons
do not infer cross-host ordering from them. TCP order governs each connection.
"""
import contextlib
import hashlib
import socket
import struct
import threading
import time

REQ = struct.Struct('<BQQQQQ64s')
RESP = struct.Struct('<BQQ64s')
INFO = struct.Struct('<BQQQ256s')
HEADER = struct.Struct('<QQ32s16x')


class Client:
    def __init__(self, port, host='127.0.0.1'):
        self.sock = socket.create_connection((host, port), timeout=120)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.sendall(REQ.pack(2, 0, 0, 0, 0, 0, b''))
        status, self.base, backing_capacity, lines, name = INFO.unpack(self.recv(INFO.size))
        # GET_SHM_INFO includes the server header in its backing size. Only
        # complete data cachelines are addressable; the final line must not
        # wrap through the server's modulo addressing.
        self.capacity = min(backing_capacity, lines * 64)
        if status or self.base != 0:
            raise RuntimeError('this adapter requires a zero-base server')
        self.info = dict(base=self.base, capacity=self.capacity, cachelines=lines,
                         backing_capacity=backing_capacity,
                         backing=name.rstrip(b'\0').decode())
        self.stats = dict(requests=0, read_bytes=0, write_bytes=0, cas=0,
                          cas_failed=0, modeled_latency_ns_sum=0, io_s=0.)

    def recv(self, size):
        buf = bytearray(size)
        view = memoryview(buf)
        done = 0
        while done < size:
            n = self.sock.recv_into(view[done:])
            if not n:
                raise EOFError('CXLMemSim connection closed')
            done += n
        return buf

    def account(self, result):
        status, latency, old, data = result
        if status:
            raise RuntimeError('CXLMemSim status {}'.format(status))
        self.stats['requests'] += 1
        self.stats['modeled_latency_ns_sum'] += latency
        return old, data

    def call(self, op, addr, size=8, value=0, expected=0, data=b''):
        if addr < 0 or addr + size > self.capacity:
            raise ValueError('address outside allocated simulator capacity')
        start = time.perf_counter()
        self.sock.sendall(REQ.pack(op, addr, size, time.time_ns(), value, expected, data))
        result = self.account(RESP.unpack(self.recv(RESP.size)))
        self.stats['io_s'] += time.perf_counter()-start
        return result

    def transfer(self, addr, data=None, size=None):
        """Bounded 256-KiB batches, simultaneous send/drain to avoid deadlock."""
        write = data is not None
        size = len(data) if write else size
        if addr % 64 or size <= 0 or addr < 0 or addr+size > self.capacity:
            raise ValueError('unaligned or out-of-range transfer')
        start = time.perf_counter()
        result = bytearray() if not write else None
        for chunk in range(0, size, 256*1024):
            end = min(size, chunk+256*1024)
            requests = bytearray()
            sizes = []
            ts = time.time_ns()
            for off in range(chunk, end, 64):
                count = min(64, end-off)
                sizes.append(count)
                requests.extend(REQ.pack(int(write), addr+off, count, ts, 0, 0,
                                         data[off:off+count] if write else b''))
            errors = []
            def send():
                try:
                    self.sock.sendall(requests)
                except BaseException as e:
                    errors.append(e)
                    self.sock.shutdown(socket.SHUT_RDWR)
            sender = threading.Thread(target=send)
            sender.start()
            try:
                raw = self.recv(RESP.size*len(sizes))
            finally:
                sender.join(125)
            if sender.is_alive():
                self.sock.close()
                raise TimeoutError('simulator send did not drain')
            if errors:
                raise errors[0]
            for i, count in enumerate(sizes):
                _, payload = self.account(RESP.unpack_from(raw, i*RESP.size))
                if not write:
                    result.extend(payload[:count])
        self.stats['write_bytes' if write else 'read_bytes'] += size
        self.stats['io_s'] += time.perf_counter()-start
        return result

    def cas(self, addr, expected, value):
        old, _ = self.call(4, addr, value=value, expected=expected)
        self.stats['cas'] += 1
        self.stats['cas_failed'] += int(old != expected)
        return old

    @contextlib.contextmanager
    def lock(self, addr, token, timeout=120):
        deadline = time.monotonic()+timeout
        while self.cas(addr, 0, token) != 0:
            if time.monotonic() >= deadline:
                raise TimeoutError('CXL CAS lock acquisition')
            time.sleep(.001)
        try:
            yield
        finally:
            if self.cas(addr, token, 0) != token:
                raise RuntimeError('CXL CAS lock ownership lost')

    def publish(self, desc, raw, token):
        if len(raw) != desc['size']:
            raise ValueError('publication length mismatch')
        digest = hashlib.sha256(raw).digest()
        with self.lock(desc['lock'], token):
            ready, _, _ = HEADER.unpack(self.transfer(desc['header'], size=64))
            if ready:
                raise ValueError('immutable object already published')
            self.transfer(desc['addr'], data=raw)
            self.transfer(desc['header'], data=HEADER.pack(desc['version'], len(raw), digest))
        return digest.hex()

    def fetch(self, desc, token):
        with self.lock(desc['lock'], token):
            ready, size, digest = HEADER.unpack(self.transfer(desc['header'], size=64))
        if ready != desc['version'] or size != desc['size']:
            raise ValueError('object is not ready or has wrong length')
        # The coordinator keeps the DSec pin until the acknowledged task exits.
        # Published objects are immutable; there is no concurrent overwrite.
        raw = self.transfer(desc['addr'], size=size)
        if hashlib.sha256(raw).digest() != digest:
            raise ValueError('payload hash mismatch')
        return raw

    def close(self):
        self.sock.close()
