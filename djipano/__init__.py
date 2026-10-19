"""DJI Pano Builder — shared core package.

Both the CLI (``dji_pano.py``) and the GUI (``dji_pano_gui.py``) use this
package, so the stitching logic lives in exactly one place.

Quick start::

    from djipano import stitch

    result = stitch("PANORAMA/006_0004", output="pano.jpg")
    print(result.output, result.width, result.height)
"""

from .metadata import (Camera, read_xmp_gimbal, read_exif_focal,
                       build_gpano_xmp, build_output_exif, load_image)
from .geometry import rotation_matrix, warp_to_equirect
from .blending import estimate_gains, fill_holes
from .pipeline import (build_panorama, find_pano_folders, list_pano_files,
                       tiny_planet)
from .align import refine_poses
from .progress import UI
from .api import stitch, StitchResult

__version__ = "1.2.0"

__all__ = [
    "Camera", "read_xmp_gimbal", "read_exif_focal", "build_gpano_xmp",
    "build_output_exif", "load_image", "rotation_matrix",
    "warp_to_equirect", "estimate_gains", "fill_holes", "build_panorama",
    "find_pano_folders", "list_pano_files", "tiny_planet", "refine_poses",
    "UI", "stitch", "StitchResult", "__version__",
]
