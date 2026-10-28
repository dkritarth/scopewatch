"""Bounded ACP v1 client, distinct from the legacy dictionary dispatcher.

Wire source: agentclientprotocol/agent-client-protocol at
 d6f00fd0ce5323be07f64fca09f59f26a3a5d77a, schema/v1/schema.json and
 docs/protocol/v1/{initialization,transports,session-setup,prompt-turn}.mdx.
Only fs/read_text_file and fs/write_text_file have gateway effects.
This launches a process; it does not sandbox that process or intercept native I/O.
"""
from __future__ import annotations

import asyncio
import json
import ipaddress
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
from typing import Any

import httpx

from scopewatch.acp_adapter import (
    AcpClientAdapter, AcpRpcError, HoldConfig, MAX_RELAYED_FILE_CHARS,
    _bounded_text,
)

PROTOCOL_VERSION = 1
SCHEMA_REVISION = 'd6f00fd0ce5323be07f64fca09f59f26a3a5d77a'
MAX_FRAME_BYTES = 256 * 1024
MAX_SESSION_BYTES = 2 * 1024 * 1024
MAX_SESSION_FRAMES = 512
MAX_HTTP_BYTES = 512 * 1024
MAX_SUMMARY_CHARS = 4000
MAX_TASK_CHARS = 16000
STOP_REASONS = {'end_turn', 'max_tokens', 'max_turn_requests', 'refusal', 'cancelled'}
COVERAGE_STATEMENT = (
    'ACP v1 client mediates only advertised fs/read_text_file and fs/write_text_file '
    'requests through the gateway. Commands, terminal lifecycle, MCP servers, and '
    'permission grants are unsupported. All permission requests are cancelled. '
    'Agent thought chunks are agent-authored summaries, never provider-exposed traces. '
    'Native agent filesystem, process, and network activity is outside this boundary. '
    'Missing gateway evidence does not prove no action occurred. Synthetic stdio peer '
    'verification is not real Codex bridge verification; that manual acceptance is pending.'
)
_SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')


class AcpClientError(Exception):
    """Local stable error code. Never wraps peer bodies or exception messages."""


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise AcpClientError('ACP_TIMEOUT')
    return left


class WorkspaceMapping:
    """Map absolute paths under one explicit local session root to run resources.

    Only metadata resolution occurs here. Gateway policy checks both the submitted
    alias and canonical target in its *own run workspace*, and owns all file I/O.
    A caller must bind this mapping to the intended run; no host path is discovered
    from the public API. Remote workspace translation is outside this client.
    """

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise AcpClientError('ACP_INVALID_WORKSPACE')
        try:
            self.root = root.resolve(strict=True)
            if not self.root.is_dir() or self.root == Path('/'):
                raise AcpClientError('ACP_INVALID_WORKSPACE')
        except (OSError, RuntimeError):
            raise AcpClientError('ACP_INVALID_WORKSPACE') from None

    def resource(self, value: Any) -> str:
        if (not isinstance(value, str) or len(value) > 4096
                or not value.startswith('/') or value.startswith('//')
                or '\\' in value or any(ord(c) < 32 for c in value)
                or any(part in ('', '.', '..') for part in value.split('/')[1:])):
            raise AcpClientError('ACP_INVALID_PATH')
        try:
            path = Path(value)
            relative = path.relative_to(self.root)
            path.resolve().relative_to(self.root)
            if not relative.parts:
                raise ValueError
        except (ValueError, OSError, RuntimeError):
            raise AcpClientError('ACP_PATH_OUTSIDE_WORKSPACE') from None
        # Preserve the alias so gateway policy can deny a blocked alias even
        # when its canonical target would otherwise be allowed.
        return relative.as_posix()


