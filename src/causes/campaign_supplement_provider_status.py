"""Bounded, read-only provider status with independently verifiable raw bodies.

The caller pins this source, the native source, plan and runtime identity before
remote execution. This module opens no remote connection and grants no admission.
"""
import base64
import copy
import datetime
import decimal
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import zlib

SCHEMA = 'causes.supplement_burst_provider_status.v1'
MAX_HTTP = 4 * 1024**2
MAX_PACKET = 65536
MAX_PACKED_BODY = 16384
COUNTERS = {
    'running_requests': 'vllm:num_requests_running',
    'waiting_requests': 'vllm:num_requests_waiting',
    'preemptions': 'vllm:num_preemptions_total',
}
METRIC = re.compile(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*\})?\s+([^\s]+)(?:\s+\d+)?')


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read_boot_id():
    with Path('/proc/sys/kernel/random/boot_id').open('rb') as stream:
        raw = stream.read(129)
    require(len(raw) <= 128, 'boot_id_bound')
    return raw.decode().strip()


def load_native(raw, reference):
    """Load only source bytes matching the caller's independently pinned input."""
    require(type(raw) is bytes and len(raw) <= 1024**2, 'native_source_bound')
    require(type(reference.get('bytes')) is int and len(raw) == reference['bytes'] and
            hashlib.sha256(raw).hexdigest() == reference['sha256'], 'native_source_changed')
    namespace = {'__name__': '_saved_provider_status_native', '__file__': reference['path']}
    exec(compile(raw, reference['path'], 'exec'), namespace)
    require(namespace.get('MAX_HTTP') == MAX_HTTP and
            all(callable(namespace.get(k)) for k in ('identity', 'http')), 'native_interface')
    namespace['_verified_native_source'] = copy.deepcopy(reference)
    return namespace


def binding(plan, runtime_identity):
    require(runtime_identity['host'] == plan['execution_host'] and
            runtime_identity['process'] == plan['provider'], 'runtime_identity_changed')
    process = plan['provider']
    require(type(process['pid']) is int and process['pid'] > 0 and
            type(process['argv']) is list and
            all(type(x) is str for x in process['argv']), 'provider_identity_shape')
    argv = process['argv']
    require(argv.count('--max-num-seqs') == 1, 'provider_sequence_flag')
    index = argv.index('--max-num-seqs')
    require(index + 1 < len(argv) and re.fullmatch(r'[1-9][0-9]*', argv[index+1]),
            'provider_sequence_capacity')
    require(type(plan['boot_id']) is str and plan['boot_id'], 'provider_boot_binding')
    return int(argv[index+1])


def service_origin(plan):
    argv = plan['provider']['argv']
    require(argv.count('--host') == argv.count('--port') == 1, 'provider_address_flags')
    host = argv[argv.index('--host')+1]; port = argv[argv.index('--port')+1]
    require(host in ('127.0.0.1', 'localhost') and re.fullmatch(r'[0-9]+', port) and
            0 < int(port) <= 65535, 'provider_address')
    return 'http://'+host+':'+port


