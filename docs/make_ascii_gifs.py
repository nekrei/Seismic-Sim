"""Render themed ASCII-art GIFs for the README from real solver output.

Run from anywhere: python docs/make_ascii_gifs.py  (needs Pillow + NumPy and
the Consolas font, which ships with Windows). Writes docs/media/ascii-*.gif.
"""
import csv, json, math, os
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REC = os.path.join(ROOT, "src", "frontend", "out", "KOCAELI_AYD")
DST = os.path.join(ROOT, "docs", "media")
FONT = ImageFont.truetype(r"C:\Windows\Fonts\consola.ttf", 16)
SMALL = ImageFont.truetype(r"C:\Windows\Fonts\consola.ttf", 13)
CW, CH = 9, 19

BG, BAR, MUTED = (22, 22, 46), (14, 14, 32), (122, 118, 160)
GOLD, ORANGE, RED = (242, 193, 78), (214, 110, 58), (235, 72, 60)
VIOLET, SLATE, COL = (150, 130, 235), (123, 139, 176), (170, 178, 210)
GROUND, WHITE = (58, 53, 100), (236, 232, 245)


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


class Grid:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.c = [[(" ", None)] * w for _ in range(h)]

    def put(self, x, y, s, col):
        for i, ch in enumerate(s):
            if ch != "\0" and 0 <= x + i < self.w and 0 <= y < self.h:
                self.c[y][x + i] = (ch, col)


