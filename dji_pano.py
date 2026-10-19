#!/usr/bin/env python3
"""
DJI Pano Builder
================

Quality stitching of a spherical panorama from a set of DJI drone frames
(Sphere Panorama / 360 panorama mode).

Idea: DJI panorama frames already carry the exact gimbal orientation
(yaw/pitch/roll) in their XMP metadata. Instead of expensive feature
matching we use that orientation directly and reproject everything onto a
sphere (equirectangular).

Input : a folder with PANO*.JPG frames (usually 25: 3 rings x 8 + nadir).
Output: a 2:1 equirectangular panorama with GPano metadata + preview.

Dependencies: numpy, Pillow.

Examples:
    python3 dji_pano.py PANORAMA/006_0004 -o 006_0004_pano.jpg
    python3 dji_pano.py PANORAMA            # process every folder
"""

import argparse
import glob
import math
import os
import re
import shutil
import sys
import threading
import time

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Пресеты камер DJI (широкая камера). Размер сенсора нужен для пересчёта
# фокусного расстояния из EXIF (мм) в пиксели.
# ---------------------------------------------------------------------------
CAMERA_PRESETS = {
    # model: (sensor_width_mm, sensor_height_mm)
    "M3E": (17.3, 13.0),   # DJI Mavic 3 Enterprise, 4/3 CMOS 20MP
    "M3T": (17.3, 13.0),
    "M30": (12.0, 9.0),    # DJI Matrice 30 wide (1/2", ~8.0mm) — уточняется
    "FC8282": (17.3, 13.0),
}
DEFAULT_SENSOR = (17.3, 13.0)  # 4/3"


# ---------------------------------------------------------------------------
# Terminal UI: colours, progress bar, ETA, spinner
# ---------------------------------------------------------------------------

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _supports_color():
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


