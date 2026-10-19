"""High-level Python API for DJI Pano Builder.

Examples
--------
Stitch a single folder::

    from djipano import stitch

    result = stitch("PANORAMA/006_0004", output="pano.jpg", width=12000)
    print(result.output, result.width, result.height)

Process every set in a root folder::

    results = stitch("PANORAMA", out_dir="output", width=12000, preview=True)

Use an explicit list of frames::

    result = stitch(sorted(glob.glob("frames/*.JPG")), output="pano.jpg")

The lower-level functions :func:`djipano.build_panorama`,
:func:`djipano.find_pano_folders` and :func:`djipano.list_pano_files` are also
public if you need more control.
"""

from dataclasses import dataclass
import os

from .pipeline import build_panorama, find_pano_folders, list_pano_files
from .progress import UI


@dataclass
class StitchResult:
    """Result of one stitched panorama."""

    output: str
    preview: str | None
    frames: int
    width: int
    height: int


def _default_name(path):
    return os.path.basename(str(path).rstrip(os.sep)) or "panorama"


def _make_jobs(source, output, out_dir):
    if isinstance(source, (list, tuple)):
        paths = [str(p) for p in source]
        if not paths:
            raise ValueError("No input frames given")
        out = output or os.path.join(out_dir, "panorama_pano.jpg")
        return [(paths, out)]

    source = str(source)
    if not os.path.exists(source):
        raise FileNotFoundError(source)

    if os.path.isdir(source):
        paths = list_pano_files(source)
        if paths:
            out = output or os.path.join(
                out_dir, _default_name(source) + "_pano.jpg")
            return [(paths, out)]

        folders = find_pano_folders(source)
        if not folders:
            raise ValueError(f"No PANO*.JPG frames found in {source}")
        return [
            (list_pano_files(folder),
             os.path.join(out_dir, _default_name(folder) + "_pano.jpg"))
            for folder in folders
        ]

    return [([source], output or os.path.join(out_dir, "panorama_pano.jpg"))]


def stitch(source, output=None, out_dir="output", width=12000,
           preview=False, fill=True, exposure="none", blend="multiband",
           sharpness=3.0, seam=True, refine=True, quiet=True,
           progress_cb=None):
    """Stitch one panorama set or a whole root folder.

    Parameters
    ----------
    source : str or sequence of str
        Folder with ``PANO*.JPG`` frames, a root folder containing several such
        folders, or an explicit list of frame paths.
    output : str, optional
        Output JPEG path (single set only).
    out_dir : str
        Output folder; used for batch mode and as the default single output.
    width : int
        Panorama width in pixels; height is ``width // 2``.
    preview : bool
        Also write a ``*_preview.jpg`` tiny-planet image.
    fill : bool
        Fill the uncovered zenith gap.
    exposure : {"none", "auto"}
    blend : {"multiband", "linear"}
    sharpness : float
        Seam sharpness (higher = narrower transition).
    seam : bool
        Use seam-based assignment (one frame per pixel).
    refine : bool
        Refine per-frame rotations from overlaps (fixes crooked horizons).
    quiet : bool
        Suppress the terminal progress UI.
    progress_cb : callable, optional
        ``progress_cb(i, n, filename)`` called while rendering.

    Returns
    -------
    StitchResult or list[StitchResult]
        A single result for one set, or a list for a batch.
    """
    jobs = _make_jobs(source, output, out_dir)
    if not jobs:
        return []

    ui = UI(enabled=not quiet)
    results = []
    for paths, out in jobs:
        if not paths:
            continue
        parent = os.path.dirname(os.path.abspath(out))
        if parent:
            os.makedirs(parent, exist_ok=True)
        res = build_panorama(
            paths, width, output=out, preview=preview, fill=fill,
            exposure=exposure, blend=blend, weight_power=sharpness,
            progress_cb=progress_cb, ui=ui, seam=seam, refine=refine)
        results.append(StitchResult(
            output=out,
            preview=res.get("preview"),
            frames=len(paths),
            width=width,
            height=width // 2,
        ))

    return results[0] if len(results) == 1 else results
