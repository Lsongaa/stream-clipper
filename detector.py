"""Detecta picos de actividad del chat (z-score contra la línea base reciente)."""
import statistics
import time
from collections import deque

from config import CFG

HYPE = ("kek", "lul", "omg", "jaj", "xd", "pog", "rip", "gg", "nooo", "???", "!!!", "wtf", "clip")
MIN_SAMPLES = 12      # muestras mínimas de línea base antes de disparar
KEEP_RECENT = 300     # segundos de mensajes guardados para darle contexto a Claude


class Detector:
    def __init__(self, cfg=None, clock=time.time):
        self.cfg = cfg or CFG["detector"]
        self.clock = clock
        self.msgs = deque()      # (t, peso) dentro de la ventana
        self.recent = deque()    # (t, user, text) últimos KEEP_RECENT s
        self.samples = deque()   # (t, conteo) para la línea base
        self.last = float("-inf")

    def add(self, user, text, now=None):
        now = self.clock() if now is None else now
        self.msgs.append((now, 2 if any(k in text.lower() for k in HYPE) else 1))
        self.recent.append((now, user, text[:300]))
        while self.recent and self.recent[0][0] < now - KEEP_RECENT:
            self.recent.popleft()

    def check(self, now=None):
        """Llamar cada ~2 s. Devuelve el puntaje del pico o None."""
        now = self.clock() if now is None else now
        while self.msgs and self.msgs[0][0] < now - self.cfg["window_seconds"]:
            self.msgs.popleft()
        cur = sum(w for _, w in self.msgs)
        self.samples.append((now, cur))
        while self.samples and self.samples[0][0] < now - self.cfg["baseline_minutes"] * 60:
            self.samples.popleft()
        vals = [v for _, v in self.samples]
        if len(vals) < MIN_SAMPLES:
            return None
        z = (cur - statistics.mean(vals)) / max(statistics.pstdev(vals), 1.0)
        if (cur >= self.cfg["min_msgs"] and z >= self.cfg["zscore"]
                and now - self.last >= self.cfg["cooldown_seconds"]):
            self.last = now
            return cur
        return None

    def sample(self, t0, t1, n=25):
        """Hasta n mensajes del chat entre t0 y t1, repartidos parejo."""
        lines = [f"{u}: {t}" for ts, u, t in list(self.recent) if t0 <= ts <= t1]
        step = max(1, len(lines) // n)
        return lines[::step][:n]
