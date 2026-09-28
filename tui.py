#!/usr/bin/env python3
"""YJL Linux TUI 入口。实现拆分在 yjl_tui/ 包内，此文件保持 run.sh / launch.sh 兼容。"""
from yjl_tui.tui import *  # noqa: F401,F403
from yjl_tui.tui import TUI, main  # noqa: F401  (TUI 为兼容再导出)

if __name__ == "__main__":
    raise SystemExit(main())
