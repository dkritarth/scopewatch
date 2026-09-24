# Agent run logs

One short file per long agent session: `<yyyy-mm-dd>-<topic>.md`. The next orchestrator reads the newest one first.

Suggested sections:

- **Goal:** milestone or issues targeted.
- **Threads:** role, tool/model, target, outcome.
- **Merged:** PR numbers.
- **Broke or blocked:** what failed and where it is tracked.
- **Decided on the fly:** choices the issues did not specify.
- **Next:** what to pick up first.

Keep secrets, private traces, and raw model output out of these files.
