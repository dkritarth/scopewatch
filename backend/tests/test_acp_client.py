"""ACP v1 client tests. All peers and gateway data are synthetic, with no LLM."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import httpx
import pytest

from scopewatch.acp_client import AcpSession, AcpClientError, WorkspaceMapping, GatewayClient
from scopewatch.acp_adapter import AcpClientAdapter, HoldConfig


def test_absolute_mapping_and_aliases(tmp_path: Path) -> None:
    root = tmp_path / 'run'
    root.mkdir()
    (root / 'outputs').mkdir()
    (root / 'private').mkdir()
    (root / 'alias').symlink_to(root / 'private', target_is_directory=True)
    (root / 'escape').symlink_to(tmp_path, target_is_directory=True)
    mapping = WorkspaceMapping(root)
    assert mapping.resource(str(root / 'outputs' / 'new.txt')) == 'outputs/new.txt'
    assert mapping.resource(str(root / 'alias' / 'data.txt')) == 'alias/data.txt'
    for path in ['outputs/new.txt', str(tmp_path / 'other'), str(root / 'escape' / 'data'), str(root) + '/../other', '//tmp/run/file', str(root) + '/outputs/./data', str(root) + '/outputs//data']:
        with pytest.raises(AcpClientError):
            mapping.resource(path)


def peer(tmp_path: Path, script: str) -> list[str]:
    path = tmp_path / 'peer.py'
    path.write_text('import json, sys, time, os\n'
                    'def recv(): return json.loads(sys.stdin.readline())\n'
                    'def send(v): print(json.dumps(v), flush=True)\n'
                    'def reply(q,r): send({"jsonrpc":"2.0","id":q["id"],"result":r})\n' + script)
    return [sys.executable, '-u', str(path)]


HANDSHAKE = '''q=recv()
assert q['method']=='initialize'
assert type(q['params']['protocolVersion']) is int and q['params']['protocolVersion']==1
assert q['params']['clientCapabilities']=={'fs':{'readTextFile':True,'writeTextFile':True},'terminal':False}
assert q['params']['clientInfo']['name']=='scopewatch'
reply(q, {'protocolVersion':1,'agentCapabilities':{},'agentInfo':{'name':'synthetic-peer','version':'1'}})
q=recv()
assert q['method']=='session/new' and q['params']['mcpServers']==[] and q['params']['cwd'].startswith('/')
root=q['params']['cwd']
reply(q, {'sessionId':'synthetic-session'})
q=recv()
assert q['method']=='session/prompt' and q['params']['prompt']==[{'type':'text','text':'synthetic task'}]
prompt=q
'''


def receipt(content: str = 'synthetic file', request: httpx.Request | None = None) -> dict:
    action = json.loads(request.content) if request is not None else {'operation':'read_text','resource':'input.txt'}
    action.update(id='action-1', run_id='run-1')
    return {'action_request': action,
            'policy_decision': {'outcome':'ALLOW','action_request_id':'action-1'},
            'execution_receipt': {'status':'EXECUTED','action_request_id':'action-1',
                'operation':action['operation'],'resource':action['resource'],
                'sanitized_result':{'content':content}}}


def test_stdio_peer_files_notifications_permissions_and_terminal_refusal(tmp_path: Path) -> None:
    sent = []
    def gateway(request):
        sent.append(json.loads(request.content))
        return httpx.Response(201, json=receipt(request=request))
    script = HANDSHAKE + '''send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'synthetic-session','update':{'sessionUpdate':'agent_thought_chunk','content':{'type':'text','text':'Synthetic plan'}}}})
send({'jsonrpc':'2.0','id':40,'method':'fs/read_text_file','params':{'sessionId':'synthetic-session','path':root+'/input.txt'}})
r=recv(); assert r['id']==40 and r['result']=={'content':'synthetic file'}
send({'jsonrpc':'2.0','id':41,'method':'fs/write_text_file','params':{'sessionId':'synthetic-session','path':root+'/output.txt','content':'synthetic output'}})
r=recv(); assert r['id']==41 and r['result']=={}
send({'jsonrpc':'2.0','id':42,'method':'session/request_permission','params':{'sessionId':'synthetic-session','toolCall':{'toolCallId':'tool','title':'write','kind':'edit'},'options':[{'optionId':'yes','name':'Always','kind':'allow_always'}]}})
r=recv(); assert r['result']=={'outcome':{'outcome':'cancelled'}}
for i,method in enumerate(['terminal/create','terminal/output','terminal/wait_for_exit','terminal/kill','terminal/release','terminal/run','fs/delete_file','fs/list_directory']):
 send({'jsonrpc':'2.0','id':50+i,'method':method,'params':{'sessionId':'synthetic-session'}})
 r=recv(); assert r['error']['code']==-32601
reply(prompt, {'stopReason':'end_turn'})
time.sleep(10)
'''
    with GatewayClient(transport=httpx.MockTransport(gateway), base_url='http://127.0.0.1:1') as http:
        with AcpSession(peer(tmp_path, script), AcpClientAdapter(http, 'run-1'), WorkspaceMapping(tmp_path), timeout_s=3) as session:
            assert session.run('synthetic task')['stopReason'] == 'end_turn'
            process = session.transport.process
        assert process.poll() is not None
    assert [a['operation'] for a in sent] == ['read_text', 'write_text']
    assert sent[0]['resource']=='input.txt'
    assert all(a['reasoning_provenance']=='AGENT_AUTHORED_SUMMARY' for a in sent)
    assert all('exposed_reasoning_trace' not in a for a in sent)


@pytest.mark.parametrize('reply', [
    {'protocolVersion': '1'}, {'protocolVersion': True}, {'protocolVersion': 2},
    {'protocolVersion': 1, 'agentCapabilities': []},
    {'protocolVersion': 1, 'agentInfo': {'name': {}, 'version': '1'}},
])
def test_rejects_invalid_initialize_and_reaps_peer(tmp_path: Path, reply: dict) -> None:
    script = 'q=recv()\nreply(q, ' + repr(reply) + ')\ntime.sleep(10)\n'
    with GatewayClient(base_url='http://127.0.0.1:1') as http:
        session = AcpSession(peer(tmp_path, script), AcpClientAdapter(http, 'run-1'), WorkspaceMapping(tmp_path), timeout_s=2)
        with pytest.raises(AcpClientError):
            session.run('synthetic task')
        assert session.transport.process.poll() is not None
        assert session.state == 'closed'


@pytest.mark.parametrize('script,code', [
    ('time.sleep(10)', 'ACP_TIMEOUT'),
    ("os.write(1,b'no-json\\n');time.sleep(10)", 'ACP_MALFORMED_FRAME'),
    ("os.write(1,b'[]\\n');time.sleep(10)", 'ACP_MALFORMED_FRAME'),
    ("os.write(1,b'{\"jsonrpc\":\"2.0\",\"id\":1,\"id\":2}\\n');time.sleep(10)", 'ACP_MALFORMED_FRAME'),
    ("os.write(1,b'x'*300000);time.sleep(10)", 'ACP_FRAME_LIMIT'),
    ("os.write(1,b'\\xff\\n');time.sleep(10)", 'ACP_MALFORMED_FRAME'),
    ('sys.exit(0)', 'ACP_EOF'),
    ("send({'jsonrpc':'2.0','id':999,'result':{}});time.sleep(10)", 'ACP_INVALID_RESPONSE'),
    ("send({'jsonrpc':'2.0','id':1,'error':{'code':-1,'message':'PRIVATE synthetic provider body'}});time.sleep(10)", 'ACP_PEER_ERROR'),
])
def test_bad_peer_frames_are_bounded_and_sanitized(tmp_path: Path, script: str, code: str) -> None:
    with GatewayClient(base_url='http://127.0.0.1:1') as http:
        session = AcpSession(peer(tmp_path, script), AcpClientAdapter(http, 'run-1'), WorkspaceMapping(tmp_path), timeout_s=.3)
        with pytest.raises(AcpClientError, match=code) as caught:
            session.run('synthetic task')
        assert 'PRIVATE' not in str(caught.value)
        assert session.transport.process.poll() is not None


def test_wrong_session_paths_and_unadvertised_methods_have_no_effect(tmp_path: Path) -> None:
    sent = []
    def gateway(request):
        sent.append(request)
        return httpx.Response(500)
    script = HANDSHAKE + '''for i,params in enumerate([
 {'sessionId':'wrong-session','path':root+'/new.txt','content':'unauthorized'},
 {'sessionId':'synthetic-session','path':'relative.txt','content':'unauthorized'},
 {'sessionId':'synthetic-session','path':'/outside-synthetic/new.txt','content':'unauthorized'},
 {'sessionId':'synthetic-session','path':root+'/../new.txt','content':'unauthorized'},
 {'sessionId':'synthetic-session','path':root+'//new.txt','content':'unauthorized'},
 {'sessionId':'synthetic-session','path':root+'/new.txt','content':{}},
]):
 send({'jsonrpc':'2.0','id':i+50,'method':'fs/write_text_file','params':params})
 r=recv(); assert 'error' in r
reply(prompt, {'stopReason':'end_turn'})
'''
    with GatewayClient(transport=httpx.MockTransport(gateway), base_url='http://127.0.0.1:1') as http:
        with AcpSession(peer(tmp_path, script), AcpClientAdapter(http, 'run-1'), WorkspaceMapping(tmp_path), timeout_s=2) as session:
            assert session.run('synthetic task')['stopReason']=='end_turn'
    assert sent == []
    assert not (tmp_path / 'new.txt').exists()


def test_notification_cannot_execute_file_write(tmp_path: Path) -> None:
    sent = []
    script = HANDSHAKE + '''send({'jsonrpc':'2.0','method':'fs/write_text_file','params':{'sessionId':'synthetic-session','path':root+'/new.txt','content':'unauthorized'}})
time.sleep(10)
'''
    with GatewayClient(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(500)), base_url='http://127.0.0.1:1') as http:
        session = AcpSession(peer(tmp_path, script), AcpClientAdapter(http, 'run-1'), WorkspaceMapping(tmp_path), timeout_s=2)
        with pytest.raises(AcpClientError, match='ACP_UNSUPPORTED_NOTIFICATION'):
            session.run('synthetic task')
    assert sent == []


def test_timeout_sends_cancel_and_cleans_descendants(tmp_path: Path) -> None:
    # The synthetic peer records only cancellation metadata in an unguarded test
    # fixture. This is evidence the notification reached the process over stdio.
    marker = tmp_path / 'cancelled'
    script = HANDSHAKE + '''r=recv()
assert r['method']=='session/cancel' and 'id' not in r
open(sys.argv[1], 'w').write(r['params']['sessionId'])
time.sleep(10)
'''
    with GatewayClient(base_url='http://127.0.0.1:1') as http:
        session = AcpSession(peer(tmp_path, script) + [str(marker)], AcpClientAdapter(http, 'run-1'), WorkspaceMapping(tmp_path), timeout_s=.3)
        with pytest.raises(AcpClientError, match='ACP_TIMEOUT'):
            session.run('synthetic task')
        assert session.transport.process.poll() is not None
    assert marker.read_text() == 'synthetic-session'


def test_exchange_matches_pinned_official_v1_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jsonschema import Draft202012Validator
    from scopewatch.acp_client import StdioTransport
    schema = json.loads((Path(__file__).parent / 'fixtures/acp_v1/schema.json').read_text())
    transcript = []
    original_send, original_receive = StdioTransport.send, StdioTransport.receive
    def capture_send(self, message, deadline):
        transcript.append(('client', message))
        return original_send(self, message, deadline)
    def capture_receive(self, deadline):
        message = original_receive(self, deadline)
        transcript.append(('agent', message))
        return message
    monkeypatch.setattr(StdioTransport, 'send', capture_send)
    monkeypatch.setattr(StdioTransport, 'receive', capture_receive)
    script = HANDSHAKE + '''send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'synthetic-session','update':{'sessionUpdate':'agent_thought_chunk','content':{'type':'text','text':'Synthetic summary'}}}})
for i,method,extra in [(20,'fs/read_text_file',{}),(21,'fs/write_text_file',{'content':'new synthetic content'}),(22,'session/request_permission',{'toolCall':{'toolCallId':'t','title':'write'},'options':[{'optionId':'once','kind':'allow_once','name':'Allow once'}]})]:
 send({'jsonrpc':'2.0','id':i,'method':method,'params':{'sessionId':'synthetic-session',**({'path':root+'/file.txt'} if method.startswith('fs/') else {}),**extra}})
 r=recv(); assert 'result' in r
reply(prompt, {'stopReason':'end_turn'})
'''
    with GatewayClient(transport=httpx.MockTransport(lambda r: httpx.Response(201, json=receipt(request=r))), base_url='http://127.0.0.1:1') as http:
        with AcpSession(peer(tmp_path, script), AcpClientAdapter(http, 'run-1'), WorkspaceMapping(tmp_path), timeout_s=3) as session:
            session.run('synthetic task')
    methods = {'initialize':'Initialize', 'session/new':'NewSession', 'session/prompt':'Prompt',
               'session/update':'SessionNotification', 'fs/read_text_file':'ReadTextFile',
               'fs/write_text_file':'WriteTextFile','session/request_permission':'RequestPermission'}
    requests = {}
    for side, message in transcript:
        if 'method' in message:
            name = methods[message['method']]
            if 'id' in message:
                requests[(side, message['id'])] = name
            definition = name if message['method']=='session/update' else name+'Request'
            payload = message['params']
        else:
            opposite = 'agent' if side=='client' else 'client'
            definition = requests[(opposite, message['id'])]+'Response'
            payload = message['result']
        Draft202012Validator({'$ref': '#/$defs/'+definition, '$defs':schema['$defs']}).validate(payload)
    assert len(transcript) == 13


@pytest.fixture
def localhost_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Actual loopback HTTP, explicitly authorized by #121; no external network."""
    import socket
    import threading
    import time
    import uvicorn
    from scopewatch.app import create_app
    monkeypatch.delenv('DEMO_TOKEN', raising=False)
    monkeypatch.delenv('SCOPEWATCH_EXECUTOR', raising=False)
    monkeypatch.delenv('SCOPEWATCH_API_KEY', raising=False)
    source = tmp_path / 'source'
    (source / 'outputs').mkdir(parents=True)
    (source / 'private').mkdir()
    (source / 'outputs/input.txt').write_text('Synthetic input\nline two\n')
    (source / 'private/private.txt').write_text('Synthetic blocked content')
    app = create_app(db_path=str(tmp_path / 'gateway.db'), workspace_root=source,
                     run_workspaces_root=tmp_path / 'runs')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic()+5
    while not server.started and thread.is_alive() and time.monotonic()<deadline:
        time.sleep(.01)
    assert server.started
    try:
        with GatewayClient(base_url=f'http://127.0.0.1:{sock.getsockname()[1]}', trust_env=False) as http:
            yield http, app.state.service, source
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def create_local_run(http: GatewayClient, service, *, hold: bool = False) -> tuple[str, Path]:
    r = http.post('/api/v1/runs', json={'name':'Synthetic stdio client verification', 'task_scope':{
        'task_description':'Synthetic read/write only', 'allowed_paths':['outputs'],
        'blocked_paths':['private'], 'allowed_tools':['workspace'],
        'allowed_operations':['read_text','write_text'],
        'requires_approval':['write_text'] if hold else [], 'created_at':'2026-10-28T00:00:00Z',
    }})
    assert r.status_code==201
    run_id = r.json()['id']
    return run_id, Path(service.get_run(run_id).workspace_path)


