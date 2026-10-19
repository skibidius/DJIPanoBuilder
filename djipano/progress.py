"""Terminal progress bar, ETA and spinner (English output)."""

import os
import shutil
import sys
import threading
import time


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
