#!/usr/bin/env python3
"""Launch a locally installed ACP v1 agent for an existing synthetic run.

No bridge installation, inference, server startup, credential-file loading,
transcript logging, or guarded file I/O is performed by this script.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

# Allow direct execution from any directory without installing Scopewatch.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))


from scopewatch.acp_adapter import AcpClientAdapter, HoldConfig
from scopewatch.acp_client import AcpClientError, AcpSession, COVERAGE_STATEMENT, WorkspaceMapping, _BoundedGateway, GatewayClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gateway-url', required=True, help='Existing loopback gateway URL')
    parser.add_argument('--run-id', required=True, help='Existing active synthetic run ID')
    parser.add_argument('--workspace', required=True, type=Path, help='Absolute local session root mapped to this run')
    parser.add_argument('--synthetic-task', required=True, help='Synthetic task text only')
    parser.add_argument('--synthetic', required=True, action='store_true', help='Confirm workspace and task are synthetic')
    parser.add_argument('--timeout', type=float, default=30, help='Whole session deadline, at most 300 seconds')
    parser.add_argument('--hold-timeout', type=float, default=10, help='Maximum wait for gateway approval')
    parser.add_argument('--agent-env-name', action='append', default=[], help='Explicit environment variable name to pass to agent; never a value')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='-- /absolute/path/to/pinned-acp-agent [arguments]')
    args = parser.parse_args(argv)
    try:
        url = urlsplit(args.gateway_url)
        if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
                or url.username or url.password or url.query or url.fragment or url.path not in ('', '/')
                or not math.isfinite(args.hold_timeout) or not 0 < args.hold_timeout <= 300):
            raise AcpClientError('ACP_INVALID_CONFIGURATION')
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        if not command or not Path(command[0]).is_absolute():
            raise AcpClientError('ACP_AGENT_EXECUTABLE_MUST_BE_ABSOLUTE')
        # Gateway credentials never enter argv or the child environment.
        env = {name: os.environ[name] for name in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR') if name in os.environ}
        for name in args.agent_env_name:
            if (not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name)
                    or name.startswith('SCOPEWATCH_') or name == 'DEMO_TOKEN'
                    or name not in os.environ):
                raise AcpClientError('ACP_INVALID_AGENT_ENVIRONMENT')
            env[name] = os.environ[name]
        headers = {}
        if os.environ.get('SCOPEWATCH_API_KEY'):
            headers['X-API-Key'] = os.environ['SCOPEWATCH_API_KEY']
        if os.environ.get('DEMO_TOKEN'):
            headers['X-Demo-Token'] = os.environ['DEMO_TOKEN']
        with GatewayClient(base_url=args.gateway_url, headers=headers, timeout=5, trust_env=False,
                          follow_redirects=False) as http:
            adapter = AcpClientAdapter(http, args.run_id, hold=HoldConfig(timeout_s=args.hold_timeout))
            workspace = WorkspaceMapping(args.workspace)
            with AcpSession(command, adapter, workspace, timeout_s=args.timeout, env=env) as session:
                response = _BoundedGateway(http, time.monotonic() + 5).get(f'/api/v1/runs/{args.run_id}')
                if response.status_code != 200:
                    raise AcpClientError('ACP_RUN_UNAVAILABLE')
                run = response.json()
                if not isinstance(run, dict) or run.get('synthetic') is not True or run.get('status') != 'ACTIVE':
                    raise AcpClientError('ACP_RUN_NOT_ACTIVE_SYNTHETIC')
                # Emit coverage before launching the peer or submitting effects.
                # Redirect stdout to a private JSONL artifact to retain both records.
                print(json.dumps({'event': 'acp_session_start', 'run_id': args.run_id,
                                  'coverage': COVERAGE_STATEMENT}, sort_keys=True), flush=True)
                print(json.dumps({'event': 'acp_session_complete',
                                  **session.run(args.synthetic_task)}, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({'error': 'ACP_INTERRUPTED', 'coverage': COVERAGE_STATEMENT}), file=sys.stderr)
        return 130
    except AcpClientError as exc:
        print(json.dumps({'error': str(exc), 'coverage': COVERAGE_STATEMENT}), file=sys.stderr)
        return 1
    except Exception:
        # CLI never prints exception messages, provider output, or stack traces.
        print(json.dumps({'error': 'ACP_CLIENT_FAILED', 'coverage': COVERAGE_STATEMENT}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
