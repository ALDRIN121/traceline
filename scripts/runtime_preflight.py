#!/usr/bin/env python3
"""Print the T12a rootless Podman setup preflight as JSON."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm_agent_eval.runtime.podman import preflight  # noqa: E402


def main() -> int:
    result = preflight()
    print(json.dumps(result.to_dict(), sort_keys=True))
    return 0 if result.state == "ready_for_containment_experiment" else 1


if __name__ == "__main__":
    raise SystemExit(main())