class UI:
    """Small helper for pretty, English-language terminal output."""

    COLORS = {
        "cyan": "\033[36m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "red": "\033[31m",
        "blue": "\033[34m",
        "dim": "\033[2m",
        "bold": "\033[1m",
        "reset": "\033[0m",
    }

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.color = enabled and _supports_color()
        self.width = max(40, shutil.get_terminal_size((90, 20)).columns)

    def paint(self, text, *styles):
        if not self.color or not styles:
            return text
        return "".join(self.COLORS.get(s, "") for s in styles) + text + self.COLORS["reset"]

    def line(self, text=""):
        if self.enabled:
            print(text)

    def info(self, text):
        if self.enabled:
            print(f"{self.paint('•', 'cyan')} {text}")

    def success(self, text):
        if self.enabled:
            print(f"{self.paint('✔', 'green')} {text}")

    def warn(self, text):
        if self.enabled:
            print(f"{self.paint('!', 'yellow')} {text}")

    def error(self, text):
        print(f"{self.paint('✖', 'red')} {text}", file=sys.stderr)

    def banner(self):
        if not self.enabled:
            return
        print()
        print(self.paint("  DJI Pano Builder", "bold", "cyan")
              + self.paint("  ·  spherical panorama stitcher", "dim"))
        print(self.paint("  " + "─" * min(self.width - 2, 60), "dim"))

    def spinner(self, label):
        return _Spinner(self, label)


class _Spinner:
    """Animated indeterminate spinner (runs while a blocking step executes)."""

    def __init__(self, ui, label):
        self.ui = ui
        self.label = label
        self.enabled = ui.enabled and sys.stdout.isatty()
        self._stop = False
        self._thread = None

    def spin(self):
        i = 0
        while not self._stop:
            frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
            text = f"\r  {self.ui.paint(frame, 'cyan')} {self.label}..."
            sys.stdout.write(text)
            sys.stdout.flush()
            time.sleep(0.08)
            i += 1

    def __enter__(self):
        if self.enabled:
            self._thread = threading.Thread(target=self.spin, daemon=True)
            self._thread.start()
        elif self.ui.enabled:
            print(f"  {self.label}...")
        return self

    def _clear(self):
        if self.enabled:
            sys.stdout.write("\r" + " " * (self.ui.width - 1) + "\r")
            sys.stdout.flush()

    def __exit__(self, exc_type, exc, tb):
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._clear()
        return False


class _ProgressBar:
    """Progress bar with percentage, counter and ETA."""

    def __init__(self, ui, total, label="Rendering"):
        self.ui = ui
        self.total = max(1, total)
        self.label = label
        self.done = 0
        self.start = time.time()
        self.enabled = ui.enabled and sys.stdout.isatty()
        self.width = min(30, max(10, ui.width - 58))
        self._last_len = 0

    @staticmethod
    def _fmt(seconds):
        seconds = int(max(0, seconds))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def update(self, done, detail=""):
        self.done = done
        if not self.enabled:
            return
        frac = min(1.0, done / self.total)
        filled = int(round(frac * self.width))
        bar = "█" * filled + "░" * (self.width - filled)
        elapsed = time.time() - self.start
        if done > 0:
            eta = elapsed / done * (self.total - done)
            eta_txt = self._fmt(eta)
        else:
            eta_txt = "--:--"
        detail = f"  {detail}" if detail else ""
        text = (f"\r  {self.ui.paint(bar, 'cyan')} {frac * 100:5.1f}%  "
                f"{done:>3}/{self.total}  ETA {eta_txt}{detail}")
        pad = max(0, self._last_len - len(text) + 8)
        sys.stdout.write(text + " " * pad)
        sys.stdout.flush()
        self._last_len = len(text)

    def close(self, detail=""):
        if self.enabled:
            self.update(self.total, detail)
            sys.stdout.write("\n")
            sys.stdout.flush()
        elif self.ui.enabled:
            print(f"  {self.label}: {self.total}/{self.total} done")


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

XMP_NAMESPACE = r"drone-dji:"


def _first(match):
    return match.group(1) if match else None


def read_xmp_gimbal(path):
    """Возвращает (yaw, pitch, roll) в градусах из XMP drone-dji."""
    try:
        with Image.open(path) as im:
            xmp = im.info.get("xmp")
    except Exception:
        return None, None, None
    if not xmp:
        return None, None, None
    text = xmp.decode("utf-8", errors="replace") if isinstance(xmp, bytes) else str(xmp)

    def grab(name):
        return _first(re.search(XMP_NAMESPACE + name + r'="([^"]+)"', text))

    def to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return (
        to_float(grab("GimbalYawDegree")),
        to_float(grab("GimbalPitchDegree")),
        to_float(grab("GimbalRollDegree")),
    )


def read_exif_focal(path):
    """Возвращает (focal_mm, focal35_mm, make, model) из EXIF."""
    try:
        with Image.open(path) as im:
            ex = im.getexif() or {}
    except Exception:
        ex = {}

    def rat_to_float(v):
        try:
            if hasattr(v, "numerator") and hasattr(v, "denominator"):
                return float(v.numerator) / float(v.denominator)
            return float(v)
        except (TypeError, ValueError):
            return None

    return (
        rat_to_float(ex.get(37386)),   # FocalLength
        rat_to_float(ex.get(41989)),   # FocalLengthIn35mmFilm
        ex.get(271),                    # Make
        ex.get(272),                    # Model
    )


# ---------------------------------------------------------------------------
# Геометрия камеры
# ---------------------------------------------------------------------------

class Camera:
    def __init__(self, width, height, fx, fy):
        self.width = width
        self.height = height
        self.fx = fx
        self.fy = fy
        self.cx = width / 2.0
        self.cy = height / 2.0

    @classmethod
    def from_image(cls, path, model=None, sensor=None):
        with Image.open(path) as im:
            w, h = im.size
        focal_mm, focal35, make, mdl = read_exif_focal(path)
        model = model or mdl or make or "M3E"
        if sensor is None:
            sensor = CAMERA_PRESETS.get(model, DEFAULT_SENSOR)
        sw, sh = sensor

        # Фокусное в мм: берём из EXIF, если нет — из 35мм-эквивалента.
        if focal_mm:
            f_mm = focal_mm
        elif focal35:
            # переводим 35мм-эквивалент обратно в реальное фокусное
            crop = math.sqrt(36.0 ** 2 + 24.0 ** 2) / math.sqrt(sw ** 2 + sh ** 2)
            f_mm = focal35 / crop
        else:
            f_mm = 12.29  # запасное значение для Mavic 3

        fx = f_mm * w / sw
        fy = f_mm * h / sh
        return cls(w, h, fx, fy)


def rotation_matrix(yaw_deg, pitch_deg, roll_deg):
    """
    Матрица поворота из координат камеры в мировые (ENU: X - восток,
    Y - север, Z - вверх). Соглашение DJI: yaw=0 — север, по часовой;
    pitch=0 — горизонт, положительный — вверх; roll — вокруг оптической оси.
    """
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    roll = math.radians(roll_deg)

    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)

    # Направление оптической оси в мире
    f = np.array([cp * sy, cp * cy, sp], dtype=np.float64)

    z_up = np.array([0.0, 0.0, 1.0])
    r = np.cross(f, z_up)
    norm = np.linalg.norm(r)
    if norm < 1e-12:
        r = np.array([1.0, 0.0, 0.0])  # вырождение: смотрит строго в зенит/надир
    else:
        r /= norm
    u = np.cross(r, f)  # "вверх" камеры в мире

    # Столбцы R = [right, down, forward] (из камерных в мировые координаты)
    R = np.stack([r, -u, f], axis=1)

    cr, sr = math.cos(roll), math.sin(roll)
    Rz = np.array([[cr, -sr, 0.0], [sr, cr, 0.0], [0.0, 0.0, 1.0]])
    return R @ Rz


# ---------------------------------------------------------------------------
# Проекция на сферу (equirectangular)
# ---------------------------------------------------------------------------

