#!/usr/bin/env python3

from dataclasses import dataclass
from typing import List, Optional, Tuple

# -------------------------
# DATA MODEL
# -------------------------


@dataclass
class Bar:
    bpm: Optional[float] = None
    time_sig: Tuple[int, int] = (4, 4)


@dataclass
class Anchor:
    bar: int
    time: float


# -------------------------
# SCORE OBJECT
# -------------------------


class Score:
    def __init__(self):
        self.bars: List[Bar] = []
        self.anchors: List[Anchor] = []

    def add_bar(self, bpm=None, time_sig=(4, 4)):
        self.bars.append(Bar(bpm, time_sig))
        return self

    def anchor(self, bar, time):
        self.anchors.append(Anchor(bar, time))
        return self


# -------------------------
# TIME SOLVER
# -------------------------


def solve_bar_times(score: Score):
    """
    Returns absolute time for each bar using:
    - per-bar BPM
    - linear interpolation between anchors
    """

    bars = score.bars
    n = len(bars)

    # default bar durations from BPM
    bar_durations = []
    for b in bars:
        bpm = b.bpm if b.bpm else 120
        beats = b.time_sig[0]
        bar_durations.append((60.0 / bpm) * beats)

    # anchor map
    anchors = sorted(score.anchors, key=lambda a: a.bar)

    # result times
    times = [0.0] * (n + 1)

    # apply anchors via piecewise scaling
    if not anchors:
        for i in range(1, n + 1):
            times[i] = times[i - 1] + bar_durations[i - 1]
        return times

    # helper: find anchor segment
    anchor_idx = 0

    for i in range(1, n + 1):
        # next anchor?
        if anchor_idx < len(anchors) and i == anchors[anchor_idx].bar:
            times[i] = anchors[anchor_idx].time
            anchor_idx += 1
        else:
            # interpolate forward using nominal duration
            times[i] = times[i - 1] + bar_durations[i - 1]

    return times


# -------------------------
# BEATMAP GENERATION
# -------------------------


def emit_beatmap(score: Score, out_path="out.beatmap"):
    bar_times = solve_bar_times(score)

    with open(out_path, "w") as f:
        for i, b in enumerate(score.bars):
            start = bar_times[i]
            end = bar_times[i + 1]

            beats = b.time_sig[0]
            dt = (end - start) / beats if beats > 0 else 0

            for beat in range(beats):
                t = start + beat * dt
                tempo = (60.0 / dt) if dt > 0 else 0

                line = f"- 1 {i + 1} {beat + 1} {secs(t)} 1 1 {tempo:.6f}"
                f.write(line + "\n")


# -------------------------
# UTIL
# -------------------------


def secs(t):
    m, s = divmod(t, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:06.3f}"


# -------------------------
# EXAMPLE
# -------------------------

if __name__ == "__main__":
    s = Score()

    # bars with tempo + time sig changes
    for _ in range(10):
        s.add_bar(bpm=120, time_sig=(4, 4))

    for _ in range(10):
        s.add_bar(bpm=90, time_sig=(4, 4))

    # fermata / rubato anchor
    s.anchor(bar=11, time=22.5)

    emit_beatmap(s)
