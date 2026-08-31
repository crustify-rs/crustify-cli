Audit the Rust crate in `{workspace}` for undefined behavior reachable from
safe code. The repository root is the subject; locate the
Rust within it yourself.

## Objective

`{objective}`:

- `audit`: investigate and report; do not modify the target.
- `audit+patch`: investigate, report, and repair confirmed findings.
- `patch`: repair the confirmed findings already in `advisories/`; do not hunt
  for new ones.
- `revisit`: re-investigate the leads named in your workset; do not hunt for
  new ones and do not modify the target.

## Workset

{workset}

## Instruments

{instruments}

For an auditing objective, confine the hunt to bugs that one of these
instruments can demonstrate. A confirmed reproducer must trigger at least one
of them. The bug-class lists are the hunt scope: do not spend the run on other
classes merely because they are also undefined behavior.

## Audit record

Use `{workspace}/crustify/audit/`:

- `leads/`: one Markdown file for every candidate you investigate, including
  candidates you clear.
- `advisories/`: one directory per confirmed bug with same stem containing
  the reproducer and a `report.md` with the advisory.
- `scratch/`: disposable investigation files.

Read existing leads and advisories first. Do not duplicate completed work.

## Evidence

A finding is confirmed only when safe code can trigger undefined behavior in
the real audited crate. Write a minimal reproducer that depends on that crate,
calls its public API without using `unsafe`, and triggers at least one selected
instrument above.

Store a confirmed reproducer in `advisories/<name>/` with everything needed to
run it from a clean checkout. The advisory must identify the safe path to the
bug and include the exact command and relevant instrument output. If you cannot
produce this evidence, record the result as a lead, not an advisory.

## Revisit

Only when your objective is `revisit`. Your workset names lead files, not source
files. A lead is a question an earlier run could not settle, and your job is to
settle it. For each lead that you manage to reproduce with a sanitizer crash,
promote it to and advisory and delete the lead.

## Repair

Only when your objective includes patching. Under `audit`, an advisory is
finished when it is written.

Work in a git worktree, never in the checkout. Follow the repository's
contributor instructions, make the smallest sound fix with focused regression
tests, run the relevant project checks, and rerun the original reproduction.
Commit the repair in the worktree. Do not merge, do not push, and do not remove
the worktree. Record the worktree, branch, commit, commands, and results in the
advisory.

## Avoid leaks

Do not include any API keys in core dumps or any other reproducer artifacts
as we might be making the audit tree open source.

## Draft disclosure notice

Read the affected repo's conventions for disclosing security / UB issues and
draft a disclosure notice that the user could send outside the box without
further adjustments. If it should be first sent via email, use an email-friendly
format, and name the maintainers to which it should be disclosed. Keep disclosure
notices under 450 words. Only offer to provide fix patches or PRs when your objective
included that, otherwise we only disclose. Mentioned reproducers are only provided
on request but add in the disclosure a small snippet and the santizer crash.

Add the following note to the disclosure: `Found by [Crustify](https://github.com/crustify-rs/crustify-audit),
an experimental UB/soundness auditing agent developed at UC Berkeley and running on <model name>,
then manually reviewed and independently reproduced.`