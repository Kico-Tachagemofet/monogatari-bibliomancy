# Implementation notes

## Deviations
- The requested review edition is private and includes the 18 EPUBs used by the current corpus. The other 14 volumes remain out of scope. A future public edition must be created separately without book files or their Git history.
- The original material-only skill says source files are not in the repository. Its prerequisite statement will be corrected for this private distribution; interpretation rules stay unchanged.

## Discovered edge cases
- Card status values must remain byte-identical, including three legacy approval markers. Privacy review must distinguish these immutable machine values from prose.
- The existing reader emits following-page context despite the skill's information boundary. The independent reader will emit preceding context only.

- A translated arc title is not always a spelling variant: matching remains explicit via aliases; unknown names fail closed.
- The mixed original folder contains 14 unregistered books. An explicit file selection controls scanning without becoming an identity heuristic.
- EPUB3 navigation and several anchors within one XHTML are covered by synthetic tests.
- Local alias titles must be omitted from body text as well as used for boundary matching.
- A proposed cleanup of private-name checks was rejected by automatic approval review. No such removal was applied; original checks remain with a regression test.

## Questions for review
- None

## Completion summary
Deviations: 3 (private bundled edition, prerequisite wording, suppressing following-page context).
Most likely to revisit: publish a separate clean-history edition without books.
Edge cases: 7 documented above; semantic order also prevents filename-dependent page numbering.
Validation: 31 synthetic tests, also passed without real EPUBs; all 2471 reference pages and lookup 1945 match.
Read first next session: README.md and REVIEW.md.
