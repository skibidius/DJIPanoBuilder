"""Pose refinement: align overlapping frames on the sphere.

The gimbal angles in DJI XMP are close but not perfect (the gimbal can lag or
settle slightly differently between shots).  The residual error is small
(~0.5°), but it is exactly what makes the horizon look crooked at seams.

This module measures dense correspondences between overlapping frames **in the
equirectangular domain** (where matching is stable) and solves for a small
3-DoF rotation correction per frame with robust least squares.
"""

import math
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .geometry import warp_to_equirect
from .metadata import load_image


def _rodrigues(w):
    theta = float(np.linalg.norm(w))
    K = np.array([[0.0, -w[2], w[1]],
                  [w[2], 0.0, -w[0]],
                  [-w[1], w[0], 0.0]])
    if theta < 1e-12:
        return np.eye(3) + K
    k = w / theta
    K = np.array([[0.0, -k[2], k[1]],
                  [k[2], 0.0, -k[0]],
                  [-k[1], k[0], 0.0]])
    return np.eye(3) + math.sin(theta) * K + (1.0 - math.cos(theta)) * (K @ K)


def _warp_gray(path, cam, R, W, H):
    im, cam_i = load_image(path, cam, W, "L", min_width=2700)
    w, wt = warp_to_equirect(im[:, :, None].repeat(3, 2), cam_i, R, W, H)
    out = np.zeros((H, W), dtype=np.float32)
    m = wt > 1e-4
    out[m] = w[..., 0][m] / wt[m]
    return out, m


def _best_shift(a, b, ms):
    from scipy.ndimage import uniform_filter
    from scipy.signal import fftconvolve
    a = a - a.mean()
    b = b - b.mean()
    am2 = float((a * a).sum())
    if am2 < 1e-6:
        return 0, 0, -1.0
    corr = fftconvolve(b, a[::-1, ::-1], mode="valid")
    # Local energy via a separable box filter (≈5x faster than a second FFT).
    u = uniform_filter(b * b, size=a.shape, mode="constant")
    ph, pw = a.shape[0] // 2, a.shape[1] // 2
    bh, bw = b.shape
    b2 = u[ph:ph + (bh - a.shape[0] + 1),
           pw:pw + (bw - a.shape[1] + 1)] * a.size
    ncc = corr / np.sqrt(np.maximum(am2 * b2, 1e-9))
    idx = np.unravel_index(np.argmax(ncc), ncc.shape)
    return idx[1] - ms, idx[0] - ms, float(ncc[idx])


def _directions(xs, ys, W, H):
    lon = (xs + 0.5) / W * 2.0 * math.pi - math.pi
    lat = math.pi / 2.0 - (ys + 0.5) / H * math.pi
    return np.stack([np.cos(lat) * np.sin(lon),
                     np.cos(lat) * np.cos(lon),
                     np.sin(lat)], axis=-1)


