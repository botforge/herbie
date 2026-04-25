# Project rules for the coding agent

## Comments and code must stay consistent

Whenever you edit code, re-read every nearby comment, docstring, and
referenced file/variable name in the same change. If the edit makes any
of them stale — even partially — update them in the same edit. Do not
leave comments that describe behavior the code no longer exhibits, or
references to files, fixtures, or fields that were renamed or removed.

This applies equally to:
- inline comments and block comments
- module and function docstrings
- test narration ("this step asserts X")
- identifiers referenced by name inside strings or comments (slugs,
  file_ids, fixture paths, env var names)

If a comment would need to be rewritten to stay true after your change,
rewrite it. If it no longer adds value, delete it. Silent drift between
prose and code is a defect.

## Comments read as a numbered instruction manual

Structure comments as a numbered, nested walkthrough so the reader can
follow the code like a story:

    1. First step / phase of the algorithm or test
       1A. Sub-step
       1B. Sub-step
    2. Next phase
       2A. ...

Why: dry "this function does X" statements force the reader to
re-derive the flow themselves. The user wants to be told "start here,
then do this, then this" in the order the code actually runs.

How to apply:
- Tests: each logical phase (setup, action, assertion group) is a
  numbered comment; individual sub-actions or sub-assertions inside a
  phase get letters.
- Service / library code: the module or function docstring walks the
  end-to-end flow top-to-bottom; inline comments follow the same
  numbering scheme when the body is long enough to warrant it.
- Do NOT write one-liners like "validates input" or "returns the feed".
  Write "1. Pull the sidecar from disk. 2. If it's missing, return
  early. 3. Otherwise, ..."
- Favor narrative and order over description.
