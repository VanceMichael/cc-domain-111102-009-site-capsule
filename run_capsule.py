#!/usr/bin/env python3 -I
"""隔离模式启动器：把项目根注入 sys.path 后调用 CLI。

用法（从项目根）：
    python3 -I run_capsule.py archive --config fixtures/demo-scope.json \
        --output runtime/demo --replay fixtures/demo-routes.json
    python3 -I run_capsule.py check --output runtime/demo
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