def pack_response(response):
    """Retain exact successful GET bodies; oversize evidence fails closed."""
    body = response['body']
    require(type(body['bytes']) is int and 0 <= body['bytes'] <= MAX_HTTP,
            'provider_http_body_bound')
    require(type(body['base64']) is str and len(body['base64']) <= 4*((MAX_HTTP+2)//3),
            'provider_http_base64_bound')
    raw = base64.b64decode(body['base64'], validate=True)
    require(len(raw) == body['bytes'] and hashlib.sha256(raw).hexdigest() == body['sha256'],
            'provider_http_body_changed')
    packed = gzip.compress(raw, mtime=0)
    saved = dict(url=response['url'], status=response['status'],
                 body=dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
    if len(packed) > MAX_PACKED_BODY:
        saved['body'].update(retention='digest_only_over_bound', packed_bytes=len(packed))
    else:
        saved['body'].update(encoding='gzip+base64', data=base64.b64encode(packed).decode())
    return saved


def unpack_response(saved, suffix, origin):
    require(saved['status'] == 200 and type(saved['status']) is int and
            saved['url'] == origin+suffix, 'provider_http_identity')
    body = saved['body']
    require(type(body['bytes']) is int and 0 <= body['bytes'] <= MAX_HTTP and
            body.get('encoding') == 'gzip+base64', 'provider_saved_body_bound')
    require(type(body['data']) is str and len(body['data']) <= 4*((MAX_PACKED_BODY+2)//3),
            'provider_packed_body_bound')
    packed = base64.b64decode(body['data'], validate=True)
    require(len(packed) <= MAX_PACKED_BODY, 'provider_packed_body_bound')
    decoder = zlib.decompressobj(31)
    raw = decoder.decompress(packed, body['bytes']+1)
    require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail and
            len(raw) == body['bytes'] and hashlib.sha256(raw).hexdigest() == body['sha256'],
            'provider_saved_body_changed')
    return raw


def parse_metrics(raw):
    rows = []
    for line in raw.decode().splitlines():
        if not line or line.startswith('#'):
            continue
        match = METRIC.fullmatch(line)
        require(match is not None, 'malformed_metric')
        number = float(match[3])
        rows.append(dict(name=match[1], labels=match[2] or '', text=match[3],
                         number=number))
    for name in ('vllm:num_requests_running', 'vllm:num_requests_waiting',
                 'vllm:kv_cache_usage_perc'):
        selected = [row for row in rows if row['name'] == name]
        require(selected and all(math.isfinite(row['number']) and row['number'] >= 0
                                for row in selected), 'required_metric_invalid:'+name)
        if name.endswith('kv_cache_usage_perc'):
            require(all(row['number'] <= 1 for row in selected), 'kv_fraction_out_of_range')
    counters = {}; labels = {}
    for key, name in COUNTERS.items():
        selected = [row for row in rows if row['name'] == name]
        require(len(selected) == 1, 'provider_counter_not_unique:'+name)
        row = selected[0]; value = decimal.Decimal(row['text'])
        require(value.is_finite() and 0 <= value <= 2**63-1 and
                value == value.to_integral_value(), 'provider_counter_not_integer:'+name)
        require(len(row['labels']) <= 1024, 'provider_counter_labels_bound')
        counters[key] = int(value); labels[key] = row['labels']
    return counters, labels


def validate_status(value, plan, runtime_identity):
    """Recompute successful status from retained HTTP bodies and exact bindings.

    The caller separately checks freshness, pressure, source pins and the actual
    observation envelope. This function never substitutes for those checks.
    """
    require(len(encoded(value)) <= MAX_PACKET, 'provider_status_packet_bound')
    require(value['schema'] == SCHEMA and value['healthy'] is True and
            type(value['model_calls']) is type(value['control_mutations']) is int and
            value['model_calls'] == value['control_mutations'] == 0,
            'provider_status_not_healthy')
    require(value['identity'] == runtime_identity and value['native_source'] == plan['native_source'],
            'provider_status_binding')
    require(value['process_before'] == value['process_after'] == plan['provider'] and
            value['boot_before'] == value['boot_after'] == plan['boot_id'], 'provider_changed')
    require(value['server_max_sequences'] == binding(plan, runtime_identity),
            'provider_capacity_changed')
    start = datetime.datetime.fromisoformat(value['observed_from'])
    end = datetime.datetime.fromisoformat(value['observed_until'])
    require(start.utcoffset() is not None and end.utcoffset() is not None and start <= end,
            'provider_status_clock')
    origin = service_origin(plan)
    models = json.loads(unpack_response(value['responses']['models'], '/v1/models', origin))
    require([row['id'] for row in models['data']] == [plan['model']], 'served_model_changed')
    counters, labels = parse_metrics(unpack_response(value['responses']['metrics'], '/metrics', origin))
    require(all(type(value.get(k)) is int and value[k] == v for k, v in counters.items()) and
            value['counter_labels'] == labels, 'provider_counter_changed')
    require(value['error'] is None, 'provider_error_present')
    return counters


def capture_status(native, plan, runtime_identity, *, clock=now, read_boot=read_boot_id):
    """One identity/GET/GET/identity observation; no retries or model requests.

    Exceptions produce a bounded unhealthy record with all already retained
    response bodies. An incompressible body retains its digest and explicit
    missing-evidence marker; such a record can never admit a scientific request.
    """
    require(len(encoded(runtime_identity)) <= 8192 and len(encoded(plan['native_source'])) <= 2048,
            'provider_binding_bound')
    value = dict(schema=SCHEMA, observed_from=clock(), observed_until=None, healthy=False,
                 identity=copy.deepcopy(runtime_identity), native_source=copy.deepcopy(plan['native_source']),
                 process_before=None, process_after=None, boot_before=None, boot_after=None,
                 responses={}, model_calls=0, control_mutations=0, error=None)
    phase = 'binding'
    try:
        require(native['MODEL'] == plan['model'] and native['MAX_HTTP'] == MAX_HTTP,
                'native_model_or_bound_changed')
        require(native['_verified_native_source'] == plan['native_source'], 'native_source_binding')
        value['server_max_sequences'] = binding(plan, runtime_identity)
        origin = service_origin(plan)
        phase = 'boot_before'; value['boot_before'] = read_boot()
        require(value['boot_before'] == plan['boot_id'], 'provider_boot_changed')
        phase = 'process_before'; value['process_before'] = native['identity'](plan['provider'])
        require(value['process_before'] == plan['provider'], 'provider_process_changed')
        for name, suffix in (('models', '/v1/models'), ('metrics', '/metrics')):
            phase = name
            value['responses'][name] = pack_response(native['http'](suffix))
            raw = unpack_response(value['responses'][name], suffix, origin)
            if name == 'models':
                require([row['id'] for row in json.loads(raw)['data']] == [plan['model']],
                        'served_model_changed')
            else:
                counters, labels = parse_metrics(raw)
                value.update(counters); value['counter_labels'] = labels
        phase = 'process_after'; value['process_after'] = native['identity'](plan['provider'])
        phase = 'boot_after'; value['boot_after'] = read_boot()
        value['observed_until'] = clock(); value['healthy'] = True
        phase = 'validation'; validate_status(value, plan, runtime_identity)
    except Exception as error:
        value['healthy'] = False
        value['observed_until'] = clock()
        value['error'] = dict(phase=phase, type=type(error).__name__, message=str(error)[:512])
    if len(encoded(value)) > MAX_PACKET:
        # The control attempt remains explicit even when evidence exceeds its cap.
        for response in value['responses'].values():
            body = response['body']
            body.pop('data', None); body.pop('encoding', None)
            body['retention'] = 'digest_only_packet_over_bound'
        value['healthy'] = False
        value['error'] = dict(phase='retention', type='ValueError', message='provider_status_packet_bound')
    require(len(encoded(value)) <= MAX_PACKET, 'provider_identity_exceeds_packet_bound')
    return value
