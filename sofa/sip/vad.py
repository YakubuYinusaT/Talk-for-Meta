"""Cuts what the caller says out of a stream of 20 ms telephone frames: where speech starts, where it ends, and the audio between.

Energy only, no model: a phone line is clean enough, and the level it must clear rises with the line's own noise, so a noisy street does not read as speech.
"""

import sys
from array import array
from dataclasses import dataclass

from .audiosocket import FRAME_MS

PRE_ROLL_FRAMES = 10   # 200 ms before the first loud frame are kept, so the start of a word is not clipped
TAIL_FRAMES = 8        # and 160 ms after the last one
START_FRAMES = 3       # this many loud frames in a row (60 ms) begin speech


def frame_rms(frame: bytes) -> float:
    samples = array("h")
    samples.frombytes(frame[: len(frame) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


@dataclass
class Utterance:
    pcm: bytes
    voiced_ms: int


class Endpointer:
    """Feed it frames one after another. `feed` returns an Utterance when a stretch of speech has just ended, else None."""

    def __init__(self, voice_rms: float = 300.0, floor_factor: float = 3.0, end_ms: int = 800, min_speech_ms: int = 240, max_ms: int = 15000):
        self.voice_rms, self.floor_factor = voice_rms, floor_factor
        self.end_frames = max(1, end_ms // FRAME_MS)
        self.min_voiced = max(1, min_speech_ms // FRAME_MS)
        self.max_frames = max(1, max_ms // FRAME_MS)
        self.floor = 40.0           # the line's own noise level: follows the quiet moments down fast, and rises only slowly, so speech does not become the floor
        self.speaking = False       # speech has started and not yet ended
        self.run = 0                # loud frames in a row, now (the caller talking over Talk is measured with this)
        self.frames = 0             # how many frames the line has sent, and how loud the loudest was (for the log of a call)
        self.peak = 0.0
        self._pre: list[bytes] = []
        self._frames: list[bytes] = []
        self._voiced = 0
        self._quiet = 0

    @property
    def threshold(self) -> float:
        """The level that counts as speech: the fixed one, raised on a noisy line, but never above three times the fixed one (ordinary speech must clear it)."""
        return max(self.voice_rms, min(self.floor * self.floor_factor, self.voice_rms * 3))

    def reset(self) -> None:
        self.speaking, self.run, self._pre, self._frames, self._voiced, self._quiet = False, 0, [], [], 0, 0

    def finish(self) -> Utterance | None:
        """Speech that was under way when the line stopped sending frames (a trunk may send nothing at all while the caller is quiet): it has ended."""
        if not self.speaking:
            return None
        frames, voiced = self._frames, self._voiced
        self.reset()
        return Utterance(b"".join(frames), voiced * FRAME_MS) if voiced >= self.min_voiced else None

    def feed(self, frame: bytes) -> Utterance | None:
        level = frame_rms(frame)
        self.frames += 1
        self.peak = max(self.peak, level)
        loud = level > self.threshold
        if level < self.floor:  # the line is quieter than thought: the noise level drops to it quickly
            self.floor = 0.9 * self.floor + 0.1 * level
        else:  # steady noise that is louder than thought raises it slowly, speech or not, so a noisy line is never "speech" for ever
            self.floor += 0.004 * (level - self.floor)
        self.run = self.run + 1 if loud else 0
        if not self.speaking:
            self._pre.append(frame)
            del self._pre[:-(PRE_ROLL_FRAMES + START_FRAMES)]
            if self.run >= START_FRAMES:
                self.speaking, self._frames, self._pre = True, list(self._pre), []
                self._voiced, self._quiet = self.run, 0
            return None
        self._frames.append(frame)
        if loud:
            self._voiced += 1
            self._quiet = 0
        else:
            self._quiet += 1
        if self._quiet >= self.end_frames or len(self._frames) >= self.max_frames:
            frames = self._frames[: len(self._frames) - max(0, self._quiet - TAIL_FRAMES)]
            voiced = self._voiced
            self.reset()
            if voiced >= self.min_voiced:
                return Utterance(b"".join(frames), voiced * FRAME_MS)
        return None
