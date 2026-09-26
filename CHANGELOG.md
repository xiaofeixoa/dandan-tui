# 更新日志

## 1.2.0（2026-09-27）

### 仓库与自包含

- 主仓库迁移至 `xiaofeixoa/dandan-tui`：launch.sh、WARP 升级地址、geosite 回退源、
  nft-forward 安装器的自引用全部指向新仓库，并新增运行时无旧仓库 URL 的守门测试。
- `nginx-ui` 网页面板改为仓库内固定快照（`tools/nginx-ui/`，v2.5.7 三架构归档 +
  服务文件 + 改造安装器，SHA256SUMS 校验、`NGINX_UI_LOCAL_SOURCE` 离线安装）。

### 菜单与脚本

- 新增「nodeseek合集」分类：收录 NodeSeek 社区合集帖的 37 个在线脚本入口（DD 重装、
  综合与性能测试、流媒体与 IP 质量、测速、回程、功能与环境脚本、杜甫检测），
  已剔除失效来源（bench.im/git.io/ghproxy/DNS-Alice-Unlock）。
- online 动作支持 `prompt_args` 执行前参数输入；DD 重装改为交互式向导
  （分步选择系统/版本、密码 getpass、汇总打码、输入 DD 才开始）。
- 全量审计 62 个 online 动作的交互形态并以
  `test_online_actions_declare_interaction_model` 固化，未声明的新动作会被 CI 拒绝。

### 菜单可用性

- TUI 内按 `/` 跨全部分类搜索动作（匹配 id/标题/描述），结果挂到虚拟分类下选择执行。
- 新增「最近使用」虚拟分类：自动记录动作使用次数与时间（`STATE/usage.json`，
  原子写入），按最近时间倒序置顶展示，文件损坏自动重置。

### CI

- shellcheck 任务（自维护脚本零告警，vendored 快照豁免）。
- windows-latest 平台回归任务：全量单测在 Git Bash 下运行（PYTHONUTF8 处理编码、
  /usr/bin 工具路径、按模块重试 + continue-on-error 抗 runner 抖动）。

### 修复

- launch.sh 的 python3 探测与 run.sh 对齐（先验证可执行再用）。
- dockerhub-mirror/tcp-brutal-manager/yjl-argo/nft-forward 全部 shellcheck 告警清零
  （SC1090/SC1091/SC2015/SC2086/SC2034）。

## 1.1.0（2026-09-25）

> 仓库迁移：本项目的主仓库现为 `xiaofeixoa/dandan-tui`，launch.sh、WARP 升级地址、
> geosite 回退源、nft-forward 安装器内的自引用已全部指向新仓库。

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

- 自包含化：`nginx-ui` 网页面板改为仓库内固定快照（`tools/nginx-ui/`，v2.5.7 三架构归档 +
  服务文件 + 改造后安装器，SHA256SUMS 校验、支持 `NGINX_UI_LOCAL_SOURCE` 离线安装），
  运行时不再调用 `dandan8511` 的任何工程；新增守门测试断言运行时文件无旧仓库 URL。
- online 动作支持 `prompt_args` 执行前参数输入（shlex 解析、支持引号；留空/解析失败
  回退为无参数运行）。
- 全量审计 62 个 online 动作的交互形态（59 个脚本自动分类 + 4 个存疑项人工核实）：
  哪吒补 `prompt_args`（agent 交互安装入口）、chsrc 描述澄清；新增
  `test_online_actions_declare_interaction_model` 守门测试，未声明交互形态的新动作会被 CI 拒绝。
- DD 重装改为交互式向导：分步选择目标系统/版本（leitbogioro 支持 Debian/Ubuntu/
  Windows/CentOS/RockyLinux/AlmaLinux，moeclub 支持 Debian/Ubuntu/CentOS）、新密码
  getpass 输入、SSH 端口与 -firmware 选项；汇总预览（密码打码）后需输入 `DD` 才开始，
  执行阶段展示不带密码。fcurrk 的 NewReinstall 自带菜单直接运行。
- 新增「nodeseek合集」分类：收录 NodeSeek 社区合集帖的 37 个在线脚本入口
  （DD 重装、综合/性能测试、流媒体与 IP 质量、测速、回程、功能与环境脚本、杜甫检测），
  已剔除失效来源（bench.im/git.io/ghproxy/DNS-Alice-Unlock）。
- `launch.sh` 新增离线安装模式：`YJL_TUI_LOCAL_SOURCE=/path/to/dandan-tui bash launch.sh`
  直接从本地工程目录复制全部文件（不访问网络），SHA256SUMS 校验照常执行——用于 GitHub
  不可达或完全离线的 VPS。
- 新增 `tests/test_launch_e2e.py`：launch.sh 全链路端到端测试（本机 HTTP 真实下载 /
  离线目录 / 缺文件拒绝三场景），部署级回归有了守门测试，套件 98 → 112 项。
- curses 初始化失败（TERM 异常等）改为友好报错退出；singbox_manager 交互循环不再被
  磁盘/命令层的意外异常带走。
- `launch.sh` 对 `YJL_TUI_REF` 做合法性校验（只允许 ref 字符），杜绝拼进 URL 的畸形值；
  `--list` 按分类分组输出；pyflakes 静态清扫（无占位符 f-string、未用导入等）。
- 新增 `yjl-tui --doctor` 自诊断（Python 版本、配置完整性、local_script 存在性、launch 清单新鲜度、
  依赖命令、目录可写性）与 `--version`；`--check` 输出版本号。
- 新增 GitHub Actions CI（unittest + 全量 `bash -n` + 清单新鲜度 + Windows 平台回归 +
  shellcheck），自维护脚本全部通过 shellcheck 零告警（vendored 快照按惯例豁免）。
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
