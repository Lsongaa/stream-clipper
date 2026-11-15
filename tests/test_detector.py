from detector import Detector, KEEP_RECENT

CFG = {"window_seconds": 10, "baseline_minutes": 10, "zscore": 3.0, "min_msgs": 8,
       "cooldown_seconds": 90}


def quiet(det, t0, seconds, every=4.0):
    """Chat tranquilo: 1 mensaje cada `every` s, check() cada 2 s. Devuelve picos detectados."""
    hits, t, next_msg = [], t0, t0
    while t < t0 + seconds:
        if t >= next_msg:
            det.add("u", "hola", now=t)
            next_msg += every
        if det.check(now=t) is not None:
            hits.append(t)
        t += 2
    return hits, t


def burst(det, t, n=30, text="JAJAJA"):
    for i in range(n):
        det.add(f"u{i}", text, now=t + i * 0.1)
    return det.check(now=t + n * 0.1)


def test_no_dispara_sin_linea_base():
    det = Detector(CFG)
    assert burst(det, 0) is None  # menos de 12 muestras de línea base


def test_detecta_pico():
    det = Detector(CFG)
    hits, t = quiet(det, 0, 120)
    assert hits == []
    assert burst(det, t) is not None


def test_cooldown():
    # control: sin cooldown, el segundo pico a los ~34 s sí dispara
    ctl = Detector(dict(CFG, cooldown_seconds=0))
    _, t = quiet(ctl, 0, 120)
    assert burst(ctl, t) is not None
    _, t2 = quiet(ctl, t + 4, 30)
    assert burst(ctl, t2) is not None

    det = Detector(CFG)
    _, t = quiet(det, 0, 120)
    assert burst(det, t) is not None
    _, t2 = quiet(det, t + 4, 30)
    assert burst(det, t2) is None                     # mismo pico, dentro del cooldown (90 s)
    _, t3 = quiet(det, t2 + 4, 100)
    assert t3 - t >= 90
    assert burst(det, t3) is not None                 # pasado el cooldown vuelve a disparar


def test_min_msgs():
    det = Detector(dict(CFG, min_msgs=50))
    _, t = quiet(det, 0, 120)
    assert burst(det, t, n=20, text="hola") is None   # z alto pero pocos mensajes


def test_palabras_hype_pesan_doble():
    det = Detector(CFG)
    det.add("a", "jajaja", now=0)
    det.add("b", "hola", now=0)
    assert sum(w for _, w in det.msgs) == 3


def test_memoria_acotada():
    det = Detector(CFG)
    t = 0.0
    for _ in range(3000):            # ~2 h simuladas, 1 msg + 1 check cada 2.4 s
        det.add("u", "x", now=t)
        det.check(now=t)
        t += 2.4
    assert len(det.recent) <= KEEP_RECENT / 2.4 + 2
    assert len(det.samples) <= CFG["baseline_minutes"] * 60 / 2.4 + 2
    assert len(det.msgs) <= CFG["window_seconds"] / 2.4 + 2


def test_sample_reparte_mensajes():
    det = Detector(CFG)
    for i in range(100):
        det.add(f"u{i}", f"m{i}", now=i)
    s = det.sample(0, 99, n=10)
    assert len(s) == 10 and s[0] == "u0: m0"
