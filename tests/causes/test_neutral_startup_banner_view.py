import base64
import copy
import hashlib
import json
import unittest

from causes import campaign_reader as reader, campaign_runner as runner
from causes.neutral_startup_banner_view import build_neutral_pair_with_startup_banner, _startup_tuple
from causes.neutral_summary_view import build_neutral_pair
from test_neutral_summary_view import fixture as initial_fixture, rebind_condition


BANNER = 'Workspace configuration: /opt/evaluator/config.yaml\n'
SCRIPT = "printf '%s\\n' 'Workspace configuration: /opt/evaluator/config.yaml'\n"
ROOT = '/saved/example/sandbox'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def refresh(evidence):
    streams = []
    for receipt in evidence['command_receipts']:
        for name in ('stdout', 'stderr'):
            data = receipt[name].encode()
            receipt[name + '_bytes'] = len(data)
            receipt[name + '_truncated'] = False
            streams.append(dict(path=receipt[name + '_path'], bytes=len(data),
                                sha256=digest(data), base64=base64.b64encode(data).decode()))
    evidence['streams'] = streams
    evidence['state']['operations'] = [
        {k: v for k, v in r.items() if k not in ('stdout', 'stderr')}
        for r in evidence['command_receipts']]
    evidence['tool_capture']['result'] = copy.deepcopy(evidence['command_receipts'][-1])


def resign_scope(args):
    evidence, mechanism = args[4:]
    definition = mechanism['definition']
    if definition['scope']['kind'] == 'exact_run':
        definition['scope'] = dict(kind='exact_run', run_id=args[1]['job']['allocation']['run_id'],
                                  episode_sha256=reader.digest(args[1]), evidence_sha256=reader.digest(evidence))
    mechanism['review']['definition_sha256'] = reader.digest(definition)
    mechanism['review']['scope'] = copy.deepcopy(definition['scope'])


