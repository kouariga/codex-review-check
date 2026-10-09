#!/usr/bin/env python3
"""Bundle the single observer source into a trusted-default-branch workflow."""
from pathlib import Path
import sys
import textwrap

HEADER = """# Generated from tools/codex_review_gate.py. Do not edit inline code.
name: Codex Review Check
on:
  workflow_call:
permissions:
  contents: read
  pull-requests: read
  issues: read
  checks: write
concurrency:
  group: codex-review-check-${{ github.event.pull_request.number || github.event.issue.number || github.event.inputs.pr }}
  cancel-in-progress: false
jobs:
  observe:
    if: >-
      (github.event_name == 'pull_request_target' && !github.event.pull_request.draft) ||
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'issue_comment' && github.event.issue.pull_request &&
       github.event.comment.user.id == 199175422 &&
       github.event.comment.performed_via_github_app.id == 1144995)
    runs-on: ubuntu-latest
    timeout-minutes: 25
    steps:
      - name: Observe metadata without executing PR code
        env:
          GITHUB_TOKEN: ${{ github.token }}
        shell: bash
        run: |
          python3 - <<'CODEX_GATE_PY'
"""


def render():
    source = Path(__file__).with_name("codex_review_gate.py").read_text()
    return HEADER + textwrap.indent(source, "          ") + "          CODEX_GATE_PY\n"


if __name__ == "__main__":
    output = render()
    if len(sys.argv) == 2:
        Path(sys.argv[1]).write_text(output)
    else:
        print(output, end="")