class StdioTransport:
    """Single-reader/writer POSIX pipe transport with no background workers."""

    def __init__(self, command: list[str], *, env: dict[str, str] | None = None) -> None:
        if not command or any(not isinstance(x, str) or not x for x in command):
            raise AcpClientError('ACP_INVALID_COMMAND')
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env, start_new_session=True, bufsize=0,
            )
        except (OSError, ValueError):
            raise AcpClientError('ACP_LAUNCH_FAILED') from None
        assert self.process.stdin is not None and self.process.stdout is not None
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)
        self.buffer = bytearray()
        self.byte_count = 0
        self.frame_count = 0
        self.closed = False

    def _count(self, size: int, *, frame: bool = False) -> None:
        self.byte_count += size
        self.frame_count += int(frame)
        if self.byte_count > MAX_SESSION_BYTES or self.frame_count > MAX_SESSION_FRAMES:
            raise AcpClientError('ACP_OUTPUT_LIMIT')

    @staticmethod
    def _ready(stream: Any, event: int, deadline: float) -> None:
        with selectors.DefaultSelector() as selector:
            selector.register(stream, event)
            if not selector.select(_remaining(deadline)):
                raise AcpClientError('ACP_TIMEOUT')

    def send(self, message: dict[str, Any], deadline: float) -> None:
        raw = (json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n').encode()
        if len(raw) > MAX_FRAME_BYTES:
            raise AcpClientError('ACP_FRAME_LIMIT')
        self._count(len(raw), frame=True)
        pending = memoryview(raw)
        try:
            while pending:
                self._ready(self.process.stdin, selectors.EVENT_WRITE, deadline)
                try:
                    written = os.write(self.process.stdin.fileno(), pending)
                except BlockingIOError:
                    continue
                pending = pending[written:]
        except (BrokenPipeError, OSError, ValueError):
            raise AcpClientError('ACP_EOF') from None

    def receive(self, deadline: float) -> dict[str, Any]:
        while b'\n' not in self.buffer:
            if len(self.buffer) >= MAX_FRAME_BYTES:
                raise AcpClientError('ACP_FRAME_LIMIT')
            self._ready(self.process.stdout, selectors.EVENT_READ, deadline)
            try:
                chunk = os.read(self.process.stdout.fileno(), min(8192, MAX_FRAME_BYTES - len(self.buffer)))
            except BlockingIOError:
                continue
            if not chunk:
                raise AcpClientError('ACP_EOF')
            self._count(len(chunk))
            self.buffer.extend(chunk)
        raw, _, tail = self.buffer.partition(b'\n')
        self.buffer = bytearray(tail)
        self._count(0, frame=True)
        try:
            message = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant,
                                 object_pairs_hook=_unique_keys)
        except (ValueError, UnicodeError, RecursionError):
            raise AcpClientError('ACP_MALFORMED_FRAME') from None
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            raise AcpClientError('ACP_MALFORMED_FRAME')
        return message

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # Kill the process group too: descendants can retain pipe descriptors.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue
            if sig == signal.SIGKILL:
                break
        self.process.stdin.close()
        self.process.stdout.close()


def _reject_constant(value: str) -> None:
    raise ValueError('non-JSON constant')


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


