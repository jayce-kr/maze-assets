# connector.py
import struct
import threading
import queue
from time import time, monotonic, sleep

import numpy as np
import serial

SYNC0 = 0xAA
SYNC1 = 0xBB
HEADER_FMT = "<BBHIbH"        # sync0, sync1, seq, ts_us, rssi, len
HEADER_LEN = struct.calcsize(HEADER_FMT)  # 11
MAX_CSI_LEN = 384


class FrameParser:
    """바이트 스트림 → 검증된 프레임 dict 리스트. I/O 없음."""

    def __init__(self, on_stat=None):
        self.buf = bytearray()
        self.bad_cs = 0
        self.resyncs = 0

    def feed(self, data: bytes):
        """data를 버퍼에 추가하고, 완성된 프레임 dict들의 리스트 반환."""
        self.buf.extend(data)
        frames = []
        while True:
            # 1. SYNC0 찾기
            idx = self.buf.find(SYNC0)
            if idx < 0:
                self.buf.clear()
                break
            if idx > 0:
                del self.buf[:idx]
                self.resyncs += 1

            # 2. 헤더 확보
            if len(self.buf) < HEADER_LEN:
                break
            if self.buf[1] != SYNC1:
                del self.buf[:1]
                continue

            # 3. 헤더 파싱
            _, _, seq, ts_us, rssi, length = struct.unpack_from(
                HEADER_FMT, self.buf, 0)
            if length == 0 or length > MAX_CSI_LEN:
                del self.buf[:1]
                self.resyncs += 1
                continue

            total = HEADER_LEN + length + 1
            if len(self.buf) < total:
                break

            # 4. 체크섬 검증 (offset 2 .. HEADER_LEN+length-1 의 XOR)
            cs = 0
            for b in self.buf[2:HEADER_LEN + length]:
                cs ^= b
            if cs != self.buf[HEADER_LEN + length]:
                self.bad_cs += 1
                del self.buf[:1]   # sync 1바이트만 버려 재동기화
                continue

            # 5. payload 추출
            payload = bytes(self.buf[HEADER_LEN:HEADER_LEN + length])
            del self.buf[:total]

            csi = np.frombuffer(payload, dtype=np.int8).reshape(-1, 2)
            frames.append({
                "seq": seq, "ts_us": ts_us, "rssi": rssi, "csi": csi,
                "ts_pc": time(),
            })
        return frames


class SerialReaderThread(threading.Thread):
    def __init__(self, ser, out_queue, drop_oldest=True, debug=False):
        super().__init__(daemon=True)
        self.ser = ser
        self.q = out_queue
        self.parser = FrameParser()
        self.stop_event = threading.Event()
        self.drop_oldest = drop_oldest
        self.dropped = 0
        self.received = 0
        self.stat_lines = []   # '#'로 시작하는 라인 모음 (옵션)
        self.debug = debug

    def run(self):
        while not self.stop_event.is_set():
            try:
                data = self.ser.read(4096)  # timeout 만큼 블로킹
            except serial.SerialException:
                break
            if not data:
                continue

            if self.debug:
                print(f"[DBG raw] {len(data)}B | hex: {data[:16].hex(' ')}")

            for f in self.parser.feed(data):
                self.received += 1
                if self.drop_oldest and self.q.full():
                    try:
                        self.q.get_nowait()
                    except queue.Empty:
                        pass
                    self.dropped += 1
                self.q.put(f)

    def stop(self):
        self.stop_event.set()


class WiFiDataProcessor:
    def __init__(self, port, baudrate=115200, queue_size=200, debug=False):
        self.ser = serial.Serial(
            port, baudrate, timeout=0.05,  # 짧은 timeout — read가 자주 깨어남
            dsrdtr=False, rtscts=False,
        )
        print(f"Serial Opened: {port} @ {baudrate}")
        sleep(2)
        self.ser.reset_input_buffer()
        self.queue = queue.Queue(maxsize=queue_size)
        self.reader = SerialReaderThread(self.ser, self.queue, debug=debug)
        self.reader.start()

        print("Waiting for first packet...")
        first = self.queue.get(timeout=30.0)  # 최대 10초 대기
        self.queue.put(first)  # 다시 넣어둠 (버리지 않음)
        print("First packet received. Ready!")

    def close(self):
        self.reader.stop()
        self.reader.join(timeout=2)
        if self.ser.is_open:
            self.ser.close()
        print("Serial Closed!")

    def clear_buffer(self):
        self.ser.reset_input_buffer()
        with self.queue.mutex:
            self.queue.queue.clear()

    def sync_start(self, number_to_ignore=10):
        self.clear_buffer()
        for i in range(number_to_ignore):
            self.queue.get(timeout=2.0)
            # print(f"DBG: Packet #{i} Ignored!")

    def get_wifi_data_dict(self, timeout=2.0):
        return self.queue.get(timeout=timeout)

    def collect_wifi_data_for_duration(self, duration_sec):
        collected = []
        t0 = monotonic()
        deadline = t0 + duration_sec + 0.5
        while monotonic() < deadline:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            try:
                collected.append(self.queue.get(timeout=remaining))
            except queue.Empty:
                break
        print(f"Collection complete. {len(collected)} packets collected. "
              f"(reader rx={self.reader.received}, drop={self.reader.dropped}, "
              f"bad_cs={self.reader.parser.bad_cs})")
        return collected