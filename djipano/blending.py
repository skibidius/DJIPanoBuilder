"""Exposure matching, multiband blending, seams and hole filling."""

import math
import os

import numpy as np
from PIL import Image

from .geometry import warp_to_equirect
from .metadata import Camera, load_image


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
    # Slicing is ~4x faster than reshape+mean and gives the same box average.
    return (crop[0::2, 0::2] + crop[1::2, 0::2]
            + crop[0::2, 1::2] + crop[1::2, 1::2]) * 0.25


def _upsample2(img, target_h, target_w):
    """Быстрое билинейное увеличение вдвое (numpy)."""
    h, w = img.shape[:2]
    if h < 2 or w < 2:
        if img.ndim == 2:
            return np.asarray(
                Image.fromarray(img.astype(np.float32), mode="F").resize(
                    (target_w, target_h), Image.BILINEAR))
        out = np.empty((target_h, target_w, img.shape[2]), dtype=np.float32)
        for c in range(img.shape[2]):
            out[..., c] = np.asarray(
                Image.fromarray(img[..., c].astype(np.float32), mode="F").resize(
                    (target_w, target_h), Image.BILINEAR))
        return out

    # horizontal 2x (edge-replicated, matching pixel-centre bilinear)
    tmp = np.empty((h, 2 * w) + img.shape[2:], dtype=np.float32)
    tmp[:, 0] = img[:, 0]
    tmp[:, 1:-1:2] = 0.75 * img[:, :-1] + 0.25 * img[:, 1:]
    tmp[:, 2:-1:2] = 0.25 * img[:, :-1] + 0.75 * img[:, 1:]
    tmp[:, -1] = img[:, -1]

    # vertical 2x
    out = np.empty((2 * h, 2 * w) + img.shape[2:], dtype=np.float32)
    out[0] = tmp[0]
    out[1:-1:2] = 0.75 * tmp[:-1] + 0.25 * tmp[1:]
    out[2:-1:2] = 0.25 * tmp[:-1] + 0.75 * tmp[1:]
    out[-1] = tmp[-1]

    if out.shape[0] < target_h or out.shape[1] < target_w:
        pad_h = max(0, target_h - out.shape[0])
        pad_w = max(0, target_w - out.shape[1])
        if out.ndim == 2:
            out = np.pad(out, ((0, pad_h), (0, pad_w)), mode="edge")
        else:
            out = np.pad(out, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
    return out[:target_h, :target_w]


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
        small_w = max(1, cams[i].width // 8)
        small_h = max(1, cams[i].height // 8)
        with Image.open(path) as im:
            im.draft("RGB", (small_w, small_h))
            small = im.convert("RGB").resize((small_w, small_h))
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
# Заполнение «дыры» в зените
# ---------------------------------------------------------------------------

def _down_to(a, tw, th):
    """Fast area downsample to (th, tw) via repeated 2x box + PIL BOX."""
    while a.shape[0] >= th * 2 and a.shape[1] >= tw * 2:
        a = _downsample2(a)
    if a.shape[0] == th and a.shape[1] == tw:
        return a
    if a.ndim == 2:
        return np.asarray(Image.fromarray(a.astype(np.float32), mode="F").resize(
            (tw, th), Image.BOX))
    out = np.empty((th, tw, a.shape[2]), dtype=np.float32)
    for c in range(a.shape[2]):
        out[..., c] = np.asarray(
            Image.fromarray(a[..., c].astype(np.float32), mode="F").resize(
                (tw, th), Image.BOX))
    return out


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

    den = _down_to(weight, lw, lh)
    low = np.zeros((lh, lw, C), dtype=np.float32)
    for c in range(C):
        num = _down_to(equirect[..., c] * weight, lw, lh)
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
        arr, cam_i = load_image(p, cams[i], out_w, "RGB")
        arr *= gains[i]
        warped, weight = warp_to_equirect(
            arr, cam_i, rotations[i], out_w, out_h,
            offset_deg=offsets[i], weight_power=weight_power)
        acc += warped
        acc_w += weight
        del warped, weight
    equirect = np.zeros_like(acc)
    ok = acc_w > 1e-12
    equirect[ok] = acc[ok] / acc_w[ok, None]
    return equirect, acc_w


def _compute_assignment(paths, cams, rotations, out_w, out_h, offsets,
                        weight_power, divisor=2):
    """
    Строит «карту владения»: для каждого пикселя — индекс кадра с наибольшим
    весом. Нужна для шовного (seam-based) смешивания без двоения.
    """
    aw = max(8, out_w // divisor)
    ah = max(4, out_h // divisor)
    best_w = np.zeros((ah, aw), dtype=np.float32)
    best_i = np.full((ah, aw), -1, dtype=np.int16)
    dummy = np.zeros((2, 2, 3), dtype=np.float32)
    for i in range(len(paths)):
        _, w = warp_to_equirect(dummy, cams[i], rotations[i], aw, ah,
                                offset_deg=offsets[i], weight_power=weight_power)
        better = w > best_w
        best_w[better] = w[better]
        best_i[better] = i
    return best_i, aw, ah


def _render_multiband(paths, cams, rotations, gains, offsets, out_w, out_h,
                      weight_power, progress_cb, max_levels=5,
                      assignment=None):
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
        arr, cam_i = load_image(p, cams[i], out_w, "RGB")
        arr *= gains[i]
        warped, weight = warp_to_equirect(
            arr, cam_i, rotations[i], out_w, out_h,
            offset_deg=offsets[i], weight_power=weight_power, weighted=False)
        del arr

        # Seam-based assignment: each pixel belongs to exactly one frame.
        # Bilinear upsampling of the binary mask gives a ~2px soft seam while
        # keeping fine detail from a single frame (no ghosting).
        if assignment is not None:
            best_i, aw, ah = assignment
            m_half = (best_i == i).astype(np.float32)
            m_full = np.asarray(
                Image.fromarray(m_half, mode="F").resize(
                    (out_w, out_h), Image.BILINEAR))
            weight = m_full * (weight > 0)
            del m_half, m_full

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
