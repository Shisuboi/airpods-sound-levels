"""
The decibel panel: Liquid Glass background + Control Center style content.

Layout follows Apple's headphone-level module:
  - a header with the device and the Windows volume
  - the caption, then a status row with icon and value
  - a 20-segment meter. The taller, darker segment is the fixed 80 dB
    threshold marker: it never moves, only the fill follows the level.
  - a fixed 20 / 80 / 110 scale underneath
"""

import math
import os
import numpy as np
from scipy import ndimage
from PySide6.QtCore import Qt, QRectF, QByteArray
from PySide6.QtGui import (QImage, QPainter, QColor, QFont, QFontDatabase,
                           QPainterPath)
from PySide6.QtSvg import QSvgRenderer

from . import glass as lg
from .i18n import t

HERE = os.path.dirname(os.path.abspath(__file__))

# --- typeface ---------------------------------------------------------------
# Inter, not the Windows system font. It was drawn for screens against the same
# brief as SF Pro and carries an optical-size axis, which is why it reads as
# Apple where Segoe does not. SIL OFL, so it can actually ship here - SF Pro's
# licence covers Apple platforms only.
_FAMILY = None


def load_fonts():
    global _FAMILY
    if _FAMILY is not None:
        return _FAMILY
    path = os.path.join(HERE, "assets", "fonts", "Inter.ttf")
    fid = QFontDatabase.addApplicationFont(path)
    families = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
    _FAMILY = families[0] if families else "Segoe UI"
    return _FAMILY


def font(size, weight=QFont.Normal, tracking=0.0):
    f = QFont(load_fonts(), size)
    f.setWeight(weight)
    if tracking:
        f.setLetterSpacing(QFont.AbsoluteSpacing, tracking)
    return f


# --- elevation --------------------------------------------------------------
# Surfaces in macOS 26 sit at different heights, and the height is the ranking:
# the more important the surface, the further it lifts off what is behind it.
# Without this every card reads as painted onto the same plane.
_shadow_cache = {}


def shadow_image(w, h, radius, blur=14.0, alpha=0.42, dy=5):
    """A soft drop shadow for one card size, cached; black, alpha only."""
    w, h = int(round(w)), int(round(h))
    key = (w, h, round(radius, 1), round(blur, 1), round(alpha, 2), dy)
    if key in _shadow_cache:
        return _shadow_cache[key]

    pad = int(blur * 2 + 6)
    cw, ch = w + pad * 2, h + pad * 2
    a = np.zeros((ch, cw), dtype=np.float32)
    a[pad + dy:pad + dy + h, pad:pad + w] = lg.build_mask(w, h, radius)
    if blur > 0:
        a = ndimage.gaussian_filter(a, blur / 2.0, mode="constant")
    a = np.clip(a * alpha, 0.0, 1.0)

    buf = np.zeros((ch, cw, 4), dtype=np.uint8)
    buf[..., 3] = (a * 255).astype(np.uint8)
    img = QImage(buf.data, cw, ch, cw * 4, QImage.Format_RGBA8888).copy()
    _shadow_cache[key] = (img, pad)
    return _shadow_cache[key]


def draw_elevated(painter, path, rect, fill, radius, blur=14.0, alpha=0.42,
                  dy=5, highlight=30):
    """Fill a shape and sit it at a height above what is behind it.

    In dark mode a black shadow on a near-black surface conveys nothing, so
    height is carried mainly by two other things: the fill gets lighter as it
    rises, and light catches the raised top edge. The shadow only tightens
    the boundary.
    """
    img, pad = shadow_image(rect.width(), rect.height(), radius, blur, alpha,
                            dy)
    painter.drawImage(int(rect.x()) - pad, int(rect.y()) - pad, img)
    painter.fillPath(path, fill)

    if highlight:
        painter.save()
        painter.setClipPath(path)
        painter.fillRect(QRectF(rect.x(), rect.y(), rect.width(), 1),
                         QColor(255, 255, 255, highlight))
        painter.restore()

W, H, RADIUS = 360, 176, 22

