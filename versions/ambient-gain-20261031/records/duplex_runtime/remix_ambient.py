"""Make an explicitly labelled audio-only derivative of a preserved rollout."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import io
import json
from pathlib import Path
import wave

import numpy as np

from duplex_runtime.audio_gain import AmbientGainPolicy, ambient_gain_key, scale_pcm16
from duplex_runtime.speech import decode_wav, digest, encode_wav
from duplex_runtime.user_simulator import write_json
from duplex_runtime.verify_synthetic import read, require, verify


def stereo_pcm(path):
    with wave.open(str(path), "rb") as f:
        require((f.getframerate(), f.getnchannels(), f.getsampwidth(), f.getcomptype()) == (16000, 2, 2, "NONE"),
                "remix requires the original 16 kHz stereo PCM16 WAV")
        count = f.getnframes()
        samples = np.frombuffer(f.readframes(count), dtype="<i2").copy()
    require(len(samples) == count * 2, "source stereo audio is truncated")
    return samples.reshape(-1, 2)


def encode_stereo(samples):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as f:
        f.setnchannels(2); f.setsampwidth(2); f.setframerate(16000)
        f.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return buffer.getvalue()


def remix_pcm(source, placements, load_clip, case_id, policy):
    """Replace only disjoint user-aside samples; preserve every other sample."""
    output = source.copy()
    mask = np.zeros(len(source), dtype=bool)
    counts, clips = {}, []
    for placement in placements:
        if placement.get("kind") != "side_speech":
            continue
        require(placement["speaker"] == "user", "aside is not on the user channel")
        start, frames = placement["start_frame"], placement["frames"]
        end = start + frames
        require(0 <= start < end <= len(source) and not np.any(mask[start:end]), "aside bounds overlap or exceed the timeline")
        for other in placements:
            if other is placement or other["speaker"] != "user":
                continue
            require(other["start_frame"] + other["frames"] <= start or other["start_frame"] >= end,
                    "cannot attenuate an aside that overlaps another user clip")
        original = load_clip(placement)
        require(len(original) == frames and np.array_equal(source[start:end, 0], original),
                "aside source does not match its original mixed samples")
        turn = placement["focus_turn_id"]
        index = counts.get(turn, 0); counts[turn] = index + 1
        gain = policy.record(ambient_gain_key(case_id, turn, index))
        output[start:end, 0] = scale_pcm16(original, gain["factor"])
        mask[start:end] = True
        clips.append({"audio_id": placement["audio_id"], "focus_turn_id": turn, "start_frame": start,
                      "frames": frames, "source_path": placement["path"], "gain": gain})
    require(bool(clips), "source has no aside audio to attenuate")
    require(np.array_equal(output[:, 1], source[:, 1]) and np.array_equal(output[~mask, 0], source[~mask, 0]),
            "remix changed main speech, backchannels, or assistant audio")
    return output, clips


def verify_remix(directory):
    directory = Path(directory).resolve()
    recipe = read(directory/"mix.json"); source = Path(recipe["source_rollout"])
    require(recipe["model_calls"] == recipe["tts_calls"] == 0 and recipe["is_new_model_trajectory"] is False,
            "an audio-only derivative must not claim a new model trajectory")
    for name, expected in recipe["source_sha256"].items():
        require(digest(source/name) == expected, "original rollout changed: "+name)
    policy = AmbientGainPolicy(**recipe["ambient_gain_policy"])
    original = stereo_pcm(source/recipe["source_audio_path"])
    expected, clips = remix_pcm(original, read(source/"timeline.json")["placements"],
                                lambda p: decode_wav((source/p["path"]).read_bytes()), recipe["case_id"], policy)
    output = directory/recipe["audio"]["path"]
    require(digest(output) == recipe["audio"]["sha256"] and np.array_equal(stereo_pcm(output), expected),
            "remixed audio differs from its exact per-clip gain recipe")
    require(len(clips) == len(recipe["clips"]), "remix clip count differs")
    for expected_clip, recorded in zip(clips, recipe["clips"]):
        require(all(recorded.get(k) == v for k, v in expected_clip.items()), "remix clip provenance changed")
        pcm = expected[recorded["start_frame"]:recorded["start_frame"]+recorded["frames"], 0]
        require(digest(directory/recorded["path"]) == recorded["sha256"] and
                np.array_equal(decode_wav((directory/recorded["path"]).read_bytes()), pcm), "derived aside changed")
    return {"status": "passed", "aside_clips": len(clips), "frames": len(expected),
            "assistant_channel_unchanged": True, "main_user_and_backchannels_unchanged": True,
            "timing_and_word_alignment_unchanged": True, "source_rollout_unchanged": True,
            "audio_sha256": digest(output), "is_new_model_trajectory": False, "model_calls": 0, "tts_calls": 0}


def create_remix(source, output, policy=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    require(not output.exists() and not output.is_relative_to(source), "use a new derivative directory outside the original rollout")
    integrity = verify(source)
    result = read(source/"result.json")
    require(result["status"] == "completed", "remix requires a complete original trajectory")
    require(read(source/"manifest.json").get("ambient_gain_policy") is None,
            "source already has ambient gain; reuse its audio instead of attenuating it twice")
    audio = source/result["audio"]["path"]
    require(digest(audio) == result["audio"]["sha256"], "original stereo audio changed")
    policy = policy or AmbientGainPolicy()
    timeline = read(source/"timeline.json")
    mixed, clips = remix_pcm(stereo_pcm(audio), timeline["placements"],
                             lambda p: decode_wav((source/p["path"]).read_bytes()), result["case_id"], policy)
    output.mkdir(parents=True); (output/"clips").mkdir()
    (output/"dialogue-stereo.wav").write_bytes(encode_stereo(mixed))
    originals = {result["audio"]["path"], "timeline.json", "result.json", "manifest.json", "user-results.json"}
    for index, clip in enumerate(clips):
        path = output/"clips"/f"side-{index:02d}.wav"
        path.write_bytes(encode_wav(mixed[clip["start_frame"]:clip["start_frame"]+clip["frames"], 0]))
        clip.update(path=str(path.relative_to(output)), sha256=digest(path))
        originals.add(clip["source_path"])
    recipe = {"schema": "duplex-runtime.ambient-remix.v1", "case_id": result["case_id"],
              "source_rollout": str(source), "source_audio_path": result["audio"]["path"],
              "source_sha256": {name: digest(source/name) for name in sorted(originals)},
              "ambient_gain_policy": asdict(policy), "clips": clips,
              "audio": {**result["audio"], "path": "dialogue-stereo.wav", "sha256": digest(output/"dialogue-stereo.wav")},
              "is_new_model_trajectory": False, "model_calls": 0, "tts_calls": 0,
              "evaluation": "Original trajectory scores are retained; this is an audio-only derivative, not a new model evaluation."}
    write_json(output/"mix.json", recipe)
    write_json(output/"source-integrity.json", integrity)
    proof = verify_remix(output)
    write_json(output/"verification.json", proof)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("create"); make.add_argument("--source-rollout", type=Path, required=True)
    make.add_argument("--output-dir", type=Path, required=True)
    make.add_argument("--seed", type=int)
    check = sub.add_parser("verify"); check.add_argument("--mix-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "verify":
        proof = verify_remix(args.mix_dir)
    else:
        proof = create_remix(args.source_rollout, args.output_dir,
                             AmbientGainPolicy(seed=args.seed) if args.seed is not None else None)
    print(json.dumps(proof, ensure_ascii=False))


if __name__ == "__main__":
    main()
