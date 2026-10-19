"""High-level panorama building pipeline."""

import glob
import math
import os

import numpy as np
from PIL import Image

from .metadata import (Camera, read_xmp_gimbal, build_gpano_xmp,
                       build_output_exif)
from .geometry import rotation_matrix, _sample_bilinear
from .blending import (estimate_gains, _render_linear, _render_multiband,
                       _compute_assignment, fill_holes)
from .align import refine_poses
from .progress import UI, _ProgressBar


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


def build_panorama(paths, out_width, out_height=None, output=None,
                   preview=False, fill=True, progress=True, exposure="none",
                   blend="multiband", weight_power=3.0, offsets=None,
                   progress_cb=None, ui=None, seam=True, refine=True):
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

    if refine and len(paths) > 2:
        with ui.spinner("Refining alignment"):
            rotations = refine_poses(paths, cams, rotations)

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
        assignment = None
        if seam:
            with ui.spinner("Finding seams"):
                assignment = _compute_assignment(
                    paths, cams, rotations, out_width, out_height, offsets,
                    weight_power)
        equirect, acc_w = _render_multiband(
            paths, cams, rotations, gains, offsets, out_width, out_height,
            weight_power, cb, assignment=assignment)
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
