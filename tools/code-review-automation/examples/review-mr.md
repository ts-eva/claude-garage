---
description: Prepare draft reviews of GitLab MRs, run by code-review-automation
---
# Review MRs: $ARGUMENTS

This runs in the background from a scheduled job, so there's no one to answer questions mid-run.
Don't wait for input. If something is unclear, say so in the final summary instead of guessing.

Findings go out as **draft comments**: GitLab keeps them private until the reviewer reads them
and clicks Submit review. The one thing this pass does publicly is resolve the reviewer's own
threads once new code fixes them. It never approves or merges (those commands are blocked).

## Context

The last argument above is the path of a JSON file for this pass. Read it first (with the
Read tool). It has:
- `project_id`
- `helper`: the command for saving drafts and resolving threads (written `<helper>` below)
- for each MR: `iid`, `web_url`, `head_sha`, and `last_reviewed_head` (the head your last
  finished pass reviewed, or `null` if none has yet)

## For each MR

1. Read the description and existing threads: `glab mr view <iid> --comments`.
   Also list drafts already waiting from earlier passes, so you don't repeat one:
   `glab api "projects/<project_id>/merge_requests/<iid>/draft_notes"`.
2. Get the code to review:
   - First time (`last_reviewed_head` is null): the whole MR, `glab mr diff <iid>`.
   - Otherwise only what changed since then:
     `glab api "projects/<project_id>/repository/compare?from=<last_reviewed_head>&to=<head_sha>"`.
     After a rebase this also includes the target branch's changes, so compare file by file
     against `glab mr diff <iid>` and skip anything the MR itself didn't change.
3. Threads you started earlier: if the new code fixes one, resolve it:
   `<helper> resolve <project_id> <iid> <discussion_id>`.
   Only resolve when the code change itself fixes it, not because someone replied. A reply
   with a question or pushback stays open for a human. The helper refuses threads started by
   anyone else.
4. Review against the checklist below. Read surrounding code before calling something a bug.
5. Save each finding as its own draft:
   - On a line: `<helper> draft <project_id> <iid> --path <file> --line <new line number> --body "<comment>"`
     (use `--old-line` instead of `--line` for a removed line)
   - General: `<helper> draft <project_id> <iid> --body "<comment>"`

## What to look for

<!--
Write your own list here. This is the part that makes your reviews yours.
If every reviewer on a team uses the same list, everyone misses the same bugs.
Ideas to start from: the bugs you have caught by hand before, the mistakes you make yourself,
the parts of this codebase that break most often.
-->

- ...

## How comments should read

<!-- How you want your comments worded. Leave it empty to let Claude choose. -->

## Final output

One line per MR: `!<iid>: <n> drafts saved, <n> threads resolved` or `!<iid>: nothing new`.