def local_actions(http: GatewayClient, run_id: str) -> list[dict]:
    events = http.get(f'/api/v1/runs/{run_id}/events').json()
    ids = list(dict.fromkeys(e['action_request_id'] for e in events if e.get('action_request_id')))
    return [http.get(f'/api/v1/runs/{run_id}/actions/{action_id}').json() for action_id in ids]


def test_stdio_with_actual_localhost_gateway_effects_and_refusals(tmp_path: Path, localhost_gateway) -> None:
    http, service, source = localhost_gateway
    run_id, workspace = create_local_run(http, service)
    # Preserve alias names: gateway checks alias *and* canonical target.
    (workspace / 'private/allowed-alias').symlink_to(workspace / 'outputs', target_is_directory=True)
    (workspace / 'outputs/blocked-alias').symlink_to(workspace / 'private', target_is_directory=True)
    script = HANDSHAKE + '''send({'jsonrpc':'2.0','id':20,'method':'fs/read_text_file','params':{'sessionId':'synthetic-session','path':root+'/outputs/input.txt','line':2,'limit':1}})
r=recv(); assert r['result']=={'content':'line two\\n'}
send({'jsonrpc':'2.0','id':21,'method':'fs/write_text_file','params':{'sessionId':'synthetic-session','path':root+'/outputs/new.txt','content':'Synthetic output'}})
r=recv(); assert r['result']=={}
for i,path in enumerate(['/private/new.txt','/outputs/blocked-alias/new.txt','/private/allowed-alias/new.txt']):
 send({'jsonrpc':'2.0','id':30+i,'method':'fs/write_text_file','params':{'sessionId':'synthetic-session','path':root+path,'content':'unauthorized'}})
 r=recv(); assert r['error']['data']['reason_code']=='BLOCKED_PATH' and r['error']['data']['action_id']
send({'jsonrpc':'2.0','id':40,'method':'fs/read_text_file','params':{'sessionId':'synthetic-session','path':root+'/outputs/missing.txt'}})
r=recv(); assert r['error']['data']['status']=='FAILED' and r['error']['data']['action_id']
reply(prompt, {'stopReason':'end_turn'})
'''
    with AcpSession(peer(tmp_path, script), AcpClientAdapter(http, run_id), WorkspaceMapping(workspace), timeout_s=5) as session:
        assert session.run('synthetic task')['stopReason']=='end_turn'
    assert (workspace / 'outputs/new.txt').read_text()=='Synthetic output'
    assert not (source / 'outputs/new.txt').exists()
    assert not (workspace / 'private/new.txt').exists()
    actions = local_actions(http, run_id)
    assert len(actions)==6
    assert all(a['action_request']['reasoning_provenance']=='UNAVAILABLE' for a in actions)