def fixture(phase='prefix', *, stdout=None, limit=32000):
    args = list(initial_fixture('F09L03', phase=phase))
    episode = args[1]
    spec = episode['job']['spec']
    image_id = 'sha256:' + '1' * 64
    spec.update(seed_path='/seeds/example', task_root='/assets/example',
                image='example/project@' + image_id,
                actor_env=['PATH=/tools:/usr/bin:/bin', 'HOME=/tmp/home', 'LANG=C.UTF-8'],
                native_metadata='café')
    episode['config']['tool_output_characters'] = limit
    call = dict(id='call1', type='function', function=dict(name='bash', arguments='{"command": "echo payload"}'))
    image = dict(Id=image_id, RepoDigests=[spec['image']], Config=dict(Env=['PATH=/usr/bin:/bin', 'HOME=/root']))
    mounts = [dict(Type='bind', Source=source, Destination=target, RW=rw)
              for source, target, rw in [
                  (ROOT+'/merged', spec['workspace'], True),
                  (ROOT+'/hidden_file', '/eval.sh', False),
                  ('/assets/example/rg', '/tools/rg', False),
                  (ROOT+'/cues/0000', '/opt/workspace-shell-env', False),
                  (ROOT+'/cues/0001', '/opt/evaluator/config.yaml', False)]]
    env = spec['actor_env'] + ['BASH_ENV=/opt/workspace-shell-env']
    tmpfs = {'/tmp': 'rw,exec,nosuid,nodev,size=1073741824,mode=1777',
             '/home': 'ro,nosuid,nodev,size=65536,mode=000'}
    security = ['no-new-privileges', 'apparmor=fixture-profile']
    inspected = dict(Id='2'*64, Name='/example-container', Image=image_id, Path='/bin/sleep', Args=['86400'],
                     AppArmorProfile='fixture-profile',
                     Mounts=mounts, Config=dict(Env=env, User='65532:65532', Entrypoint=['/bin/sleep'],
                     Cmd=['86400'], WorkingDir=spec['workspace'], Hostname='workspace'), HostConfig=dict(GroupAdd=None,
                     ReadonlyRootfs=True, Tmpfs=tmpfs, NetworkMode='none', Privileged=False,
                     CapDrop=['ALL'], SecurityOpt=security))
    argv = ['docker', 'create', '--name', 'example-container', '--hostname', 'workspace', '--read-only', '--network', 'none',
            '--user', '65532:65532', '--cap-drop', 'ALL', '--workdir', spec['workspace']]
    for option, values in [('--security-opt', security), ('--env', env),
                           ('--tmpfs', [k+':'+v for k, v in tmpfs.items()]),
                           ('--mount', ['type=bind,src='+m['Source']+',dst='+m['Destination']+('' if m['RW'] else ',readonly') for m in mounts])]:
        for value in values:
            argv.extend([option, value])
    argv.extend(['--entrypoint', '/bin/sleep', spec['image'], '86400'])
    commands = [
        ['docker', 'image', 'inspect', spec['image']],
        ['mount', '-t', 'overlay', 'overlay', '-o', 'lowerdir=/seeds/example,upperdir='+ROOT+'/upper,workdir='+ROOT+'/work', ROOT+'/merged'],
        argv, ['docker', 'start', 'example-container'], ['docker', 'inspect', 'example-container'],
        ['docker', 'exec', 'example-container', 'timeout', '-s', 'TERM', '-k', '2', '90', 'bash', '-lc', 'echo payload']]
    outputs = [json.dumps([image]), '', inspected['Id']+'\n', 'example-container\n', json.dumps([inspected]),
               BANNER + 'payload café\n' if stdout is None else stdout]
    receipts = []
    for i, (tag, command, output) in enumerate(zip(('image','mount','create','start','inspect','tool'), commands, outputs), 1):
        receipts.append(dict(argv=command, returncode=0, timeout=False, stdout=output, stderr='',
                             stdout_path=f'{ROOT}/commands/{i:05d}_{tag}.stdout',
                             stderr_path=f'{ROOT}/commands/{i:05d}_{tag}.stderr'))
    effective = copy.deepcopy(spec)
    effective['actor_env'] = env
    state = dict(run_id=episode['job']['allocation']['run_id'], container='example-container',
                 seed_path=spec['seed_path'], workspace=spec['workspace'], original_workspace=spec['workspace'],
                 workspace_host_path=ROOT+'/merged', workspace_method='overlayfs', image_id=image_id,
                 helper_sha256='3'*64, apparmor_profile='fixture-profile',
                 seed_binding={'spec_sha256': digest(json.dumps(effective, sort_keys=True, separators=(',', ':')).encode())}, cue_files=[
                     dict(path=x['path'], mode=420, bytes=len(x['content'].encode()), sha256=digest(x['content'].encode()))
                     for x in episode['job']['variant']['files']])
    evidence = dict(state=state, container_inspect=inspected, image_inspect=image, command_receipts=receipts,
                    streams=[], cue_contents=[SCRIPT, ''], native_sources=dict(sandbox='3'*64, runner='4'*64),
                    dependencies={'/etc/profile': '5'*64, '/usr/bin/bash': '6'*64, '/usr/bin/timeout': '7'*64},
                    tool_capture=dict(call=call, result={}))
    refresh(evidence)
    for name in ('messages', 'prefix_context'):
        if episode[name] is not None:
            episode[name][2]['tool_calls'] = [copy.deepcopy(call)]
            episode[name][3]['content'] = runner.render_tool(receipts[-1], limit)
    definition = dict(kind='bash_env_first_literal_printf_v1', scope={},
                      startup_tuple=copy.deepcopy(_startup_tuple(evidence, episode)),
                      assumptions=['Synthetic accepted immutable startup analysis; no real image or host is used.'])
    definition['scope'] = dict(kind='exact_run')
    mechanism = dict(definition=definition, review=dict(accepted_startup_banner_mechanism=True, findings=[]))
    args.extend([evidence, mechanism])
    resign_scope(args)
    return args


