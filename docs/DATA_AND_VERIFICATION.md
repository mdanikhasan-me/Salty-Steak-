# Data controls and executable verification

## Data & storage

Settings exposes two separately confirmed operations:

- **Delete chat sessions:** conversations, attachments, generated images, saved drafts,
  related operation records, and unchanged files whose creation is recorded by a
  conversation's automation audit. Explicitly saved global memories are retained.
- **Clear application data:** the above plus managed datasets/prepared data, caches,
  logs, generated automation files, stored data backups, and semantic/mission memory.
  The application, settings, runtimes, tokenizer and model weights remain.

This is ownership-based cleanup, not a drive wipe. External files are eligible only
with an audit of their creation and a matching content checksum. Edits to existing
user files, changed external outputs, links/junctions and protected models are excluded.
Externally configured bulk storage roots are rejected instead of swept recursively.

The server supplies a ten-minute, single-use preview token. Confirmation text must
match the chosen scope. A changed database/file inventory or newly saved memory
invalidates the preview. Active generation, automation and other operations block
cleanup. Same-volume staging permits rollback of ordinary file/transaction failures;
successful deletion is permanent. Old tabs cannot restore drafts across a cleanup
epoch. The UI requires reloading the fresh workspace afterward.

Limits: this does not promise forensic erasure on SSDs or removal of copies held by
external backup services. Legacy external outputs without ownership records remain.
Abrupt power loss during staged deletion and recovery of a partially completed
cleanup still need dedicated fault-injection acceptance before production release.
No production data is cleared merely by adding or testing these controls.

## Coding verification

Lock In checks generated artifacts using the installed toolchains in fresh work
folders. Model-generated plans map source files, provide test harnesses and declare
requirement coverage. Syntax/build success alone is not a behavioral pass. Logs,
exit codes, assertions and file hashes are recorded.

Invalid plans get one bounded plan correction before any source repair. Once a plan
is valid its requirements, tests and commands remain fixed across source repairs.
Python harnesses inherit only the generated project root as PYTHONPATH, not private
ambient import paths. Missing prerequisites remain **unverified**, rather than
triggering speculative source rewrites. No dependency is installed automatically.

Fresh work folders and bounded processes are not an operating-system sandbox.
Tests establish only the behaviors they actually exercise. A model critique is not
execution evidence, and a successful replay is not a fresh end-to-end model turn.
