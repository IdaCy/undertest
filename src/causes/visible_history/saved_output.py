"""Exact saved output extraction for the visible-history diagnostic."""
from signals.validation.recognition_measurement.categories import require as need

def exact_output_text(response, saved_answer):
    text = []
    for item in response.get('output', []):
        need(item.get('type') in ('reasoning', 'message'), 'unrequested_action_not_executed')
        if item.get('type') == 'message':
            for content in item.get('content', []):
                need(content.get('type') == 'output_text', 'nontext_response_retained')
                text.append(content['text'])
    need(len(text) == 1 and isinstance(text[0], str) and bool(text[0].strip()), 'missing_or_multiple_text_blocks')
    need(text[0].encode() == saved_answer, 'answer_exact_original_response_output_text')
