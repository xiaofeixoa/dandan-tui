"""YJL Linux TUI 内部实现包，按职责分模块：

- ``yjl_tui.paths``      路径常量、版本号与 scripts.json 读取
- ``yjl_tui.probes``     只读的系统 / CPU / 虚拟化探测
- ``yjl_tui.tcp_brutal`` TCP Brutal 配置与订阅修补（纯文本处理，无 TUI 依赖）
- ``yjl_tui.doctor``     ``--doctor`` 自诊断
- ``yjl_tui.tui``        菜单、动作执行与 curses 界面
"""
from .paths import VERSION

__version__ = VERSION
