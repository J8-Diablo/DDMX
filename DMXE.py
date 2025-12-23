# DMXE.py (DMXEngine)
import socket
import threading
import time
import logging
import re
import random
from copy import deepcopy

log = logging.getLogger("DMXEngine")
if not log.handlers:
    h = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    h.setFormatter(fmt)
    log.addHandler(h)
log.setLevel(logging.DEBUG)

ARTNET_PORT = 6454
ARTNET_HEADER = b"Art-Net\x00"
OP_DMX = 0x5000          # ArtDMX
PROT_VER = 14            # Art-Net protocol version

def clamp(v, lo=0, hi=255):
    return lo if v < lo else hi if v > hi else v

def outside_in_order(ids):
    """Outside-in order: [1,2,3,4,5] -> [1,5,2,4,3]"""
    out = []
    l, r = 0, len(ids) - 1
    while l <= r:
        if l == r:
            out.append(ids[l])
        else:
            out.append(ids[l])
            out.append(ids[r])
        l += 1
        r -= 1
    return out

def compute_device_schedule(duration_field, device_ids):
    """
    Returns dict dev_id -> (start_ms, end_ms)

    duration_field examples:
      "500"               -> all devices fade 0..500 together
      "100 > 1000"        -> cascade forward in selection order
      "100 < 1000"        -> cascade reverse in selection order
      "100 || 1000"       -> outside-in forward
      "100 | 1000"        -> outside-in reverse
      "? > 500" / "? 100 > 500" -> random order forward
    """
    s = (str(duration_field) if duration_field is not None else "0").strip()
    if not s:
        s = "0"

    randomize = "?" in s

    mode = None
    if "||" in s:
        mode = "||"
    elif "|" in s:
        mode = "|"
    elif ">" in s:
        mode = ">"
    elif "<" in s:
        mode = "<"

    nums = [int(x) for x in re.findall(r"\d+", s)]
    if len(nums) == 0:
        a = b = 0
    elif len(nums) == 1:
        a, b = 0, nums[0]
    else:
        a, b = nums[0], nums[1]

    t0, t1 = min(a, b), max(a, b)
    n = len(device_ids)

    ordered = list(device_ids)
    if randomize:
        random.shuffle(ordered)

    if mode == ">":
        pass
    elif mode == "<":
        ordered = list(reversed(ordered))
    elif mode == "||":
        ordered = outside_in_order(ordered)
    elif mode == "|":
        ordered = outside_in_order(ordered)
        ordered = list(reversed(ordered))
    else:
        # no mode => simultaneous fade
        return {dev_id: (0, t1) for dev_id in ordered}

    if n <= 1:
        return {ordered[0]: (0, t1)} if n == 1 else {}

    times = [t0 + (t1 - t0) * i / (n - 1) for i in range(n)]

    sched = {}
    prev = 0.0
    for i, dev_id in enumerate(ordered):
        end = float(times[i])
        start = float(prev) if i > 0 else 0.0
        sched[dev_id] = (int(round(start)), int(round(end)))
        prev = end

    return sched


