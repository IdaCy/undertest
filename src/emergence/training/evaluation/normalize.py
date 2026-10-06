"""Lossless native token-channel boundary. Never mine reasoning for an answer."""
def need(value, message):
    if not value:
        raise ValueError(message)

EOS=248046
OPEN=248068
CLOSE=248069
STRUCTURAL={248045,EOS,OPEN,CLOSE}

def normalize(raw,decode):
    ids=raw['generated_token_ids']
    need(type(ids) is list and all(type(i) is int and i>=0 for i in ids),'exact emitted token IDs')
    text=decode(ids)
    need(type(text) is str,'exact decoded text')
    if 'raw_decoded_text' in raw:need(raw['raw_decoded_text']==text,'decoded text changed')
    terminal=bool(ids and ids[-1]==EOS)
    content=ids[:-1] if terminal else ids
    unsupported=(any(i in (EOS,248045,OPEN) for i in content) or content.count(CLOSE)>1)
    reason=None;final=None;status='ok'
    if unsupported:
        status='unsupported_channel_layout'
    elif CLOSE in content:
        k=content.index(CLOSE);reason=decode(content[:k]);final=decode(content[k+1:])
        need(text==reason+'</think>'+final+('<|im_end|>' if terminal else ''),'channel decoding is not exact reconstruction')
    else:
        reason=decode(content)
        need(text==reason+('<|im_end|>' if terminal else ''),'reasoning decoding is not exact reconstruction')
        status='final_channel_not_emitted'
    finish=raw['finish_reason'];transport=raw['transport_status']
    need(transport in ('ok','request_error'),'explicit transport status')
    if transport=='ok':need((finish=='eos' and terminal) or (finish=='length' and not terminal),'finish/token consistency')
    return {'reasoning_content':reason,'final_content':final,'raw_decoded_text':text,
      'reasoning_exposure':'exposed' if reason is not None else 'unknown',
      'normalization_status':status,'transport_status':transport,'finish_reason':finish,
      'truncated':finish=='length' if finish in ('eos','length') else None,
      'channels_complete_as_emitted':not unsupported,'emitted_token_count':len(ids),
      'terminal_eos_included':terminal,'no_final_recovery_from_reasoning':True}
