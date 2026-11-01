"""Validate T02 provider boundaries and request bounded, identical-parameter TTS repairs."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from duplex_runtime.speech import SpeechError, decode_wav, trim_edges
from duplex_runtime.user_simulator import write_json

METHOD = 't02_existing_trim_and_provider_word_boundaries_v1'
MAX_REGENERATIONS = 2


class TTSAlignmentError(SpeechError):
    pass


def check_t02_segment(raw, directory, text, *, continuation=False):
    from duplex_runtime.alignment import word_spans
    pcm=decode_wav(raw); kept,trim=trim_edges(pcm,16000)
    spans=word_spans(text,json.loads((Path(directory)/'subtitle.json').read_text()),len(pcm))
    # The second segment follows a strictly positive inserted pause. Its first
    # word may end at local frame zero, exactly as in existing rendered_spans.
    previous=-1 if continuation else 0
    for span in spans:
        end=min(len(kept),max(0,span.end_frame-trim['kept_start_frame']))
        if end<=previous:
            raise TTSAlignmentError('edge trim destroyed a timestamp boundary; inspect this sample')
        previous=end


def synthesize_checked(client,text,output,*,continuation=False):
    output=Path(output)
    key=METHOD+(':continuation' if continuation else ':initial')
    def validator(raw,directory): check_t02_segment(raw,directory,text,continuation=continuation)
    validated=getattr(client,'synthesize_validated',None)
    if validated:
        return validated(text,output,validator=validator,validation_key=key,max_regenerations=MAX_REGENERATIONS)
    for number in range(MAX_REGENERATIONS+1):
        folder=output/'validation-attempts'/f'{number:03d}';folder.mkdir(parents=True)
        raw=client.synthesize(text,folder)
        try:
            validator(raw,folder)
        except TTSAlignmentError as exc:
            write_json(folder/'alignment-validation.json',{'status':'failed','reason':str(exc),'method':key})
            if number==MAX_REGENERATIONS:raise TTSAlignmentError('T02 alignment repair exhausted two identical-parameter regenerations')from exc
            continue
        write_json(folder/'alignment-validation.json',{'status':'passed','method':key})
        for p in folder.iterdir():
            if p.is_file():shutil.copyfile(p,output/p.name)
        write_json(output/'tts-quality.json',{'method':key,'regenerations':number,'max_regenerations':MAX_REGENERATIONS})
        return raw


def verify_quality_history(journal):
    from duplex_runtime.speech import digest
    rejected=0
    for path in Path(journal).glob('*/*/tts-quality-policy.json'):
        policy=json.loads(path.read_text());entry=path.parent
        if policy['max_regenerations']!=MAX_REGENERATIONS or policy['identity_sha256']!=digest(entry/'identity.json'):
            raise ValueError('TTS quality policy or original request identity changed')
        if policy['validation_key'] not in {METHOD+':initial',METHOD+':continuation'}:
            raise ValueError('unknown TTS validation method')
        trials=list((entry/'tries').glob('*'))
        if len(trials)-policy['baseline_trial_count']>MAX_REGENERATIONS:
            raise ValueError('TTS validation exceeded its authorized regeneration budget')
        identity=json.loads((entry/'identity.json').read_text());text=identity['request']['input']['text']
        for folder in sorted((entry/'tts-validation').glob('revision-*')):
            commit=json.loads((folder/'commit-before.json').read_text())
            if not all(digest(entry/name)==value for name,value in commit['files'].items()):
                raise ValueError('rejected original TTS call was modified')
            record=json.loads((folder/'rejection.json').read_text())
            if record['identity_sha256']!=policy['identity_sha256'] or record['validation_key']!=policy['validation_key']:
                raise ValueError('TTS rejection changed its request or validation context')
            source=entry/commit['result_directory']
            try:
                check_t02_segment((source/'audio.wav').read_bytes(),source,text,
                                  continuation=policy['validation_key'].endswith(':continuation'))
            except TTSAlignmentError:
                rejected+=1
            else:raise ValueError('a compatible TTS clip was unnecessarily regenerated')
    return rejected