def test_localhost_hold_times_out_without_file_effect(tmp_path: Path, localhost_gateway) -> None:
    http, service, _ = localhost_gateway
    run_id, workspace = create_local_run(http, service, hold=True)
    script = HANDSHAKE + '''send({'jsonrpc':'2.0','id':20,'method':'fs/write_text_file','params':{'sessionId':'synthetic-session','path':root+'/outputs/held.txt','content':'Synthetic held output'}})
r=recv(); assert r['error']['data']['reason_code']=='HOLD_TIMEOUT' and r['error']['data']['action_id']
reply(prompt, {'stopReason':'end_turn'})
'''
    adapter = AcpClientAdapter(http, run_id, hold=HoldConfig(timeout_s=.15, poll_interval_s=.01))
    with AcpSession(peer(tmp_path, script), adapter, WorkspaceMapping(workspace), timeout_s=3) as session:
        assert session.run('synthetic task')['stopReason']=='end_turn'
    assert not (workspace / 'outputs/held.txt').exists()
    actions = local_actions(http, run_id)
    assert len(actions)==1
    assert actions[0]['policy_decision']['outcome']=='HOLD'
    assert actions[0]['execution_receipt'] is None


def test_localhost_approved_hold_failed_receipt_is_a_tool_error(tmp_path: Path, localhost_gateway) -> None:
    http, service, _ = localhost_gateway
    run_id, workspace = create_local_run(http, service, hold=True)
    def reviewer(response):
        if response.request.method=='POST' and response.request.url.path.endswith('/actions'):
            response.read()
            body=response.json()
            approval=body.get('approval_request')
            if approval:
                r=http.post(f"/api/v1/approvals/{approval['id']}/approve", json={'resolution_reason':'Synthetic reviewer approval'})
                assert r.status_code==200
    http.event_hooks['response'].append(reviewer)
    # Fits the client character limit, exceeds the gateway write byte bound.
    script = HANDSHAKE + '''send({'jsonrpc':'2.0','id':20,'method':'fs/write_text_file','params':{'sessionId':'synthetic-session','path':root+'/outputs/failed.txt','content':'é'*40000}})
r=recv(); assert r['error']['data']['status']=='FAILED' and r['error']['data']['action_id']
reply(prompt, {'stopReason':'end_turn'})
'''
    with AcpSession(peer(tmp_path, script), AcpClientAdapter(http, run_id), WorkspaceMapping(workspace), timeout_s=5) as session:
        assert session.run('synthetic task')['stopReason']=='end_turn'
    assert not (workspace / 'outputs/failed.txt').exists()
    action = local_actions(http, run_id)[0]
    assert action['policy_decision']['outcome']=='HOLD'
    assert action['approval_request']['status']=='CONSUMED'
    assert action['execution_receipt']['status']=='FAILED'