def _lonlat_to_xyz(lon, lat):
    """Долгота/широта (радианы) -> единичный вектор в ENU."""
    return np.stack([
        np.cos(lat) * np.sin(lon),
        np.cos(lat) * np.cos(lon),
        np.sin(lat),
    ], axis=-1)


def image_lonlat_bbox(cam, R, n_edge=32):
    """
    Определяет область выходной равнопромежуточной панорамы, которую
    покрывает данный кадр. Возвращает интервалы долготы в градусах
    (в диапазоне [0, 360)) и диапазон широты [lat_min, lat_max].

    Граница кадра сэмплируется плотно: экстремумы широты часто лежат на
    середине ребра, а не в углу.
    """
    w, h = cam.width, cam.height
    fx, fy = cam.fx, cam.fy

    ts = np.linspace(0.0, 1.0, n_edge + 1)
    us = np.concatenate([ts * w, ts * w, np.zeros_like(ts), np.full_like(ts, w)])
    vs = np.concatenate([np.zeros_like(ts), np.full_like(ts, h), ts * h, ts * h])

    d_cam = np.stack([
        (us - cam.cx) / fx,
        (vs - cam.cy) / fy,
        np.ones_like(us),
    ], axis=1)
    d_cam /= np.linalg.norm(d_cam, axis=1, keepdims=True)
    d_world = d_cam @ R.T

    lon = np.degrees(np.arctan2(d_world[:, 0], d_world[:, 1])) % 360.0
    lat = np.degrees(np.arcsin(np.clip(d_world[:, 2], -1, 1)))

    # Полудиагональный угол обзора — для определения, попадает ли полюс внутрь
    half_diag = math.atan(
        math.sqrt((w / 2.0) ** 2 + (h / 2.0) ** 2) / math.sqrt(fx * fy)
    )
    f = R[:, 2]
    south = np.array([0.0, 0.0, -1.0])
    north = np.array([0.0, 0.0, 1.0])
    contains_south = math.acos(max(-1.0, min(1.0, float(f @ south)))) < half_diag
    contains_north = math.acos(max(-1.0, min(1.0, float(f @ north)))) < half_diag

    if contains_south:
        lat_min = -90.0
        lat_max = float(lat.max())
        lon_intervals = [(0.0, 360.0)]
    elif contains_north:
        lat_min = float(lat.min())
        lat_max = 90.0
        lon_intervals = [(0.0, 360.0)]
    else:
        lat_min = float(lat.min())
        lat_max = float(lat.max())
        lon_intervals = _lon_interval(lon)

    return lon_intervals, lat_min, lat_max


def _lon_interval(lon_deg):
    """Из набора долгот (градусы, [0,360)) строит один/два покрываемых интервала."""
    lon = np.sort(np.asarray(lon_deg, dtype=np.float64) % 360.0)
    gaps = np.diff(lon)
    wrap_gap = lon[0] + 360.0 - lon[-1]
    all_gaps = np.concatenate([gaps, [wrap_gap]])
    gi = int(np.argmax(all_gaps))
    if gi < len(gaps):
        start = lon[gi + 1]
        end = lon[gi] + 360.0
    else:
        start = lon[0]
        end = lon[-1]
    if end - start >= 360.0:
        return [(0.0, 360.0)]
    if end > 360.0:
        return [(start, 360.0), (0.0, end - 360.0)]
    return [(start, end)]


def _lon_arcs_to_x(lon_intervals, out_w):
    """Переводит интервалы долготы [0,360) в пиксельные диапазоны X выходной карты."""
    ranges = []
    for (a, b) in lon_intervals:
        a = max(0.0, min(360.0, a))
        b = max(0.0, min(360.0, b))
        if b - a < 1e-9:
            continue
        subs = []
        if a < 180.0:
            subs.append((a, min(b, 180.0)))
        if b > 180.0:
            subs.append((max(a, 180.0), b))
        for s, e in subs:
            if e <= 180.0:
                x0 = (s + 180.0) / 360.0 * out_w
                x1 = (e + 180.0) / 360.0 * out_w
            else:
                x0 = (s - 180.0) / 360.0 * out_w
                x1 = (e - 180.0) / 360.0 * out_w
            ranges.append((x0, x1))
    return ranges


def _sample_bilinear(img, u, v, valid):
    """Билинейная выборка. u, v — массивы координат; valid — маска валидности."""
    H, W = img.shape[:2]
    u = np.clip(u, 0, W - 1)
    v = np.clip(v, 0, H - 1)
    u0 = np.floor(u).astype(np.int32)
    v0 = np.floor(v).astype(np.int32)
    u1 = np.minimum(u0 + 1, W - 1)
    v1 = np.minimum(v0 + 1, H - 1)
    du = (u - u0)[..., None]
    dv = (v - v0)[..., None]

    ia = img[v0, u0].astype(np.float32)
    ib = img[v0, u1].astype(np.float32)
    ic = img[v1, u0].astype(np.float32)
    id_ = img[v1, u1].astype(np.float32)

    out = (ia * (1 - du) * (1 - dv) + ib * du * (1 - dv)
           + ic * (1 - du) * dv + id_ * du * dv)
    out[~valid] = 0.0
    return out


