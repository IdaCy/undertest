"""Teacher-bank contract: pure, finite scalar checks; no source/model access."""
import hashlib
import json
import re
from emergence.training.math_reward import parse_scalar_answer, _extract_answer, COMPLETE_FINISH_REASONS, MAX_RESPONSE_CHARS
from emergence.training.rewards.audit_contract import overlap_fingerprints
from emergence.training.rewards.terminal_legacy import extract_answer

REPO = 'open-r1/OpenR1-Math-220k'
REVISION = 'e4e141ec9dea9f8326f4d347be56105859b2bd68'
SEED = 'dtrai-openr1-teacher-bank-v1-20261001'
SCORER = 'exact-scalar-v1+latex-token-representation-v1+balanced-terminal-math-v1'
BANK_POLICY = 'teacher_structure_complete_nullable_finish_v1'
SOURCE_COLUMNS = ('problem','answer','solution','source','uuid','problem_type','question_type',
                  'generations','correctness_math_verify','correctness_llama',
                  'is_reasoning_complete','finish_reasons','correctness_count','messages')
ALIGNED_COLUMNS = ('correctness_math_verify','correctness_llama','is_reasoning_complete','finish_reasons')


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def rank(purpose,value):
    return sha(canonical([SEED,purpose,value]))


def scalar(text):
    """Exact original parser, then only whole-scalar representation changes.

    No expression evaluation, rounding, unit removal, answer selection or text
    search. LaTeX unbraced fraction operands are each exactly one digit token.
    """
    result=parse_scalar_answer(text).to_dict()
    steps=[];normalized=text
    if result['status']=='valid' or not isinstance(text,str) or len(text)>2048:
        return {'raw':text,'parser':result,'normalized_input':normalized,'steps':steps}
    normalized=text.strip()
    for left,right in [('$$','$$'),('$','$'),(r'\(',r'\)'),(r'\[',r'\]')]:
        if normalized.startswith(left) and normalized.endswith(right) and len(normalized)>len(left)+len(right):
            normalized=normalized[len(left):-len(right)].strip();steps.append('whole_math_wrapper_removed');break
    # Only an entire formatting wrapper; no trailing material is discarded.
    m=re.fullmatch(r'\\(?:boxed|text|mathrm)\{([^{}]*)\}',normalized)
    if m:
        normalized=m[1].strip();steps.append('whole_scalar_format_wrapper_removed')
    if '\u2212' in normalized:
        normalized=normalized.replace('\u2212','-');steps.append('unicode_minus_to_ascii')
    changed=re.sub(r'^([+-])\s+(?=[0-9]|\\(?:dfrac|tfrac|frac))',r'\1',normalized)
    if changed!=normalized:normalized=changed;steps.append('leading_sign_whitespace_removed')
    # A single TeX token, or already braced signed integer, is supported.
    m=re.fullmatch(r'([+-]?)\\(?:dfrac|tfrac|frac)\s*(\{\s*[+-]?[0-9]+\s*\}|[0-9])\s*(\{\s*[+-]?[0-9]+\s*\}|[0-9])',normalized)
    if m:
        num=m[2].strip('{} ').strip();den=m[3].strip('{} ').strip()
        converted=m[1]+r'\frac'+'{'+num+'}{'+den+'}'
        if converted!=normalized:normalized=converted;steps.append('latex_single_token_fraction_braced')
    result=parse_scalar_answer(normalized).to_dict()
    return {'raw':text,'parser':result,'normalized_input':normalized,'steps':steps}


def channels(raw):
    if not isinstance(raw,str):return {'status':'generation_not_string'}
    if len(raw)>MAX_RESPONSE_CHARS:return {'status':'generation_character_cap'}
    if raw.count('</think>')!=1:return {'status':'missing_or_multiple_think_close'}
    opening='';body=raw
    if '<think>' in body:
        if body.count('<think>')!=1 or not body.lstrip().startswith('<think>'):
            return {'status':'ambiguous_think_open'}
        end=body.index('<think>')+len('<think>');opening=body[:end];body=body[end:]
    reasoning,final=body.split('</think>',1)
    trailer=''
    if final.endswith('<｜end▁of▁sentence｜>'):
        trailer='<｜end▁of▁sentence｜>';final=final[:-len(trailer)]
    if not reasoning.strip() or not final.strip():return {'status':'empty_reasoning_or_final'}
    reconstructed=opening+reasoning+'</think>'+final+trailer
    if reconstructed!=raw:raise AssertionError('nonreversible_channel_split')
    return {'status':'ok','opening_prefix':opening,'reasoning':reasoning,'separator':'</think>',
            'final':final,'trailer':trailer,'reconstructed_sha256':sha(reconstructed.encode()),
            'observed_generated_eos_claim':False}


def prompt_risks(problem,question_type=''):
    if not isinstance(problem,str) or not problem.strip():return ['invalid_problem']
    reasons=[]
    if len(problem)>16384:reasons.append('problem_character_cap')
    low=problem.lower()
    patterns={
      'missing_visual_context':r'\bdiagram\b|\bfigure\b|\bgraph shown\b|shown (?:above|below)|following (?:table|graph|image)|<img|!\[|\\includegraphics|https?://',
      'proof_task':r'\bprove\b|\bshow that\b|\bjustify\b',
      'multiple_requested_outputs':r'find both|find all|all possible values|ordered pair|\(a\).*\(b\)|\(i\).*\(ii\)|\(1\).*\(2\)',
      'repeated_requested_outputs':r'\bhow many\b.*\bhow many\b|\bwhat is\b.*\bwhat is\b|\bwhat are\b.*\bwhat are\b|\bfind\b.*\band\b.*\bfind\b|\bcalculate\b.*\band\b.*\bcalculate\b',
      'choice_task':r'\(A\).*\(B\)|(?:^|\n)\s*A[.)].*(?:\n)\s*B[.)]|which (?:of the following|option)|multiple.choice',
      'answer_or_solution_hint':r'(?:correct|final) answer\s*[:=]|\bsolution\s*:|\bhint\s*:',
    }
    for name,pattern in patterns.items():
        if re.search(pattern,problem,re.IGNORECASE|re.DOTALL):reasons.append(name)
    if isinstance(question_type,str) and re.search(r'proof|multiple.choice',question_type,re.IGNORECASE):
        reasons.append('source_question_type_risk')
    return reasons