@pytest.mark.parametrize('mode', ['frames', 'bytes'])
def test_session_output_budget_stops_notification_flood(tmp_path: Path, mode: str) -> None:
    from scopewatch.acp_client import MAX_SESSION_FRAMES
    count = MAX_SESSION_FRAMES + 1 if mode == 'frames' else 30
    size = 1 if mode == 'frames' else 100000
    script = HANDSHAKE + f'''for _ in range({count}):
 send({{'jsonrpc':'2.0','method':'session/update','params':{{'sessionId':'synthetic-session','update':{{'sessionUpdate':'agent_message_chunk','content':{{'type':'text','text':'x'*{size}}}}}}}}})
time.sleep(10)
'''
    with GatewayClient(base_url='http://127.0.0.1:1') as http:
        session = AcpSession(peer(tmp_path, script), AcpClientAdapter(http, 'run-1'), WorkspaceMapping(tmp_path), timeout_s=3)
        with pytest.raises(AcpClientError, match='ACP_OUTPUT_LIMIT'):
            session.run('synthetic task')
        assert session.transport.process.poll() is not None


@pytest.mark.parametrize('body', [
    {'action_request':{'id':'action-1'},'policy_decision':{'outcome':'ALLOW'}},
    {'action_request':{'id':'action-1'},'policy_decision':{'outcome':'ALLOW'},'execution_receipt':{'status':'NOT_EXECUTED'}},
    {'action_request':{'id':'action-1'},'policy_decision':{'outcome':'ALLOW'},'execution_receipt':{'status':'FAILED','sanitized_result':{'error':'PRIVATE synthetic provider body'}}},
    receipt('x'*65537),
    {**receipt(), 'execution_receipt':{'status':'EXECUTED','sanitized_result':{'content':'short','truncated':True}}},
])
def test_failed_missing_oversized_and_partial_read_receipts_are_tool_errors(tmp_path: Path, body: dict) -> None:
    script = HANDSHAKE + '''send({'jsonrpc':'2.0','id':20,'method':'fs/read_text_file','params':{'sessionId':'synthetic-session','path':root+'/input.txt'}})
r=recv(); assert 'error' in r and 'PRIVATE' not in json.dumps(r)
reply(prompt, {'stopReason':'end_turn'})
'''
    with GatewayClient(transport=httpx.MockTransport(lambda r: httpx.Response(201, json=body)), base_url='http://127.0.0.1:1') as http:
        with AcpSession(peer(tmp_path, script), AcpClientAdapter(http, 'run-1'), WorkspaceMapping(tmp_path), timeout_s=3) as session:
            assert session.run('synthetic task')['stopReason']=='end_turn'


