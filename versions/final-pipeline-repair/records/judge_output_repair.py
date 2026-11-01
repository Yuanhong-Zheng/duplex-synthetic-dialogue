"""Repair malformed evaluator output; semantic rejections stay with the artifact owner."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from minimax_client import parse_json_text


class JudgeFormatExhausted(ValueError):
    pass


def complete_checked_judge(client,prompt,context,*,keys,validator,directory,max_tokens):
    directory=Path(directory);feedback=None
    for attempt in range(3):
        folder=directory/f'format-attempt-{attempt+1:02d}';folder.mkdir(parents=True,exist_ok=True)
        actual={**copy.deepcopy(context),**({'format_repair_feedback':feedback}if feedback else {})}
        actual_prompt=prompt+('\n[输出格式定向修复] 上次裁判输出未通过原 JSON/字段/引用/算术检查。仅修 format_repair_feedback 所列格式问题；原候选、冻结输入、审查规则和通过门不变。合法 repair/fail 决定应原样保留，不以通过为修复目标。'if feedback else '')
        identity={'model':getattr(client,'model',None),'provider':getattr(client,'provider',None),
                  'prompt':actual_prompt,'context':actual,'max_tokens':max_tokens,'response_format':{'type':'json_object'}}
        identity_path=folder/'identity.json';commit=folder/'commit.json'
        if identity_path.exists()and json.loads(identity_path.read_text())!=identity:
            raise ValueError('evaluator format-repair request changed on resume')
        identity_path.write_text(json.dumps(identity,ensure_ascii=False,indent=2)+'\n')
        if commit.exists():
            saved=json.loads(commit.read_text())
            if any(hashlib.sha256((folder/name).read_bytes()).hexdigest()!=value for name,value in saved.items()):
                raise ValueError('evaluator format-repair cache changed')
            raw=(folder/'raw_response.txt').read_text();meta={**json.loads((folder/'metadata.json').read_text()),'new_model_request':False,'reused_from':str(folder)}
        else:
            request_client=client
            if attempt:
                from record_model_call import RecordedCompletion
                if isinstance(client,RecordedCompletion):
                    request_client=RecordedCompletion(client.client,client.directory/f'format-request-{attempt+1:02d}')
            raw,meta=request_client.complete(actual_prompt,actual,max_tokens=max_tokens,response_format={'type':'json_object'})
            secret=getattr(client,'api_key','')
            (folder/'raw_response.txt').write_text(raw.replace(secret,'[REDACTED]')if secret else raw)
            (folder/'metadata.json').write_text(json.dumps({k:v for k,v in meta.items()if k.lower()not in {'api_key','authorization','headers'}},ensure_ascii=False,indent=2)+'\n')
            commit.write_text(json.dumps({name:hashlib.sha256((folder/name).read_bytes()).hexdigest()for name in ['identity.json','raw_response.txt','metadata.json']},indent=2)+'\n')
        try:
            value=parse_json_text(raw,keys);errors=validator(value)
        except ValueError as exc:
            errors=[str(exc)]
        (folder/'validation.json').write_text(json.dumps({'errors':errors,'original_rules_used':True},ensure_ascii=False,indent=2)+'\n')
        if not errors:return raw,{**meta,'judge_format_repairs':attempt}
        feedback={'errors':errors,'rejected_raw_response':raw}
    raise JudgeFormatExhausted('evaluator output remained malformed after two model corrections; original candidate and all reviews retained')