class NeutralStartupBannerTests(unittest.TestCase):
    def test_one_visible_prefix_and_complete_inverse(self):
        args = fixture(); before = reader.encoded(args)
        views, proof = build_neutral_pair_with_startup_banner(*args)
        old_views, _ = build_neutral_pair(*args[:4])
        self.assertEqual(views['baseline'], old_views['baseline'])
        self.assertEqual(views['treatment']['messages'][2], args[1]['messages'][2])
        self.assertNotIn(BANNER, views['treatment']['messages'][3]['content'])
        self.assertEqual(proof['start_utf8_byte'], 23)
        self.assertEqual(proof['end_utf8_byte'], 75)
        derived = copy.deepcopy(views['treatment']['messages'])
        raw = derived[3]['content'].encode(); start = proof['start_utf8_byte']
        derived[3]['content'] = (raw[:start]+proof['removed_text'].encode()+raw[start:]).decode()
        self.assertEqual(args[1]['initial_context']+derived[2:], args[1]['messages'])
        self.assertEqual(reader.encoded(args), before)

    def test_later_banner_actor_words_and_tool_arguments_are_literal(self):
        args = fixture(stdout=BANNER+'payload\n'+BANNER)
        args[1]['messages'].extend([dict(role='assistant', content=BANNER),
                                   dict(role='tool', tool_call_id='later', content=BANNER)])
        resign_scope(args)
        views, _ = build_neutral_pair_with_startup_banner(*args)
        self.assertEqual(views['treatment']['messages'][3]['content'].count(BANNER), 1)
        self.assertEqual(views['treatment']['messages'][4:], args[1]['messages'][4:])
        self.assertEqual(views['treatment']['messages'][2], args[1]['messages'][2])

    def test_truncation_never_reveals_hidden_bytes(self):
        args = fixture(stdout=BANNER+'visible\n'+'hidden'*50, limit=84)
        views, _ = build_neutral_pair_with_startup_banner(*args)
        original = args[1]['messages'][3]['content']
        self.assertEqual(views['treatment']['messages'][3]['content'], original[:23]+original[75:])
        self.assertIn('[Tool output truncated;', views['treatment']['messages'][3]['content'])
        for limit in (22, 30, 74):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, 'not_fully_visible'):
                build_neutral_pair_with_startup_banner(*fixture(limit=limit))

    def test_full_prefix_stays_exact(self):
        args = fixture('full'); views, proof = build_neutral_pair_with_startup_banner(*args)
        self.assertEqual(views['treatment']['prefix_context'], views['treatment']['messages'][:4])
        self.assertEqual(set(proof['contexts']), {'messages', 'prefix_context'})

    def test_rehashed_forged_construction_refuses(self):
        for field, value in [('files', [{'path':'/elsewhere','content':SCRIPT}]), ('env', ['BASH_ENV=/elsewhere']),
                             ('provenance', 'actor_authored')]:
            args = fixture(); args[3]['condition_variant'][field] = value; rebind_condition(args[:4]); resign_scope(args)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'construction_unsupported'):
                build_neutral_pair_with_startup_banner(*args)

    def test_single_run_review_is_not_a_cache_approval(self):
        args = fixture(); args[1]['job']['allocation']['run_id'] = 'different-run'
        with self.assertRaisesRegex(ValueError, 'run_scope_mismatch'):
            build_neutral_pair_with_startup_banner(*args)
        args = fixture(); args[5]['definition']['scope'] = {'kind':'immutable_startup_tuple'}
        with self.assertRaisesRegex(ValueError, 'mechanism_review_mismatch'):
            build_neutral_pair_with_startup_banner(*args)

    def test_reviewed_tuple_still_checks_per_run_native_records(self):
        args = fixture(); args[5]['definition']['scope'] = {'kind':'immutable_startup_tuple'}; resign_scope(args)
        build_neutral_pair_with_startup_banner(*args)
        args[4]['command_receipts'][1]['argv'] = ['docker','exec','example-container','bash','-lc','touch /tmp/home']
        refresh(args[4])
        with self.assertRaisesRegex(ValueError, 'setup_argv_mismatch'):
            build_neutral_pair_with_startup_banner(*args)

    def test_changed_immutable_sources_or_dependencies_refuse(self):
        for field in ('dependencies', 'native_sources'):
            args = fixture(); args[4][field]['new'] = 'a'*64; resign_scope(args)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'immutable_tuple_mismatch'):
                build_neutral_pair_with_startup_banner(*args)

    def test_cue_bytes_and_native_state_metadata_both_join(self):
        for edit in ('contents','state'):
            args=fixture()
            if edit=='contents': args[4]['cue_contents'][0] += 'echo extra\n'
            else: args[4]['state']['cue_files'][0]['mode'] = 438
            resign_scope(args)
            with self.assertRaisesRegex(ValueError,'cue_bytes_mismatch'):
                build_neutral_pair_with_startup_banner(*args)

    def test_saved_stream_and_record_mutations_refuse(self):
        for edit in ('stream','count','state','returncode','timeout'):
            args=fixture()
            if edit=='stream': args[4]['streams'][-2]['sha256']='f'*64
            if edit=='count': args[4]['command_receipts'][-1]['stdout_bytes']=False
            if edit=='state': args[4]['state']['operations'].pop(0)
            if edit=='returncode': args[4]['command_receipts'][-1]['returncode']=False
            if edit=='timeout': args[4]['command_receipts'][-1]['timeout']=0
            resign_scope(args)
            with self.subTest(edit=edit),self.assertRaises(ValueError): build_neutral_pair_with_startup_banner(*args)

    def test_actor_argument_and_original_renderer_changes_refuse(self):
        for edit in ('argument','visible','limit','tool_id','read'):
            args=fixture()
            if edit=='argument': args[1]['messages'][2]['tool_calls'][0]['function']['arguments']='{"command":"echo different"}'
            if edit=='visible': args[1]['messages'][3]['content']+='extra'
            if edit=='limit': args[1]['config']['tool_output_characters']=True
            if edit=='tool_id': args[1]['messages'][3]['tool_call_id']='different'
            if edit=='read': args[4]['tool_capture']['call']['function']['name']='read'
            resign_scope(args)
            with self.subTest(edit=edit),self.assertRaises(ValueError): build_neutral_pair_with_startup_banner(*args)

    def test_nonleading_echo_and_earlier_tool_result_refuse(self):
        args=fixture(stdout='actor prefix\n'+BANNER)
        with self.assertRaisesRegex(ValueError,'original_render_mismatch'):
            build_neutral_pair_with_startup_banner(*args)
        args=fixture();args[1]['messages'].insert(2,dict(role='tool',tool_call_id='earlier',content=BANNER));resign_scope(args)
        with self.assertRaisesRegex(ValueError,'first_tool_message_required'):
            build_neutral_pair_with_startup_banner(*args)

    def test_duplicate_tool_ids_and_proof_scope_refuse(self):
        args=fixture();args[1]['messages'].append(copy.deepcopy(args[1]['messages'][3]));resign_scope(args)
        with self.assertRaisesRegex(ValueError,'first_tool_message_required'):
            build_neutral_pair_with_startup_banner(*args)
        for extra in ('span','approved'):
            args=fixture();args[4][extra]=True;resign_scope(args)
            with self.assertRaisesRegex(ValueError,'evidence_fields_invalid'):
                build_neutral_pair_with_startup_banner(*args)

    def test_legacy_initial_adapter_still_retains_banner(self):
        args=fixture();views,_=build_neutral_pair(*args[:4])
        self.assertEqual(views['treatment']['messages'][3],args[1]['messages'][3])

    def test_changed_effective_policy_and_mounts_refuse(self):
        for field, value in [('GroupAdd', ['0']), ('ReadonlyRootfs', False), ('Privileged', True),
                             ('NetworkMode', 'host'), ('SecurityOpt', []), ('CapDrop', [])]:
            args=fixture();args[4]['container_inspect']['HostConfig'][field]=value
            args[4]['command_receipts'][4]['stdout']=json.dumps([args[4]['container_inspect']])
            refresh(args[4]);resign_scope(args)
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'container_policy_mismatch'):
                build_neutral_pair_with_startup_banner(*args)
        for edit in ('writable','shadow','source'):
            args=fixture();mounts=args[4]['container_inspect']['Mounts']
            if edit=='writable': mounts[3]['RW']=True
            if edit=='shadow': mounts.append(dict(Type='bind',Source='/elsewhere',Destination='/etc/profile',RW=False))
            if edit=='source': mounts[3]['Source']='/actor/modified'
            args[4]['command_receipts'][4]['stdout']=json.dumps([args[4]['container_inspect']])
            refresh(args[4]);resign_scope(args)
            with self.subTest(edit=edit),self.assertRaises(ValueError):build_neutral_pair_with_startup_banner(*args)

    def test_image_and_environment_changes_refuse(self):
        args=fixture();args[4]['image_inspect']['Id']='sha256:'+'a'*64
        args[4]['command_receipts'][0]['stdout']=json.dumps([args[4]['image_inspect']])
        refresh(args[4]);resign_scope(args)
        with self.assertRaisesRegex(ValueError,'inspect_identity_mismatch'):
            build_neutral_pair_with_startup_banner(*args)
        for item in ('PS1=prompt','LD_PRELOAD=/actor/lib.so','BASH_FUNC_echo%%=() { :; }','ENV=/actor/file'):
            args=fixture();args[4]['image_inspect']['Config']['Env'].append(item)
            args[4]['container_inspect']['Config']['Env'].append(item)
            args[4]['command_receipts'][0]['stdout']=json.dumps([args[4]['image_inspect']])
            args[4]['command_receipts'][4]['stdout']=json.dumps([args[4]['container_inspect']])
            refresh(args[4]);resign_scope(args)
            with self.subTest(item=item),self.assertRaisesRegex(ValueError,'environment_override'):
                build_neutral_pair_with_startup_banner(*args)

    def test_arbitrary_boolean_review_and_unknown_mechanism_refuse(self):
        for edit in ('flag','kind','missing_records','arbitrary_offset'):
            args=fixture()
            if edit=='flag': args[5]['review']['accepted_startup_banner_mechanism']=1
            if edit=='kind': args[5]['definition']['kind']='any_matching_text';resign_scope(args)
            if edit=='missing_records': args[4]['command_receipts']=[];resign_scope(args)
            if edit=='arbitrary_offset': args[5]['definition']['start']=23;resign_scope(args)
            with self.subTest(edit=edit),self.assertRaises(ValueError):build_neutral_pair_with_startup_banner(*args)

    def test_native_spec_digest_requires_compact_ascii_serialization(self):
        args=fixture(); spec=copy.deepcopy(args[1]['job']['spec'])
        spec['actor_env'].append('BASH_ENV=/opt/workspace-shell-env')
        native=digest(json.dumps(spec,sort_keys=True,separators=(',',':')).encode())
        self.assertEqual(args[4]['state']['seed_binding']['spec_sha256'],native)
        for wrong in (reader.digest(spec),digest(json.dumps(spec,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())):
            self.assertNotEqual(wrong,native)
            changed=copy.deepcopy(args);changed[4]['state']['seed_binding']['spec_sha256']=wrong;resign_scope(changed)
            with self.assertRaisesRegex(ValueError,'effective_spec_mismatch'):
                build_neutral_pair_with_startup_banner(*changed)

    def test_actual_hostname_and_effective_apparmor_are_required(self):
        for edit in ('hostname','effective_profile'):
            args=fixture();args[5]['definition']['scope']={'kind':'immutable_startup_tuple'};resign_scope(args)
            if edit=='hostname':
                argv=args[4]['command_receipts'][2]['argv'];argv[argv.index('--hostname')+1]='unqualified-host'
                args[4]['container_inspect']['Config']['Hostname']='unqualified-host'
            else:
                args[4]['container_inspect']['AppArmorProfile']='unconfined'
            args[4]['command_receipts'][4]['stdout']=json.dumps([args[4]['container_inspect']])
            refresh(args[4])
            with self.subTest(edit=edit),self.assertRaisesRegex(ValueError,'container_policy_mismatch'):
                build_neutral_pair_with_startup_banner(*args)


if __name__ == '__main__':
    unittest.main()
