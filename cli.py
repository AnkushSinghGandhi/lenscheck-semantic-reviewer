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
jobs:
  lenscheck:
    runs-on: ubuntu-latest
    steps:
      - uses: AnkushSinghGandhi/lenscheck-semantic-reviewer@v1
        with:
          # Request beta access, then add the token as a repo secret named LENSCHECK_API_KEY.
          api_key: ${{ secrets.LENSCHECK_API_KEY }}
"""


def cmd_init(argv):
    """Drop a ready-to-run GitHub Action into the current repo — the one-command way to adopt.

    Writes .github/workflows/lenscheck.yml (advisory: comments, never blocks). Refuses to clobber an
    existing file unless --force.
    """
    force = "--force" in argv
    path = WORKFLOW_PATH

    if os.path.exists(path) and not force:
        print(f"{path} already exists — re-run with --force to overwrite.")
        return 1

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(WORKFLOW_TEMPLATE)

    print(f"✓ wrote {path}  —  advisory (comments, never blocks)")
    print("\nNext:")
    print("  1. add your beta token as a repo secret named LENSCHECK_API_KEY")
    print("  2. commit it:   git add .github/workflows/lenscheck.yml && git commit -m 'add lenscheck'")
    print("  3. open a PR    — Lenscheck reviews it and comments automatically.")
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Lenscheck Semantic Reviewer")
        print("\nUsage: lenscheck <command> [options]")
        print("\nCommands:")
        print("  init      Add the GitHub Action to this repo (one-command setup)")
        print("  review    Semantic review of a PR / commit range (needs LENSCHECK_API_KEY)")
        print("  map       Map every endpoint in a repo, grouped by app, worst-first")
        print("  post      Post a review to a PR (sticky comment, inline comments, label)")
        print("  usage     Show your plan and reviews used this month")
        print("\nSet LENSCHECK_API_KEY (request access) and, if self-hosting, LENSCHECK_API_URL.")
        print("Run 'lenscheck <command> --help' for more information on a command.")
        sys.exit(0)

    command = sys.argv[1]
    # Remove the command from sys.argv so sub-parsers work correctly
    sys.argv.pop(1)

    if command == "init":
        sys.exit(cmd_init(sys.argv[1:]))
    elif command in ("review", "map", "usage"):
        import cloud
        if not cloud.have_token():
            sys.exit("lenscheck: no API key. Request beta access, then "
                     "`export LENSCHECK_API_KEY=sk_live_...` (self-hosting? also set LENSCHECK_API_URL).")
        getattr(cloud, command)(sys.argv[1:])    # thin client: extract locally, rank in the cloud
    elif command == "post":
        from ci import post_review                # engine-free GitHub poster (used by the Action)
        if len(sys.argv) == 1:
            sys.argv.append("--help")
        post_review.main()
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

if __name__ == "__main__":
    main()
