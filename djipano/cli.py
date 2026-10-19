"""Command-line interface for DJI Pano Builder."""

import argparse
import os
import sys
import time

from .pipeline import build_panorama, find_pano_folders, list_pano_files
from .progress import UI


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
    ap.add_argument("--no-seam", action="store_true",
                    help="disable seam-based assignment (smooth blending only)")
    ap.add_argument("--no-refine", action="store_true",
                    help="disable pose refinement (faster, less accurate seams)")
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
                           weight_power=args.sharpness, ui=ui,
                           seam=not args.no_seam,
                           refine=not args.no_refine)
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