# meter scale, same as iOS
DB_LO, DB_HI, SEGMENTS = 20.0, 110.0, 20
LOUD_THRESHOLD = 80.0
HIGH_THRESHOLD = 95.0
# the segment the 80 dB mark falls in - fixed, never follows the level
THRESHOLD_INDEX = int((LOUD_THRESHOLD - DB_LO) / (DB_HI - DB_LO) * SEGMENTS)

GREEN = QColor("#30D158")
GREEN_DEEP = QColor("#1F8F3C")
YELLOW = QColor("#FFD60A")
YELLOW_DEEP = QColor("#C9A200")
RED = QColor("#FF453A")
RED_DEEP = QColor("#B32921")
WHITE = QColor(255, 255, 255)
DIM = QColor(235, 235, 245, 153)      # 0.60
FAINT = QColor(235, 235, 245, 102)    # 0.40
TRACK = QColor(255, 255, 255, 36)
TRACK_MARK = QColor(255, 255, 255, 72)
HAIRLINE = QColor(255, 255, 255, 28)

# Geometry tuned by hand in tuner.py against the reference photos.
# The tint stays low on purpose: Apple's material is near-clear in the middle
# and only reads as glass at the rim. Legibility comes from a local scrim
# behind the text plus a text shadow, not from veiling the whole panel.
# rim_gain/edge_shade stay small: a bright ring all the way round reads as a
# plastic frame. The rim is directional, strongest on two opposite arcs.
GLASS = dict(surface="convex-squircle", bezel=12.0, thickness=17.0, ior=1.89,
             blur=4.5, refraction=2.2, specular=0.32, angle=-95.0,
             saturation=1.4, brightness=1.02,
             tint=(0.42, 0.42, 0.47), tint_alpha=0.12, adaptive=0.45,
             # (over bright content, over dark content) - the material flips
             # so it separates from the desktop either way
             tint_polarity=((0.20, 0.20, 0.24), (0.78, 0.78, 0.83)),
             scrim_light=0.72,
             dispersion=0.035, scrim=0.38,
             rim_gain=0.11, edge_shade=0.13, rim_ambient=0.28,
             supersample=3, bezel_clarity=0.55,
             shadow=0.34, shadow_blur=17.0, shadow_lift=5.0)

SHADOW_PAD = 20   # room around the panel for the drop shadow

# iOS 27 walked Liquid Glass back from the iOS 26 look: less transparency by
# default, heavier diffusion so busy content stays readable, a darkened edge
# for separation, and brighter speculars. Large surfaces get more of all of
# it than small ones, which is Apple's own rule.
# The shell is near-black on purpose: it plays systemGroupedBackground, and
# the content cards sit on top of it as the lighter secondary fill. Getting
# that order backwards is what makes grouped content stop reading as grouped.
# Now the geometry base for GLASS_SIDEBAR rather than a surface of its own:
# the exposure window went light, so nothing ships this dark shell.
GLASS_WINDOW = dict(GLASS,
                    bezel=16.0, thickness=22.0, blur=11.0, refraction=1.5,
                    specular=0.62, tint=(0.03, 0.03, 0.04), tint_alpha=0.50,
                    tint_polarity=((0.03, 0.03, 0.05), (0.30, 0.30, 0.35)),
                    # a sidebar that floats over content should stay neutral;
                    # boosting saturation makes it grab the wallpaper's hue.
                    # Exactly 1.0 also skips the luma pass, worth ~8 ms here.
                    saturation=1.0, adaptive=0.07, scrim=0.0,
                    rim_gain=0.13, edge_shade=0.26, rim_ambient=0.32,
                    # geometry is built once, so it can be evaluated finer
                    supersample=3,
                    # 454 kpx of surface, seven times the panel, and all of
                    # it but the sidebar strip sits under an opaque fill. The
                    # interior tone runs a step coarser; none of it shows.
                    interior_scale=2,
                    # and the veil thins towards the edge, where the lens is
                    bezel_clarity=0.78,
                    shadow=0.46, shadow_blur=26.0, shadow_lift=8.0)

