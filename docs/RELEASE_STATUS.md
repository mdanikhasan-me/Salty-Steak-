# Release status — 8 October 2026

**r211 has passed prelaunch checks; it is not installed.** Production remains r208.
The owner's full priorities and constraints remain in `PERSONAL_ASSISTANT_ROADMAP.md`.

## Completed checks

- Final source: 1,707 backend tests passed, 8 skipped; 325 frontend tests passed;
  production frontend build passed.
- Actual full-app local27B coding: generated16-test suite passed; independent14-case
  replay passed. A separate fresh model-authored source/test-plan run passed plus31
  independent cases. Final source also reports actual host execution after drafting.
- Actual full-app research: RFC, Python TaskGroup and NASA answers passed fact and
  citation-membership checks using the application's own search/retrieval pipeline.
  The original RFC source contains both requested facts and is cited for both.
- Data controls: separate chat/all-data scopes, model preservation, stale-preview and
  stale-tab protection, recovery from10 abrupt-process exit cases, and disposable HTTP
  deletion/reopen tests. No production chats or model weights were cleared.
- Packaged runtime:12 disposable checks passed, including actual code execution and
  both data-clear scopes. Packaged browser helper:14 action checks passed.
- Initial independent r211 audit:15,572 files /5,117,018,913 bytes checked; no missing,
  extra, mismatched or forbidden bytecode files. Repository import isolation passed.
  The post-test integrity audit also passed, confirming those checks did not mutate
  the sealed package.

Candidate: `D:/Salty Steak/dist/Salty-Steak-native-r211`

Build ID: `2.0.0+20261008.native-r211`

Frozen source SHA-256:
`b127323ef40c41fd6dd151bdf3c0e0b62c56d14fd6580842b1bf120a6898c191`

Local evidence: `validation/RELEASE_R211.md`, `r211-audit.json`,
`r211-disposable-check.json`, `agent-bottlenecks/action-matrix-r211-packaged-20261008.json`,
`code-quality/full-current-app-20261008-score.json`,
`research-coverage/full-current-app-20261008-score.json` and its audit report.
Validation artifacts and private workspaces are deliberately not uploaded to Git.

## Not accepted or complete

- Installed native UI, shortcut routing, production data-preservation and postlaunch
  checks. Windows Computer Use still fails with missing native pipe OS error2. Client
  reset/reimport and plugin discovery found no available reconnection operation. Browser
  and API acceptance are not substitutes for installed/native acceptance.
- Old r209 candidate cleanup: deletion was blocked by execution controls; files remain.
  No attempt was made to bypass the rejection using a different deletion mechanism.
- Performance: the full Lock In clamp run took1,793.3 seconds; research cases took
  approximately157–347 seconds. These are measured limitations, not acceptable-speed
  or competitor-parity claims.
- RFC coverage-model review returned invalid JSON after bounded correction. Its final
  answer passed source checks, but that does not turn the failed review into a pass.
- Image-only PDF OCR, universal website access, broad unseen-task reliability and future
  model fine-tuning are not claimed complete.

The candidate must pass remaining native and cutover gates before replacement of r208.
