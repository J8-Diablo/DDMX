# DMXE.py (DMXEngine)
import socket
import threading
import logging
import logging.handlers
import os

log = logging.getLogger("DMXEngine")
if not log.handlers:
    h = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    h.setFormatter(fmt)
    log.addHandler(h)
log.propagate = False

_level_name = os.environ.get("DMX_LOG_LEVEL", "INFO").upper()
log.setLevel(getattr(logging, _level_name, logging.INFO))

LOG_ARTNET = os.environ.get("DMX_LOG_ARTNET", "0").strip().lower() in ("1", "true", "yes", "on")
LOG_ARTNET_FULL = os.environ.get("DMX_LOG_ARTNET_FULL", "0").strip().lower() in ("1", "true", "yes", "on")
LOG_ARTNET_FILE = os.environ.get("DMX_LOG_ARTNET_FILE", "").strip()

if LOG_ARTNET and LOG_ARTNET_FILE:
    try:
        # Avoid duplicate file handlers
        exists = any(
            isinstance(h, logging.handlers.RotatingFileHandler) and getattr(h, "baseFilename", "") == os.path.abspath(LOG_ARTNET_FILE)
            for h in log.handlers
        )
        if not exists:
            fh = logging.handlers.RotatingFileHandler(
                LOG_ARTNET_FILE,
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            log.addHandler(fh)
    except Exception:
        pass

ARTNET_PORT = 6454
ARTNET_HEADER = b"Art-Net\x00"
OP_DMX = 0x5000          # ArtDMX
PROT_VER = 14            # Art-Net protocol version

def clamp(v, lo=0, hi=255):
    return lo if v < lo else hi if v > hi else v

class DMXEngine:
    """
    Emetteur ArtNet DMX.
    - Maintient un state par univers (512 canaux)
    - Envoie un univers complet (``send_universe``) ou seulement les canaux
      modifiés (``send_channels``); le séquencement est fait par
      ``dmx_engine.DMXRenderEngine``.
    """

    def __init__(
        self,
        bind_ip="0.0.0.0",
        target_ip="255.255.255.255",
        port=ARTNET_PORT,
        # rétro-compat
        bind_iface=None,
        broadcast=True,
    ):
        if bind_iface is not None:
            bind_ip = bind_iface

        self.bind_ip = bind_ip
        self.target_ip = target_ip
        self.port = port
        self.broadcast = broadcast

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if self.broadcast:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        # Larger send buffer reduces tail-latency jitter on bursts of UDP packets
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
        except OSError:
            pass

        self.sock.bind((self.bind_ip, 0))

        self.state = {}  # {universe: [512 ints]}
        self.state_lock = threading.Lock()

        # Cached ArtNet packet templates per universe to avoid rebuilding the
        # 18-byte header on every send (reduces per-universe send jitter).
        self._packet_cache: dict = {}  # {universe: bytearray(530)} full ArtDMX
        self._target_addr = (self.target_ip, self.port)

        log.info("DMXEngine initialized.")

    def _build_full_packet(self, universe: int) -> bytearray:
        """Return a full 530-byte ArtDMX packet template for `universe`.
        Header is pre-filled; only the 512-byte payload region (offset 18)
        needs updating per send."""
        opcode = OP_DMX.to_bytes(2, "little")
        prot = PROT_VER.to_bytes(2, "big")
        seq = (0).to_bytes(1, "big")
        phys = (0).to_bytes(1, "big")
        uni = int(universe).to_bytes(2, "little")
        ln = (512).to_bytes(2, "big")
        header = ARTNET_HEADER + opcode + prot + seq + phys + uni + ln
        pkt = bytearray(530)
        pkt[:18] = header
        return pkt


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
        if LOG_ARTNET:
            if LOG_ARTNET_FULL:
                log.info("[ARTNET] send_channels universe=%s channels=%s length=%s", universe, channels_dict, length)
            else:
                sample = list(channels_dict.items())[:8]
                log.info(
                    "[ARTNET] send_channels universe=%s count=%s length=%s sample=%s",
                    universe, len(channels_dict), length, sample
                )
        else:
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
            data_in = [b & 0xFF for b in buf512]
        else:
            data_in = [clamp(int(v)) for v in buf512]

        if len(data_in) < 512:
            data_in += [0] * (512 - len(data_in))
        elif len(data_in) > 512:
            data_in = data_in[:512]

        with self.state_lock:
            self.state[universe] = data_in[:]
            pkt = self._packet_cache.get(universe)
            if pkt is None:
                pkt = self._build_full_packet(universe)
                self._packet_cache[universe] = pkt
            # Write payload in-place into the cached packet (single 18-byte header,
            # 512-byte payload region). Single bytes() copy on send.
            pkt[18:530] = bytes(data_in)
            packet = bytes(pkt)

        self.sock.sendto(packet, self._target_addr)
        if LOG_ARTNET:
            if LOG_ARTNET_FULL:
                log.info("[ARTNET] send_universe universe=%s values=%s", universe, data_in)
            else:
                nonzero = [(i, v) for i, v in enumerate(data_in) if v]
                sample = nonzero[:8]
                log.info(
                    "[ARTNET] send_universe universe=%s nonzero=%s sample=%s",
                    universe, len(nonzero), sample
                )
        else:
            log.debug(f"[SEND-FULL] universe={universe} sent_len=512")

    # ----------------------------
    # Run / stop
    # ----------------------------
    # ----------------------------
    # Internal runner
    # ----------------------------
    # ----------------------------
    # Step formats
    # ----------------------------