def _measure(paths, cams, rotations, W, H, search=32, block=64):
    """Return (i_idx, j_idx, dir_i, dir_j) correspondences."""
    try:
        from scipy.ndimage import gaussian_filter
    except Exception:  # pragma: no cover
        return None

    n = len(paths)
    workers = min(8, os.cpu_count() or 4)

    def _one(i):
        img, mask = _warp_gray(paths[i], cams[i], rotations[i], W, H)
        return gaussian_filter(img, 1.2), mask

    # Warping + decoding is the expensive part and releases the GIL, so
    # threads scale well here (≈2x on Apple Silicon).
    with ThreadPoolExecutor(max_workers=workers) as ex:
        warped = list(ex.map(_one, range(n)))

    # cheap overlap detection on downsampled masks
    small = [m[::10, ::10] for _, m in warped]
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            if (small[i] & small[j]).sum() > 120:
                pairs.append((i, j))

    step = block // 2

    def _pair(pair):
        i, j = pair
        A, mA = warped[i]
        B, mB = warped[j]
        ov = mA & mB
        ys, xs = np.where(ov)
        if len(xs) == 0:
            return ()
        # Expand the bbox by one block so blocks lying *on* a thin overlap
        # band are considered too (coverage check filters the rest).
        x0 = max(search, int(xs.min()) - block)
        x1 = min(W - search, int(xs.max()) + block)
        y0 = max(search, int(ys.min()) - block)
        y1 = min(H - search, int(ys.max()) + block)
        found = []
        count = 0
        for y in range(y0, y1 - block, step):
            for x in range(x0, x1 - block, step):
                if ov[y:y + block, x:x + block].mean() < 0.70:
                    continue
                a = A[y:y + block, x:x + block]
                if a.std() < 3.0:
                    continue
                b = B[y - search:y + block + search,
                      x - search:x + block + search]
                dx, dy, nc = _best_shift(a, b, search)
                if nc < 0.5:
                    continue
                # reject peaks pinned to the search boundary (spurious matches)
                if abs(dx) >= search - 1 or abs(dy) >= search - 1:
                    continue
                cx = x + block // 2
                cy = y + block // 2
                found.append((i, j, cx, cy, cx + dx, cy + dy))
                count += 1
                if count >= 40:
                    break
            if count >= 40:
                break
        return found

    matches = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(_pair, pairs):
            matches.extend(res)

    if not matches:
        return None
    arr = np.array(matches, dtype=np.float64)
    i_idx = arr[:, 0].astype(np.int32)
    j_idx = arr[:, 1].astype(np.int32)
    di = _directions(arr[:, 2] % W, arr[:, 3], W, H)
    dj = _directions(arr[:, 4] % W, arr[:, 5], W, H)
    return i_idx, j_idx, di, dj


def refine_poses(paths, cams, rotations, W=3000, H=1500, iterations=2,
                 progress=None, max_correction_deg=3.0):
    """Return refined rotation matrices for every frame.

    ``progress`` is called as ``progress(iteration, total)``.
    Falls back to the input rotations if scipy is unavailable or matching fails.
    """
    try:
        from scipy.optimize import least_squares
    except Exception:  # pragma: no cover
        return rotations

    rots = [np.array(R, dtype=np.float64) for R in rotations]
    n = len(paths)
    max_corr = math.radians(max_correction_deg)

    for it in range(max(1, iterations)):
        if progress:
            progress(it, iterations)
        found = _measure(paths, cams, rots, W, H)
        if found is None:
            break
        i_idx, j_idx, di, dj = found
        if len(i_idx) < 30:
            break

        # drop gross outliers before the robust solve
        diff = np.linalg.norm(di - dj, axis=1)
        keep = diff < math.radians(2.0)
        i_idx, j_idx, di, dj = i_idx[keep], j_idx[keep], di[keep], dj[keep]
        if len(i_idx) < 30:
            break

        def residual(w):
            Wm = np.clip(w.reshape(n, 3), -max_corr, max_corr)
            Rs = np.stack([_rodrigues(Wm[k]) for k in range(n)])
            ri = np.einsum("mij,mj->mi", Rs[i_idx], di)
            rj = np.einsum("mij,mj->mi", Rs[j_idx], dj)
            res = (ri - rj).ravel()
            reg = 1e-3
            return np.concatenate([res, reg * Wm.ravel()])

        x0 = np.zeros(n * 3, dtype=np.float64)
        try:
            sol = least_squares(residual, x0, method="trf", loss="soft_l1",
                                f_scale=0.01, max_nfev=80)
            w = np.clip(sol.x.reshape(n, 3), -max_corr, max_corr)
        except Exception:  # pragma: no cover
            break

        # remove the global rotation gauge so only relative corrections remain
        G = _rodrigues(w[0])
        for k in range(n):
            rots[k] = np.linalg.inv(G) @ _rodrigues(w[k]) @ rots[k]

    return rots
