"""Causal prefix release from provider word timestamps, never estimated reading speed."""
from __future__ import annotations

import json
import math
from pathlib import Path
import unicodedata

from duplex_runtime.playback import TextSpan
from duplex_runtime.speech import SpeechError, digest


SOURCE = "minimax_word_end_ms_ceiled_to_pcm_frame"


def grouped_words(segment):
    """A provider token can span several syllables of one exact source range.

    Release that range only at its last syllable's provider end. Partial
    overlaps, changed text and nonconsecutive reuse remain invalid.
    """
    words=segment.get('timestamped_words')
    if not isinstance(words,list) or not words:
        raise SpeechError('sentence subtitles cannot substitute for word alignment')
    result=[]
    for item in words:
        word=dict(item)
        tb,te=word.get('time_begin'),word.get('time_end')
        if not all(type(v)in(int,float) and math.isfinite(v) for v in (tb,te)) or not 0 <= tb <= te:
            raise SpeechError('invalid provider word time')
        if result and (word.get('word_begin'),word.get('word_end')) == (result[-1].get('word_begin'),result[-1].get('word_end')):
            previous=result[-1]
            tb,te=word.get('time_begin'),word.get('time_end')
            if (word.get('word')!=previous.get('word') or
                not all(type(v)in(int,float) and math.isfinite(v) for v in (previous.get('time_end'),tb,te)) or
                not previous['time_end'] <= tb <= te):
                raise SpeechError('invalid repeated source-range subtitles')
            previous['time_end']=te
            previous['_grouped_source_range']=True
        else:result.append(word)
    return result


def word_spans(text: str, subtitles: list, frames: int, sample_rate=16000) -> list[TextSpan]:
    """Return contiguous text prefixes, released at the provider's word END time.

    Span starts bound the previous prefix, not a phonetic word-start estimate.
    Punctuation may share an end time with its word and is merged conservatively.
    """
    if not isinstance(subtitles, list) or not subtitles:
        raise SpeechError("provider word subtitles are required")
    original_text = text
    provider_text = "".join(segment.get("text", "") for segment in subtitles)
    mapping = None
    if provider_text != text:
        # MiniMax offsets omit paragraph newlines. Translate ONLY whitespace loss,
        # never fuzzy-match names, punctuation, numbers or rewritten spoken text.
        leading = 0
        while leading < len(text) and (text[leading].isspace() or unicodedata.category(text[leading]).startswith("P")):
            leading += 1
        def whitespace_subsequence(start):
            positions=[];cursor=start
            for character in provider_text:
                while cursor<len(text) and text[cursor]!=character and text[cursor].isspace():cursor+=1
                if cursor>=len(text) or text[cursor]!=character:return None
                positions.append(cursor);cursor+=1
            return positions if cursor==len(text) or text[cursor:].isspace() else None
        indices=whitespace_subsequence(0)
        if indices is None:
            # At a T02 splice the provider may omit the nonspoken leading
            # punctuation, independently of which internal spaces it retains.
            indices=whitespace_subsequence(leading)
        if not indices:
            raise SpeechError("provider rewrote non-whitespace input; no fuzzy alignment")
        mapping = [0] + [i+1 for i in indices]
        mapping[-1] = len(text)
        text = provider_text
    # Some MiniMax responses strip a leading comma at sentence level while
    # keeping word offsets in the exact original input coordinate system.
    # Translate only when EVERY word matches one complete, exact basis.
    inverse_begin, inverse_end = {}, {}
    if mapping is not None:
        inverse_begin = {position: i for i, position in enumerate(indices)}
        inverse_end = {position + 1: i + 1 for i, position in enumerate(indices)}
    word_bases = []
    for segment in subtitles:
        a, b = segment.get("text_begin"), segment.get("text_end")
        if type(a) is not int or type(b) is not int or text[a:b] != segment.get("text"):
            raise SpeechError("subtitle text/offset differs from exact TTS input")
        words = grouped_words(segment)
        def exact_basis(omitted_prefix):
            expected = a
            for word in words:
                begin, end = word.get("word_begin"), word.get("word_end")
                if type(begin) is not int or type(end) is not int:
                    return False
                if omitted_prefix is not None:
                    begin += omitted_prefix; end += omitted_prefix
                    if begin not in inverse_begin or end not in inverse_end or original_text[begin:end] != word.get("word"):
                        return False
                    begin, end = inverse_begin[begin], inverse_end[end]
                if (not expected <= begin < end <= b or text[begin:end] != word.get("word") or
                    (begin != expected and not text[expected:begin].isspace())):
                    return False
                expected = end
            return expected == b or (expected < b and text[expected:b].isspace())
        if exact_basis(None):
            word_bases.append(("provider", 0))
            continue
        # Provider synthesis chunks sometimes omit preceding paragraph breaks
        # from their offset origin while retaining whitespace inside this chunk.
        # A chunk can omit only part of the preceding whitespace. Bound the
        # translation by actual omissions and require an exact whole sentence.
        offsets = range(len(original_text)-len(text)+1) if mapping is not None else []
        basis = next((offset for offset in offsets if exact_basis(offset)), None)
        if basis is None:
            raise SpeechError("word offsets match neither exact provider nor original input coordinates")
        word_bases.append(("original", basis))
    result, cursor, previous = [], 0, 0
    for segment_index, segment in enumerate(subtitles):
        word_basis, omitted_prefix = word_bases[segment_index]
        word_source = SOURCE + ((";original_input_word_coordinates" if omitted_prefix == 0 else ";exact_chunk_whitespace_basis="+str(omitted_prefix)) if word_basis == "original" else "")
        a, b = segment.get("text_begin"), segment.get("text_end")
        if type(a) is not int or type(b) is not int or text[a:b] != segment.get("text"):
            raise SpeechError("subtitle text/offset differs from exact TTS input")
        words = grouped_words(segment)
        for word in words:
            begin, end = word.get("word_begin"), word.get("word_end")
            if word_basis == "original":
                begin, end = inverse_begin[begin+omitted_prefix], inverse_end[end+omitted_prefix]
            if (type(begin) is not int or type(end) is not int or not cursor <= begin < end <= len(text) or
                text[begin:end] != word.get("word") or (begin != cursor and not text[cursor:begin].isspace())):
                raise SpeechError("noncontiguous or rewritten word subtitles")
            source=word_source
            if word.get('_grouped_source_range'):source+=';identical_source_range_last_provider_end'
            if begin!=cursor:source+=';exact_whitespace_gap'
            tb, te = word.get("time_begin"), word.get("time_end")
            if not all(type(x) in (int, float) and math.isfinite(x) for x in (tb, te)) or not 0 <= tb <= te:
                raise SpeechError("invalid provider word time")
            frame = math.ceil(te * sample_rate / 1000)
            if not previous <= frame <= frames:
                raise SpeechError("provider word end goes backward or beyond PCM")
            if frame == previous:
                if not result or any(c.isalnum() for c in text[begin:end]):
                    raise SpeechError("zero-duration spoken word cannot supply reliable causal alignment")
                old = result.pop()
                result.append(TextSpan(old.start_char, end, old.start_frame, frame, source))
            else:
                result.append(TextSpan(cursor, end, previous, frame, source))
            cursor, previous = end, frame
        if cursor < b and text[cursor:b].isspace():
            old=result.pop();result.append(TextSpan(old.start_char,b,old.start_frame,old.end_frame,old.source+';exact_trailing_whitespace'))
            cursor=b
    if cursor != len(text):
        raise SpeechError("word subtitles do not cover complete input")
    if mapping is not None:
        result = [TextSpan(mapping[s.start_char], mapping[s.end_char], s.start_frame, s.end_frame,
                           SOURCE+";exact_whitespace_and_leading_punctuation_offset_translation"+(s.source[len(SOURCE):] if s.source != SOURCE else "")) for s in result]
        if result[-1].end_char != len(original_text):
            raise SpeechError("whitespace offset translation is incomplete")
    return result


