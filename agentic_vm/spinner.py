from __future__ import annotations

import threading
from typing import TextIO

DEFAULT_SPINNER_FRAME_INTERVAL_SECONDS = 0.1
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class Spinner:
    def __init__(
        self,
        stream: TextIO,
        message: str,
        frame_interval_seconds: float,
    ):
        self.stream = stream
        self.message = message
        self.frame_interval_seconds = frame_interval_seconds
        self.index = 0
        self.active = False
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.active = True
        self.stop_event.clear()
        self.render()
        self.thread = threading.Thread(target=self.spin, daemon=True)
        self.thread.start()

    def tick(self) -> None:
        if not self.active:
            return
        self.index = (self.index + 1) % len(SPINNER_FRAMES)
        self.render()

    def finish(self) -> None:
        if not self.active:
            return
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
            self.thread = None
        width = len(self.message) + 4
        self.stream.write("\r" + (" " * width) + "\r")
        self.stream.flush()
        self.active = False

    def render(self) -> None:
        self.stream.write(f"\r{SPINNER_FRAMES[self.index]} {self.message}")
        self.stream.flush()

    def spin(self) -> None:
        while not self.stop_event.wait(self.frame_interval_seconds):
            self.tick()