class DMXEngine:
    """
    Engine ArtNet DMX.
    - Maintient un state par univers (512 canaux)
    - CUT/apply_state immédiat
    - Joue une sequence au format:
        {"devices":{id:{"channels":{"Universe":0,"7":255,...}},...},
         "sleep":"500","duration":"100>1000","name":"Cue X","device_order":[...]}
      ou legacy:
        {"universe":0,"channels":{...},"sleep":...,"duration":...}
    """

    def __init__(
        self,
        bind_ip="0.0.0.0",
        target_ip="255.255.255.255",
        port=ARTNET_PORT,
        tick_ms=20,
        # rétro-compat
        bind_iface=None,
        broadcast=True,
    ):
        if bind_iface is not None:
            bind_ip = bind_iface

        self.bind_ip = bind_ip
        self.target_ip = target_ip
        self.port = port
        self.tick_ms = tick_ms
        self.broadcast = broadcast

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.broadcast:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        self.sock.bind((self.bind_ip, 0))

        self.state = {}  # {universe: [512 ints]}
        self.state_lock = threading.Lock()

        self._run_thread = None
        self._stop_event = threading.Event()

        log.info("DMXEngine initialized.")


    # ----------------------------
    # Low-level send
    # ----------------------------
    def _ensure_universe(self, universe):
        with self.state_lock:
            if universe not in self.state:
                self.state[universe] = [0] * 512

    def send_channels(self, universe, channels_dict):
        """
        channels_dict: { absolute_channel(int): value(int 0..255) }
        """
        self._ensure_universe(universe)

        with self.state_lock:
            uni_state = self.state[universe]
            for ch, v in channels_dict.items():
                if 0 <= ch < 512:
                    uni_state[ch] = clamp(int(v))
            data = uni_state[:]

        max_ch = max(channels_dict.keys()) if channels_dict else 0
        length = max(2, max_ch + 1)
        if length % 2 == 1:
            length += 1
        length = min(length, 512)
        payload = bytes(data[:length])

        opcode = OP_DMX.to_bytes(2, "little")
        prot = PROT_VER.to_bytes(2, "big")
        seq = (0).to_bytes(1, "big")
        phys = (0).to_bytes(1, "big")
        uni = int(universe).to_bytes(2, "little")
        ln = int(length).to_bytes(2, "big")

        packet = ARTNET_HEADER + opcode + prot + seq + phys + uni + ln + payload
        self.sock.sendto(packet, (self.target_ip, self.port))
        log.debug(f"[SEND] universe={universe} channels_in={len(channels_dict)} sent_len={length}")

    # Compat ancienne API (utilisée par certains app.py)
    def send_universe(self, universe, buf512):
        """
        Envoie un univers complet (512 canaux).
        buf512 peut être list/bytes/bytearray.
        """
        universe = int(universe)
        self._ensure_universe(universe)

        if isinstance(buf512, (bytes, bytearray)):
            data_in = list(buf512)
        else:
            data_in = list(buf512)

        if len(data_in) < 512:
            data_in += [0] * (512 - len(data_in))
        data_in = data_in[:512]
        data_in = [clamp(int(v)) for v in data_in]

        with self.state_lock:
            self.state[universe] = data_in[:]
            payload = bytes(data_in)

        opcode = OP_DMX.to_bytes(2, "little")
        prot = PROT_VER.to_bytes(2, "big")
        seq = (0).to_bytes(1, "big")
        phys = (0).to_bytes(1, "big")
        uni = universe.to_bytes(2, "little")
        ln = (512).to_bytes(2, "big")

        packet = ARTNET_HEADER + opcode + prot + seq + phys + uni + ln + payload
        self.sock.sendto(packet, (self.target_ip, self.port))
        log.debug(f"[SEND-FULL] universe={universe} sent_len=512")

    def apply_state(self, universe, channels_dict):
        self.send_channels(int(universe), {int(k): int(v) for k, v in channels_dict.items()})

    def current_state_snapshot(self):
        with self.state_lock:
            return deepcopy(self.state)


    # ----------------------------
    # Run / stop
    # ----------------------------
    def run_from_payload(self, payload):
        seq = payload.get("sequence") or []
        loop = bool(payload.get("loop", False))
        loop_count = payload.get("loop_count")
        log.info(f"[API] run from payload seq_len={len(seq)} loop={loop}")
        self.run_sequence(seq, loop=loop, loop_count=loop_count)

    def run_sequence(self, sequence, loop=False, loop_count=None):
        if self._run_thread and self._run_thread.is_alive():
            log.info("[RUN] already running -> stop previous")
            self.stop_run()

        self._stop_event.clear()
        self._run_thread = threading.Thread(
            target=self._run_worker,
            args=(sequence, loop, loop_count),
            daemon=True,
        )
        self._run_thread.start()

    def stop_run(self):
        log.info("[API] stop requested")
        self._stop_event.set()
        if self._run_thread:
            self._run_thread.join(timeout=1.0)
        log.info("[RUN] end thread")


    # ----------------------------
    # Internal runner
    # ----------------------------
    def _sleep_ms(self, ms):
        end = time.time() + ms / 1000.0
        while time.time() < end:
            if self._stop_event.is_set():
                return False
            time.sleep(0.01)
        return True

    def _run_worker(self, sequence, loop, loop_count):
        log.info(f"[RUN] start sequence_len={len(sequence)} loop={loop} tick_ms={self.tick_ms}")

        iter_count = 0
        max_iter = float("inf") if loop_count is None else int(loop_count)

        while not self._stop_event.is_set():
            if not loop and iter_count > 0:
                break
            if iter_count >= max_iter:
                break

            iter_count += 1
            log.info(f"[RUN] loop iteration {iter_count}")

            for idx, step in enumerate(sequence):
                if self._stop_event.is_set():
                    break
                keys = list(step.keys())
                log.info(f"[RUN] step#{idx} keys={keys} name={step.get('name','')}")
                if "devices" in step:
                    log.info(f"[RUN] step#{idx} format=devices")
                    self._run_step_devices(step)
                elif "channels" in step and "universe" in step:
                    log.info(f"[RUN] step#{idx} format=legacy")
                    self._run_step_legacy(step)
                else:
                    log.warning(f"[RUN] step#{idx} unknown format")

        log.info("[RUN] worker finished")


    # ----------------------------
    # Step formats
    # ----------------------------
    def _run_step_legacy(self, step):
        sleep_ms = int(step.get("sleep", 0) or 0)
        duration_ms = int(step.get("duration", 0) or 0)
        universe = int(step.get("universe", 0) or 0)
        channels = step.get("channels", {}) or {}

        log.debug(f"[STEP-LEGACY] sleep={sleep_ms} duration={duration_ms} universe={universe} ch={len(channels)}")

        if sleep_ms > 0:
            if not self._sleep_ms(sleep_ms):
                log.debug("[STEP-LEGACY] stop flag during sleep")
                return

        if duration_ms <= 0:
            self.send_channels(universe, {int(k): int(v) for k, v in channels.items()})
            return

        snapshot = self.current_state_snapshot()
        start_uni = snapshot.get(universe, {})
        start_vals = {int(k): int(start_uni.get(int(k), 0)) for k in channels.keys()}
        end_vals = {int(k): int(v) for k, v in channels.items()}

        t = 0
        while t <= duration_ms and not self._stop_event.is_set():
            k = min(1.0, t / duration_ms)
            frame = {}
            for ch, v_end in end_vals.items():
                v0 = start_vals.get(ch, 0)
                frame[ch] = int(round(v0 + (v_end - v0) * k))
            self.send_channels(universe, frame)
            if not self._sleep_ms(self.tick_ms):
                return
            t += self.tick_ms

        self.send_channels(universe, end_vals)


    def _run_step_devices(self, step):
        devices = step.get("devices", {}) or {}
        sleep_ms = int(step.get("sleep", 0) or 0)
        duration_field = step.get("duration", "0")

        order = step.get("device_order")
        if order:
            device_ids = [str(x) for x in order if str(x) in devices]
        else:
            device_ids = list(devices.keys())

        schedule = compute_device_schedule(duration_field, device_ids)
        total_ms = 0
        for st, en in schedule.values():
            total_ms = max(total_ms, en)

        log.debug(
            f"[STEP-DEV] name={step.get('name','')} sleep={sleep_ms} "
            f"duration_field='{duration_field}' total_ms={total_ms} "
            f"order={order} num_devices={len(device_ids)}"
        )

        if sleep_ms > 0:
            log.debug(f"[STEP-DEV] sleeping {sleep_ms}ms")
            if not self._sleep_ms(sleep_ms):
                log.debug("[STEP-DEV] stop flag during sleep")
                return

        # CUT
        if total_ms <= 0:
            log.debug("[STEP-DEV] CUT apply")
            finals = {}
            for dev_id, entry in devices.items():
                ch = entry.get("channels", {}) or {}
                uni = int(ch.get("Universe", 0))
                finals.setdefault(uni, {})
                for k, v in ch.items():
                    if str(k).lower() == "universe":
                        continue
                    finals[uni][int(k)] = int(v)
            for uni, chmap in finals.items():
                self.send_channels(uni, chmap)
            return

        # FADE
        log.debug("[STEP-DEV] FADE begin")
        snapshot = self.current_state_snapshot()

        t = 0
        while t <= total_ms and not self._stop_event.is_set():
            frame_by_uni = {}

            for dev_id, entry in devices.items():
                st, en = schedule.get(dev_id, (0, total_ms))
                if t < st:
                    continue

                local_k = 1.0 if en <= st else min(1.0, (t - st) / (en - st))

                ch = entry.get("channels", {}) or {}
                uni = int(ch.get("Universe", 0))
                frame_by_uni.setdefault(uni, {})

                for kch, v_end in ch.items():
                    if str(kch).lower() == "universe":
                        continue
                    abs_ch = int(kch)
                    v_end = int(v_end)
                    v0 = snapshot.get(uni, {}).get(abs_ch, 0)
                    v = int(round(v0 + (v_end - v0) * local_k))
                    frame_by_uni[uni][abs_ch] = v

            for uni, chmap in frame_by_uni.items():
                self.send_channels(uni, chmap)

            if t % 200 == 0:
                log.debug(f"[STEP-DEV] FADE progress t={t}/{total_ms}")

            if not self._sleep_ms(self.tick_ms):
                return
            t += self.tick_ms

        log.debug("[STEP-DEV] FADE end; applying finals")
        finals = {}
        for dev_id, entry in devices.items():
            ch = entry.get("channels", {}) or {}
            uni = int(ch.get("Universe", 0))
            finals.setdefault(uni, {})
            for k, v in ch.items():
                if str(k).lower() == "universe":
                    continue
                finals[uni][int(k)] = int(v)

        for uni, chmap in finals.items():
            self.send_channels(uni, chmap)
