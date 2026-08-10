# Preserve complete Git evidence without mutating the index

Status: accepted

GitWorkspaceHarness must include tracked and untracked workspace changes in a deterministic, reusable patch without changing the source checkout or workspace index. Tracked evidence uses `git diff <base> --binary --no-ext-diff --no-textconv`; untracked paths are read with `git ls-files --others --exclude-standard -z`, sorted, diffed individually against the null device with `git diff --no-index --binary`, and appended. We rejected `git add`, `git add -N`, staged diff, and stash-based collection because they mutate Git state during evidence capture.

`diff.patch` is a byte-preserving artifact rather than guaranteed UTF-8 text so binary and non-UTF-8 text remain applyable; the other five evidence files remain UTF-8 text. A no-index return code of 1 is accepted only for the controlled two-path form when Git produced diff output. External diff drivers and text conversion are disabled, ignored files stay excluded, and future callers that read arbitrary blob content must use `GitRunner.run_bytes()`.