# The exposure window's light surface, after Finder and Music. It is rendered
# once over a flat field rather than over the desktop - see
# HistoryWindow._build_shell - so what these values settle is a tone and an
# edge, not a live material. `blur` is deliberately absent: over a uniform
# field it is a no-op, and carrying a value that does nothing invites someone
# to tune it. The geometry is GLASS_WINDOW's.
GLASS_SIDEBAR = dict(GLASS_WINDOW,
                     # 0.84 where the dark window used 0.50. The tone has to
                     # land light with only the flat field underneath it.
                     tint=(0.96, 0.96, 0.97), tint_alpha=0.84,
                     tint_polarity=((0.88, 0.88, 0.90), (0.99, 0.99, 1.00)),
                     adaptive=0.10, brightness=1.0, saturation=1.0,
                     # a light window's edge is a thin bright line and a soft
                     # shadow, not the deep bevel a dark one can carry
                     specular=0.30, rim_gain=0.16, rim_ambient=0.42,
                     edge_shade=0.10, bezel_clarity=0.70,
                     shadow=0.30)

SHADOW = QColor(0, 0, 0, 130)


def level_style(level_db):
    """(label, accent, deep accent) for a level, or None when silent."""
    if level_db is None:
        return t("level.silent"), None, None
    if level_db >= HIGH_THRESHOLD:
        return t("level.veryloud"), RED, RED_DEEP
    if level_db >= LOUD_THRESHOLD:
        return t("level.loud"), YELLOW, YELLOW_DEEP
    return t("level.ok"), GREEN, GREEN_DEEP


def level_icon_name(level_db):
    if level_db is None:
        return None
    if level_db >= HIGH_THRESHOLD:
        return "octagon"
    return "warning" if level_db >= LOUD_THRESHOLD else "check"


def _icon(name, color, size):
    """Render an icon SVG at `size`, recoloured.

    Phosphor rather than Bootstrap: it is drawn monoline on a 256 grid in six
    hand-tuned weights, which is the closest open set to how SF Symbols are
    built. Bootstrap's icons are chunkier and inconsistent between glyphs.
    """
    path = os.path.join(HERE, "assets", "icons", name + ".svg")
    if not os.path.exists(path):
        path = os.path.join(HERE, "assets", "icons", name + ".svg")
    with open(path, "r", encoding="utf-8") as fh:
        svg = fh.read().replace("currentColor", color.name())
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(p)
    p.end()
    return img


def squircle_path(rect, radius, power=lg.CORNER_POWER, steps=16):
    """
    Continuous-curvature rounded rectangle - the shape Apple actually uses.

    Qt's addRoundedRect glues circular arcs onto straight edges, which leaves
    a visible break in curvature at the join. A superellipse corner
    (|x/r|^n + |y/r|^n = 1, n about 4-5) flattens the apex and blends into the
    edge smoothly, which is what reads as "Apple" at a glance.
    """
    x0, y0 = rect.x(), rect.y()
    x1, y1 = x0 + rect.width(), y0 + rect.height()
    r = min(radius, rect.width() / 2.0, rect.height() / 2.0)
    k = 2.0 / power

    path = QPainterPath()
    if r <= 0:
        path.addRect(rect)
        return path

    # each corner, clockwise: centre of curvature, then how the offset from
    # that centre sweeps from the corner's entry point to its exit point
    corners = (
        ((x1 - r, y0 + r), lambda s, c: (s, -c)),   # top right:    (0,-r) -> (r,0)
        ((x1 - r, y1 - r), lambda s, c: (c, s)),    # bottom right: (r,0)  -> (0,r)
        ((x0 + r, y1 - r), lambda s, c: (-s, c)),   # bottom left:  (0,r)  -> (-r,0)
        ((x0 + r, y0 + r), lambda s, c: (-c, -s)),  # top left:     (-r,0) -> (0,-r)
    )

    first = True
    for (cx, cy), sweep in corners:
        for i in range(steps + 1):
            t = (math.pi / 2.0) * i / steps
            dx, dy = sweep(r * (math.sin(t) ** k), r * (math.cos(t) ** k))
            if first:
                path.moveTo(cx + dx, cy + dy)
                first = False
            else:
                path.lineTo(cx + dx, cy + dy)
    path.closeSubpath()
    return path


