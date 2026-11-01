"""Private, deterministic execution of the frozen side-speech speaker/placement.

The incoming channel is the user's microphone, including nearby people/devices.
Speaker roles here never become privileged labels in the assistant's context.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import numpy as np

from duplex_runtime.speech import SpeechError, SpeechSettings, decode_wav, digest, encode_wav

METHOD = "provider_word_boundary_asides_v1"
PADDING_FRAMES = 1280  # 80 ms either side of the same user's turned-away aside.


def side_audio_id(turn_id, index=0):
    # Keep the original first-aside identifier, while supporting two in one turn.
    key = turn_id if index == 0 else f"{turn_id}:{index}"
    return "input-" + hashlib.sha256(key.encode()).hexdigest()[:12]


def side_voice_ids(user, assistant, third_party=None, machine=None):
    used = {user, assistant}
    pool = ["male-qn-jingying", "female-tianmei", "female-yujie", "male-qn-badao"]
    result = {}
    for role, configured in (("third_party", third_party), ("machine", machine)):
        voice = configured or next(v for v in pool if v not in used)
        SpeechSettings(voice_id=voice)
        if voice in used:
            raise SpeechError("side-speech roles require distinct pinned voices")
        result[role] = voice
        used.add(voice)
    return result


def plan_during_user(main, asides, spans):
    """Return a plan in original-main coordinates, without changing any speech."""
    selected = [(i, a) for i, a in enumerate(asides) if a.get("placement") == "during_user"]
    if not selected:
        return {"insertions": [], "schedules": []}
    pause = main.get("pause")
    boundaries = [s for s in spans if s.end_char < len(main["text"]) and 0 < s.end_frame < main["frames"]
                  and not (pause and pause["start_frame"] <= s.end_frame <= pause["end_frame"])]
    if not boundaries:
        raise SpeechError("during_user aside requires an internal provider word boundary")
    points = []
    for ordinal, (index, aside) in enumerate(selected):
        desired = len(main["text"]) * (ordinal + 1) / (len(selected) + 1)
        boundary = min(boundaries, key=lambda s: (abs(s.end_char - desired), s.end_frame))
        points.append((boundary.end_frame, index, aside, boundary.end_char))
    insertions, schedules, shift = [], [], 0
    for source_frame, index, aside, end_char in sorted(points, key=lambda p: (p[0], p[1])):
        start = source_frame + shift
        if aside["speaker"] == "user":
            count = aside["frames"] + 2 * PADDING_FRAMES
            insertions.append({"source_frame": source_frame, "start_frame": start, "frames": count,
                               "aside_index": index, "boundary_end_char": end_char})
            offset = start + PADDING_FRAMES
            shift += count
        else:
            offset = start
        schedules.append({"aside_index": index, "start_frame": offset, "method": METHOD,
                          "placement": "during_user", "boundary_end_char": end_char})
    return {"insertions": insertions, "schedules": schedules}


def insert_silence(pcm, insertions):
    parts, cursor = [], 0
    for insertion in insertions:
        position = insertion["source_frame"]
        if not cursor <= position <= len(pcm):
            raise SpeechError("invalid declared side-speech splice")
        parts.extend((pcm[cursor:position], np.zeros(insertion["frames"], dtype="<i2")))
        cursor = position
    parts.append(pcm[cursor:])
    return np.concatenate(parts)


def attach_during_user(main, asides, directory: Path, folder: Path):
    if not any(a.get("placement") == "during_user" for a in asides):
        return main, asides
    from duplex_runtime.alignment import rendered_spans
    spans, _ = rendered_spans(directory, main)
    plan = plan_during_user(main, asides, spans)
    asides = copy.deepcopy(asides)
    for schedule in plan["schedules"]:
        asides[schedule["aside_index"]]["schedule"] = schedule
    if not plan["insertions"]:
        return main, asides
    original = copy.deepcopy(main)
    pcm = decode_wav((directory / main["path"]).read_bytes())
    joined = insert_silence(pcm, plan["insertions"])
    path = folder / "main-with-side-pauses.wav"
    path.write_bytes(encode_wav(joined))
    main = {**copy.deepcopy(main), "path": str(path.relative_to(directory)), "sha256": digest(path),
            "frames": len(joined), "duration_seconds": len(joined) / 16000,
            "uninterrupted_source": original, "side_speech_insertions": plan["insertions"]}
    if main.get("pause"):
        pause = main["pause"]
        shift = sum(i["frames"] for i in plan["insertions"] if i["source_frame"] <= pause["start_frame"])
        pause["start_frame"] += shift
        pause["end_frame"] += shift
        pause["start_seconds"] = pause["start_frame"] / 16000
    return main, asides


def verify_during_user(main, asides, directory):
    if not main.get("side_speech_insertions") and not any(a.get("placement") == "during_user" for a in asides):
        return {"insertions": [], "schedules": []}
    from duplex_runtime.alignment import rendered_spans
    original = main.get("uninterrupted_source", main)
    spans, _ = rendered_spans(directory, original)
    plan = plan_during_user(original, asides, spans)
    for schedule in plan["schedules"]:
        if asides[schedule["aside_index"]].get("schedule") != schedule:
            raise SpeechError("during_user scheduling differs from provider word boundaries")
    if main.get("side_speech_insertions", []) != plan["insertions"]:
        raise SpeechError("same-user aside pause differs from the declared plan")
    if plan["insertions"]:
        source = directory / original["path"]
        target = directory / main["path"]
        if digest(source) != original["sha256"] or digest(target) != main["sha256"]:
            raise SpeechError("same-user aside main audio changed")
        expected = insert_silence(decode_wav(source.read_bytes()), plan["insertions"])
        if len(expected) != main["frames"] or not np.array_equal(expected, decode_wav(target.read_bytes())):
            raise SpeechError("same-user aside changed main speech instead of inserting a pause")
    return plan
