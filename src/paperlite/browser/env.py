"""
paperlite/browser/env.py
────────────────────────
启动浏览器进程用的环境变量。session.py 和 turnstile.py 都要用，单放一处（两者不再互相 import）。
"""

import os


def browser_env() -> dict[str, str]:
    # macOS 上 DYLD_LIBRARY_PATH=/opt/homebrew/lib 会让 Chromium 启动即 SIGBUS，浏览器进程一律剔除 DYLD_*
    return {k: v for k, v in os.environ.items() if not k.startswith("DYLD_")}
