# ACP v1 schema fixture

Retrieved 2026-10-28 by GPT-6.1 Sol from the official repository:

https://raw.githubusercontent.com/agentclientprotocol/agent-client-protocol/d6f00fd0ce5323be07f64fca09f59f26a3a5d77a/schema/v1/schema.json

Original artifact SHA-256:
`1c57095acf9c0844054dd1fdc3624c121b3fdc24209ecc987fd6748c705fdef5`.

`schema.json` is a subset of that artifact: initialize, new session, prompt,
session notification, filesystem read/write, permission, cancellation, and their
transitive `$defs`. Definitions are copied without edits. The comment and top-level
wrapper identify the subset; its checksum differs from the original artifact.
The tests run offline and never fetch protocol sources.

Original schema license: Apache-2.0, per the official repository's LICENSE.