@pytest.mark.parametrize('response', [httpx.Response(500, text='PRIVATE synthetic provider body'),
                                     httpx.Response(201, content=b'x'*600000)])
def test_gateway_body_withheld_and_response_bytes_bounded(tmp_path: Path, response: httpx.Response) -> None:
    script = HANDSHAKE + '''send({'jsonrpc':'2.0','id':20,'method':'fs/read_text_file','params':{'sessionId':'synthetic-session','path':root+'/input.txt'}})
r=recv(); assert 'error' in r and 'PRIVATE' not in json.dumps(r) and len(json.dumps(r))<1000
reply(prompt, {'stopReason':'end_turn'})
'''
    with GatewayClient(transport=httpx.MockTransport(lambda r: response), base_url='http://127.0.0.1:1') as http:
        with AcpSession(peer(tmp_path, script), AcpClientAdapter(http, 'run-1'), WorkspaceMapping(tmp_path), timeout_s=3) as session:
            session.run('synthetic task')


def test_cli_runs_local_synthetic_peer_and_withholds_gateway_credentials(tmp_path: Path, localhost_gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import subprocess
    http, service, _ = localhost_gateway
    run_id, workspace = create_local_run(http, service)
    script = HANDSHAKE + '''assert 'SCOPEWATCH_API_KEY' not in os.environ
assert 'DEMO_TOKEN' not in os.environ
assert 'SYNTHETIC_AGENT_CREDENTIAL' in os.environ
assert 'HOME' not in os.environ
reply(prompt, {'stopReason':'end_turn'})
'''
    monkeypatch.setenv('SCOPEWATCH_API_KEY', 'synthetic-gateway-key')
    monkeypatch.setenv('SYNTHETIC_AGENT_CREDENTIAL', 'synthetic-agent-key')
    cli = Path(__file__).resolve().parents[2] / 'scripts/run_acp_client.py'
    result = subprocess.run([sys.executable, str(cli), '--gateway-url', str(http.base_url),
        '--run-id', run_id, '--workspace', str(workspace), '--synthetic-task', 'synthetic task',
        '--synthetic', '--agent-env-name', 'SYNTHETIC_AGENT_CREDENTIAL', '--', *peer(tmp_path, script)],
        capture_output=True, text=True, timeout=10, env=os.environ.copy())
    assert result.returncode==0, result.stderr
    records=[json.loads(line) for line in result.stdout.splitlines()]
    assert records[0]['event']=='acp_session_start' and records[0]['run_id']==run_id
    assert records[0]['coverage']
    report=records[1]
    assert report['protocol_version']==1 and report['stopReason']=='end_turn'
    assert report['run_id']==run_id and 'manual acceptance is pending' in report['coverage']
    assert 'synthetic-gateway-key' not in result.stdout+result.stderr
    assert 'synthetic-agent-key' not in result.stdout+result.stderr


@pytest.mark.parametrize('change', ['foreign-run','foreign-action','foreign-resource','preview-only','nontext-content'])
def test_receipts_must_match_run_action_resource_and_whole_content(tmp_path: Path, change: str) -> None:
    def gateway(request):
        body=receipt(request=request)
        if change=='foreign-run':
            body['action_request']['run_id']='other-run'
        elif change=='foreign-action':
            body['execution_receipt']['action_request_id']='other-action'
        elif change=='foreign-resource':
            body['execution_receipt']['resource']='private/file.txt'
        elif change=='preview-only':
            body['execution_receipt']['sanitized_result']={'preview':'partial data'}
        else:
            body['execution_receipt']['sanitized_result']={'content':{'unexpected':'object'}}
        return httpx.Response(201,json=body)
    script = HANDSHAKE + '''send({'jsonrpc':'2.0','id':20,'method':'fs/read_text_file','params':{'sessionId':'synthetic-session','path':root+'/input.txt'}})
r=recv(); assert 'error' in r and 'content' not in r.get('result',{})
reply(prompt, {'stopReason':'end_turn'})
'''
    with GatewayClient(transport=httpx.MockTransport(gateway),base_url='http://127.0.0.1:1') as http:
        with AcpSession(peer(tmp_path,script),AcpClientAdapter(http,'run-1'),WorkspaceMapping(tmp_path),timeout_s=3) as session:
            session.run('synthetic task')


def test_bidirectional_id_zero_number_and_string_do_not_collide(tmp_path: Path) -> None:
    script=HANDSHAKE+'''for request_id in [0,1,'1',prompt['id']]:
 send({'jsonrpc':'2.0','id':request_id,'method':'fs/read_text_file','params':{'sessionId':'synthetic-session','path':root+'/input.txt'}})
 r=recv(); assert type(r['id']) is type(request_id) and r['id']==request_id and r['result']=={'content':'synthetic file'}
reply(prompt, {'stopReason':'end_turn'})
'''
    with GatewayClient(transport=httpx.MockTransport(lambda r: httpx.Response(201,json=receipt(request=r))),base_url='http://127.0.0.1:1') as http:
        with AcpSession(peer(tmp_path,script),AcpClientAdapter(http,'run-1'),WorkspaceMapping(tmp_path),timeout_s=3) as session:
            session.run('synthetic task')


@pytest.mark.parametrize('options,expected', [
    ({},'one\r\ntwo\nthree'), ({'line':None,'limit':None},'one\r\ntwo\nthree'),
    ({'line':1,'limit':0},''), ({'line':2,'limit':1},'two\n'),
    ({'line':3},'three'), ({'line':10},''),
])
def test_read_ranges_are_lines_and_preserve_line_endings(tmp_path: Path, options: dict, expected: str) -> None:
    script=HANDSHAKE+f'''send({{'jsonrpc':'2.0','id':20,'method':'fs/read_text_file','params':{{'sessionId':'synthetic-session','path':root+'/input.txt',**{options!r}}}}})
r=recv(); assert r['result']=={{'content':{expected!r}}}
reply(prompt, {{'stopReason':'end_turn'}})
'''
    with GatewayClient(transport=httpx.MockTransport(lambda r: httpx.Response(201,json=receipt('one\r\ntwo\nthree',r))),base_url='http://127.0.0.1:1') as http:
        with AcpSession(peer(tmp_path,script),AcpClientAdapter(http,'run-1'),WorkspaceMapping(tmp_path),timeout_s=3) as session:
            session.run('synthetic task')


@pytest.mark.parametrize('options', [{'line':0},{'line':-1},{'line':True},{'line':1.5},{'limit':False},{'limit':-1},{'limit':2**32}])
def test_invalid_read_ranges_do_not_submit_gateway_actions(tmp_path: Path, options: dict) -> None:
    sent=[]
    script=HANDSHAKE+f'''send({{'jsonrpc':'2.0','id':20,'method':'fs/read_text_file','params':{{'sessionId':'synthetic-session','path':root+'/input.txt',**{options!r}}}}})
r=recv(); assert 'error' in r
reply(prompt, {{'stopReason':'end_turn'}})
'''
    with GatewayClient(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(500)),base_url='http://127.0.0.1:1') as http:
        with AcpSession(peer(tmp_path,script),AcpClientAdapter(http,'run-1'),WorkspaceMapping(tmp_path),timeout_s=3) as session:
            session.run('synthetic task')
    assert sent==[]