def warp_to_equirect(img, cam, R, out_w, out_h, offset_deg=(0.0, 0.0),
                     weight_power=1.0, weighted=True):
    """
    Проецирует кадр на равнопромежуточную панораму (обратное отображение).
    offset_deg — небольшой сдвиг кадра по (долготе, широте) в градусах
    (используется после уточнения позы).
    weight_power — насколько резко вес падает к краю кадра (больше = уже шов).
    Возвращает (warped float32 HxWx3, weight HxW).
    """
    lon_intervals, lat_min, lat_max = image_lonlat_bbox(cam, R)

    ox = offset_deg[0] / 360.0 * out_w
    oy = offset_deg[1] / 180.0 * out_h

    warped = np.zeros((out_h, out_w, 3), dtype=np.float32)
    weight = np.zeros((out_h, out_w), dtype=np.float32)

    y0 = int(math.floor((90.0 - lat_max) / 180.0 * out_h + oy))
    y1 = int(math.ceil((90.0 - lat_min) / 180.0 * out_h + oy))
    y0 = max(0, y0)
    y1 = min(out_h, y1)
    if y1 <= y0:
        return warped, weight

    for (fx0, fx1) in _lon_arcs_to_x(lon_intervals, out_w):
        x0 = max(0, int(math.floor(fx0 + ox)))
        x1 = min(out_w, int(math.ceil(fx1 + ox)))
        if x1 <= x0:
            continue

        xs = np.arange(x0, x1, dtype=np.float64) + 0.5
        ys = np.arange(y0, y1, dtype=np.float64) + 0.5
        X, Y = np.meshgrid(xs, ys)

        lon = ((X - ox) / out_w) * 2.0 * math.pi - math.pi
        lat = math.pi / 2.0 - ((Y - oy) / out_h) * math.pi
        d_world = _lonlat_to_xyz(lon, lat)  # (ny, nx, 3)

        # В координаты камеры
        d_cam = d_world @ R  # R^T, так как R ортонормирована
        z = d_cam[..., 2]
        front = z > 1e-6

        u = cam.cx + cam.fx * d_cam[..., 0] / z
        v = cam.cy + cam.fy * d_cam[..., 1] / z

        valid = front & (u >= 0) & (u <= cam.width - 1) & (v >= 0) & (v <= cam.height - 1)

        if not valid.any():
            continue

        sampled = _sample_bilinear(img, u, v, valid)

        # Косинусоидальный вес: 1 в центре, 0 на краях кадра
        ru = (u - cam.cx) / (cam.width / 2.0)
        rv = (v - cam.cy) / (cam.height / 2.0)
        r = np.sqrt(ru * ru + rv * rv)
        w = np.where(valid, np.cos(np.clip(r, 0.0, 1.0) * math.pi / 2.0) ** (2.0 * weight_power), 0.0)
        # не даём весу обнулиться у самого края кадра: иначе край попадёт
        # в «дыру» и будет залит диффузией
        w = np.where(valid, np.maximum(w, 1e-9), 0.0)
        w = w.astype(np.float32)

        warped[y0:y1, x0:x1] += (sampled * w[..., None]) if weighted else sampled
        weight[y0:y1, x0:x1] += w

    return warped, weight


# ---------------------------------------------------------------------------
# Многополосное (Laplacian pyramid) смешивание
# ---------------------------------------------------------------------------

def _downsample2(img):
    """Уменьшает изображение вдвое (box-фильтр). Работает с HxW или HxWxC."""
    H, W = img.shape[:2]
    H2, W2 = H // 2, W // 2
    if H2 < 1 or W2 < 1:
        return img.copy()
    crop = img[:H2 * 2, :W2 * 2]
    if img.ndim == 2:
        return crop.reshape(H2, 2, W2, 2).mean(axis=(1, 3))
    return crop.reshape(H2, 2, W2, 2, img.shape[2]).mean(axis=(1, 3))


def _upsample2(img, target_h, target_w):
    """Билинейно увеличивает вдвое (до заданного размера)."""
    if img.ndim == 2:
        return np.asarray(
            Image.fromarray(img.astype(np.float32), mode="F").resize(
                (target_w, target_h), Image.BILINEAR)
        )
    out = np.empty((target_h, target_w, img.shape[2]), dtype=np.float32)
    for c in range(img.shape[2]):
        out[..., c] = np.asarray(
            Image.fromarray(img[..., c].astype(np.float32), mode="F").resize(
                (target_w, target_h), Image.BILINEAR)
        )
    return out


# ---------------------------------------------------------------------------
# Экспозиция: выравнивание яркости кадров
# ---------------------------------------------------------------------------

