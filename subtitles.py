"""Arma un .ass (subtítulos estilo shorts) con los timestamps por palabra de Whisper."""
import re

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,80,80,520,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ts(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _clean(text):
    # sin llaves ni barras (son códigos de override en ASS) ni saltos de línea
    return re.sub(r"\s+", " ", re.sub(r"[{}\\]", "", text)).strip()


def group_words(words, max_words=4, max_dur=2.5, max_gap=0.8):
    """words: [(inicio, fin, texto)] -> [(inicio, fin, frase)] cortas para pantalla vertical."""
    chunks, cur = [], []
    for s, e, w in words:
        w = _clean(w)
        if not w:
            continue
        if cur and (len(cur) >= max_words or e - cur[0][0] > max_dur or s - cur[-1][1] > max_gap):
            chunks.append(cur)
            cur = []
        cur.append((s, e, w))
    if cur:
        chunks.append(cur)
    return [(c[0][0], c[-1][1], " ".join(x[2] for x in c)) for c in chunks]


def build_ass(words):
    """Devuelve el texto del .ass o None si no hay nada que mostrar."""
    lines = []
    chunks = group_words(words)
    for i, (s, e, text) in enumerate(chunks):
        if i + 1 < len(chunks):
            e = min(e + 0.3, chunks[i + 1][0])  # que no se pisen
        else:
            e += 0.3
        lines.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Default,,0,0,0,,{text.upper()}")
    return HEADER + "\n".join(lines) + "\n" if lines else None