def test_cli_process_never_opens_guarded_files_even_for_successful_write(tmp_path: Path, localhost_gateway) -> None:
    import subprocess
    http, service, _ = localhost_gateway
    run_id, workspace = create_local_run(http, service)
    cli = Path(__file__).resolve().parents[2] / 'scripts/run_acp_client.py'
    wrapper = tmp_path / 'guarded_cli.py'
    wrapper.write_text('''import os, runpy, sys
root=os.fsencode(sys.argv[1]).rstrip(b'/')
script=sys.argv[2]
sys.argv=[script,*sys.argv[3:]]
def audit(event,args):
 if event in ('open','os.remove','os.mkdir','os.rmdir','os.rename','os.link','os.symlink','os.truncate'):
  for value in args[:2]:
   if isinstance(value,(str,bytes)):
    path=os.fsencode(value)
    if path==root or path.startswith(root+b'/'):
     raise RuntimeError('guarded file access forbidden in client process')
sys.addaudithook(audit)
runpy.run_path(script,run_name='__main__')
''')
    script=HANDSHAKE+'''send({'jsonrpc':'2.0','id':20,'method':'fs/read_text_file','params':{'sessionId':'synthetic-session','path':root+'/outputs/input.txt'}})
r=recv(); assert r['result']['content']=='Synthetic input\\nline two\\n'
send({'jsonrpc':'2.0','id':21,'method':'fs/write_text_file','params':{'sessionId':'synthetic-session','path':root+'/outputs/client-guard.txt','content':'Synthetic mediated output'}})
r=recv(); assert r['result']=={}
reply(prompt,{'stopReason':'end_turn'})
'''
    result=subprocess.run([sys.executable,str(wrapper),str(workspace),str(cli),
        '--gateway-url',str(http.base_url),'--run-id',run_id,'--workspace',str(workspace),
        '--synthetic','--synthetic-task','synthetic task','--',*peer(tmp_path,script)],
        capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout.splitlines()[-1])['stopReason']=='end_turn'
    assert (workspace / 'outputs/client-guard.txt').read_text()=='Synthetic mediated output'
    assert len(local_actions(http,run_id))==2


@pytest.mark.parametrize('init', [
    {'protocolVersion':1},
    {'protocolVersion':1,'agentCapabilities':{},'agentInfo':None},
    {'protocolVersion':1,'agentCapabilities':{'futureCapability':{'enabled':True}}},
])
def test_optional_initialize_fields_and_unknown_additions(tmp_path: Path, init: dict) -> None:
    # Replace only the peer initialize result; all later lifecycle checks stand.
    old="reply(q, {'protocolVersion':1,'agentCapabilities':{},'agentInfo':{'name':'synthetic-peer','version':'1'}})"
    script=HANDSHAKE.replace(old,'reply(q, '+repr(init)+')')+"reply(prompt,{'stopReason':'end_turn'})\n"
    with GatewayClient(base_url='http://127.0.0.1:1') as http:
        with AcpSession(peer(tmp_path,script),AcpClientAdapter(http,'run-1'),WorkspaceMapping(tmp_path),timeout_s=2) as session:
            session.run('synthetic task')


