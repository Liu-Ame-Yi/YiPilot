# Preserve complete Git evidence without mutating the index

Status: accepted

GitWorkspaceHarness must include tracked and untracked workspace changes in a deterministic, reusable patch without changing the source checkout or workspace index. Tracked evidence uses `git diff <base> --binary --no-ext-diff --no-textconv`; untracked paths are read with `git ls-files --others --exclude-standard -z`, sorted, diffed individually against the null device with `git diff --no-index --binary`, and appended. We rejected `git add`, `git add -N`, staged diff, and stash-based collection because they mutate Git state during evidence capture.

`changed-files.nul` is the authoritative machine-readable changed-path evidence. Git paths cannot contain NUL but can contain newlines, so each path is stored as its original bytes followed by one NUL; an empty path set is stored as `b""`. The canonical path tuple is sorted by raw bytes, and both tracked `git diff --name-only -z` output and untracked `git ls-files -z` output are read through `GitRunner.run_bytes()` before the tuple is built.

`changed-files.txt` remains an escaped, one-JSON-string-per-line human-readable view derived from the same canonical tuple. It is display-only and is never parsed to recover paths or make path-safety decisions. Evidence freshness compares `changed-files.nul` together with `status.txt`, `diff.patch`, `diff-stat.txt`, `changed-files.txt`, `head.txt`, and `base.txt`.

An existing six-file evidence bundle without `changed-files.nul` is legacy or incomplete. The Harness fails it explicitly and preserves the original files; it does not silently add, migrate, or overwrite evidence. Stale bundles are likewise preserved. Git index collection remains side-effect-free.

`diff.patch` is a byte-preserving artifact rather than guaranteed UTF-8 text so binary and non-UTF-8 text remain applyable; the remaining text views are UTF-8. A no-index return code of 1 is accepted only for the controlled two-path form when Git produced diff output. External diff drivers and text conversion are disabled, ignored files stay excluded, and future callers that read arbitrary blob content must use `GitRunner.run_bytes()`.
