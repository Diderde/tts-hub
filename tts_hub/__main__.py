# Copyright (C) 2026 Diderde
# SPDX-License-Identifier: MIT
"""``python -m tts_hub`` 入口，等价于 ``tts-hub`` 命令。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
