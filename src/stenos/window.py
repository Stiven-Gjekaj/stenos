"""The window itself, which is a rendering of what ``interface.py`` derives.

Deliberately thin. Every decision about what to show is made next door and
tested without a display; this arranges labels and refreshes them, so a fault
here is a layout fault rather than a wrong answer.

Tkinter is imported at the top of this module and nowhere else in the package,
so importing ``stenos`` never depends on it. It ships with Python on Windows
and macOS, and on Linux it is routinely a separate package the distribution
does not install by default, which the caller reports rather than crashing on.

This first window is read only. It shows what is recording, what has been
recorded, and what a crash left behind, and it can transcribe the last of
those. Starting and stopping a recording needs the bot running in the same
process as the event loop that draws this, which is a piece of design in its
own right and is deliberately not attempted here.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from . import __version__
from .config import Config
from .interface import Library, library

__all__ = ["Window", "run"]

#: How often the library is read again from disk, in milliseconds. A recording
#: appears when it is written, and a person is not watching for it to the
#: second.
REFRESH_MS = 2000


class Window:
    """The main window, with a tab per thing it shows."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.root = tk.Tk()
        self.root.title(f"Stenos {__version__}")
        self.root.minsize(720, 420)

        # Work that touches the disk runs on a thread, and what it produces
        # comes back through here. Tk is not safe to call from another thread,
        # so nothing else does.
        self._finished: queue.SimpleQueue[str] = queue.SimpleQueue()

        tabs = ttk.Notebook(self.root)
        tabs.pack(fill="both", expand=True, padx=8, pady=8)

        self.library_rows = self._table(tabs, "Transcripts", ("Recording", "Length", "Speakers"))
        self.unfinished_rows = self._table(tabs, "Unfinished", ("Recording", "Held"))
        self._settings_tab(tabs)

        self.status = ttk.Label(self.root, text="", anchor="w")
        self.status.pack(fill="x", padx=8, pady=(0, 8))

        self.recover_button = ttk.Button(
            self.root, text="Transcribe unfinished recordings", command=self.recover
        )
        self.recover_button.pack(padx=8, pady=(0, 8))

        self.refresh()

    def _table(self, tabs: ttk.Notebook, title: str, columns: tuple[str, ...]) -> ttk.Treeview:
        frame = ttk.Frame(tabs)
        tabs.add(frame, text=title)
        table = ttk.Treeview(frame, columns=columns, show="headings")
        for column in columns:
            table.heading(column, text=column)
            table.column(column, anchor="w", width=240)
        table.pack(fill="both", expand=True)
        return table

    def _settings_tab(self, tabs: ttk.Notebook) -> None:
        """The resolved configuration, read only.

        Editing belongs to a later window. Showing what is in force is worth
        having now, because the commonest question about a recording is which
        setting decided something.
        """
        frame = ttk.Frame(tabs)
        tabs.add(frame, text="Settings")
        for row, (name, value) in enumerate(_settings(self.config)):
            ttk.Label(frame, text=name, anchor="w").grid(row=row, column=0, sticky="w", padx=6)
            ttk.Label(frame, text=value, anchor="w").grid(row=row, column=1, sticky="w", padx=6)

    def refresh(self) -> None:
        """Read the output directory again and redraw both tables."""
        self._drain()
        found = library(self.config.output_dir)
        self._fill(self.library_rows, _library_rows(found))
        self._fill(self.unfinished_rows, _unfinished_rows(found))
        self.recover_button.state(["!disabled"] if found.unfinished else ["disabled"])
        self.root.after(REFRESH_MS, self.refresh)

    def _drain(self) -> None:
        """Show whatever the worker thread finished since the last refresh."""
        while True:
            try:
                self.status.configure(text=self._finished.get_nowait())
            except queue.Empty:
                return

    def _fill(self, table: ttk.Treeview, rows: list[tuple[str, ...]]) -> None:
        table.delete(*table.get_children())
        for row in rows:
            table.insert("", "end", values=row)

    def recover(self) -> None:
        """Transcribe every unfinished recording, off the drawing thread.

        Transcription takes minutes. Run here it would freeze the window for
        the whole of it, which reads as a crash.
        """
        self.recover_button.state(["disabled"])
        self.status.configure(text="Transcribing unfinished recordings...")
        threading.Thread(target=self._recover, name="stenos-recover", daemon=True).start()

    def _recover(self) -> None:
        from .bot import recover

        try:
            code = recover(self.config)
        except Exception as error:
            self._finished.put(f"Recovery failed: {error.__class__.__name__}: {error}")
            return
        self._finished.put(
            "Recovered what could be read." if code == 0 else "Some recordings could not be read."
        )

    def run(self) -> int:
        self.root.mainloop()
        return 0


def _settings(config: Config) -> list[tuple[str, str]]:
    """The configuration as rows, in the order the documentation lists them."""
    return [
        ("Backend", config.whisper_backend),
        ("Model", config.whisper_model),
        ("Language", config.language or "auto"),
        ("Segment gap", f"{config.segment_gap:g}s"),
        ("Minimum segment", f"{config.min_segment:g}s"),
        ("Maximum segment", f"{config.max_segment:g}s"),
        ("Buffer limit", _limit(config.max_buffer_mb, "MB")),
        ("Disk limit", _limit(config.max_disk_mb, "MB")),
        ("Disconnect grace", _limit(config.disconnect_grace, "s")),
        ("Maximum outage", _limit(config.max_outage, "s")),
        ("Output directory", str(config.output_dir)),
        ("Keep audio", "yes" if config.keep_audio else "no"),
    ]


def _limit(value: float, unit: str) -> str:
    return "none" if value <= 0 else f"{value:g}{unit}"


def _library_rows(found: Library) -> list[tuple[str, ...]]:
    return [
        (item.title, _length(item.duration), ", ".join(item.speakers) or "nobody named")
        for item in found.transcripts
    ]


def _unfinished_rows(found: Library) -> list[tuple[str, ...]]:
    return [(item.summary, str(item.directory.name)) for item in found.unfinished]


def _length(duration: float) -> str:
    from .bot import format_duration

    return format_duration(duration)


def run(config: Config) -> int:
    """Open the window and stay in it until it is closed."""
    return Window(config).run()
