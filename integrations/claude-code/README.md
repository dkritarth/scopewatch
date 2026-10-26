# Claude Code permission preflight (experimental)

This is an executable **PreToolUse permission hook**, based on the [official
Claude Code hook protocol](https://code.claude.com/docs/en/hooks). It implements
issue #291's narrow preflight path. It is not a routed executor or proof of a
live external-agent run. No Claude session or provider call was run for this
change; the CLI is unavailable in the implementation environment.

## Exact coverage

| Native tool | Mapping | Hook result |
| --- | --- | --- |
| Read with file_path | workspace.read_text | Preview DENY → deny; ALLOW/HOLD → ask |
| Write with file_path and content | workspace.write_text | Preview DENY → deny; ALLOW/HOLD → ask |
| Edit, Bash, Glob, Grep, web, task, MCP and other tools | Unsupported, no translation | deny without contacting gateway |
| Malformed input, unavailable gateway, unknown outcome, escaping path | Unsupported/failure | deny |

The hook never prints `permissionDecision: allow`, never executes tools,
never calls the mutating `/actions` endpoint and never consumes an approval.
The preview creates no action, receipt, approval or reasoning-audit evidence.
A human answering the native ASK is **not** granting a gateway approval.
The native tool subsequently runs in Claude's workspace outside the gateway;
the gateway's isolated run workspace is used for scope preflight only. They
may differ in bytes/symlinks and can change between preview and native use.
Thus this does not prove evidence-before-effect, single-use authorization or
execution confinement for native tools. Do not demonstrate it as such.

No reasoning trace is exposed by this tool-hook input. The request explicitly
uses UNAVAILABLE provenance, does not read transcript_path and never infers
intent from a README. An in-scope injected README read returns ASK, not an
invented “injection detected” HOLD. A real reasoning-evidence/routed-tool demo
must use the gateway agent/tool submission path and record its actual audit
and receipt. Required-reasoning scope rules are tracked separately in #295;
this hook's preview must not be presented as a reasoning-audit substitute.

## Operator setup for an actual session

1. Use a disposable synthetic workspace only. Start the mock gateway with
   `./scripts/run_demo.sh` and create/select a run whose allowed paths match
   that workspace. Ensure `synthetic.env` is a blocked path and docs is an
   allowed directory. The gateway workspace is an isolated copy, not the host
   checkout. Do not point the demonstration at private files.
2. In a shell with the repository's Python environment active, export:

   ```bash
   export PYTHONPATH="$PWD/backend"
   export SCOPEWATCH_HOOK_GATEWAY=http://127.0.0.1:8000
   export SCOPEWATCH_HOOK_RUN_ID=replace-with-created-run-id
   export SCOPEWATCH_HOOK_WORKSPACE=/absolute/path/to/synthetic-native-workspace
   ```

   `SCOPEWATCH_HOOK_WORKSPACE` must be an existing directory: the hook
   resolves it strictly and denies the action if the path is missing.
   Only loopback HTTP gateway URLs are accepted. Existing DEMO_TOKEN is sent
   only as an HTTP header if needed and never included in output. Use the same
   Python executable in the settings command as the environment containing
   httpx; replace `python3` with that executable if necessary.
3. Copy the example hook object into the disposable workspace's
   `.claude/settings.local.json` (merge with existing settings deliberately).
   The matcher covers **all** tools, so unsupported operations refuse rather
   than silently bypass. Restart Claude Code and verify the hook in `/hooks`.
   Do not use an unsafe permission bypass mode. User edits/removal of hooks,
   execution outside Claude, tools not routed through this hook, or failure to
   launch the configured interpreter are outside this protection. Hook command
   failure/timeouts may be handled differently by Claude versions; verify the
   active client does not silently proceed. Python-level errors are converted
   to valid deny JSON when this process runs.
4. First run the hook contract manually:

   ```bash
   printf '%s\n' '{"hook_event_name":"PreToolUse","tool_name":"Read","tool_input":{"file_path":"synthetic.env"}}' | python3 -m scopewatch.claude_hook
   ```

   Expect a PreToolUse deny. A docs/README.md read should ASK, never auto-allow.
   Unsupported Bash should deny. Stop setup if the installed client does not
   honor these controls. This command performs preflight, not execution.
5. In a real operator-owned Claude session, request a read of synthetic.env,
   then an allowed README read. Record the native refusal/prompt and gateway
   preview response separately. Include tool/client version, scope, exact
   supported coverage and absent execution receipts. An injection test may
   only claim reasoning detection when actual reasoning evidence and a real
   submit-time audit are available. Filming and external-agent/provider
   evidence remain open acceptance items under #291, not completed evidence.

## Automated checks

`PYTHONPATH=backend python3 -m pytest backend/tests/test_claude_hook.py -q`
checks protocol mapping, unsupported tools, escape/symlink refusal, HTTP and
transport failure, bounded stdin and real gateway deterministic preview. A
synthetic secret read is denied; allowed/injected README content is never
claimed audited, no execution occurs and the event list stays unchanged.
