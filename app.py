"""根目录可运行入口：python3 app.py [serve|demo|check] ...

不带参数时启动 HTTP 服务（默认 http://127.0.0.1:8000）。
"""

import sys

from mutual_aid.cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.extend(["serve"])
    raise SystemExit(main())