def _text(painter, rect, flags, text, color, shadow=True):
    """Text with a 1 px dark drop, so it holds up over clear glass."""
    if shadow:
        painter.setPen(SHADOW)
        painter.drawText(rect.translated(0, 1), flags, text)
    painter.setPen(color)
    painter.drawText(rect, flags, text)


def _font(size, weight=QFont.Normal):
    return font(size, weight)


class PanelRenderer:
    """Builds the panel image. Filter fields are computed once and reused."""

    def __init__(self, width=W, height=H, radius=RADIUS, glass=None,
                 pad=SHADOW_PAD):
        self.w, self.h, self.radius = width, height, radius
        self.pad = pad
        self.cw, self.ch = width + pad * 2, height + pad * 2
        g = dict(GLASS)
        if glass:
            g.update(glass)
        self.g = g
        self._rebuild()

    def _rebuild(self):
        g = self.g
        # Every field below is geometry, computed once when the panel is
        # built. So they can be evaluated on a finer grid and averaged down
        # for free: the edge curve, the rim and the refraction gradient all
        # stop stair-stepping, and no frame pays for it.
        s = int(g.get("supersample", 1))
        W, Hh, R = self.w * s, self.h * s, self.radius * s
        bez, thick = g["bezel"] * s, g["thickness"] * s

        def down(a):
            if s == 1:
                return a
            return a.reshape(self.h, s, self.w, s).mean((1, 3)).astype(
                np.float32)

        profile = lg.build_profile(g["surface"], thick, bez, ior=g["ior"])
        dx, dy = lg.build_displacement(W, Hh, R, bez, profile,
                                       refraction=g["refraction"])
        self.dx, self.dy = down(dx) / s, down(dy) / s
        self.spec = down(lg.build_specular(W, Hh, R, bez,
                                           opacity=g["specular"],
                                           angle_deg=g["angle"]))
        self.mask = down(lg.build_mask(W, Hh, R, softness=s))
        add, mul = lg.build_edge_shading(
            W, Hh, R, bez,
            rim_gain=g.get("rim_gain", 0.11),
            edge_shade=g.get("edge_shade", 0.13),
            angle_deg=g["angle"],
            rim_ambient=g.get("rim_ambient", 0.28),
            scale=s)
        self.edge_add, self.edge_mul = down(add), down(mul)
        self.depth = down(lg.build_bezel_depth(W, Hh, R, bez))
        profile["max_shift"] /= s
        self.coords = lg.build_coords(self.w, self.h, self.dx, self.dy,
                                      g.get("dispersion", 0.0))

        # darken only the two text bands, so the middle stays clear glass
        scale = self.h / float(H)
        bands = [(6 * scale, 40 * scale), (44 * scale, 96 * scale)]
        self.scrim = lg.build_text_scrim(self.w, self.h, bands,
                                         strength=g.get("scrim", 0.38))

        self.shadow = self._build_shadow()
        self.max_shift = profile["max_shift"]
        self._bake()
        self._bake_split()

    def _build_shadow(self):
        """Soft drop shadow on the padded canvas, under the panel."""
        from scipy import ndimage
        g = self.g
        pad = self.pad
        a = np.zeros((self.ch, self.cw), dtype=np.float32)
        lift = int(g.get("shadow_lift", 5.0))
        a[pad + lift:pad + lift + self.h, pad:pad + self.w] = self.mask
        blur = g.get("shadow_blur", 17.0)
        if blur > 0:
            a = ndimage.gaussian_filter(a, blur / 2.0, mode="constant")
        return np.clip(a * g.get("shadow", 0.34), 0.0, 1.0)

    def _bake(self):
        """Collapse every backdrop-independent layer into constants.

        None of these change between frames, so folding them here turns six
        full-image passes per frame into one multiply and one add.
        """
        pad = self.pad
        mask = self.mask.astype(np.float32)

        self.shade = (self.edge_mul * (1.0 - self.scrim)).astype(np.float32)
        self.light = (self.edge_add + self.spec).astype(np.float32)

        # alpha of panel-over-shadow, and the factor that un-premultiplies it
        shadow_under = self.shadow[pad:pad + self.h, pad:pad + self.w]
        out_a = mask + shadow_under * (1.0 - mask)
        safe = np.where(out_a > 1e-4, out_a, 1.0)
        self.coef = (mask / safe).astype(np.float32)
        self.coef255 = (self.coef * 255.0).astype(np.float32)

        # persistent canvas: alpha is final everywhere, colour only changes
        # inside the panel rectangle
        canvas = np.zeros((self.ch, self.cw, 4), dtype=np.uint8)
        canvas[..., 3] = (np.clip(self.shadow, 0, 1) * 255).astype(np.uint8)
        canvas[pad:pad + self.h, pad:pad + self.w, 3] = \
            (np.clip(out_a, 0, 1) * 255).astype(np.uint8)
        self.canvas = canvas
        # one QImage over the persistent buffer: copying 2 MB every frame was
        # pure waste, the painter consumes it before the next write
        self._qimage = QImage(self.canvas.data, self.cw, self.ch,
                              self.cw * 4, QImage.Format_RGBA8888)

    def set_glass(self, **kwargs):
        """Update material values; rebuilds the fields when geometry changes."""
        heavy = {"surface", "bezel", "thickness", "ior", "refraction",
                 "specular", "angle", "scrim", "rim_gain", "edge_shade",
                 "rim_ambient", "dispersion", "shadow", "shadow_blur",
                 "shadow_lift"}
        self.g.update(kwargs)
        if heavy & set(kwargs):
            self._rebuild()

    # -- background ---------------------------------------------------------

    def _bake_split(self):
        """Pre-compute what the split renderer needs.

        The split works because of one fact about the static layers: the rim
        light and the alpha mask are zero and one respectively everywhere
        except the bezel, and the bezel darkening is flat inside it. So the
        interior depends only on the blurred backdrop and a smooth shade -
        both low-frequency, both safe at half resolution. Only the bezel band
        carries sharp detail, and it is a small share of the pixels.
        """
        h, w = self.h, self.w
        self.h2, self.w2 = h // 2, w // 2

        # The interior can be coarser still than the band source. Halving is
        # what the bezel needs, because it is the warp source; the interior is
        # only a blurred backdrop under a smooth shade. `interior_scale` is
        # that extra divisor, on top of the half the band already works at.
        # Worth about 2 ms of 24 on the exposure window - the tone arithmetic
        # is the only pass that scales with it, and it was smaller than the
        # blur, the shrink and the band warp that do not.
        self.qi = max(1, int(self.g.get("interior_scale", 1)))
        self.hi, self.wi = self.h2 // self.qi, self.w2 // self.qi
        self.fi = 2 * self.qi          # interior grid -> full resolution

        def coarse(field):
            f = self.fi
            a = field[:self.hi * f, :self.wi * f].astype(np.float32)
            return a.reshape(self.hi, f, self.wi, f).mean((1, 3))

        self.shade_half = coarse(self.shade)
        # The scrim is applied twice: once as a darkening folded into `shade`,
        # and once as a tone added back on top (see glass_image). The split
        # renderer needs the add-back at both resolutions.
        self.scrim_half = coarse(self.scrim)

        # Write band: only pixels the displacement actually moves, i.e. the
        # bezel itself. Beyond it the field is zero and the half-res interior
        # is already correct.
        self.band = int(self.g["bezel"]) + 6

        # Everything about the band geometry is fixed for the life of the
        # renderer, including the sampling coordinates once they are rebased
        # onto each block. Recomputing them per frame cost 6.3 ms.
        ms = int(self.max_shift) + 2
        self.band_jobs = []
        for ys, xs in self._bands():
            y0, y1 = max(0, ys.start - ms), min(h, ys.stop + ms)
            x0, x1 = max(0, xs.start - ms), min(w, xs.stop + ms)
            sy0, sy1 = y0 // 2, min(self.h2, -(-y1 // 2))
            sx0, sx1 = x0 // 2, min(self.w2, -(-x1 // 2))
            cc = []
            for c in range(3):
                k = self.coords[c][:, ys, xs].copy()
                k[0] -= sy0 * 2
                k[1] -= sx0 * 2
                cc.append(k)
            self.band_jobs.append({
                "ys": ys, "xs": xs,
                "src": (slice(sy0, sy1), slice(sx0, sx1)),
                "coords": cc,
                "shade": self.shade[ys, xs, None].copy(),
                "light": self.light[ys, xs, None].copy(),
                "scrim": self.scrim[ys, xs, None].astype(np.float32),
                "coef": self.coef255[ys, xs, None].copy(),
                "shape": (ys.stop - ys.start, xs.stop - xs.start, 3),
                # tint thinned towards the outer edge so the lens is visible
                "alpha": (self.g["tint_alpha"] * (
                    1.0 - self.g.get("bezel_clarity", 0.0)
                    * (1.0 - self.depth[ys, xs]))
                )[..., None].astype(np.float32),
            })

    def _bands(self):
        h, w, b = self.h, self.w, self.band
        return ((slice(0, b), slice(0, w)),
                (slice(h - b, h), slice(0, w)),
                (slice(b, h - b), slice(0, b)),
                (slice(b, h - b), slice(w - b, w)))

    def glass_image_split(self, backdrop):
        """Bezel at full resolution, everything else at half.

        What actually makes the bezel look sharp is the displacement field,
        the rim line and the alpha edge - not the source pixels, which are
        deliberately blurred. So the blur itself can run entirely at half
        resolution, and only those three sharp layers need full pixels. That
        leaves the expensive work proportional to the bezel band alone.

        Nothing full-size is ever held as float: the interior is converted to
        bytes while still small and expanded straight into the canvas.
        """
        from scipy import ndimage
        g = self.g
        h, w = self.h, self.w
        h2, w2 = self.h2, self.w2
        pad = self.pad

        # Four strided adds beat reshape().mean(): the reshape turns the
        # 2x2 average into a gather over a 5-D view, which numpy walks
        # element by element.
        src = backdrop
        a = src[0:h2 * 2:2, 0:w2 * 2:2]
        b = src[1:h2 * 2:2, 0:w2 * 2:2]
        c_ = src[0:h2 * 2:2, 1:w2 * 2:2]
        d = src[1:h2 * 2:2, 1:w2 * 2:2]
        if src.dtype == np.uint8:
            # cast the quarter-size slices, not the full frame: same number of
            # converted values, but it rides along with the shrink
            small = (a.astype(np.float32) + b + c_ + d)
            small *= np.float32(0.25 / 255.0)
        else:
            small = (a + b + c_ + d) * np.float32(0.25)
        for c in range(3):
            ndimage.gaussian_filter(small[..., c], g["blur"] / 4.0,
                                    output=small[..., c], mode="nearest",
                                    truncate=2.5)
        blurred = small.copy()          # warp source, before any tinting

        # The bezel warps `blurred` and needs it at half; the interior only
        # gets a flat tone, so on a large surface it runs one step coarser
        # again. Every pass below is then over a quarter of the pixels.
        if self.qi > 1:
            q, hi, wi = self.qi, self.hi, self.wi
            acc = small[0:hi * q:q, 0:wi * q:q].copy()
            for oy in range(q):
                for ox in range(q):
                    if oy or ox:
                        acc += small[oy:hi * q:q, ox:wi * q:q]
            small = acc
            small *= np.float32(1.0 / (q * q))

        # --- tone, at the interior resolution ------------------------------
        tint = g["tint"]
        lum = float(0.2126 * small[..., 0].mean()
                    + 0.7152 * small[..., 1].mean()
                    + 0.0722 * small[..., 2].mean())
        kk = float(np.clip((lum - 0.20) / 0.38, 0.0, 1.0))
        polarity = kk * kk * (3.0 - 2.0 * kk)
        if g.get("tint_polarity"):
            dk, lt = g["tint_polarity"]
            tint = tuple(d * polarity + l * (1.0 - polarity)
                         for d, l in zip(dk, lt))
        tint_arr = np.array(tint, dtype=np.float32)
        if g.get("adaptive", 0.0) > 0.0:
            a = g["adaptive"]
            tint_arr = tint_arr * (1.0 - a) + small.reshape(-1, 3).mean(0) * a

        keep = 1.0 - g["tint_alpha"]
        bias = tint_arr * g["tint_alpha"]
        sat = g["saturation"]
        if sat != 1.0:
            luma = (0.2126 * small[..., 0] + 0.7152 * small[..., 1]
                    + 0.0722 * small[..., 2])
            luma *= (1.0 - sat) * g["brightness"] * keep
            small *= sat * g["brightness"] * keep
            small += luma[..., None]
        else:
            small *= g["brightness"] * keep
        small += bias
        small *= self.shade_half[..., None]

        # the scrim has taken its share out of the image; put back a tone that
        # goes dark over bright content and light over dark, so text keeps its
        # contrast either way. Without this the text bands read as black holes.
        target = g.get("scrim_light", 0.80) * (1.0 - polarity)
        if target > 0.0:
            small += self.scrim_half[..., None] * target
        small *= 255.0
        np.clip(small, 0, 255, out=small)

        # bytes while still small, then expanded straight into the canvas
        f = self.fi
        tile = np.repeat(np.repeat(small.astype(np.uint8), f, axis=0), f,
                         axis=1)
        th, tw = self.hi * f, self.wi * f
        dst = self.canvas[pad:pad + h, pad:pad + w, :3]
        dst[:th, :tw] = tile
        if th < h:
            dst[th:, :tw] = tile[-1:, :]
        if tw < w:
            dst[:, tw:] = dst[:, tw - 1:tw]

        # --- bezel band, full resolution -----------------------------------
        for job in self.band_jobs:
            block = np.repeat(np.repeat(blurred[job["src"]], 2, axis=0),
                              2, axis=1)
            res = np.empty(job["shape"], dtype=np.float32)
            for c in range(3):
                ndimage.map_coordinates(block[..., c], job["coords"][c],
                                        order=1, mode="nearest",
                                        output=res[..., c])
            a = job["alpha"]
            if sat != 1.0:
                luma = (0.2126 * res[..., 0] + 0.7152 * res[..., 1]
                        + 0.0722 * res[..., 2])[..., None]
                luma *= (1.0 - sat) * g["brightness"] * (1.0 - a)
                res *= sat * g["brightness"] * (1.0 - a)
                res += luma
            else:
                res *= g["brightness"] * (1.0 - a)
            res += tint_arr * a
            res *= job["shade"]
            if target > 0.0:
                res += job["scrim"] * target
            res += job["light"]
            np.clip(res, 0.0, 1.0, out=res)
            res *= job["coef"]
            dst[job["ys"], job["xs"]] = res.astype(np.uint8)

        return self._qimage

    def glass_image(self, backdrop):
        """Refract a captured backdrop, then sit it on its own drop shadow.

        `backdrop` is panel-sized; the returned image is canvas-sized, with
        the shadow drawn as plain black alpha so Qt composites it over the
        real desktop - no need to capture outside the panel.
        """
        g = self.g
        rgb = lg.render(backdrop, self.dx, self.dy, None, self.spec,
                        blur=g["blur"], saturation=g["saturation"],
                        brightness=g["brightness"],
                        tint=g["tint"], tint_alpha=g["tint_alpha"],
                        coords=self.coords,
                        adaptive=g.get("adaptive", 0.0),
                        tint_polarity=g.get("tint_polarity"),
                        scrim_layer=self.scrim,
                        scrim_light=g.get("scrim_light", 0.80),
                        shade=self.shade, light=self.light,
                        ring=int(g["bezel"]) + int(self.max_shift) + 2)

        # coef un-premultiplies the shadow underneath and the 255 scaling is
        # folded into the same constant, so this is one pass, not three
        rgb *= self.coef255[..., None]

        pad = self.pad
        canvas = self.canvas
        np.clip(rgb, 0, 255, out=rgb)
        canvas[pad:pad + self.h, pad:pad + self.w, :3] = rgb.astype(np.uint8)
        return self._qimage

    # -- content ------------------------------------------------------------

    def draw_content(self, painter, level_db, playing=True,
                     device="", volume_pct=None):
        w, h = self.w, self.h
        pad = 18

        # the canvas carries the drop shadow, so content sits inset by it
        painter.save()
        painter.translate(self.pad, self.pad)

        live = playing and level_db is not None
        word, accent, accent_deep = level_style(level_db if live else None)
        if accent is None:
            accent, accent_deep = GREEN, GREEN_DEEP

        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        # --- header: device + Windows volume
        head_y = 14
        icon = _icon("headphones", QColor(235, 235, 245, 200), 17)
        painter.drawImage(pad, head_y + 3, icon)

        painter.setFont(_font(10, QFont.Medium))
        _text(painter, QRectF(pad + 25, head_y, w - pad * 2 - 80, 22),
              Qt.AlignLeft | Qt.AlignVCenter, device, WHITE)

        if volume_pct is not None:
            painter.setFont(_font(10))
            _text(painter, QRectF(0, head_y, w - pad, 22),
                  Qt.AlignRight | Qt.AlignVCenter,
                  "{:.0f} %".format(volume_pct), DIM)

        # hairline
        sep_y = head_y + 28
        painter.fillRect(QRectF(pad, sep_y, w - pad * 2, 1), HAIRLINE)

        # --- caption
        cap_y = sep_y + 8
        painter.setFont(_font(9))
        _text(painter, QRectF(pad, cap_y, w, 16),
              Qt.AlignLeft | Qt.AlignVCenter, t("panel.headphone_volume"), DIM)

        # --- status row
        row_y = cap_y + 18
        if not live:
            value, icon_img, word_color = t("panel.no_value"), None, DIM
        else:
            value = "{:.0f} dB".format(level_db)
            word_color = WHITE
            icon_img = _icon(level_icon_name(level_db), accent, 16)

        x = pad
        if icon_img is not None:
            painter.drawImage(x, row_y + 5, icon_img)
            x += 23

        painter.setFont(_font(13, QFont.DemiBold))
        _text(painter, QRectF(x, row_y, w, 24),
              Qt.AlignLeft | Qt.AlignVCenter, word, word_color)
        _text(painter, QRectF(0, row_y, w - pad, 24),
              Qt.AlignRight | Qt.AlignVCenter, value, WHITE if live else DIM)

        # --- meter
        bar_top = row_y + 32
        bar_h, mark_h, gap = 20, 28, 3
        inner = w - pad * 2
        seg_w = (inner - gap * (SEGMENTS - 1)) / SEGMENTS

        if live:
            frac = (level_db - DB_LO) / (DB_HI - DB_LO)
            lit = int(round(max(0.0, min(1.0, frac)) * SEGMENTS))
        else:
            lit = 0

        base = bar_top + mark_h
        for i in range(SEGMENTS):
            sx = pad + i * (seg_w + gap)
            is_mark = (i == THRESHOLD_INDEX)   # fixed 80 dB marker
            on = i < lit
            sh = mark_h if is_mark else bar_h
            if is_mark:
                color = accent_deep if on else TRACK_MARK
            else:
                color = accent if on else TRACK
            path = QPainterPath()
            path.addRoundedRect(QRectF(sx, base - sh, seg_w, sh), 3, 3)
            painter.fillPath(path, color)

        # --- scale
        painter.setFont(_font(8))
        ticks_y = base + 5
        mark_cx = pad + THRESHOLD_INDEX * (seg_w + gap) + seg_w / 2
        _text(painter, QRectF(pad, ticks_y, 40, 14),
              Qt.AlignLeft | Qt.AlignTop, "20", FAINT)
        _text(painter, QRectF(mark_cx - 20, ticks_y, 40, 14),
              Qt.AlignHCenter | Qt.AlignTop, "80", FAINT)
        _text(painter, QRectF(w - pad - 40, ticks_y, 40, 14),
              Qt.AlignRight | Qt.AlignTop, "110", FAINT)

        painter.restore()

    def render(self, backdrop, level_db, playing=True, **kw):
        img = self.glass_image(backdrop)
        p = QPainter(img)
        self.draw_content(p, level_db, playing, **kw)
        p.end()
        return img