def row_shape(row):
    generations=row.get('generations')
    if not isinstance(generations,list) or not 1<=len(generations)<=8:
        return ['generation_list_shape']
    errors=[]
    for name in ALIGNED_COLUMNS:
        values=row.get(name)
        if values is None and name!='is_reasoning_complete':continue
        if not isinstance(values,list) or len(values)!=len(generations):errors.append(name+'_alignment')
        elif name in ('correctness_math_verify','correctness_llama') and any(x is not None and type(x) is not bool for x in values):
            errors.append(name+'_types')
        elif name=='is_reasoning_complete' and any(type(x) is not bool for x in values):errors.append(name+'_types')
        elif name=='finish_reasons' and any(x is not None and not isinstance(x,str) for x in values):errors.append(name+'_types')
    return errors


def score(final,gold):
    """Pure final-channel scalar score; caller must separately enforce completion.

    This function does not claim a finish reason, generated EOS or reasoning
    completion. It accepts a raw gold string or this module's scalar() record.
    """
    gold=scalar(gold) if isinstance(gold,str) else gold
    result={'scorer':SCORER,'status':'unscorable','reward':None,'reason':None,
            'normalized_gold':gold['parser'].get('canonical')}
    if gold['parser']['status']!='valid':
        return result|{'reason':'gold_'+gold['parser']['reason']}
    if not isinstance(final,str):return result|{'status':'invalid','reward':0.0,'reason':'final_not_string'}
    if len(final)>MAX_RESPONSE_CHARS:return result|{'reason':'final_character_cap'}
    extraction=extract_answer(final)
    result['extraction']={'status':extraction.status,'reason':extraction.reason,'answer':extraction.answer}
    if extraction.status!='valid':
        invalid=extraction.status=='invalid'
        return result|{'status':'invalid' if invalid else 'unscorable','reward':0.0 if invalid else None,
                       'reason':'final_'+extraction.reason}
    parsed=scalar(extraction.answer);result['prediction_parse']=parsed
    if parsed['parser']['status']!='valid':
        invalid=parsed['parser']['status']=='invalid'
        return result|{'status':'invalid' if invalid else 'unscorable','reward':0.0 if invalid else None,
                       'reason':'prediction_'+parsed['parser']['reason']}
    prediction=parsed['parser']['canonical'];correct=prediction==gold['parser']['canonical']
    return result|{'status':'correct' if correct else 'incorrect','reward':1.0 if correct else 0.0,
                   'reason':'exact_match' if correct else 'exact_mismatch','normalized_prediction':prediction}


def score_generation(raw,gold,finish,reasoning_complete):
    ch=channels(raw)
    record={'scorer':SCORER,'status':'excluded','reason':None,'normalized_gold':gold['parser'].get('canonical'),
            'finish_reason':finish,'reasoning_complete':reasoning_complete,'channel_status':ch['status'],
            'supplied_label_used_for_reward':False,'bank_policy':BANK_POLICY,
            'completion_evidence':'textually_complete_teacher_record_finish_unknown' if finish is None else 'source_finish_flag_and_structure',
            'observed_decoder_completion':False}
    if gold['parser']['status']!='valid':record['reason']='gold_'+gold['parser']['reason']
    elif reasoning_complete is not True:record['reason']='source_reasoning_incomplete'
    elif finish is not None and finish not in COMPLETE_FINISH_REASONS:record['reason']='source_finish_incomplete_or_unknown'
    elif ch['status']!='ok':record['reason']='channel_'+ch['status']
    else:
        record.update(score(ch['final'],gold))
    return ch,record


def response_record(row,index,source_row_id,gold):
    raw=row['generations'][index]
    def optional(name):
        value=row.get(name)
        return None if value is None else value[index]
    finish=optional('finish_reasons');author_math=optional('correctness_math_verify');author_llama=optional('correctness_llama')
    ch,score=score_generation(raw,gold,finish,row['is_reasoning_complete'][index])
    digest=sha(raw.encode()) if isinstance(raw,str) else sha(canonical(raw))
    return {'response_id':sha(canonical([REVISION,source_row_id,index,digest])),
            'source_generation_index':index,'raw_sha256':digest,'raw_generation':raw,
            'reasoning':ch.get('reasoning'),'final':ch.get('final'),
            'channel_map':{k:v for k,v in ch.items() if k not in ('reasoning','final')},
            'score':score,'source_finish_reason':finish,
            'source_reasoning_complete':row['is_reasoning_complete'][index],
            'source_correctness_math_verify':author_math,'source_correctness_llama':author_llama,
            'label_disagreement':None if type(author_math) is not bool or score['status'] not in ('correct','incorrect') else author_math!=(score['status']=='correct'),
            'source_character_length':len(raw) if isinstance(raw,str) else None,
            'native_token_count':None,'native_context_fit':'not_measured'}
