"""Reproducible per-clip ambient attenuation, independent of TTS and dialogue policy."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
import secrets

import numpy as np


@dataclass(frozen=True)
class AmbientGainPolicy:
    minimum: float = 0.3
    maximum: float = 0.7
    seed: int = field(default_factory=lambda: secrets.randbits(64))

    def __post_init__(self):
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (self.minimum, self.maximum)):
            raise ValueError("ambient gain bounds must be finite numbers")
        if not 0 < self.minimum <= self.maximum <= 1:
            raise ValueError("ambient gain requires 0 < minimum <= maximum <= 1")
        if type(self.seed) is not int or not 0 <= self.seed < 2**64:
            raise ValueError("ambient gain seed must be an unsigned 64-bit integer")

    def record(self, key: str) -> dict:
        if not isinstance(key, str) or not key:
            raise ValueError("ambient gain requires a stable clip key")
        value = int.from_bytes(hashlib.sha256(f"{self.seed}\0{key}".encode()).digest()[:8], "big")
        factor = self.minimum + (self.maximum - self.minimum) * value / (2**64 - 1)
        return {"method": "pcm16_linear_amplitude_v1", "factor": factor, "key": key, "seed": self.seed}


def ambient_gain_key(case_id: str, turn_id: str, index: int, kind="side_speech") -> str:
    if kind not in {"side_speech", "background_audio"} or type(index) is not int or index < 0:
        raise ValueError("invalid ambient clip identity")
    return f"{case_id}/{turn_id}/{kind}/{index}"


def scale_pcm16(samples: np.ndarray, factor: float) -> np.ndarray:
    if samples.dtype.kind != "i" or samples.dtype.itemsize != 2 or samples.ndim not in (1, 2):
        raise ValueError("gain input must be mono or multichannel PCM16")
    if type(factor) not in (int, float) or not math.isfinite(factor) or not 0 < factor <= 1:
        raise ValueError("attenuation factor must be finite and in (0, 1]")
    return np.rint(samples.astype(np.float64) * factor).astype("<i2")


def episode_gain_seed(batch_seed: int, episode_id: str, case_id: str) -> int:
    raw = f"ambient-gain-v1\0{batch_seed}\0{episode_id}\0{case_id}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")