def estimate_gains(paths, cams, rotations, out_w=256, out_h=128, mode="auto"):
    """
    Оценивает множители яркости кадров.

    В режиме "none" яркость не трогается (доверяем камере: у DJI все кадры
    панорамы обычно сняты с одинаковой выдержкой/ISO).

    В режиме "auto" решается система по перекрытиям кадров: для каждой пары
    перекрывающихся снимков сравнивается медианная яркость в общей области.
    """
    n = len(paths)
    if mode == "none" or n == 0:
        return np.ones(n, dtype=np.float32)

    # Рендерим уменьшенные версии и запоминаем медианную яркость перекрытий.
    layers = []
    for i, path in enumerate(paths):
        with Image.open(path) as im:
            im = im.convert("RGB")
            small = im.resize((cams[i].width // 8, cams[i].height // 8))
        arr = np.asarray(small, dtype=np.float32)
        cam_small = Camera(small.width, small.height,
                           cams[i].fx / 8.0, cams[i].fy / 8.0)
        warped, weight = warp_to_equirect(arr, cam_small, rotations[i], out_w, out_h)
        mask = weight > 1e-3
        lum = np.zeros((out_h, out_w), dtype=np.float32)
        lum[mask] = warped[mask].mean(axis=1) / weight[mask]
        layers.append((lum, mask))

    # Уравнения: log(g_i) - log(g_j) = log(median_j / median_i) в перекрытии.
    rows = []
    rhs = []
    for i in range(n):
        li, mi = layers[i]
        for j in range(i + 1, n):
            lj, mj = layers[j]
            overlap = mi & mj
            if overlap.sum() < 20:
                continue
            vi = li[overlap]
            vj = lj[overlap]
            good = (vi > 5) & (vj > 5)
            if good.sum() < 20:
                continue
            ri = float(np.median(vi[good]))
            rj = float(np.median(vj[good]))
            if ri <= 0 or rj <= 0:
                continue
            eq = np.zeros(n)
            eq[i] = 1.0
            eq[j] = -1.0
            rows.append(eq)
            rhs.append(math.log(rj / ri))
    if not rows:
        return np.ones(n, dtype=np.float32)

    A = np.vstack(rows)
    b = np.array(rhs)
    # Регуляризация к 1 (log=0), чтобы не «уплывать» при слабых перекрытиях.
    reg = 0.5
    A = np.vstack([A, reg * np.eye(n)])
    b = np.concatenate([b, np.zeros(n)])
    x, *_ = np.linalg.lstsq(A, b, rcond=None)
    gains = np.exp(x).astype(np.float32)
    return np.clip(gains, 0.33, 3.0)


# ---------------------------------------------------------------------------
# Стандартный формат: XMP GPano (чтобы панораму понимали просмотрщики)
# ---------------------------------------------------------------------------

def build_gpano_xmp(width, height, heading=0.0, software="DJI Pano Builder"):
    """Формирует XMP-пакет Google Photo Sphere (equirectangular)."""
    text = f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:GPano="http://ns.google.com/photos/1.0/panorama/"
    GPano:ProjectionType="equirectangular"
    GPano:UsePanoramaViewer="True"
    GPano:CroppedAreaImageWidthPixels="{width}"
    GPano:CroppedAreaImageHeightPixels="{height}"
    GPano:FullPanoWidthPixels="{width}"
    GPano:FullPanoHeightPixels="{height}"
    GPano:CroppedAreaLeftPixels="0"
    GPano:CroppedAreaTopPixels="0"
    GPano:InitialViewHeadingDegrees="{heading:g}"
    GPano:InitialViewPitchDegrees="0"
    GPano:InitialViewRollDegrees="0"
    GPano:StitchingSoftware="{software}"
    GPano:PoseHeadingDegrees="{heading:g}"
    GPano:PosePitchDegrees="0"
    GPano:PoseRollDegrees="0"/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    return text.encode("utf-8")


def build_output_exif(paths):
    """Копирует GPS и базовые EXIF-теги из первого кадра в итоговую панораму."""
    ex = Image.Exif()
    try:
        with Image.open(paths[0]) as im:
            src = im.getexif()
    except Exception:
        return None
    for tag in (271, 272, 306, 36867):  # Make, Model, DateTime, DateTimeOriginal
        v = src.get(tag)
        if v is not None:
            ex[tag] = v
    try:
        gps = src.get_ifd(0x8825)
        if gps:
            ex[0x8825] = gps
    except Exception:
        pass
    return ex


# ---------------------------------------------------------------------------
# Заполнение «дыры» в зените
# ---------------------------------------------------------------------------

def fill_holes(equirect, weight, low_width=160, iters=8000, feather=0.04):
    """
    Аккуратно заполняет не покрытые снимками пиксели (зенит/надир).

    Идея: на маленьком разрешении решаем уравнение Лапласа (диффузия цвета
    из покрытых областей внутрь «дыры»), затем поднимаем результат обратно
    и мягко подмешиваем. Это убирает ступеньки и вертикальные полосы,
    которые получаются при простом копировании ближайшего пикселя.
    """
    H, W, C = equirect.shape
    invalid = weight < 1e-10
    if not invalid.any():
        return equirect

    lw = max(4, low_width)
    lh = max(4, int(round(lw * H / float(W))))

    def resize_f32(a2d, size, resample):
        return np.asarray(
            Image.fromarray(a2d.astype(np.float32), mode="F").resize(size, resample)
        )

    den = resize_f32(weight, (lw, lh), Image.BOX)
    low = np.zeros((lh, lw, C), dtype=np.float32)
    for c in range(C):
        num = resize_f32(equirect[..., c] * weight, (lw, lh), Image.BOX)
        low[..., c] = num / np.maximum(den, 1e-6)

    hole_low = den < 1e-4
    valid_low = ~hole_low
    if not valid_low.any():
        return np.zeros_like(equirect)

    filled = low.copy()
    if hole_low.any():
        filled[hole_low] = low[valid_low].mean(axis=0)
        for _ in range(iters):
            nb = np.empty_like(filled)
            nb[1:-1, 1:-1] = 0.25 * (
                filled[:-2, 1:-1] + filled[2:, 1:-1]
                + filled[1:-1, :-2] + filled[1:-1, 2:]
            )
            nb[0, 1:-1] = filled[1, 1:-1]
            nb[-1, 1:-1] = filled[-2, 1:-1]
            nb[1:-1, 0] = filled[1:-1, 1]
            nb[1:-1, -1] = filled[1:-1, -2]
            nb[0, 0] = filled[1, 1]
            nb[0, -1] = filled[1, -2]
            nb[-1, 0] = filled[-2, 1]
            nb[-1, -1] = filled[-2, -2]
            delta = float(np.abs(nb[hole_low] - filled[hole_low]).max())
            filled[hole_low] = nb[hole_low]
            if delta < 0.02:
                break

    # Мягкая маска: 1 внутри дыры, плавно убывает наружу.
    r = max(1, int(round(feather * lw)))
    alpha = hole_low.astype(np.float32)
    for _ in range(r):
        p = np.pad(alpha, 1, mode="edge")
        alpha = 0.25 * (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:])
    alpha[hole_low] = 1.0

    up = np.zeros_like(equirect)
    for c in range(C):
        up[..., c] = resize_f32(filled[..., c], (W, H), Image.BILINEAR)
    alpha_full = np.clip(resize_f32(alpha, (W, H), Image.BILINEAR), 0.0, 1.0)

    # Внутри дыры подмешиваем заполнение полностью, иначе на границе
    # останется тёмная кайма (0 * (1-alpha)).
    alpha_full[invalid] = 1.0
    alpha_full = alpha_full[..., None]

    result = equirect * (1.0 - alpha_full) + up * alpha_full
    return result


# ---------------------------------------------------------------------------
# Превью «маленькая планета» (стереографическая проекция)
# ---------------------------------------------------------------------------

def tiny_planet(equirect, size=1600, heading=0.0):
    """Стереографическая проекция («маленькая планета») из equirect-панорамы."""
    H, W, _ = equirect.shape
    c0 = size / 2.0
    scale = c0  # горизонт (lat=0) попадает на окружность радиуса size/2

    xs = np.arange(size, dtype=np.float64) + 0.5 - c0
    ys = np.arange(size, dtype=np.float64) + 0.5 - c0
    X, Y = np.meshgrid(xs, ys)
    rho = np.sqrt(X * X + Y * Y)

    # Угол от надира: 0 в центре, pi/2 на горизонте.
    c = 2.0 * np.arctan2(rho, scale)
    lat = -math.pi / 2.0 + c
    lon = np.arctan2(Y, X) + math.radians(heading)

    u = ((lon + math.pi) / (2.0 * math.pi)) * W
    v = (math.pi / 2.0 - lat) / math.pi * H
    valid = lat <= math.pi / 2.0
    out = _sample_bilinear(equirect, u, v, valid)
    out[~valid] = 0.0
    return out


# ---------------------------------------------------------------------------
# Основная сборка
# ---------------------------------------------------------------------------

def _render_linear(paths, cams, rotations, gains, offsets, out_w, out_h,
                   weight_power, progress_cb):
    acc = np.zeros((out_h, out_w, 3), dtype=np.float32)
    acc_w = np.zeros((out_h, out_w), dtype=np.float32)
    n = len(paths)
    for i, p in enumerate(paths):
        if progress_cb:
            progress_cb(i, n, os.path.basename(p))
        with Image.open(p) as im:
            arr = np.asarray(im.convert("RGB"), dtype=np.float32) * gains[i]
        warped, weight = warp_to_equirect(
            arr, cams[i], rotations[i], out_w, out_h,
            offset_deg=offsets[i], weight_power=weight_power)
        acc += warped
        acc_w += weight
        del warped, weight
    equirect = np.zeros_like(acc)
    ok = acc_w > 1e-12
    equirect[ok] = acc[ok] / acc_w[ok, None]
    return equirect, acc_w


def _render_multiband(paths, cams, rotations, gains, offsets, out_w, out_h,
                      weight_power, progress_cb, max_levels=5):
    n = len(paths)
    levels = 1
    m = min(out_w, out_h)
    while levels < max_levels and m >= 24:
        levels += 1
        m //= 2

    # Shapes of every pyramid level (deterministic from the canvas size).
    shapes = []
    h, w = out_h, out_w
    for _ in range(levels):
        shapes.append((h, w))
        h = max(1, h // 2)
        w = max(1, w // 2)

    acc_lap = [np.zeros((sh, sw, 3), dtype=np.float32) for (sh, sw) in shapes]
    acc_w = [np.zeros((sh, sw), dtype=np.float32) for (sh, sw) in shapes]

    for i, p in enumerate(paths):
        if progress_cb:
            progress_cb(i, n, os.path.basename(p))
        with Image.open(p) as im:
            arr = np.asarray(im.convert("RGB"), dtype=np.float32) * gains[i]
        warped, weight = warp_to_equirect(
            arr, cams[i], rotations[i], out_w, out_h,
            offset_deg=offsets[i], weight_power=weight_power, weighted=False)
        del arr

        # Build the Laplacian pyramid on the fly, accumulating level by level.
        prev_i = warped
        prev_w = weight
        for l in range(levels - 1):
            nxt_i = _downsample2(prev_i)
            nxt_w = _downsample2(prev_w)
            up = _upsample2(nxt_i, shapes[l][0], shapes[l][1])
            prev_i -= up
            del up
            acc_lap[l] += prev_i * prev_w[..., None]
            acc_w[l] += prev_w
            del prev_i
            prev_i = nxt_i
            prev_w = nxt_w
        acc_lap[levels - 1] += prev_i * prev_w[..., None]
        acc_w[levels - 1] += prev_w
        del warped, weight, prev_i, prev_w

    full_w = acc_w[0]

    # In-place normalisation of every level.
    for l in range(levels):
        wl = np.maximum(acc_w[l], 1e-12)
        acc_lap[l] /= wl[..., None]
        if l > 0:
            acc_w[l] = None

    # Collapse (destructively frees levels as it goes).
    res = acc_lap[-1]
    for l in reversed(range(levels - 1)):
        res = _upsample2(res, shapes[l][0], shapes[l][1]) + acc_lap[l]
        acc_lap[l] = None
    return res, full_w


def build_panorama(paths, out_width, out_height=None, output=None,
                   preview=False, fill=True, progress=True, exposure="none",
                   blend="multiband", weight_power=3.0, offsets=None,
                   progress_cb=None, ui=None):
    out_height = out_height or out_width // 2
    ui = ui or UI(enabled=False)

    cams = []
    rotations = []
    for p in paths:
        cam = Camera.from_image(p)
        yaw, pitch, roll = read_xmp_gimbal(p)
        if yaw is None or pitch is None:
            raise ValueError(f"No drone-dji gimbal metadata in {p}")
        roll = roll or 0.0
        cams.append(cam)
        rotations.append(rotation_matrix(yaw, pitch, roll))

    with ui.spinner("Reading exposure"):
        gains = estimate_gains(paths, cams, rotations, mode=exposure)
    if offsets is None:
        offsets = [(0.0, 0.0)] * len(paths)

    bar = None
    cb = progress_cb
    if ui.enabled and progress:
        bar = _ProgressBar(ui, len(paths), "Stitching")

        def cb(i, n, name):
            bar.update(i + 1, name)

    if blend == "multiband":
        equirect, acc_w = _render_multiband(
            paths, cams, rotations, gains, offsets, out_width, out_height,
            weight_power, cb)
    else:
        equirect, acc_w = _render_linear(
            paths, cams, rotations, gains, offsets, out_width, out_height,
            weight_power, cb)
    if bar is not None:
        bar.close()

    with ui.spinner("Filling zenith gap"):
        if fill:
            equirect = fill_holes(equirect, acc_w)

    equirect = np.clip(equirect, 0, 255).astype(np.uint8)

    results = {}
    if output:
        with ui.spinner("Saving panorama"):
            xmp = build_gpano_xmp(out_width, out_height)
            exif = build_output_exif(paths)
            save_kwargs = dict(quality=95, xmp=xmp)
            if exif is not None:
                save_kwargs["exif"] = exif
            Image.fromarray(equirect).save(output, **save_kwargs)
        results["equirect"] = output
        if ui.enabled:
            size_mb = os.path.getsize(output) / (1024 * 1024)
            ui.success(f"Saved {ui.paint(output, 'bold')} "
                       f"({out_width}×{out_height}, {size_mb:.1f} MB, 2:1 + GPano)")

    if preview:
        with ui.spinner("Rendering tiny planet"):
            tp = tiny_planet(equirect)
            tp_img = Image.fromarray(np.clip(tp, 0, 255).astype(np.uint8))
            if output:
                base, ext = os.path.splitext(output)
                preview_path = base + "_preview" + ext
                tp_img.save(preview_path, quality=95)
                results["preview"] = preview_path
            else:
                preview_path = "preview.jpg"
                tp_img.save(preview_path, quality=95)
                results["preview"] = preview_path
        if ui.enabled:
            ui.info(f"Preview: {preview_path}")

    return results


def list_pano_files(folder):
    """Список кадров панорамы (PANO*.jpg/jpeg/JPG), регистронезависимо."""
    out = []
    for name in os.listdir(folder):
        low = name.lower()
        if low.startswith("pano") and low.endswith((".jpg", ".jpeg")):
            out.append(os.path.join(folder, name))
    return sorted(out)


def find_pano_folders(base):
    """Находит папки, содержащие снимки панорамы PANO*."""
    out = []
    # сама папка base может быть папкой с кадрами
    if list_pano_files(base):
        out.append(base)
    for d in sorted(glob.glob(os.path.join(base, "*"))):
        if os.path.isdir(d) and list_pano_files(d):
            out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="dji_pano.py",
        description="Stitch a set of DJI drone frames into a 2:1 equirectangular panorama.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 dji_pano.py PANORAMA/006_0004 -o pano.jpg\n"
            "  python3 dji_pano.py PANORAMA/006_0004 -o pano.jpg --preview\n"
            "  python3 dji_pano.py PANORAMA --out-dir output -w 12000 --preview\n"
        ),
    )
    ap.add_argument("input", help="folder with PANO*.JPG frames, or a root folder with several sets")
    ap.add_argument("-o", "--output", help="output JPEG path (single folder)")
    ap.add_argument("-w", "--width", type=int, default=12000,
                    help="panorama width in pixels; height is width/2 (default: 12000)")
    ap.add_argument("--preview", action="store_true",
                    help="also save a 'tiny planet' preview")
    ap.add_argument("--no-fill", action="store_true",
                    help="do not fill the uncovered zenith gap")
    ap.add_argument("--exposure", choices=["none", "auto"], default="none",
                    help="brightness correction: none (keep camera) or auto (match overlaps)")
    ap.add_argument("--blend", choices=["multiband", "linear"], default="multiband",
                    help="blending mode (default: multiband)")
    ap.add_argument("--sharpness", type=float, default=3.0,
                    help="seam sharpness; higher = narrower transition / less ghosting (default: 3)")
    ap.add_argument("--out-dir", "--out", dest="out_dir", default=".",
                    help="output folder for batch mode (default: current)")
    ap.add_argument("--no-color", action="store_true", help="disable colored output")
    ap.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    args = ap.parse_args(argv)

    ui = UI(enabled=not args.quiet)
    if args.no_color:
        ui.color = False

    if not os.path.exists(args.input):
        ui.error(f"Path not found: {args.input}")
        return 1

    folders = find_pano_folders(args.input)
    if not folders:
        ui.error("No folder with PANO*.JPG / PANO*.jpg frames found.")
        return 1

    ui.banner()

    single = len(folders) == 1 and args.output
    if single:
        jobs = [(folders[0], args.output)]
    else:
        os.makedirs(args.out_dir, exist_ok=True)
        jobs = []
        for folder in folders:
            name = os.path.basename(folder.rstrip(os.sep))
            jobs.append((folder, os.path.join(args.out_dir, name + "_pano.jpg")))

    started = time.time()
    failed = 0
    for idx, (folder, out) in enumerate(jobs, 1):
        paths = list_pano_files(folder)
        if not paths:
            ui.warn(f"Skipping {folder}: no frames")
            continue

        if len(jobs) > 1:
            print()
            ui.line(ui.paint(f"[{idx}/{len(jobs)}] {os.path.basename(folder)}",
                             "bold", "blue")
                    + ui.paint(f"  ·  {len(paths)} frames", "dim"))
        else:
            ui.info(f"{len(paths)} frames  ·  {os.path.basename(folder)}")

        try:
            build_panorama(paths, args.width, output=out,
                           preview=args.preview, fill=not args.no_fill,
                           exposure=args.exposure, blend=args.blend,
                           weight_power=args.sharpness, ui=ui)
        except KeyboardInterrupt:
            ui.error("Interrupted.")
            return 130
        except Exception as exc:  # noqa: BLE001
            failed += 1
            ui.error(f"Failed on {folder}: {exc}")

    elapsed = time.time() - started
    mins, secs = divmod(int(elapsed), 60)
    print()
    if failed:
        ui.warn(f"Done with {failed} failure(s) in {mins}m {secs:02d}s")
        return 1
    ui.success(f"Done in {mins}m {secs:02d}s  ·  {len(jobs)} panorama(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
