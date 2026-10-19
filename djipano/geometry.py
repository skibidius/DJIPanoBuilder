"""Spherical geometry: rotations and equirectangular reprojection."""

import math

import numpy as np


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
    u0 = u.astype(np.int32)
    v0 = v.astype(np.int32)
    u1 = np.minimum(u0 + 1, W - 1)
    v1 = np.minimum(v0 + 1, H - 1)
    du = (u - u0)[..., None]
    dv = (v - v0)[..., None]

    # Flattened gather is ~2x faster than 2-D advanced indexing.
    flat = img.reshape(-1, img.shape[2])
    i00 = v0 * W + u0
    i01 = v0 * W + u1
    i10 = v1 * W + u0
    i11 = v1 * W + u1
    out = (np.take(flat, i00, axis=0) * (1 - du) * (1 - dv)
           + np.take(flat, i01, axis=0) * du * (1 - dv)
           + np.take(flat, i10, axis=0) * (1 - du) * dv
           + np.take(flat, i11, axis=0) * du * dv)
    out[~valid] = 0.0
    return out.astype(np.float32, copy=False)


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
        zs = np.where(front, z, 1.0)

        u = cam.cx + cam.fx * d_cam[..., 0] / zs
        v = cam.cy + cam.fy * d_cam[..., 1] / zs

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