def rendered_spans(directory: Path, rendered: dict) -> tuple[list[TextSpan], list[dict]]:
    """Transform original per-segment completion times through declared T02 trims."""
    if rendered.get("side_speech_insertions"):
        original = rendered["uninterrupted_source"]
        spans, provenance = rendered_spans(directory, original)
        insertions = rendered["side_speech_insertions"]
        mapped = []
        for old in spans:
            end = old.end_frame + sum(i["frames"] for i in insertions if i["source_frame"] < old.end_frame)
            mapped.append(TextSpan(old.start_char, old.end_char, mapped[-1].end_frame if mapped else 0,
                                   end, old.source + ";declared_side_speech_pause"))
        if original["frames"] + sum(i["frames"] for i in insertions) != rendered["frames"]:
            raise SpeechError("side-speech insertion frame count differs")
        return mapped, provenance + [{"side_speech_insertions": insertions,
                                     "uninterrupted_source_sha256": original["sha256"]}]
    spans, provenance = [], []
    char_offset, frame_offset = 0, 0
    for number, segment in enumerate(rendered["segments"]):
        if digest(directory / segment["path"]) != segment["sha256"]:
            raise SpeechError("aligned TTS source changed")
        path = directory / segment["call"] / "subtitle.json"
        raw_spans = word_spans(segment["request"]["input"]["text"], json.loads(path.read_text()), segment["frames"])
        trim = segment.get("trim")
        left = trim["kept_start_frame"] if trim else 0
        right = trim["kept_end_frame"] if trim else segment["frames"]
        for old in raw_spans:
            end = frame_offset + min(right-left, max(0, old.end_frame-left))
            start = spans[-1].end_frame if spans else 0
            if end <= start:
                raise SpeechError("edge trim destroyed a timestamp boundary; inspect this sample")
            spans.append(TextSpan(char_offset+old.start_char, char_offset+old.end_char, start, end, SOURCE+";declared_edge_trim"))
        provenance.append({"subtitle_path": str(path.relative_to(directory)), "sha256": digest(path),
                           "raw_frames": segment["frames"], "trim": trim,
                           "output_frame_offset": frame_offset, "output_char_offset": char_offset})
        char_offset += len(segment["request"]["input"]["text"])
        frame_offset += right-left
        if number == 0 and rendered.get("pause"):
            frame_offset += rendered["pause"]["frames"]
    if char_offset != len(rendered["text"]) or frame_offset != rendered["frames"]:
        raise SpeechError("aligned source composition differs from rendered PCM/text")
    return spans, provenance
