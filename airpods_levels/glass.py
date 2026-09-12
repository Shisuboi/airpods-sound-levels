"""
Liquid Glass material, rendered onto a captured screen region.

This is a numpy port of the browser implementation in
"Downloads/test liquid glass" (Snell-Descartes refraction -> displacement map,
after https://kube.io/blog/liquid-glass-css-svg/).

Why a port rather than reusing that code directly: `backdrop-filter` only
samples pixels that live inside the page. A floating desktop panel has the
Windows desktop behind it, which no webview can hand to CSS. So the desktop
region is captured, refracted here, and painted as the panel background.

Pipeline, identical to the original:
  1. height function f(x) over the bezel, x in [0, 1]
  2. surface normal from f'(x), rescaled by thickness / bezelWidth
  3. Snell-Descartes refraction of a vertical ray (n1 = 1, n2 = ior)
  4. march the ray to the background plane -> lateral shift in px
  5. a rounded-rect signed distance field gives, for every pixel, its distance
     to the border and the inward normal; shift * normal is the displacement
"""

import ctypes
import numpy as np
from scipy import ndimage

# --- step 1: surface height functions ---------------------------------------


def _convex_circle(x):
    return np.sqrt(np.clip(1.0 - (1.0 - x) ** 2, 0.0, None))


def _convex_squircle(x):
    return (1.0 - (1.0 - x) ** 4) ** 0.25


def _smootherstep(x):
    return x * x * x * (x * (x * 6 - 15) + 10)


SURFACES = {
    "convex-circle": _convex_circle,
    "convex-squircle": _convex_squircle,
    "concave": lambda x: 1.0 - _convex_circle(x),
    "lip": lambda x: (_convex_squircle(x) * (1 - _smootherstep(x))
                      + (1 - _convex_squircle(x)) * _smootherstep(x)),
}

_DELTA = 0.0015  # finite-difference step for f'(x)


# --- steps 2-4: radial profile of lateral shifts ----------------------------


def build_profile(surface="convex-squircle", thickness=26.0, bezel_width=18.0,
                  ior=1.5, samples=128):
    """Lateral shift in px for every normalised depth into the bezel."""
    f = SURFACES.get(surface, _convex_squircle)
    bezel_width = max(1.0, float(bezel_width))

    x = np.linspace(0.0, 1.0, samples)
    y1 = f(np.clip(x - _DELTA, 0.0, 1.0))
    y2 = f(np.clip(x + _DELTA, 0.0, 1.0))
    slope = (y2 - y1) / (2 * _DELTA) * (thickness / bezel_width)

    # normal = tangent (1, slope) rotated -90 deg
    length = np.hypot(-slope, 1.0)
    nx = -slope / length
    ny = 1.0 / length

    eta = 1.0 / ior
    cos_i = ny
    k = 1.0 - eta * eta * (1.0 - cos_i * cos_i)
    cos_t = np.sqrt(np.clip(k, 0.0, None))
    factor = eta * cos_i - cos_t
    rx = factor * nx
    ry = -eta + factor * ny

    height = f(np.clip(x, 0.0, 1.0)) * thickness
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(ry < 0, height / -ry, 0.0)
    shifts = np.where((k >= 0) & (ry < 0), rx * t, 0.0)

    max_shift = float(np.max(np.abs(shifts))) or 1e-6
    return {"shifts": shifts, "max_shift": max_shift}


# --- rounded-rectangle signed distance field --------------------------------


# Apple's corners are not circular arcs but continuous-curvature squircles.
# Raising the norm used in the corner region from 2 (a circle) to about 4-5
# reproduces that flatter apex blending smoothly into the straight edge.
CORNER_POWER = 4.2


def _sd_round_rect(px, py, hw, hh, r, power=2.0):
    qx = np.abs(px) - hw + r
    qy = np.abs(py) - hh + r
    ax = np.maximum(qx, 0.0)
    ay = np.maximum(qy, 0.0)
    if power == 2.0:
        corner = np.hypot(ax, ay)
    else:
        # Only the four corner squares have both components non-zero; along
        # the straight edges one of them is 0 and the p-norm collapses to a
        # plain sum. Restricting the pow to those few pixels turns a
        # whole-image transcendental into a rounding error.
        corner = ax + ay
        both = (ax > 0) & (ay > 0)
        if both.any():
            cx, cy = ax[both], ay[both]
            corner[both] = (cx ** power + cy ** power) ** (1.0 / power)
    return np.minimum(np.maximum(qx, qy), 0.0) + corner - r


