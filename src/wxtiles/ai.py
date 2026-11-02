"""AI model runners.

In production Pangu-Weather ran four times a day on a g5.xlarge Spot instance: the job pulled the
GFS analysis it needs (five upper-air variables on 13 pressure levels plus four surface fields) by
byte range, ran the 24 h and 6 h ONNX models with onnxruntime-gpu to 120 h, and wrote GRIB2 with a
wgrib2 index to our own bucket, where the normal ingest path picked it up like any other feed.
That runner needs the model weights and a GPU, so it is abstracted here behind the same interface:
`SyntheticRunner` writes the same files from the synthetic atmosphere.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from wxtiles.registry import Source, Step
from wxtiles.synth.writers import PrecipBook, indexed_file


class Runner:
    def run(self, cycle: datetime) -> Iterator[tuple[Step, bytes, str]]:
        """Yield (step, GRIB2 bytes, inventory text) for every forecast step of one run."""
        raise NotImplementedError


class SyntheticRunner(Runner):
    def __init__(self, src: Source):
        self.src = src

    def run(self, cycle: datetime) -> Iterator[tuple[Step, bytes, str]]:
        book = PrecipBook()
        for step in self.src.steps():
            data, idx = indexed_file(self.src, cycle, step, book)
            yield step, data, idx


def runner_for(src: Source) -> Runner:
    return SyntheticRunner(src)