def test_empty_write_and_forged_context_cannot_change_bound_gateway_action(tmp_path: Path) -> None:
    sent=[]
    def gateway(request):
        sent.append(json.loads(request.content))
        return httpx.Response(201,json=receipt(request=request))
    script=HANDSHAKE+'''send({'jsonrpc':'2.0','id':0,'method':'fs/write_text_file','params':{'sessionId':'synthetic-session','path':root+'/empty.txt','content':'','run_id':'other-run','workspace':'/outside','requested_by':'forged-actor','reasoning_provenance':'PROVIDER_EXPOSED_TRACE','reasoning_summary':'forged trace'}})
r=recv(); assert r['id']==0 and r['result']=={}
reply(prompt,{'stopReason':'end_turn'})
'''
    with GatewayClient(transport=httpx.MockTransport(gateway),base_url='http://127.0.0.1:1') as http:
        with AcpSession(peer(tmp_path,script),AcpClientAdapter(http,'run-1'),WorkspaceMapping(tmp_path),timeout_s=2) as session:
            session.run('synthetic task')
    assert len(sent)==1
    assert sent[0]['arguments']=={'content':''}
    assert sent[0]['requested_by']=='codex-acp' and sent[0]['reasoning_provenance']=='UNAVAILABLE'
    assert 'reasoning_summary' not in sent[0]


def test_blocked_stdin_write_and_stderr_flood_are_bounded(tmp_path: Path) -> None:
    from scopewatch.acp_client import StdioTransport
    import time
    # No stdin reads, unlimited stderr writes: stderr is discarded and cannot
    # stall the client. A sufficiently large outbound frame fills the pipe.
    command=peer(tmp_path,"while True: os.write(2,b'synthetic log'*10000)\n")
    transport=StdioTransport(command)
    try:
        with pytest.raises(AcpClientError,match='ACP_TIMEOUT'):
            transport.send({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'synthetic':'x'*200000}},time.monotonic()+.15)
    finally:
        transport.close()
    assert transport.process.poll() is not None


@pytest.mark.parametrize('session_id', ['session:synthetic/1', 'synthetic session', '', '会話/1'])
def test_session_ids_are_bounded_opaque_strings(tmp_path: Path, session_id: str) -> None:
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).parent / 'fixtures/acp_v1/schema.json').read_text())
    Draft202012Validator({**schema, '$ref': '#/$defs/NewSessionResponse'}).validate({'sessionId': session_id})
    handshake = HANDSHAKE.replace("'synthetic-session'", repr(session_id))
    script = handshake + f'''assert prompt['params']['sessionId']=={session_id!r}
send({{'jsonrpc':'2.0','id':20,'method':'session/request_permission','params':{{'sessionId':{session_id!r}}}}})
r=recv(); assert r['result']=={{'outcome':{{'outcome':'cancelled'}}}}
reply(prompt, {{'stopReason':'end_turn'}})
'''
    with GatewayClient(base_url='http://127.0.0.1:1') as http:
        with AcpSession(peer(tmp_path, script), AcpClientAdapter(http, 'run-1'), WorkspaceMapping(tmp_path)) as session:
            assert session.run('synthetic task')['session_id'] == session_id