_sdf_cache = {}


def _sdf_grids(w, h, radius, power=CORNER_POWER):
    """Distance to the border and the outward normal.

    Cached: five different layers ask for the same field while a panel is
    being built, and at supersampled sizes recomputing it each time was the
    whole build cost.
    """
    key = (w, h, round(float(radius), 3), round(float(power), 3))
    hit = _sdf_cache.get(key)
    if hit is not None:
        return hit

    hw, hh = w / 2.0, h / 2.0
    radius = min(radius, hw, hh)

    # The field is separable until the very last combine: qx depends only on
    # the column, qy only on the row. Building them as (1, w) and (h, 1) and
    # letting numpy broadcast avoids materialising several full-size grids -
    # mgrid alone was allocating two 33 MB integer arrays at supersampled
    # sizes.
    px = (np.arange(w, dtype=np.float32) + 0.5 - hw)[None, :]
    py = (np.arange(h, dtype=np.float32) + 0.5 - hh)[:, None]
    qx = np.abs(px) - hw + radius
    qy = np.abs(py) - hh + radius
    ax = np.maximum(qx, 0.0)
    ay = np.maximum(qy, 0.0)

    corner = ax + ay                       # exact wherever one side is zero
    both = (ax > 0) & (ay > 0)
    if power != 2.0 and both.any():
        cx = np.broadcast_to(ax, both.shape)[both]
        cy = np.broadcast_to(ay, both.shape)[both]
        corner[both] = (cx ** power + cy ** power) ** (1.0 / power)
    elif power == 2.0 and both.any():
        cx = np.broadcast_to(ax, both.shape)[both]
        cy = np.broadcast_to(ay, both.shape)[both]
        corner[both] = np.hypot(cx, cy)

    dist = -(np.minimum(np.maximum(qx, qy), 0.0) + corner - radius)

    # Outward normal, analytically. Four finite-difference evaluations of the
    # same field were four fifths of the cost for a vector we can write down.
    sx, sy = np.sign(px), np.sign(py)
    nx = sx * ax + np.zeros_like(py)
    ny = sy * ay + np.zeros_like(px)
    flat = ~both & (ax <= 0) & (ay <= 0)
    if flat.any():
        use_x = np.broadcast_to(qx >= qy, flat.shape)
        nx = np.where(flat, np.where(use_x, sx, 0.0), nx)
        ny = np.where(flat, np.where(use_x, 0.0, sy), ny)
    gl = np.hypot(nx, ny)
    gl[gl == 0] = 1.0

    out = (dist, nx / gl, ny / gl)
    if len(_sdf_cache) > 8:
        _sdf_cache.clear()
    _sdf_cache[key] = out
    return out


# --- step 5: displacement field ---------------------------------------------


def build_displacement(w, h, radius, bezel_width, profile, refraction=1.0):
    """Per-pixel (dx, dy) sampling offsets, in pixels."""
    bezel_width = max(1.0, float(bezel_width))
    dist, ox, oy = _sdf_grids(w, h, radius)

    t = np.clip(dist / bezel_width, 0.0, 1.0)
    shifts = np.interp(t, np.linspace(0.0, 1.0, profile["shifts"].size),
                       profile["shifts"])

    inside_bezel = (dist > 0) & (dist < bezel_width)
    shifts = np.where(inside_bezel, shifts, 0.0) * refraction

    # displacement points inwards, orthogonal to the border
    return -ox * shifts, -oy * shifts


def build_mask(w, h, radius, softness=1.0):
    """Antialiased coverage mask of the rounded rectangle."""
    dist, _, _ = _sdf_grids(w, h, radius)
    return np.clip(dist / softness + 0.5, 0.0, 1.0)


def build_bezel_depth(w, h, radius, bezel_width):
    """0 at the outer edge, 1 at the inner end of the bezel and beyond.

    Used to thin the tint inside the bezel: a uniform veil over the whole
    panel hides the very refraction the bezel exists to show.
    """
    bezel_width = max(1.0, float(bezel_width))
    dist, _, _ = _sdf_grids(w, h, radius)
    return np.clip(dist / bezel_width, 0.0, 1.0).astype(np.float32)


# --- specular rim -----------------------------------------------------------