def render(g, title):
    pad, bar = 14, 28
    im = Image.new("RGB", (g.w * CW + 2 * pad, g.h * CH + 2 * pad + bar), BG)
    d = ImageDraw.Draw(im)
    d.fontmode = "1"
    d.rectangle([0, 0, im.width, bar], fill=BAR)
    for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        d.ellipse([12 + i * 18, 9, 22 + i * 18, 19], fill=c)
    d.text((im.width // 2, bar // 2), title, font=SMALL, fill=MUTED, anchor="mm")
    for y, row in enumerate(g.c):
        x = 0
        while x < g.w:  # draw runs of one colour as a single string
            ch, col = row[x]
            if col is None:
                x += 1
                continue
            run = ch
            while x + len(run) < g.w and row[x + len(run)][1] == col:
                run += row[x + len(run)][0]
            d.text((pad + x * CW, bar + pad + y * CH), run, font=FONT, fill=col)
            x += len(run)
    return im


def save(frames, name, ms):
    path = os.path.join(DST, name)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=ms,
                   loop=0, optimize=True, disposal=1)
    print(name, len(frames), "frames", os.path.getsize(path) // 1024, "KB")


# ---- real data: Kocaeli 1999, Aydin station, default 7-story building ----
with open(os.path.join(REC, "response_X.csv")) as f:
    rows = list(csv.reader(f))
data = np.array(rows[1:], dtype=float)
t_rec, ground, floors = data[:, 0], data[:, 1], data[:, 2:]
rel = floors - ground[:, None]
acc = np.array(json.load(open(os.path.join(REC, "ground_accel.json")))["X"])
meta = json.load(open(os.path.join(REC, "building_data.json")))
N = rel.shape[1]
DT = t_rec[1] - t_rec[0]
peak = int(np.argmax(np.abs(rel[:, -1])))


def slab_col(i, n):
    return mix(ORANGE, SLATE, i / max(1, n - 1))


def draw_building(g, cx, base, offs, n, colors=None, story_h=3, width=14,
                  furniture=True):
    """offs[0] is the ground offset, offs[i] floor i's; returns nothing."""
    half = width // 2
    for i in range(1, n + 1):
        a, b = offs[i - 1], offs[i]
        top = base - story_h * i
        col = colors[i] if colors else COL
        for k in range(1, story_h):
            y = top + k
            frac = (story_h - k) / story_h
            x = round(a + (b - a) * frac)
            ch = "/" if b > a else "\\" if b < a else "│"
            if abs(b - a) < 0.35:
                ch = "│"
            g.put(cx + x - half, y, ch, col)
            g.put(cx + x + half - 1, y, ch, col)
        if furniture:
            g.put(cx + round(a) - half + 3, top + story_h - 1, "π  h", (160, 110, 80))
            if i % 2:
                g.put(cx + round(a) + 2, top + story_h - 1, "¤", (200, 190, 150))
        g.put(cx + round(b) - half, top, "▄" * width, slab_col(i - 1, n))


# ---- 1. swaying building driven by the real response ----------------------
def gif_sway():
    W, H = 74, 30
    frames = []
    step = 8  # samples per frame (0.08 s of record at 0.01 s)
    idx = range(peak - 60 * step, peak + 60 * step, step)
    win = rel[idx.start:idx.stop]
    scale = 7.0 / np.abs(win[:, -1]).max()
    gscale = 2.0 / np.abs(ground[idx.start:idx.stop] - ground[idx.start]).max()
    a_win = acc[idx.start - 400:idx.stop]
    amax = np.abs(a_win).max()
    for j in idx:
        g = Grid(W, H)
        base = H - 5
        gx = (ground[j] - ground[idx.start]) * gscale
        offs = [gx] + [gx + rel[j, i] * scale for i in range(N)]
        g.put(0, base + 1, "▀" * 44, GROUND)
        g.put(round(gx) + 12, base + 1, "▀" * 16, (90, 90, 130))
        draw_building(g, 22, base, offs, N)
        # live readout
        g.put(46, 1, "KOCAELI 1999 · Aydin", WHITE)
        g.put(46, 2, f"t = {t_rec[j]:6.2f} s", MUTED)
        g.put(46, 4, "floor  u_rel(t)", MUTED)
        for i in range(N):
            y = 6 + (N - 1 - i) * 2
            v = rel[j, i]
            pos = 9 + round(v * scale / 7.0 * 8)
            g.put(46, y, f"F{i + 1}", slab_col(i, N))
            g.put(50, y, "─" * 19, (60, 58, 96))
            g.put(50 + 9, y, "┃", (80, 78, 120))
            g.put(50 + max(0, min(18, pos)), y, "●", slab_col(i, N))
            g.put(46, y + 1, f"   {v * 100:+7.2f} cm", MUTED)
        # scrolling seismograph of the ground acceleration
        seg = acc[j - 60 * 4:j:4]
        for x, v in enumerate(seg):
            r = round(v / amax * 1.8)
            g.put(2 + x, H - 2 - r, "•", GOLD if x > 52 else mix(GROUND, GOLD, x / 60))
        g.put(64, H - 2, "ẍg(t)", GOLD)
        frames.append(render(g, "python src/backend/server.py  ·  7-story frame, first-mode sway"))
    save(frames, "ascii-building.gif", 80)


# ---- 2. input × transfer = output ----------------------------------------
def gif_fft():
    W, H, B = 76, 28, 60
    fn = np.array(meta["natural_frequencies_Hz_X"])
    phi = np.array(meta["mode_shapes_X"])
    gam = np.array(meta["participation_factors_X"])
    zeta = meta["damping_ratio"]
    phi_roof = phi[-1] if phi.shape[0] == N else phi[:, -1]
    nwin, fmax = 1024, 6.0
    f = np.fft.rfftfreq(nwin, DT)
    w, wn = 2 * np.pi * f, 2 * np.pi * fn
    Hf = np.abs(sum(-gam[k] * phi_roof[k] / (wn[k] ** 2 - w ** 2 + 2j * zeta * wn[k] * w)
                    for k in range(len(fn))))
    edges = np.linspace(0, fmax, B + 1)

    def bins(spec):
        return np.array([spec[(f >= edges[k]) & (f < edges[k + 1])].mean() for k in range(B)])

    hann = np.hanning(nwin)
    starts = list(range(1000, len(acc) - nwin, 60))[:90]
    Xs = [np.abs(np.fft.rfft(acc[s:s + nwin] * hann)) for s in starts]
    Xmax = max(bins(x).max() for x in Xs)
    Ymax = max(bins(x * Hf).max() for x in Xs)
    Hb = bins(Hf) / bins(Hf).max()
    amax = np.abs(acc).max()
    frames = []
    for s, X in zip(starts, Xs):
        g = Grid(W, H)
        g.put(2, 0, "Y(ω) = H(ω) · X(ω)", WHITE)
        g.put(46, 0, f"window {s * DT:5.1f}–{(s + nwin) * DT:5.1f} s", MUTED)
        # time trace
        seg = acc[s:s + nwin:nwin // B][:B]
        rs = [round(math.copysign(math.sqrt(abs(v) / amax), v) * 3) for v in seg]
        for x, r in enumerate(rs):
            lo, hi = sorted((r, rs[x - 1] if x else r))
            for rr in range(lo, hi + 1):  # join samples like an oscilloscope
                g.put(14 + x, 4 - rr, "│" if rr != r else "•", GOLD if rr == r else mix(BG, GOLD, 0.55))
        g.put(2, 4, "ẍg(t)", GOLD)
        g.put(14, 7, "── FFT ─────────────────────────────────────────────────▼", MUTED)

        def bars(y0, vals, col, label):
            g.put(2, y0 + 1, label, col)
            for x, v in enumerate(vals):
                lv = round(max(0.0, min(1.0, v)) * 10)  # 5 rows × 2 half-levels
                for r in range(5):
                    rest = lv - 2 * r
                    ch = "█" if rest >= 2 else "▄" if rest == 1 else ""
                    if ch:
                        g.put(14 + x, y0 + 4 - r, ch, mix(col, WHITE, r / 12))

        bars(8, bins(X) / Xmax, GOLD, "|X(ω)|")
        bars(14, Hb, VIOLET, "|H(ω)|")
        g.put(2, 16, "modes", MUTED)
        for k, fk in enumerate(fn[:3]):
            x = 14 + round(fk / fmax * B)
            if x < 14 + B:
                g.put(x, 19, "▲", VIOLET)
        bars(20, bins(X * Hf) / Ymax, ORANGE, "|Y(ω)|")
        g.put(14, 25, "0 Hz" + " " * 23 + "3 Hz" + " " * 25 + "6 Hz", MUTED)
        g.put(2, 26, f"T1 = {meta['fundamental_period_s_X']:.2f} s   ζ = {zeta:.2f}   "
                     "resonance: X × H peaks where the building rings", MUTED)
        frames.append(render(g, "frequency-domain solve  ·  KOCAELI 1999 · Aydin"))
    save(frames, "ascii-fft.gif", 90)


# ---- 3. collapse -------------------------------------------------------
def gif_collapse():
    W, H, n, sh = 60, 24, 4, 4
    base, cx = H - 4, 30
    rng = np.random.default_rng(7)
    frames = []
    shape = np.array([0.35, 0.65, 0.88, 1.0])
    rubble, dust = [], []
    fall_y, fall_v, landed = 0.0, 0.0, None
    total = 150
    for fr in range(total):
        g = Grid(W, H)
        g.put(0, base + 1, "▀" * W, GROUND)
        colors = [COL] * (n + 1)
        amp = min(1.0, fr / 40) * 3.2 if fr < 70 else max(0.0, 3.2 - (fr - 70) * 0.08)
        s = math.sin(fr * 0.62) * amp
        offs = [0.0] + list(s * shape)
        status, scol = "shaking…", MUTED
        if fr >= 32:
            colors[3] = mix(COL, RED, (fr - 32) / 20)
            status, scol = "collapse onset · story 3 · 35.50 s", GOLD
        if fr < 60:
            draw_building(g, cx, base, offs, n, colors, story_h=sh, width=16)
            if fr >= 32:
                for i in (2, 3):
                    for side in (-8, 7):
                        g.put(cx + round(offs[i]) + side, base - sh * i - 1 if i == 3 else base - sh * i + 1,
                              "●", GOLD if fr < 48 else RED)
        else:
            status, scol = "story 3 detached · 80.54 s", RED
            # lower two stories keep swaying, block above falls
            draw_building(g, cx, base, offs[:3], 2, colors, story_h=sh, width=16)
            if landed is None:
                fall_v += 0.11
                fall_y += fall_v
                gap = sh  # the failed story's height
                if fall_y >= gap:
                    landed = fr
                    for _ in range(46):
                        rubble.append([cx + rng.integers(-8, 8), base - 2 * sh - rng.integers(1, 4),
                                       rng.normal(0, 0.45), -abs(rng.normal(0.1, 0.2)),
                                       "▄▀█▓▒░▐▌"[rng.integers(0, 8)]])
                    for _ in range(40):
                        dust.append([cx + rng.normal(0, 5), base - 2 * sh, rng.normal(0, 0.35),
                                     -abs(rng.normal(0.25, 0.15)), 0])
                else:
                    y0 = base - 2 * sh + round(fall_y)
                    x0 = round(offs[2])
                    for k, lvl in enumerate((3, 4)):
                        top = y0 - sh * (lvl - 2)
                        g.put(cx + x0 - 8, top, "▄" * 16, slab_col(lvl - 1, n))
                        if lvl == 4:
                            for r in range(1, sh):
                                g.put(cx + x0 - 8, top + r, "│", COL)
                                g.put(cx + x0 + 7, top + r, "│", COL)
                    for r in range(1, max(1, sh - round(fall_y))):
                        g.put(cx + x0 - 8, y0 + r, "╳", RED)
                        g.put(cx + x0 + 7, y0 + r, "╳", RED)
            if landed is not None:
                age = fr - landed
                for p in rubble:
                    p[2] *= 0.9
                    p[3] += 0.12
                    p[0] += p[2]
                    heap = base - 2 * sh - max(0, 2 - abs(p[0] - cx) // 4)  # settle into a mound
                    p[1] = min(heap, p[1] + p[3])
                    g.put(round(p[0]), round(p[1]), p[4], mix(SLATE, ORANGE, (p[0] - cx) / 18 + 0.5))
                for p in dust:
                    p[0] += p[2]
                    p[1] += p[3]
                    p[4] += 1
                    ch = "°" if p[4] > 18 else "∙" if p[4] > 8 else "•"
                    g.put(round(p[0]), round(p[1]), ch, mix((150, 140, 150), BG, p[4] / 45))
                if age > 20:
                    g.put(2, 2, "rigid-body fall · Rapier · from the solver's hand-off", MUTED)
        g.put(2, 0, status, scol)
        frames.append(render(g, "Setup → Analysis → Run collapse analysis"))
    save(frames, "ascii-collapse.gif", 70)


# ---- 4. shaking title banner -------------------------------------------
GLYPHS = {  # figlet "ANSI Shadow"
    "L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
    "U": ["██╗   ██╗", "██║   ██║", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "C": [" ██████╗", "██╔════╝", "██║     ", "██║     ", "╚██████╗", " ╚═════╝"],
    "K": ["██╗  ██╗", "██║ ██╔╝", "█████╔╝ ", "██╔═██╗ ", "██║  ██╗", "╚═╝  ╚═╝"],
    "Y": ["██╗   ██╗", "╚██╗ ██╔╝", " ╚████╔╝ ", "  ╚██╔╝  ", "   ██║   ", "   ╚═╝   "],
    "S": ["███████╗", "██╔════╝", "███████╗", "╚════██║", "███████║", "╚══════╝"],
    "E": ["███████╗", "██╔════╝", "█████╗  ", "██╔══╝  ", "███████╗", "╚══════╝"],
    "I": ["██╗", "██║", "██║", "██║", "██║", "╚═╝"],
    "M": ["███╗   ███╗", "████╗ ████║", "██╔████╔██║", "██║╚██╔╝██║", "██║ ╚═╝ ██║", "╚═╝     ╚═╝"],
    "T": ["████████╗", "╚══██╔══╝", "   ██║   ", "   ██║   ", "   ██║   ", "   ╚═╝   "],
    "R": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██║  ██║", "╚═╝  ╚═╝"],
}


def word(text):
    return ["".join(GLYPHS[c][r] for c in text) for r in range(6)]


def gif_banner():
    W, H = 60, 23
    lines = word("LUCKY") + word("SEISMIC") + word("STRIKE")
    s0 = int(np.argmax(np.abs(acc))) - 1400
    samples = acc[s0:s0 + 3200:40]  # quiet → strong → decay
    amax = np.abs(samples).max()
    frames, prev = [], None
    for j in range(len(samples)):
        g = Grid(W, H)
        offs = []
        for r, line in enumerate(lines):
            lag = (len(lines) - r) // 3          # the wave reaches the bottom row first
            v = samples[max(0, j - lag)] / amax
            offs.append(round(v * 3))
        if prev:  # motion trail where the row was a frame ago
            for r, line in enumerate(lines):
                if prev[r] != offs[r]:
                    x0 = (W - len(line)) // 2 + prev[r]
                    trail = "".join("░" if c == "█" else "\0" for c in line)
                    g.put(x0, r + 1, trail, (70, 55, 95))
        for r, line in enumerate(lines):
            x0 = (W - len(line)) // 2 + offs[r]
            col = mix(ORANGE, GOLD, r / len(lines))
            for i, c in enumerate(line):  # blocks in colour, shadow strokes dim
                if c != " ":
                    g.put(x0 + i, r + 1, c, col if c == "█" else (110, 96, 160))
        # seismogram under the title
        for x in range(W):
            k = j - (W - 1 - x) // 2
            if k >= 0:
                v = samples[k] / amax
                ch = "█" if abs(v) > 0.6 else "▄" if abs(v) > 0.25 else "─" if abs(v) > 0.05 else "·"
                g.put(x, H - 2, ch, GOLD if x > W - 6 else mix(GROUND, ORANGE, abs(v) + 0.2))
        prev = offs
        frames.append(render(g, "real ground motion · KOCAELI 1999"))
    save(frames, "ascii-banner.gif", 70)


if __name__ == "__main__":
    os.makedirs(DST, exist_ok=True)
    gif_banner()
    gif_sway()
    gif_fft()
    gif_collapse()
