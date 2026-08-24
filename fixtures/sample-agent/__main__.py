"""``python fixtures/sample-agent/`` entry point (smoke invocation).

Runs the agent as a subprocess entrypoint. The engine's smoke gate and runs
invoke ``[python, fixtures/sample-agent]``, so this file must exist and be
self-contained: it only needs to import ``agent`` and run ``main``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import main

sys.exit(main())