def build_edge_shading(w, h, radius, bezel_width, rim_gain=0.12,
                       edge_shade=0.14, angle_deg=-95.0, rim_ambient=0.30,
                       scale=1):
    """
    Edge treatment that does NOT depend on the backdrop.

    Refraction alone is invisible over a flat background: bending uniform
    pixels yields uniform pixels. Real glass still reads because its edge
    catches light and darkens.

    The rim is mostly *directional*: a ring of even brightness all the way
    round reads as a plastic frame, not glass. `rim_ambient` is how much of
    the rim survives on the unlit sides.
    """
    bezel_width = max(1.0, float(bezel_width))
    dist, ox, oy = _sdf_grids(w, h, radius)

    angle = np.radians(angle_deg)
    facing = ox * np.cos(angle) + oy * np.sin(angle)
    front = np.clip(facing, 0.0, None)
    back = np.clip(-facing, 0.0, None)
    # two opposite arcs, the way a lit glass edge actually behaves
    directional = front ** 1.5 + 0.55 * back ** 2.0
    weight = rim_ambient + (1.0 - rim_ambient) * np.clip(directional, 0.0, 1.0)

    # a thinner, fully graded rim: the wide one clipped to flat white and
    # read as a painted stroke rather than light caught on an edge
    u = (dist - 0.8 * scale) / (0.85 * scale)
    rim = np.where(dist > 0, np.exp(-u * u), 0.0)

    t = np.clip(dist / bezel_width, 0.0, 1.0)
    inner = np.where((dist > 0) & (dist < bezel_width), (1.0 - t) ** 2, 0.0)

    return (rim * weight * rim_gain).astype(np.float32), \
           (1.0 - inner * edge_shade).astype(np.float32)


def build_coords(w, h, dx, dy, dispersion=0.0):
    """Pre-compute the sampling grids once; they never change while open."""
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    out = []
    for k in (1.0 - dispersion, 1.0, 1.0 + dispersion):
        out.append(np.array([ys + dy * k, xs + dx * k], dtype=np.float32))
    return out


def build_specular(w, h, radius, bezel_width, opacity=0.6, angle_deg=-120.0):
    bezel_width = max(1.0, float(bezel_width))
    dist, ox, oy = _sdf_grids(w, h, radius)

    angle = np.radians(angle_deg)
    facing = ox * np.cos(angle) + oy * np.sin(angle)
    front = np.clip(facing, 0.0, None)
    back = np.clip(-facing, 0.0, None)
    directional = front ** 1.7 + 0.45 * back ** 2.4

    t = np.clip(dist / bezel_width, 0.0, 1.0)
    u = (dist - 1.1) / 1.7
    line = np.exp(-u * u)
    glow = (1.0 - t) ** 3

    alpha = np.clip(0.92 * line + 0.4 * glow, 0.0, 1.0) * directional
    alpha = np.where((dist > 0) & (dist < bezel_width), alpha, 0.0)
    return np.clip(alpha * opacity, 0.0, 1.0)


# --- compositing ------------------------------------------------------------


