#!/usr/bin/env python3
"""
Lenscheck CLI Entrypoint
"""
import os
import sys

WORKFLOW_PATH = ".github/workflows/lenscheck.yml"

WORKFLOW_TEMPLATE = """\
# Added by `lenscheck init`. Reviews every PR and comments on it.
name: Lenscheck Semantic Review
on: pull_request
permissions:
  contents: read
  pull-requests: write     # to post the review comment
  issues: write            # to apply the lenscheck:* label
  security-events: write   # to upload SARIF (drop if you don't have GitHub Advanced Security)
jobs:
  lenscheck:
    runs-on: ubuntu-latest
    steps:
      - uses: AnkushSinghGandhi/lenscheck-semantic-reviewer@v1
        with:
          fail_on: {fail_on}          # advisory: comments, never blocks. Use `violation` to gate later.
"""


def cmd_init(argv):
    """Drop a ready-to-run GitHub Action into the current repo — the one-command way to adopt.

    Writes .github/workflows/lenscheck.yml in advisory mode (comments, never blocks). Refuses to
    clobber an existing file unless --force; --gate starts it blocking on a confirmed-rule violation.
    """
    force = "--force" in argv
    gate = "--gate" in argv
    fail_on = "violation" if gate else "none"
    path = WORKFLOW_PATH

    if os.path.exists(path) and not force:
        print(f"{path} already exists — re-run with --force to overwrite.")
        return 1

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(WORKFLOW_TEMPLATE.format(fail_on=fail_on))

    mode = "gating (blocks on a confirmed-rule violation)" if gate else "advisory (comments, never blocks)"
    print(f"✓ wrote {path}  —  {mode}")
    print("\nNext:")
    print("  1. commit it:   git add .github/workflows/lenscheck.yml && git commit -m 'add lenscheck'")
    print("  2. open a PR    — Lenscheck reviews it and comments automatically.")
    print("  3. (later) confirm your rules, then re-run `lenscheck init --gate` to block new violations.")
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Lenscheck Semantic Reviewer")
        print("\nUsage: lenscheck <command> [options]")
        print("\nCommands:")
        print("  init      Add the GitHub Action to this repo (one-command setup)")
        print("  map       Map every endpoint in a repo, grouped by app, worst-first")
        print("  review    Run a semantic review of a PR or commit range")
        print("  post      Post a review to a PR (sticky comment, inline comments, label)")
        print("  serve     Start the Lenscheck web UI")
        print("  invariants Discover baseline invariants from a repository's history")
        print("  digest    Org-wide leadership roll-up from the labels Lenscheck applies to PRs")
        print("\nRun 'lenscheck <command> --help' for more information on a command.")
        sys.exit(0)

    command = sys.argv[1]
    # Remove the command from sys.argv so sub-parsers work correctly
    sys.argv.pop(1)

    if command == "init":
        sys.exit(cmd_init(sys.argv[1:]))
    elif command == "map":
        import map_repo
        map_repo.main()
    elif command == "review":
        import diff_pr
        # No args → review the current repo's branch vs. its default branch (diff_pr resolves it).
        diff_pr.main()
    elif command == "post":
        from ci import post_review
        if len(sys.argv) == 1:
            sys.argv.append("--help")
        post_review.main()
    elif command == "serve":
        import serve
        serve.main()
    elif command == "invariants":
        import invariants
        if len(sys.argv) == 1:
            sys.argv.append("--help")
        invariants.main()
    elif command == "digest":
        import digest
        if len(sys.argv) == 1:
            sys.argv.append("--help")
        digest.main()
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

if __name__ == "__main__":
    main()
