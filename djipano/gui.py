"""Native Tkinter GUI for DJI Pano Builder.

The GUI is a thin layer over :mod:`djipano.pipeline` — all stitching logic is
shared with the command-line interface.

The layout deliberately uses only native ``ttk`` widgets (aqua on macOS,
vista on Windows, clam/default on Linux) and the system UI font, so it looks
and behaves like a normal desktop application.
"""

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import font as tkfont

from PIL import Image, ImageTk

from .pipeline import build_panorama, find_pano_folders, list_pano_files
from .progress import UI


APP_TITLE = "DJI Pano Builder"


def _native_bg(widget):
    style = ttk.Style(widget)
    for name in ("TFrame", "TLabel", "TPanedwindow"):
        color = style.lookup(name, "background")
        if color:
            return color
    return widget.cget("background")


class ImagePanel(ttk.Frame):
    """Canvas that scales an image to fit while keeping the aspect ratio."""

    def __init__(self, master, empty_text="No preview yet"):
        super().__init__(master)
        bg = _native_bg(master)
        self.canvas = tk.Canvas(self, background=bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.empty_text = empty_text
        self._bg = bg
        self._pil = None
        self._photo = None
        self.canvas.bind("<Configure>", lambda e: self._redraw())

    def set_image(self, path):
        try:
            self._pil = Image.open(path).convert("RGB")
        except Exception:
            self._pil = None
        self._redraw()

    def clear(self):
        self._pil = None
        self._redraw()

    def _redraw(self):
        self.canvas.delete("all")
        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())
        if self._pil is None:
            self.canvas.create_text(w // 2, h // 2, text=self.empty_text,
                                    fill="#7a7a7a",
                                    font=tkfont.nametofont("TkDefaultFont"))
            return
        iw, ih = self._pil.size
        scale = min(w / iw, h / ih)
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        resample = getattr(Image, "LANCZOS", Image.BICUBIC)
        thumb = self._pil.resize((nw, nh), resample)
        self._photo = ImageTk.PhotoImage(thumb)
        self.canvas.create_image(w // 2, h // 2, image=self._photo, anchor="center")


class App(tk.Tk):
    def __init__(self, input_path=None, autorun=False, width=None):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1120x760")
        self.minsize(980, 640)
        self.events = queue.Queue()
        self.worker = None
        self.started = 0.0
        self.all_outputs = []

        self._setup_fonts()
        self._build_menu()
        self._build_ui()
        self._bind_shortcuts()

        if width:
            self.width_var.set(int(width))
        if input_path:
            self.input_var.set(input_path)
            self._scan()
        if autorun:
            self.after(500, self._start)
        self.after(100, self._poll)

    # ------------------------------------------------------------- styling
    def _setup_fonts(self):
        base = tkfont.nametofont("TkDefaultFont")
        self.title_font = base.copy()
        self.title_font.configure(size=20, weight="bold")
        self.small_font = base.copy()
        self.small_font.configure(size=11)
        self.mono_font = tkfont.nametofont("TkFixedFont")

    def _build_menu(self):
        menubar = tk.Menu(self)

        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open Folder…", command=self._browse_input,
                             accelerator="Cmd+O" if sys.platform == "darwin" else "Ctrl+O")
        filemenu.add_command(label="Build Panorama", command=self._start,
                             accelerator="Cmd+R" if sys.platform == "darwin" else "Ctrl+R")
        filemenu.add_command(label="Open Output Folder", command=self._open_output)
        filemenu.add_separator()
        filemenu.add_command(label="Quit", command=self.destroy,
                             accelerator="Cmd+Q" if sys.platform == "darwin" else "Ctrl+Q")
        menubar.add_cascade(label="File", menu=filemenu)

        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="About DJI Pano Builder", command=self._about)
        menubar.add_cascade(label="Help", menu=helpmenu)

        self.config(menu=menubar)

    def _bind_shortcuts(self):
        mod = "Command" if sys.platform == "darwin" else "Control"
        self.bind(f"<{mod}-o>", lambda e: self._browse_input())
        self.bind(f"<{mod}-r>", lambda e: self._start())
        self.bind(f"<{mod}-q>", lambda e: self.destroy())

    def _about(self):
        messagebox.showinfo(
            APP_TITLE,
            "DJI Pano Builder\n\n"
            "Stitch DJI Sphere Panorama frames into a standard 2:1\n"
            "equirectangular panorama with GPano metadata.")

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        outer = ttk.Frame(self, padding=(12, 10))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        # ---------------- left: settings ----------------
        left = ttk.Frame(outer)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.columnconfigure(0, weight=1)

        ttk.Label(left, text=APP_TITLE, font=self.title_font).grid(
            row=0, column=0, sticky="w")
        ttk.Label(left, text="DJI Sphere Panorama → equirectangular 2:1",
                  font=self.small_font, foreground="#6b6b6b").grid(
            row=1, column=0, sticky="w", pady=(0, 10))

        # input
        inp = ttk.LabelFrame(left, text=" Input ", padding=10)
        inp.grid(row=2, column=0, sticky="ew")
        inp.columnconfigure(0, weight=1)
        row = ttk.Frame(inp)
        row.grid(row=0, column=0, sticky="ew")
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text="Folder:").grid(row=0, column=0, sticky="w")
        self.input_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.input_var).grid(
            row=0, column=1, sticky="ew", padx=8)
        ttk.Button(row, text="Choose…", command=self._browse_input).grid(row=0, column=2)
        ttk.Button(row, text="Scan", command=self._scan).grid(row=0, column=3, padx=(6, 0))
        self.sets_var = tk.StringVar(value="Choose a folder with PANO*.JPG frames")
        ttk.Label(inp, textvariable=self.sets_var, font=self.small_font,
                  foreground="#6b6b6b").grid(row=1, column=0, sticky="w", pady=(8, 0))

        # options
        opts = ttk.LabelFrame(left, text=" Options ", padding=10)
        opts.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        for c in (1, 3):
            opts.columnconfigure(c, weight=1)

        self.width_var = tk.IntVar(value=12000)
        self.blend_var = tk.StringVar(value="multiband")
        self.sharpen_var = tk.IntVar(value=3)
        self.exposure_var = tk.StringVar(value="none")

        ttk.Label(opts, text="Width (px):").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Spinbox(opts, from_=2000, to=24000, increment=1000, width=9,
                    textvariable=self.width_var).grid(row=0, column=1, sticky="w", padx=(6, 14))
        ttk.Label(opts, text="Blend:").grid(row=0, column=2, sticky="w")
        ttk.Combobox(opts, textvariable=self.blend_var, width=11, state="readonly",
                     values=["multiband", "linear"]).grid(row=0, column=3, sticky="w", padx=6)

        ttk.Label(opts, text="Sharpness:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Spinbox(opts, from_=1, to=20, width=9,
                    textvariable=self.sharpen_var).grid(row=1, column=1, sticky="w", padx=(6, 14))
        ttk.Label(opts, text="Exposure:").grid(row=1, column=2, sticky="w")
        ttk.Combobox(opts, textvariable=self.exposure_var, width=11, state="readonly",
                     values=["none", "auto"]).grid(row=1, column=3, sticky="w", padx=6)

        checks = ttk.Frame(opts)
        checks.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.preview_var = tk.BooleanVar(value=True)
        self.fill_var = tk.BooleanVar(value=True)
        self.seam_var = tk.BooleanVar(value=True)
        self.refine_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(checks, text="Tiny planet", variable=self.preview_var).pack(side="left")
        ttk.Checkbutton(checks, text="Fill zenith", variable=self.fill_var).pack(side="left", padx=12)
        ttk.Checkbutton(checks, text="Seam-based", variable=self.seam_var).pack(side="left")
        ttk.Checkbutton(checks, text="Refine alignment", variable=self.refine_var).pack(side="left", padx=(12, 0))

        # output
        out = ttk.LabelFrame(left, text=" Output ", padding=10)
        out.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        out.columnconfigure(1, weight=1)
        ttk.Label(out, text="Folder:").grid(row=0, column=0, sticky="w")
        self.out_var = tk.StringVar(value="output")
        ttk.Entry(out, textvariable=self.out_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(out, text="Choose…", command=self._browse_output).grid(row=0, column=2)

        # actions
        actions = ttk.Frame(left)
        actions.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        self.build_btn = ttk.Button(actions, text="Build Panorama", command=self._start)
        self.build_btn.pack(side="left", fill="x", expand=True)
        self.open_btn = ttk.Button(actions, text="Open Output", command=self._open_output,
                                   state="disabled")
        self.open_btn.pack(side="left", padx=(8, 0))
        self.reset_btn = ttk.Button(actions, text="Reset", command=self._reset_defaults)
        self.reset_btn.pack(side="left", padx=(8, 0))

        # ---------------- right: preview + progress ----------------
        right = ttk.Frame(outer)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(right)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.pano_panel = ImagePanel(self.notebook, "Panorama preview")
        self.planet_panel = ImagePanel(self.notebook, "Tiny planet")
        self.notebook.add(self.pano_panel, text="Panorama")
        self.notebook.add(self.planet_panel, text="Tiny planet")

        prog = ttk.LabelFrame(right, text=" Progress ", padding=10)
        prog.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        prog.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=1000)
        self.bar.grid(row=0, column=0, sticky="ew")
        info = ttk.Frame(prog)
        info.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        info.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Idle")
        self.eta_var = tk.StringVar(value="")
        ttk.Label(info, textvariable=self.status_var, font=self.small_font).grid(
            row=0, column=0, sticky="w")
        ttk.Label(info, textvariable=self.eta_var, font=self.small_font,
                  foreground="#6b6b6b").grid(row=0, column=1, sticky="e")

        self.log = tk.Text(prog, height=5, wrap="none", relief="solid",
                           borderwidth=1, highlightthickness=0,
                           font=self.mono_font)
        self.log.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.log.configure(state="disabled")

    # -------------------------------------------------------------- actions
    def _browse_input(self):
        path = filedialog.askdirectory(title="Select panorama folder")
        if path:
            self.input_var.set(path)
            self._scan()

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.out_var.set(path)

    def _scan(self):
        path = self.input_var.get().strip()
        if not path or not os.path.isdir(path):
            self.sets_var.set("Folder not found")
            return
        folders = find_pano_folders(path)
        if not folders:
            self.sets_var.set("No PANO*.JPG sets found")
            return
        total = sum(len(list_pano_files(f)) for f in folders)
        names = ", ".join(os.path.basename(f.rstrip(os.sep)) for f in folders[:5])
        if len(folders) > 5:
            names += ", …"
        self.sets_var.set(f"{len(folders)} set(s) · {total} frames · {names}")

    def _open_output(self):
        path = os.path.abspath(self.out_var.get().strip() or "output")
        if not os.path.isdir(path):
            return
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            messagebox.showinfo(APP_TITLE, path)

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _reset_defaults(self):
        """Reset all option widgets to their default values."""
        self.width_var.set(12000)
        self.blend_var.set("multiband")
        self.sharpen_var.set(3)
        self.exposure_var.set("none")
        self.preview_var.set(True)
        self.fill_var.set(True)
        self.seam_var.set(True)
        self.refine_var.set(True)
        self.out_var.set("output")

    # ---------------------------------------------------------------- build
    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        source = self.input_var.get().strip()
        if not source or not os.path.isdir(source):
            messagebox.showwarning(APP_TITLE, "Choose a valid input folder.")
            return
        folders = find_pano_folders(source)
        if not folders:
            messagebox.showwarning(APP_TITLE, "No PANO*.JPG frames found.")
            return

        opts = {
            "input": source,
            "width": int(self.width_var.get()),
            "preview": bool(self.preview_var.get()),
            "fill": bool(self.fill_var.get()),
            "seam": bool(self.seam_var.get()),
            "refine": bool(self.refine_var.get()),
            "blend": self.blend_var.get(),
            "sharpness": float(self.sharpen_var.get()),
            "exposure": self.exposure_var.get(),
            "out_dir": self.out_var.get().strip() or "output",
        }

        self.build_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.all_outputs = []
        self.pano_panel.clear()
        self.planet_panel.clear()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.bar["value"] = 0
        self.status_var.set("Starting…")
        self.eta_var.set("")
        self.started = time.time()

        self.worker = threading.Thread(target=self._run, args=(folders, opts), daemon=True)
        self.worker.start()

    def _run(self, folders, opts):
        try:
            out_dir = opts["out_dir"]
            os.makedirs(out_dir, exist_ok=True)
            total_frames = sum(max(1, len(list_pano_files(f))) for f in folders)
            completed = 0

            for fi, folder in enumerate(folders, 1):
                paths = list_pano_files(folder)
                name = os.path.basename(folder.rstrip(os.sep)) or "panorama"
                out_path = os.path.join(out_dir, name + "_pano.jpg")
                self.events.put(("log", f"[{fi}/{len(folders)}] {name}: {len(paths)} frames"))
                self.events.put(("stage", fi, len(folders), name, len(paths)))

                def progress(i, n, filename, _name=name, _base=completed):
                    done = _base + i
                    overall = done / max(1, total_frames)
                    elapsed = time.time() - self.started
                    eta = (elapsed / max(1, done)) * (total_frames - done) if done else None
                    self.events.put(("progress", overall, i, n, _name, eta))

                ui = UI(enabled=False)
                build_panorama(
                    paths, opts["width"], output=out_path,
                    preview=opts["preview"], fill=opts["fill"],
                    exposure=opts["exposure"], blend=opts["blend"],
                    weight_power=opts["sharpness"], progress_cb=progress,
                    ui=ui, seam=opts["seam"], refine=opts["refine"])

                completed += max(1, len(paths))

                eq_prev = os.path.join(out_dir, "_preview_equirect.jpg")
                with Image.open(out_path) as im:
                    im = im.convert("RGB")
                    im.thumbnail((1800, 900))
                    im.save(eq_prev, quality=90)
                planet_prev = None
                if opts["preview"]:
                    base, ext = os.path.splitext(out_path)
                    src = base + "_preview" + ext
                    if os.path.exists(src):
                        planet_prev = os.path.join(out_dir, "_preview_planet.jpg")
                        with Image.open(src) as im:
                            im = im.convert("RGB")
                            im.thumbnail((900, 900))
                            im.save(planet_prev, quality=90)

                self.events.put(("preview", eq_prev, planet_prev))
                self.events.put(("log", f"Saved {out_path}"))
                self.all_outputs.append(out_path)
                self.events.put(("progress", fi / len(folders), len(paths),
                                 len(paths), name, 0))

            self.events.put(("done", list(self.all_outputs)))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", str(exc)))

    # ---------------------------------------------------------------- poll
    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    self._log(ev[1])
                elif kind == "stage":
                    fi, total, name, frames = ev[1:]
                    self.status_var.set(f"Stitching {name} — {frames} frames "
                                        f"({fi}/{total})")
                elif kind == "progress":
                    overall, done, total, name, eta = ev[1:]
                    self.bar["value"] = int(overall * 1000)
                    self.status_var.set(f"{name} — {done}/{total} frames "
                                        f"· {overall * 100:.0f}%")
                    if eta:
                        self.eta_var.set("ETA %02d:%02d" % (int(eta) // 60, int(eta) % 60))
                    else:
                        self.eta_var.set("")
                elif kind == "preview":
                    self.pano_panel.set_image(ev[1])
                    if ev[2]:
                        self.planet_panel.set_image(ev[2])
                elif kind == "done":
                    elapsed = time.time() - self.started
                    self.bar["value"] = 1000
                    self.status_var.set(
                        "Done — %d panorama(s) in %dm %02ds"
                        % (len(ev[1]), int(elapsed) // 60, int(elapsed) % 60))
                    self.eta_var.set("")
                    self.build_btn.configure(state="normal")
                    self.open_btn.configure(state="normal")
                    self._log("Done.")
                elif kind == "error":
                    self.status_var.set("Error: " + ev[1])
                    self.eta_var.set("")
                    self.build_btn.configure(state="normal")
                    self._log("Error: " + ev[1])
                    messagebox.showerror(APP_TITLE, ev[1])
        except queue.Empty:
            pass
        self.after(100, self._poll)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Native GUI for DJI Pano Builder.")
    ap.add_argument("--input", default=None, help="input folder to prefill")
    ap.add_argument("--width", type=int, default=None, help="prefill panorama width")
    ap.add_argument("--autorun", action="store_true", help="start the build on launch")
    args = ap.parse_args(argv)

    if not _tk_available():
        print("Tkinter is not available for this Python.\n"
              "macOS (Homebrew):  brew install python-tk@3.14\n"
              "Debian/Ubuntu:     sudo apt install python3-tk\n"
              "Windows:           use the official python.org installer",
              file=sys.stderr)
        return 1
    app = App(input_path=args.input, autorun=args.autorun, width=args.width)
    app.mainloop()
    return 0


def _tk_available():
    return bool(tk)