def render(backdrop, dx, dy, mask, specular,
           blur=10.0, saturation=1.35, brightness=1.06,
           tint=(1.0, 1.0, 1.0), tint_alpha=0.10, dispersion=0.0,
           scrim=None, edge_add=None, edge_mul=None, coords=None,
           adaptive=0.0, shade=None, light=None, tint_polarity=None,
           scrim_layer=None, scrim_light=0.80, ring=0):
    """
    backdrop   : (h, w, 3) float array in 0..1, the captured desktop region
    dispersion : chromatic aberration at the bezel. Real glass refracts blue
                 more than red, which is what gives Apple's edges their faint
                 colour fringe. 0 disables it.
    scrim      : optional (h, w) alpha array, applied on top of the flat tint.
                 Used to darken only where text sits, instead of veiling the
                 whole panel and killing the transparency.
    returns    : (h, w, 4) float RGBA in 0..1
    """
    h, w = backdrop.shape[:2]
    # float32 and in-place arithmetic throughout. The naive version spent more
    # time allocating temporaries than doing the blur and the warp combined.
    out = (backdrop if backdrop.dtype == np.float32
           else backdrop.astype(np.float32)).copy()

    # 1. blur, as a CSS backdrop-filter would (sigma ~ radius / 2).
    #    truncate 2.5 rather than scipy's default 4.0: a 35 % smaller kernel
    #    for a peak error of 0.0025, under one level out of 255.
    if blur > 0:
        sigma = blur / 2.0
        for c in range(3):
            ndimage.gaussian_filter(out[..., c], sigma, output=out[..., c],
                                    mode="nearest", truncate=2.5)

    # 2. bend the backdrop along the refraction field, per channel when
    #    dispersion is on (red bends least, blue most).
    if coords is None:
        coords = build_coords(w, h, dx, dy, dispersion)

    if ring:
        # The displacement is exactly zero outside the bezel, so warping the
        # whole image was doing ~90 % of that work for no visible result.
        # Only the four border strips move. Every strip is computed from the
        # unmodified image first, then written back, so no strip reads a
        # neighbour that has already shifted.
        b = min(int(ring) + 2, h // 2, w // 2)
        strips = ((slice(0, b), slice(0, w)),
                  (slice(h - b, h), slice(0, w)),
                  (slice(b, h - b), slice(0, b)),
                  (slice(b, h - b), slice(w - b, w)))
        done = []
        for ys, xs in strips:
            chans = [ndimage.map_coordinates(out[..., c], coords[c][:, ys, xs],
                                             order=1, mode="nearest")
                     for c in range(3)]
            done.append((ys, xs, chans))
        for ys, xs, chans in done:
            for c in range(3):
                out[ys, xs, c] = chans[c]
    else:
        warped = np.empty_like(out)
        for c in range(3):
            ndimage.map_coordinates(out[..., c], coords[c], order=1,
                                    mode="nearest", output=warped[..., c])
        out = warped

    # 3-4. saturation, brightness and tint collapse into one multiply-add.
    #      sat/bright:  out*s*b + luma*(1-s)*b
    #      tint:        x*(1-a) + tint*a
    #      together:    out*(s*b*(1-a)) + luma*((1-s)*b*(1-a)) + tint*a
    # Adaptive legibility, the behaviour that separates Apple's "Regular"
    # glass from a plain scrim: the material darkens over bright content and
    # LIGHTENS over dark content. Only ever darkening is why this vanished on
    # a dark desktop while looking right on a light one.
    lum = float(0.2126 * out[..., 0].mean() + 0.7152 * out[..., 1].mean()
                + 0.0722 * out[..., 2].mean())
    k = np.clip((lum - 0.20) / 0.38, 0.0, 1.0)
    polarity = float(k * k * (3.0 - 2.0 * k))   # 1 = bright behind, 0 = dark
    if tint_polarity is not None:
        dark_tint, light_tint = tint_polarity
        tint = tuple(d * polarity + l * (1.0 - polarity)
                     for d, l in zip(dark_tint, light_tint))

    tint_arr = np.array(tint, dtype=np.float32)
    if adaptive > 0.0:
        # real glass picks up the colour of what is behind it
        ambient = out.reshape(-1, 3).mean(axis=0)
        tint_arr = tint_arr * (1.0 - adaptive) + ambient * adaptive

    keep = 1.0 - tint_alpha
    if saturation != 1.0:
        luma = (0.2126 * out[..., 0] + 0.7152 * out[..., 1]
                + 0.0722 * out[..., 2])
        luma *= (1.0 - saturation) * brightness * keep
        out *= saturation * brightness * keep
        out += luma[..., None]
    else:
        out *= brightness * keep
    out += tint_arr * tint_alpha

    # 5. one static multiply: bezel darkening and the text scrim, pre-merged
    if shade is not None:
        out *= shade[..., None]
    else:
        if edge_mul is not None:
            out *= edge_mul[..., None]
        if scrim is not None:
            out *= (1.0 - scrim)[..., None]

    # 5b. the scrim behind text has already removed its share of the image;
    #     put back a tone that goes dark over bright content and light over
    #     dark content, so the text keeps contrast either way
    if scrim_layer is not None:
        target = scrim_light * (1.0 - polarity)
        if target > 0.0:
            out += scrim_layer[..., None] * target

    # 6. one static add: rim line and specular, like mix-blend-mode plus-lighter
    if light is not None:
        out += light[..., None]
    else:
        if edge_add is not None:
            out += edge_add[..., None]
        out += specular[..., None]

    np.clip(out, 0.0, 1.0, out=out)
    if mask is None:
        return out          # caller composites the alpha itself
    return np.dstack([out, mask])


def build_text_scrim(w, h, bands, blur=18.0, strength=0.5):
    """
    A soft darkening only where text sits.

    `bands` is a list of (top, bottom) pixel ranges. Each becomes a soft
    horizontal band, so the middle of the panel stays clear glass while the
    lines of text keep their contrast.
    """
    scrim = np.zeros((h, w), dtype=np.float64)
    for top, bottom in bands:
        top = max(0, int(top))
        bottom = min(h, int(bottom))
        if bottom > top:
            scrim[top:bottom, :] = 1.0
    if blur > 0:
        scrim = ndimage.gaussian_filter(scrim, blur / 2.0, mode="nearest")
    return np.clip(scrim * strength, 0.0, 1.0)


class ScreenGrabber:
    """Desktop capture straight through GDI.

    Qt's grabWindow builds a QPixmap, converts it to a QImage and then hands
    over a copy - three passes over the frame for one read. A BitBlt into a
    bitmap we keep alive does it in one, at roughly half the cost. The device
    contexts and the target bitmap are created once and reused.
    """

    _SRCCOPY = 0x00CC0020

    class _BMI(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("w", ctypes.c_int32),
                    ("h", ctypes.c_int32), ("planes", ctypes.c_uint16),
                    ("bits", ctypes.c_uint16), ("comp", ctypes.c_uint32),
                    ("sizeimage", ctypes.c_uint32), ("xppm", ctypes.c_int32),
                    ("yppm", ctypes.c_int32), ("used", ctypes.c_uint32),
                    ("important", ctypes.c_uint32)]

    def __init__(self):
        self._user32 = ctypes.windll.user32
        self._gdi32 = ctypes.windll.gdi32
        self._screen = self._user32.GetDC(0)
        self._mem = self._gdi32.CreateCompatibleDC(self._screen)
        self._bmp = None
        self._size = None
        self._buf = None

    def _ensure(self, w, h):
        if self._size == (w, h):
            return
        if self._bmp:
            self._gdi32.DeleteObject(self._bmp)
        self._bmp = self._gdi32.CreateCompatibleBitmap(self._screen, w, h)
        self._gdi32.SelectObject(self._mem, self._bmp)
        self._buf = ctypes.create_string_buffer(w * h * 4)
        # negative height = top-down rows, so no flip afterwards
        self._bmi = self._BMI(ctypes.sizeof(self._BMI), w, -h, 1, 32,
                              0, 0, 0, 0, 0, 0)
        self._size = (w, h)

    def grab(self, x, y, w, h):
        """(h, w, 3) uint8 in RGB order, a view on the reused buffer."""
        self._ensure(w, h)
        self._gdi32.BitBlt(self._mem, 0, 0, w, h, self._screen, x, y,
                           self._SRCCOPY)
        self._gdi32.GetDIBits(self._mem, self._bmp, 0, h, self._buf,
                              ctypes.byref(self._bmi), 0)
        bgra = np.frombuffer(self._buf, np.uint8).reshape(h, w, 4)
        return bgra[..., 2::-1]          # BGRA -> RGB, no copy

    def close(self):
        if self._bmp:
            self._gdi32.DeleteObject(self._bmp)
            self._bmp = None
        self._gdi32.DeleteDC(self._mem)
        self._user32.ReleaseDC(0, self._screen)


_grabber = None


def grab_screen(x, y, w, h):
    """Shared GDI grabber; (h, w, 3) uint8 RGB."""
    global _grabber
    if _grabber is None:
        _grabber = ScreenGrabber()
    return _grabber.grab(x, y, w, h)


def capture_screen(x, y, w, h, as_float=True):
    """Grab a desktop region as (h, w, 3).

    `as_float=False` returns the raw bytes. Converting a full-size frame to
    float costs about as much as the grab itself, and a caller that shrinks
    the image first can do the conversion on a quarter of the data.
    """
    from PySide6.QtGui import QGuiApplication, QImage

    screen = QGuiApplication.primaryScreen()
    pixmap = screen.grabWindow(0, x, y, w, h)
    image = pixmap.toImage().convertToFormat(QImage.Format_RGB888)

    ptr = image.constBits()
    arr = np.frombuffer(ptr, dtype=np.uint8)
    arr = arr.reshape(image.height(), image.bytesPerLine() // 3, 3)
    arr = arr[:, :image.width(), :]
    if not as_float:
        return arr.copy()
    # float32, not float64: the whole pipeline is float32, so returning
    # doubles only bought one extra full-image conversion per frame
    return arr.astype(np.float32) / np.float32(255.0)