@pytest.mark.parametrize('stage', ['headers', 'body'])
@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost'])
def test_absolute_http_deadline_interrupts_continuing_slow_drip(stage: str, host: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import socket
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from scopewatch.acp_client import _BoundedGateway

    disconnected = threading.Event()
    class Drip(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            try:
                if stage == 'headers':
                    data = b'HTTP/1.1 200 OK\r\nX-Synthetic: ' + b'a' * 100
                    for byte in data:
                        self.connection.sendall(bytes([byte]))
                        time.sleep(.025)
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', '100')
                    self.end_headers()
                    for _ in range(100):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                        time.sleep(.025)
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()
        def do_POST(self):
            self.do_GET()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Drip)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # A real HTTP transport must not start resolver workers, even when the OS
    # resolver stalls. Literal localhost is mapped before HTTPX sees the URL.
    lookups = []
    original_lookup = socket.getaddrinfo
    def stalled_lookup(*args, **kwargs):
        lookups.append(args)
        time.sleep(1.2)
        return original_lookup(*args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', stalled_lookup)
    try:
        with GatewayClient(base_url=f'http://{host}:{server.server_port}') as http:
            start = time.monotonic()
            with pytest.raises(AcpClientError, match='ACP_TIMEOUT'):
                _BoundedGateway(http, start + .15).get('/synthetic-drip')
            assert time.monotonic() - start < .7
            assert disconnected.wait(1), 'deadline must close the HTTP connection'
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        marker = tmp_path / 'cancel-observed'
        script = HANDSHAKE + f'''send({{'jsonrpc':'2.0','id':20,'method':'fs/read_text_file','params':{{'sessionId':'synthetic-session','path':root+'/input.txt'}}}})
r=recv(); assert r['method']=='session/cancel' and 'id' not in r
open({str(marker)!r}, 'w').write('cancelled')
time.sleep(10)
'''
        with GatewayClient(base_url=f'http://{host}:{server.server_port}') as http:
            session = AcpSession(peer(tmp_path, script), AcpClientAdapter(http, 'run-1'), WorkspaceMapping(workspace), timeout_s=.25)
            start = time.monotonic()
            with pytest.raises(AcpClientError, match='ACP_TIMEOUT'):
                session.run('synthetic task')
            assert time.monotonic() - start < 1.2
            assert session.transport.process.poll() is not None
            assert marker.read_text() == 'cancelled'
        assert lookups == [], 'bounded local requests must never invoke DNS'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


@pytest.mark.parametrize('new_content', ['new synthetic content', 'old synthetic content'])
def test_hold_receipt_from_another_real_write_is_not_success(tmp_path: Path, localhost_gateway, new_content: str) -> None:
    http, service, _ = localhost_gateway
    run_id, workspace = create_local_run(http, service, hold=True)
    first = http.post(f'/api/v1/runs/{run_id}/actions', json={
        'tool': 'workspace', 'operation': 'write_text', 'resource': 'outputs/held.txt',
        'arguments': {'content': 'old synthetic content'},
    }).json()
    assert first['policy_decision']['outcome'] == 'HOLD'
    approved = http.post(f"/api/v1/approvals/{first['approval_request']['id']}/approve",
                         json={'resolution_reason': 'Synthetic first-write approval'})
    assert approved.status_code == 200
    first_id = first['action_request']['id']
    old = http.get(f'/api/v1/runs/{run_id}/actions/{first_id}').json()
    assert old['execution_receipt']['status'] == 'EXECUTED'
    substitutions = []
    def stale_poll(response):
        if (response.request.method == 'GET' and '/actions/' in response.request.url.path
                and not response.request.url.path.endswith(first_id)):
            substitutions.append(response.request.url.path)
            response._content = json.dumps(old).encode()
    http.event_hooks['response'].append(stale_poll)
    script = HANDSHAKE + '''send({'jsonrpc':'2.0','id':20,'method':'fs/write_text_file','params':{'sessionId':'synthetic-session','path':root+'/outputs/held.txt','content':'new synthetic content'}})
r=recv(); assert 'error' in r and 'result' not in r
reply(prompt, {'stopReason':'end_turn'})
'''
    script = script.replace("'new synthetic content'", repr(new_content))
    try:
        with AcpSession(peer(tmp_path, script), AcpClientAdapter(http, run_id), WorkspaceMapping(workspace), timeout_s=3) as session:
            session.run('synthetic task')
    finally:
        http.event_hooks['response'].remove(stale_poll)
    assert len(substitutions) == 1
    assert (workspace / 'outputs/held.txt').read_text() == 'old synthetic content'
    actions = local_actions(http, run_id)
    held = next(action for action in actions if action['action_request']['id'] != first_id)
    assert held['action_request']['arguments']['content'] == new_content
    assert held['approval_request']['status'] == 'PENDING'
    assert held['execution_receipt'] is None


def test_receipt_arguments_must_match_the_submitted_write(tmp_path: Path) -> None:
    def gateway(request):
        data = receipt(request=request)
        data['action_request']['arguments']['content'] = 'different synthetic content'
        return httpx.Response(201, json=data)
    script = HANDSHAKE + '''send({'jsonrpc':'2.0','id':20,'method':'fs/write_text_file','params':{'sessionId':'synthetic-session','path':root+'/output.txt','content':'requested synthetic content'}})
r=recv(); assert 'error' in r and 'result' not in r
reply(prompt, {'stopReason':'end_turn'})
'''
    with GatewayClient(base_url='http://127.0.0.1:1', transport=httpx.MockTransport(gateway)) as http:
        with AcpSession(peer(tmp_path, script), AcpClientAdapter(http, 'run-1'), WorkspaceMapping(tmp_path)) as session:
            session.run('synthetic task')


@pytest.mark.parametrize('params', [{}, None])
def test_unknown_extension_notifications_are_ignored_without_reply(tmp_path: Path, params) -> None:
    message = {'jsonrpc': '2.0', 'method': '_org.example/progress'}
    if params is not None:
        message['params'] = params
    script = HANDSHAKE + f'''send({message!r})
send({{'jsonrpc':'2.0','id':20,'method':'session/request_permission','params':{{'sessionId':'synthetic-session'}}}})
r=recv(); assert r['id']==20 and r['result']=={{'outcome':{{'outcome':'cancelled'}}}}
reply(prompt, {{'stopReason':'end_turn'}})
'''
    with GatewayClient(base_url='http://127.0.0.1:1') as http:
        with AcpSession(peer(tmp_path, script), AcpClientAdapter(http, 'run-1'), WorkspaceMapping(tmp_path)) as session:
            session.run('synthetic task')


@pytest.mark.parametrize('endpoint', [
    'http://example.test', 'http://127.0.0.1.example.test', 'http://192.0.2.1',
    'https://127.0.0.1', 'http://user:synthetic@127.0.0.1', 'http://[::1%25lo]',
    'http://127.0.0.1?query=1', 'http://127.0.0.1#fragment', 'http://127.0.0.1/path',
])
def test_gateway_refuses_non_numeric_or_nonlocal_endpoints_before_lookup(endpoint: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    def no_lookup(*args, **kwargs):
        pytest.fail('rejected gateway configuration must never resolve a hostname')
    monkeypatch.setattr(socket, 'getaddrinfo', no_lookup)
    with pytest.raises(AcpClientError, match='ACP_NUMERIC_LOOPBACK_REQUIRED'):
        GatewayClient(base_url=endpoint)


@pytest.mark.parametrize('target', ['http://example.test/', 'http://127.0.0.2/', 'http://127.0.0.1:2/'])
def test_absolute_request_target_cannot_leave_the_pinned_gateway(target: str) -> None:
    sent = []
    with GatewayClient(base_url='http://127.0.0.1:1', transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200))) as http:
        with pytest.raises(AcpClientError, match='ACP_GATEWAY_DESTINATION_MISMATCH'):
            http.get(target)
    assert sent == []


def test_request_hook_cannot_reintroduce_a_hostname_lookup() -> None:
    sent = []
    with GatewayClient(base_url='http://127.0.0.1:1', transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200))) as http:
        def retarget(request):
            request.url = httpx.URL('http://example.test/')
        http.event_hooks['request'].append(retarget)
        with pytest.raises(AcpClientError, match='ACP_GATEWAY_DESTINATION_MISMATCH'):
            http.get('/')
    assert sent == []