class GatewayClient:
    """Synchronous facade over cancellable HTTP, with an absolute I/O deadline.

    Only numeric HTTP loopback endpoints are used; localhost maps to 127.0.0.1
    without resolver work. Each request owns and closes its async client. There are
    no abandoned request threads. Hooks and injected transports are trusted
    application/test code; remote HTTP I/O runs inside asyncio's deadline.
    """

    def __init__(self, base_url: str, *, headers: dict[str, str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 5, trust_env: bool = False,
                 follow_redirects: bool = False) -> None:
        endpoint = httpx.URL(base_url)
        if endpoint.host == 'localhost':
            endpoint = endpoint.copy_with(host='127.0.0.1')
        try:
            address = ipaddress.ip_address(endpoint.host)
        except ValueError:
            raise AcpClientError('ACP_NUMERIC_LOOPBACK_REQUIRED') from None
        if (not address.is_loopback or '%' in endpoint.host or endpoint.scheme != 'http'
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                or endpoint.path not in ('', '/')):
            raise AcpClientError('ACP_NUMERIC_LOOPBACK_REQUIRED')
        # Numeric addresses avoid resolver executor workers during async cleanup.
        # Literal localhost is the sole supported alias, mapped without DNS.
        self.base_url = endpoint
        self.headers = headers or {}
        self.transport = transport
        self.timeout = timeout
        self.event_hooks: dict[str, list[Any]] = {'request': [], 'response': []}
        if trust_env or follow_redirects:
            raise AcpClientError('ACP_INVALID_HTTP_CONFIGURATION')

    def __enter__(self) -> GatewayClient:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def request(self, method: str, url: str, *, deadline: float | None = None,
                **kwargs: Any) -> httpx.Response:
        deadline = deadline if deadline is not None else time.monotonic() + self.timeout
        _remaining(deadline)
        response = asyncio.run(self._request(method, url, deadline, kwargs))
        for hook in self.event_hooks['response']:
            hook(response)
        _remaining(deadline)
        return response

    async def _request(self, method: str, url: str, deadline: float,
                       kwargs: dict[str, Any]) -> httpx.Response:
        try:
            async with asyncio.timeout(_remaining(deadline)):
                async with httpx.AsyncClient(base_url=self.base_url, headers=self.headers,
                        transport=self.transport, trust_env=False, follow_redirects=False,
                        timeout=min(self.timeout, _remaining(deadline))) as client:
                    request = client.build_request(method, url, **kwargs)
                    for hook in self.event_hooks['request']:
                        hook(request)
                    if ((request.url.scheme, request.url.host, request.url.port)
                            != (self.base_url.scheme, self.base_url.host, self.base_url.port)
                            or request.url.username or request.url.password or request.url.fragment):
                        raise AcpClientError('ACP_GATEWAY_DESTINATION_MISMATCH')
                    async with client.stream(method, str(request.url),
                            headers=request.headers, content=request.content) as response:
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > MAX_HTTP_BYTES:
                                raise AcpClientError('ACP_GATEWAY_OUTPUT_LIMIT')
                        return httpx.Response(response.status_code, content=bytes(body), request=request)
        except TimeoutError:
            raise AcpClientError('ACP_TIMEOUT') from None
        except httpx.HTTPError:
            raise AcpClientError('ACP_GATEWAY_UNAVAILABLE') from None

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('POST', url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request('GET', url, **kwargs)


class _BoundedGateway:
    """Apply one session deadline to every mediated gateway HTTP operation."""

    def __init__(self, http: GatewayClient, deadline: float) -> None:
        if not isinstance(http, GatewayClient):
            raise AcpClientError('ACP_DEADLINE_GATEWAY_REQUIRED')
        self.http, self.deadline = http, deadline

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.http.request('POST', url, deadline=self.deadline, **kwargs)

    def get(self, url: str) -> httpx.Response:
        return self.http.request('GET', url, deadline=self.deadline)


class _V1Mediator(AcpClientAdapter):
    """Reuse decisions/holds, then require matching v1 whole-content evidence."""

    def _submit_action(self, operation: str, resource: str, arguments: dict[str, Any],
                       **kwargs: Any) -> dict[str, Any]:
        data = super()._submit_action(operation, resource, arguments, **kwargs)
        action = data.get('action_request')
        receipt = data.get('execution_receipt')
        decision = data.get('policy_decision')
        if (not isinstance(action, dict) or not isinstance(receipt, dict)
                or not isinstance(decision, dict)
                or not isinstance(action.get('id'), str)
                or not _SAFE_ID.fullmatch(action['id'])
                or action.get('run_id') != self.run_id
                or action.get('operation') != operation or action.get('resource') != resource
                or action.get('arguments') != arguments
                or receipt.get('action_request_id') != action['id']
                or decision.get('action_request_id') != action['id']
                or receipt.get('operation') != operation or receipt.get('resource') != resource):
            raise AcpClientError('ACP_MISMATCHED_RECEIPT')
        if operation == 'read_text':
            sanitized = receipt.get('sanitized_result')
            if (not isinstance(sanitized, dict) or not isinstance(sanitized.get('content'), str)
                    or sanitized.get('truncated', False) is not False):
                raise AcpClientError('ACP_INCOMPLETE_READ_RECEIPT')
        return data

    def _poll_hold(self, action_id: str, initial_response: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(action_id, str) or not _SAFE_ID.fullmatch(action_id):
            raise AcpClientError('ACP_MISMATCHED_RECEIPT')
        data = super()._poll_hold(action_id, initial_response)
        if (not isinstance(data.get('action_request'), dict)
                or data['action_request'].get('id') != action_id):
            raise AcpClientError('ACP_MISMATCHED_RECEIPT')
        return data


class AcpSession:
    """One initialize/new/prompt exchange with bounded interleaved client requests.

    Use as a context manager. Timeout, malformed input, EOF, or interruption
    sends a best-effort cancel then terminates/reaps the peer and descendants.
    Cancelling the peer does not revoke a gateway HOLD already stored for review.
    """

    def __init__(self, command: list[str], adapter: AcpClientAdapter,
                 workspace: WorkspaceMapping, *, timeout_s: float = 30,
                 env: dict[str, str] | None = None) -> None:
        if not math.isfinite(timeout_s) or timeout_s <= 0 or timeout_s > 300:
            raise AcpClientError('ACP_INVALID_TIMEOUT')
        if not _SAFE_ID.fullmatch(adapter.run_id):
            raise AcpClientError('ACP_INVALID_RUN')
        self.command, self.adapter, self.workspace = command, adapter, workspace
        self.timeout_s, self.env = timeout_s, env
        self.transport: StdioTransport | None = None
        self.state = 'new'
        self.session_id: str | None = None
        self.summary = ''
        self.sequence = 0
        self.deadline = 0.0

    def __enter__(self) -> AcpSession:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self.transport is not None:
            if self.state == 'prompting':
                self.cancel()
            self.transport.close()
        self.state = 'closed'

    def cancel(self) -> None:
        if self.transport is not None and self.session_id is not None:
            try:
                self.transport.send({'jsonrpc': '2.0', 'method': 'session/cancel',
                                     'params': {'sessionId': self.session_id}}, time.monotonic() + .1)
            except AcpClientError:
                pass
            self.state = 'cancelled'
            # Give a cooperative peer a short bounded opportunity to observe
            # cancellation. Drain without ever accepting another file effect.
            grace = time.monotonic() + .1
            try:
                while True:
                    message = self.transport.receive(grace)
                    if (message.get('method') == 'session/request_permission'
                            and 'id' in message):
                        self.transport.send({'jsonrpc': '2.0', 'id': message['id'],
                            'result': {'outcome': {'outcome': 'cancelled'}}}, grace)
                    if 'result' in message or 'error' in message:
                        break
            except AcpClientError:
                pass

    def run(self, task: str) -> dict[str, Any]:
        if self.state != 'new':
            raise AcpClientError('ACP_INVALID_STATE')
        if not isinstance(task, str) or not task or len(task) > MAX_TASK_CHARS:
            raise AcpClientError('ACP_INVALID_TASK')
        self.deadline = time.monotonic() + self.timeout_s
        try:
            self.transport = StdioTransport(self.command, env=self.env)
            self.state = 'initializing'
            init = self._request('initialize', {
                'protocolVersion': PROTOCOL_VERSION,
                'clientCapabilities': {'fs': {'readTextFile': True, 'writeTextFile': True}, 'terminal': False},
                'clientInfo': {'name': 'scopewatch', 'version': '0.1.0'},
            })
            if type(init.get('protocolVersion')) is not int or init['protocolVersion'] != 1:
                raise AcpClientError('ACP_VERSION_MISMATCH')
            capabilities = init.get('agentCapabilities', {})
            if not isinstance(capabilities, dict):
                raise AcpClientError('ACP_INVALID_INITIALIZE')
            for key in ('promptCapabilities', 'mcpCapabilities', 'sessionCapabilities', 'auth'):
                if key in capabilities and not isinstance(capabilities[key], dict):
                    raise AcpClientError('ACP_INVALID_INITIALIZE')
            info = init.get('agentInfo')
            if info is not None and (not isinstance(info, dict) or any(
                not isinstance(info.get(k), str) or len(info[k]) > 128 for k in ('name', 'version')
            )):
                raise AcpClientError('ACP_INVALID_INITIALIZE')
            self.state = 'creating'
            result = self._request('session/new', {'cwd': str(self.workspace.root), 'mcpServers': []})
            sid = result.get('sessionId')
            if not isinstance(sid, str) or len(sid) > 4096:
                raise AcpClientError('ACP_INVALID_SESSION')
            self.session_id, self.state = sid, 'prompting'
            result = self._request('session/prompt', {'sessionId': sid, 'prompt': [{'type': 'text', 'text': task}]})
            if result.get('stopReason') not in STOP_REASONS:
                raise AcpClientError('ACP_INVALID_STOP_REASON')
            self.state = 'finished'
            return {'stopReason': result['stopReason'], 'session_id': sid,
                    'run_id': self.adapter.run_id, 'protocol_version': 1,
                    'schema_revision': SCHEMA_REVISION, 'coverage': COVERAGE_STATEMENT}
        except BaseException:
            self.close()
            raise

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        request_id = self.sequence
        self.transport.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}, self.deadline)
        while True:
            message = self.transport.receive(self.deadline)
            if 'method' in message:
                self._incoming(message)
                continue
            if (type(message.get('id')) is not int or message['id'] != request_id
                    or ('result' in message) == ('error' in message)):
                raise AcpClientError('ACP_INVALID_RESPONSE')
            if 'error' in message:
                raise AcpClientError('ACP_PEER_ERROR')
            if not isinstance(message['result'], dict):
                raise AcpClientError('ACP_INVALID_RESPONSE')
            return message['result']

    def _incoming(self, message: dict[str, Any]) -> None:
        method, params = message.get('method'), message.get('params')
        if (isinstance(method, str) and method.startswith('_')
                and 'id' not in message and 'result' not in message and 'error' not in message):
            return  # Unknown extension notifications have no effect channel.
        if (not isinstance(method, str) or not isinstance(params, dict)
                or 'result' in message or 'error' in message):
            raise AcpClientError('ACP_INVALID_REQUEST')
        notification = 'id' not in message
        if not notification and not (type(message['id']) is int or (
            isinstance(message['id'], str) and 0 < len(message['id']) <= 128
        )):
            raise AcpClientError('ACP_INVALID_REQUEST')
        # Filesystem notifications can never cause an effect. Updates are never
        # answered, including malformed updates, since notifications have no id.
        if notification:
            if method != 'session/update':
                raise AcpClientError('ACP_UNSUPPORTED_NOTIFICATION')
            self._check_session(params)
            update = params.get('update')
            if not isinstance(update, dict) or not isinstance(update.get('sessionUpdate'), str):
                raise AcpClientError('ACP_INVALID_UPDATE')
            if update['sessionUpdate'] == 'agent_thought_chunk':
                content = update.get('content')
                if not isinstance(content, dict) or content.get('type') != 'text' or not isinstance(content.get('text'), str):
                    raise AcpClientError('ACP_INVALID_UPDATE')
                self.summary = _bounded_text(self.summary + content['text'], MAX_SUMMARY_CHARS)
            return
        response = {'jsonrpc': '2.0', 'id': message['id']}
        try:
            self._check_session(params)
            if method == 'session/request_permission':
                result = {'outcome': {'outcome': 'cancelled'}}
            elif method in ('fs/read_text_file', 'fs/write_text_file'):
                result = self._file_request(method, params)
            else:
                raise AcpRpcError(-32601, 'Unsupported ACP client method')
            response['result'] = result
        except AcpRpcError as exc:
            # Do not forward receipt error detail, peer input, or trace-like text.
            # Keep only bounded identifiers needed to join gateway evidence.
            data = {k: v for k, v in exc.data.items() if k in (
                'action_id', 'reason_code', 'error_code', 'status', 'http_status'
            ) and (type(v) is int or (isinstance(v, str) and _SAFE_ID.fullmatch(v)))}
            response['error'] = {'code': exc.code, 'message': 'Gateway operation refused or failed', 'data': data}
        except AcpClientError:
            response['error'] = {'code': -32602, 'message': 'Invalid or unavailable ACP operation'}
        except Exception:
            response['error'] = {'code': -32603, 'message': 'Gateway mediation unavailable'}
        self.transport.send(response, self.deadline)

    def _check_session(self, params: dict[str, Any]) -> None:
        if self.state != 'prompting' or params.get('sessionId') != self.session_id:
            raise AcpClientError('ACP_WRONG_SESSION')

    def _file_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        relative = self.workspace.resource(params.get('path'))
        forwarded: dict[str, Any] = {'path': relative}
        if self.summary:
            forwarded['reasoning_summary'] = self.summary
        if method == 'fs/write_text_file':
            content = params.get('content')
            if not isinstance(content, str) or len(content) > MAX_RELAYED_FILE_CHARS:
                raise AcpClientError('ACP_INVALID_CONTENT')
            forwarded['content'] = content
        else:
            for key in ('line', 'limit'):
                value = params.get(key)
                if value is not None and (type(value) is not int or value < (1 if key == 'line' else 0) or value > 2**32 - 1):
                    raise AcpClientError('ACP_INVALID_READ_RANGE')
        left = _remaining(self.deadline)
        mediator = _V1Mediator(
            _BoundedGateway(self.adapter.http, self.deadline), self.adapter.run_id,
            requested_by=self.adapter.requested_by,
            hold=HoldConfig(timeout_s=min(left, self.adapter.hold.timeout_s),
                            poll_interval_s=min(.1, self.adapter.hold.poll_interval_s, left)),
        )
        result = mediator.dispatch_method(method, forwarded)
        if method == 'fs/write_text_file':
            return {}
        # ACP has no truncated flag: refuse a partial receipt, never disguise it
        # as a whole file. Range slicing is applied only after a successful read.
        if result.get('truncated'):
            raise AcpClientError('ACP_PARTIAL_READ')
        content = result['content']
        if params.get('line') is not None or params.get('limit') is not None:
            lines = content.splitlines(keepends=True)
            start = (params.get('line') or 1) - 1
            stop = None if params.get('limit') is None else start + params['limit']
            content = ''.join(lines[start:stop])
        return {'content': content}
