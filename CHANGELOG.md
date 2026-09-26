# 更新日志

## 1.1.0（2026-09-25）

### 结构重构（分门别类）

- 单文件 `tui.py`（2680 行）拆分为 `yjl_tui/` 包：`paths.py`（路径与配置）、`probes.py`（系统探测）、
  `tcp_brutal.py`（TCP Brutal 修补）、`tui.py`（TUI 类与界面）、`doctor.py`（自诊断）；根目录 `tui.py`
  保留为入口薄壳，`run.sh` / `launch.sh` / 既有 API 兼容不变。
- `execute` 分发收敛为「kind → handler 注册表（按方法名惰性解析）→ tcp_profile → builtin」单一链路。
- 重复代码收敛：`pause()` / `ask()` / `restore_backup()` / `_run_captured()` 助手；删除死代码
  `find_local()` 与未使用的颜色对；`sing-box` 与 TUI 重复的 `systemctl is-active` 探测合并到 probes。

### 缺陷修复

- 修复 Windows 上 `os.geteuid` / `curses` 导致 import 即崩：现在 66+ 测试可在开发机全量运行。
- 修复 `scripts.json` 中 `nodeseek_window_tuning` 引用的 `scripts/nekoneko-tools.sh` 从未被 launch.sh
  下载的死菜单项，并新增「每个 local_script 必须在下载清单中」的防回归测试。
- 修复下载日志字面量 `\n`；修复 singbox_manager 端口输入崩溃与 `restore_full_backup` 非原子目录交换。
- 修复 `/etc/gai.conf`、`/etc/network/interfaces`、tcp.sh 补丁文件以 `errors="ignore"` 读写造成的
  非 UTF-8 字节静默丢失（统一 `surrogateescape`）。
- 域名选择超出范围改为显式提示；`valid_domain` 收紧（拒绝 `a..example.com`）；静态站点 web root
  拒绝落在 `/etc`、`/boot`、`/usr` 等系统目录；`_write_new_conf` 回退失败不再静默。

### 安全与健壮性

- `launch.sh`：下载完成后用根目录 `SHA256SUMS` 校验整个清单，安装改为 staging 组装 + 原子整体替换；
  默认去掉 `?v=时间戳` 缓存穿透（`YJL_TUI_CACHE_BUSTER` 可选）。清单再生成一键化：
  `bash scripts/regen-launch-manifest.sh`。
- 危险内置动作（`apt-get upgrade -y`、Swap 创建、`docker system prune --all`）执行前要求输入 yes；
  动作内 Ctrl-C / 异常只中断当前动作并返回菜单（异常屏障）。
- Docker 两个脚本：重启失败自动还原 daemon.json 并再次重启；`dockerhub-mirror.sh` 增加
  `DOCKERHUB_MIRROR_STATE_DIR` 空值/系统目录守卫；`docker-mirror-switch.sh` 兼容 busybox `timeout`。
- `tcp-brutal-manager.sh`：在线安装器成功失败都清理 `/tmp` 临时文件；OpenRC `rc-update add` 幂等。
- 补齐 subprocess 超时（kernel_manager、singbox_manager、TUI 全部探测类调用）；`listening()` 在缺少
  `ss` 时安全降级；`apache2ctl configtest` 返回值纳入判断。

### 工程化

- `launch.sh` 新增离线安装模式：`YJL_TUI_LOCAL_SOURCE=/path/to/dandan-tui bash launch.sh`
  直接从本地工程目录复制全部文件（不访问网络），SHA256SUMS 校验照常执行——用于 GitHub
  不可达或完全离线的 VPS。
- 新增 `tests/test_launch_e2e.py`：launch.sh 全链路端到端测试（本机 HTTP 真实下载 /
  离线目录 / 缺文件拒绝三场景），部署级回归有了守门测试，套件 98 → 101 项。
- curses 初始化失败（TERM 异常等）改为友好报错退出；singbox_manager 交互循环不再被
  磁盘/命令层的意外异常带走。
- `launch.sh` 对 `YJL_TUI_REF` 做合法性校验（只允许 ref 字符），杜绝拼进 URL 的畸形值；
  `--list` 按分类分组输出；pyflakes 静态清扫（无占位符 f-string、未用导入等）。
- 新增 `yjl-tui --doctor` 自诊断（Python 版本、配置完整性、local_script 存在性、launch 清单新鲜度、
  依赖命令、目录可写性）与 `--version`；`--check` 输出版本号。
- 新增 GitHub Actions CI（unittest + 全量 `bash -n` + 清单新鲜度）。
- 新增 `tests/test_doctor.py`、`tests/test_yjl_tui.py`、`tests/test_nginx_manager.py`
  （分发注册表、助手语义、nginx 解析与写入守卫），套件 66 → 98 项。
- 在真实 Debian 13 VPS 上全量验证：97 项单测（当时）、`--doctor` root 环境 0 败 0 警、
  launch.sh 原子安装模拟、curses 真实终端渲染；据此修复 `yjl_tui/doctor.py` 漏出 launch.sh
  FILES 清单的部署级缺陷，并新增「包内模块必须全部在清单中」防回归测试。
- 新增 MIT `LICENSE`（含第三方快照许可附录）、`.gitattributes`（统一 LF + 二进制标记）、
  `.gitignore` 补全、`scripts/geosite/release-pack.sh`（为 geosite 资产迁移 Release 准备）。

## 1.0.0（2026-09-13）

- 初始版本：scripts.json 数据驱动的 curses 菜单、内核维护、Nginx/SSL、网络、TCP 调优、tcp-brutal、
  Docker、fscarmen sing-box/WARP 本地快照、nft-forward、yjl-argo。
