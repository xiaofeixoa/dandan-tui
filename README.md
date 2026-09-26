# YJL Linux TUI

面向 Linux VPS 的终端管理菜单。菜单定义在 `scripts.json`，本地脚本在 `scripts/`，主程序入口为 `tui.py`（实现拆分在 `yjl_tui/` 包内）。

## 目录

- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [系统与内核](#系统与内核)
  - [系统内核维护与 GRUB 引导管理](#系统内核维护与-grub-引导管理)
- [网络与 TCP 加速](#网络与-tcp-加速)
  - [tcp-brutal](#tcp-brutal)
  - [tcpfit 实测 TCP 调优](#tcpfit-实测-tcp-调优)
- [sing-box 与代理](#sing-box-与代理)
  - [fscarmen sing-box 与 WARP 本地快照](#fscarmen-sing-box-与-warp-本地快照)
- [Web 服务](#web-服务)
  - [Nginx 管理工具](#nginx-管理工具)
- [Docker 管理](#docker-管理)
  - [国内 Docker 源检测](#国内-docker-源检测)
  - [Docker Hub Mirror 服务端](#docker-hub-mirror-服务端)
- [高级工具](#高级工具)
  - [猴哥 nft-forward 本地副本](#猴哥-nft-forward-本地副本)
  - [yjl-argo（Cloudflare Tunnel）](#yjl-argocloudflare-tunnel)
- [维护与开发](#维护与开发)
  - [更新日志](#更新日志)
  - [运行数据](#运行数据)
  - [安全模型与确认机制](#安全模型与确认机制)
  - [测试与 CI](#测试与-ci)
  - [更新 fscarmen 快照的约定](#更新-fscarmen-快照的约定)
  - [许可证](#许可证)

## 快速开始

在项目目录直接运行：

```bash
./run.sh
```

无交互检查与自诊断：

```bash
./run.sh --list      # 列出全部动作
./run.sh --check     # 校验菜单配置
./run.sh --doctor    # 自诊断：Python 版本、配置完整性、本地脚本、launch 清单新鲜度、依赖命令、目录可写性
./run.sh --version
```

遇到「菜单里某个功能不对劲」时，先跑 `--doctor` 把输出贴出来，基本能定位是缺命令、缺文件还是清单过期。

本地运行时，所有 `local_script` 动作都直接使用本工程文件。`sing-box(fsr)` 与 `WARP 管理菜单（本地克隆）`分别使用 `scripts/fscarmen-sing-box.sh`、`scripts/fscarmen-warp.sh`，不需要从 fscarmen 下载入口脚本。涉及安装、网络、内核、服务、证书或防火墙的动作应以 root 运行；fscarmen 菜单后续下载依赖时仍需要网络。

GitHub 仓库：`https://github.com/xiaofeixoa/dandan-tui`

VPS 一行启动：

```bash
bash <(curl -fsSL "https://raw.githubusercontent.com/xiaofeixoa/dandan-tui/main/launch.sh?v=$(date +%s)")
```

只有 wget 时：

```bash
bash <(wget -qO- "https://raw.githubusercontent.com/xiaofeixoa/dandan-tui/main/launch.sh?v=$(date +%s)")
```

`launch.sh` 从 `main` 下载 TUI 本体、`yjl_tui/` 模块、菜单配置、本地脚本与各工具快照到 `${XDG_CACHE_HOME:-~/.cache}/dandan-tui`，下载完成后用仓库根目录的 `SHA256SUMS` 逐项校验，再以「staging 目录组装 + 整体替换」的方式原子升级缓存目录，最后启动。要固定某个版本：

```bash
YJL_TUI_REF=提交SHA bash <(curl -fsSL "https://raw.githubusercontent.com/xiaofeixoa/dandan-tui/main/launch.sh?v=$(date +%s)")
```

GitHub 不可达或完全离线的 VPS 可用离线安装：把整个工程目录传到服务器后
`YJL_TUI_LOCAL_SOURCE=/path/to/dandan-tui bash launch.sh`，文件从本地复制、SHA256SUMS 校验照常执行。

默认不再附加 `?v=时间戳` 缓存穿透参数（GitHub raw 自带 CDN 缓存）；需要强制拉最新时设置 `YJL_TUI_CACHE_BUSTER=1`。校验失败会直接中止且不影响已安装的旧版本。`SHA256SUMS` 由 `launch.sh` 的 `FILES` 清单生成，任何清单内文件改动后运行 `bash scripts/regen-launch-manifest.sh` 重新生成（`scripts/check-launch-manifest.sh`、测试和 CI 都会检查过期）。

需要 `bash` 和 `python3`；在线安装或检测按动作需要 `curl`、`wget`、`openssl`、`iproute2` 等工具。

## 项目结构

```text
tui.py                  入口薄壳（保持 run.sh / launch.sh 兼容）
yjl_tui/                TUI 内部实现包
├── paths.py            路径常量、运行身份（root 判定）、scripts.json 读取
├── probes.py           只读的系统 / CPU / 虚拟化探测
├── tcp_brutal.py       TCP Brutal 配置与订阅修补（纯文本处理，独立可测）
├── doctor.py           `--doctor` 自诊断
└── tui.py              TUI 类（动作执行、分发、curses 界面、main）
kernel_manager.py       系统内核维护的规划与解析（纯函数）
nginx_manager.py        Nginx 站点 / 证书管理
singbox_manager.py      fscarmen sing-box 分片配置维护（独立进程运行）
scripts.json            菜单与动作定义（分类 + 动作）
tcp_profiles.json       本地 TCP 调优方案参数
scripts/                各功能使用的本地脚本与上游快照
tools/nft-forward/      nft-forward 固定版本的本地工具包
tools/nginx-ui/         nginx-ui v2.5.7 固定快照（安装器 + 三架构归档 + 服务文件）
yjl-argo/               yjl-argo（Cloudflare Tunnel 管理器）
tests/                  unittest 套件（smoke / kernel / singbox）
docs/                   设计与实施记录
```

## 系统与内核

### 系统内核维护与 GRUB 引导管理

左侧菜单把通用系统内核与 VPN/TCP 加速专用内核分开：

- **内核管理**：`系统内核维护` 与 `GRUB 启动管理`。支持 Debian 11/12/13、Ubuntu 22.04/24.04/26.04 的 `amd64`、`arm64` 官方 APT 内核。
- **VPN 专用内核**：保留原“官方内核管理”以及 BBR、BBRplus、锐速等旧链路，供 VPN/TCP 加速用途；它不替代系统内核维护的安全检查。

系统内核维护会先显示发行版、架构、当前内核、虚拟化形态、`/boot` 空间、Secure Boot、DKMS 和已安装映像。Docker、LXC、OpenVZ 等容器会被拒绝，因为它们共享宿主机内核。所有安装路径都只安装所展示的内核包，不执行 `apt-get upgrade`、不自动清理旧内核，也**绝不自动重启**。

可选路径如下：

| 路径 | 适用范围 | 约束 |
| --- | --- | --- |
| 官方 APT 稳定内核 | Debian/Ubuntu，amd64/arm64 | 默认推荐；只在当前源同时提供 image 与 headers 元包时允许安装。 |
| Ubuntu HWE | Ubuntu | 不添加额外源；只有当前 APT 已提供完整 HWE 元包才显示。 |
| Debian Backports | Debian 11/12/13 | 只创建或移除 `/etc/apt/sources.list.d/yjl-tui-kernel-<codename>-backports.list`，不改用户的主 sources 文件。 |
| Ubuntu Mainline 预编译包 | Ubuntu amd64 | 仅在上游页面明确显示 amd64 构建成功、headers/image/modules 完整、`CHECKSUMS.gpg` 用固定的 Kernel PPA 专用 keyring 验签成功且每个包 SHA-256 一致时才允许 `dpkg -i`。Secure Boot 必须明确为 disabled。arm64 不硬装。 |
| kernel.org 源码编译 | Debian/Ubuntu，amd64/arm64 | 高级路径，使用本地固定的 MIT `scripts/kernel-installer/`；至少需要 8 GiB 根分区可用空间，仅提供已验证的 stable/longterm release tarball。 |

`GRUB 启动管理`会显示当前 `uname -r`、`/boot/vmlinuz-*`、`grub-editenv list` 和从本机 `grub.cfg` 解析出的完整启动路径。永久默认项与仅下一次启动均只能从该列表选择；后者使用 `grub-reboot` 写入 one-shot 项，不重启机器。任何内核安装的最终验证都应是：在维护窗口自行重启，重新登录后运行 `uname -r`，并核对它与预期版本一致。

源码构建工具的固定上游提交、原始 SHA-256、许可证与本地安全改动记录在
[`scripts/kernel-installer/UPSTREAM.md`](scripts/kernel-installer/UPSTREAM.md)。本地副本禁止远程 source 回退、脚本自更新、不验证证书的下载、`kexec` 与卸载入口。

## 网络与 TCP 加速

TCP 调优分类提供本地方案（BBR/FQ、FQ_PIE、CAKE、ECN、系统配置优化等，全部写入带备份的 sysctl/limits 配置并可整组卸载）、上游 tcp.sh 菜单的本地化入口，以及下列独立工具。

### tcp-brutal

`tcp-brutal` 分类提供两个 root 动作：在线安装会从 `https://tcp.hy2.sh/` 临时下载当前官方
安装器，离线安装会执行仓库内 `scripts/tcp-brutal/` 的完整 `HyNetworks/tcp-brutal` 源码快照和
已固定哈希的 `dkms.tar.gz`。离线模式不会请求 tcp-brutal 上游源码、Hysteria API 或 GitHub
Release，但 DKMS、编译器和正在运行的内核头文件仍需要从系统软件源安装。

安装动作只安装模块，默认不会创建任何 `brutalctl` 规则。`brutalctl add` 的 IP 是**当前机器发送
连接时的远端目的地址**，不是当前机器的 IP：在服务器上把服务器自身 IP 作为目的地址会被官方工具
拒绝为本地地址；若要以服务器 IP 为目的地址，应在客户端机器上运行该命令。规则添加前会执行
`ip route get <目标 IP>`，`brutalctl` 会据此自动复制正确下一跳、网卡并写出 `proto 233` 路由。
例如服务器向某个客户端发送流量时，目标应是客户端地址：

```bash
brutalctl add 188.165.226.219/32 1000
```

速率单位是 Mbps。为某个远端目的地址添加规则时使用：

```bash
bash scripts/tcp-brutal-manager.sh add <远端IP>/32 1000
```

本工具将每条确认成功的规则保存为
`/etc/tcp-brutal/rules.conf`，并在 Debian/Ubuntu 上安装、启用
`tcp-brutal-rules.service`，在 Alpine 上安装、启用同名 OpenRC 服务。开机后服务会重新执行
保存的 `brutalctl add` 命令，因此模块规则和 `proto 233` 路由不再因重启丢失。规则只影响之后
新建的 TCP 连接；已有连接需重连后才会命中。

分类中的 `3. 本机 brutal 管理` 不会重新安装模块，可查看模块、实时规则、`proto 233` 路由和
持久化服务，并新增、删除或重新应用已保存规则。

安装后也可直接查看或删除受管规则：

```bash
bash scripts/tcp-brutal-manager.sh add 188.165.226.219/32 1000
bash scripts/tcp-brutal-manager.sh list
bash scripts/tcp-brutal-manager.sh del 188.165.226.219/32
```

### tcpfit 实测 TCP 调优

`99. tcpfit 实测 TCP 调优（本地副本）` 对应
[Kylin010/tcpfit](https://github.com/Kylin010/tcpfit)。完整上游快照保存在
`scripts/tcpfit/`，TUI 实际执行 `scripts/tcpfit/tcpfit.sh`，不使用在线管道脚本。
本次固定的来源为：

```text
upstream commit: 3e285932e5f212eef9be9591ebba9a78a3b4d1c7
upstream date:   2026-08-10
script version:  0.5.3
sha256:          6c86b31c3d937736bb4d919b04b732013cbb2da958d97841b34cd663fd2a6b35
license:         MIT (Kylin010)
```

它根据带宽、BDP 和可选的 `iperf3` 实测推导参数。调优会写入 sysctl、队列规则与 systemd
配置，因此菜单要求 root 且属于高风险动作；首次修改前会保存快照，原菜单提供 `rollback` 回滚。
远端使用 `launch.sh` 时只会下载执行所需的 `tcpfit.sh`，而 GitHub 仓库保留完整上游快照、许可证、
安装器和多机编排示例。

更新快照：先确认候选提交与语法，再从临时克隆导出工作树（不要把嵌套 `.git` 目录提交进本仓库）：

```bash
git clone --depth 1 https://github.com/Kylin010/tcpfit.git /tmp/tcpfit
git -C /tmp/tcpfit log -1 --format='%H %cs %s'
bash -n /tmp/tcpfit/tcpfit.sh
sha256sum /tmp/tcpfit/tcpfit.sh
git -C /tmp/tcpfit archive --format=tar HEAD | tar -xf - -C scripts/tcpfit
python -m unittest discover -s tests -v
```

## sing-box 与代理

### fscarmen sing-box 与 WARP 本地快照

这两个菜单的实现是相同的：把上游**入口脚本完整保存**到本仓库，TUI 以 `local_script` 执行；`launch.sh`
又会把快照同步到 VPS 的 `${XDG_CACHE_HOME:-~/.cache}/dandan-tui/scripts/`。所以本地运行和 GitHub
一行启动都先使用你的 GitHub 仓库文件，而不是当场下载上游入口。

这只本地化入口，不是离线安装包。菜单继续安装时要下载系统包、sing-box 核心、WireGuard、wireproxy、
cloudflared、证书或订阅模板，仍需要网络，且这些运行依赖仍由各自上游提供。

#### sing-box(fsr)

该完整菜单对应：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/fscarmen/sing-box/main/sing-box.sh)
```

但 TUI 实际执行仓库内完整克隆 [scripts/fscarmen-sing-box.sh](scripts/fscarmen-sing-box.sh)。当前快照来源：

```text
upstream commit: 10ee5cfbbb463aaf6e5a9de6bf3cf5c9333df579
script version:  v1.3.22 (2026.08.11)
sha256:          6e964563045c094fe2b9db855e2c921134cea6386644e9ec833b8feb0357f3a6
```

同一分类的 `sing-box管理` 是本仓库自己的维护工具
[`singbox_manager.py`](singbox_manager.py)（SOCKS5 出站、服务分流、DNS 策略、备份还原），设计与操作笔记见 [`sing-box管理.md`](sing-box管理.md)。

#### WARP

该完整菜单对应：

```bash
wget -N https://gitlab.com/fscarmen/warp/-/raw/main/menu.sh && bash menu.sh
```

TUI 实际运行 [scripts/fscarmen-warp.sh](scripts/fscarmen-warp.sh)。当前快照来源：

```text
upstream commit: 3f7e4529714b7e634f05ac9f5c2efd41608f9211
script version:  3.2.7
sha256:          51a73716f23dcca716bc81083d5d50f817693550c6882107252daf5b35eb7c13
```

WARP 本身有“同步脚本至最新版本”菜单项。上游原实现会再下载 GitLab 的 `menu.sh`；本项目仅对此处做了
定向，将它改为从 `xiaofeixoa/dandan-tui` 的 GitHub Raw 下载 `scripts/fscarmen-warp.sh`。也就是说，
从 TUI 启动 WARP 后在 WARP 菜单里升级，仍会保持使用你的本地化版本；可临时用 `YJL_WARP_UPDATE_URL`
覆盖该地址测试分支。

## Web 服务

### Nginx 管理工具

“服务器配置”分类中的“Nginx 管理工具”只管理当前 VPS。本机每次进入都会实时执行
`nginx -t`、`nginx -T`、`ss -lntup` 和 `systemctl is-active nginx`，先展示本机域名、站点配置文件、
监听端口、静态目录、反代上游、证书和服务状态。

菜单支持刷新扫描、查看站点原始配置、新建反向代理站点、新建静态站点，以及证书申请和自动续期。创建新站点时会拒绝
已监听的 TCP 端口，先把临时 `.conf` 放入 `/etc/nginx/conf.d/` 参与 `nginx -t`，通过后才原子替换并
reload；失败会把新文件移入 `/var/backups/yjl-tui/nginx/`，不覆盖现有业务。新建静态站点时网站目录不允许落在 `/etc`、`/boot`、`/usr` 等系统目录内。

证书统一使用本机 `certbot`：申请成功后启用 `certbot.timer`，并执行 `certbot renew --dry-run` 验证自动
续期链路。HTTP-01 要求域名公网 TCP 80 可访问；Cloudflare DNS-01 需要安装
`python3-certbot-dns-cloudflare`，Token 会写入 `/etc/letsencrypt/yjl-tui/` 下的 `0600` 凭据文件，供
后续定时续期使用。Token 不写入 TUI 日志。

同一分类中的“nginx 网页管理工具 (nginx-ui)”是可选网页面板。`tools/nginx-ui/` 内含
`v2.5.7` 固定版本的三架构归档（x64/x32/arm64）、服务文件与改造后的安装器，运行时不再访问
`dandan8511/nginx-ui`：`launch.sh` 只缓存安装器与 `SHA256SUMS`，进入菜单后按架构从本仓库
raw 下载归档并校验；完全离线时可用 `NGINX_UI_LOCAL_SOURCE=<归档目录> bash install.sh`。
安装会新建 nginx-ui 服务和网页监听端口，不能把它当作已有 Nginx 的无影响更新。来源与升级
方法见 [`tools/nginx-ui/UPSTREAM-ASSETS.md`](tools/nginx-ui/UPSTREAM-ASSETS.md)。

## Docker 管理

### 国内 Docker 源检测

Docker 管理分类的 `16. 国内 Docker 源检测` 运行仓库内
[`scripts/docker-mirror-switch.sh`](scripts/docker-mirror-switch.sh)。默认的交互流程仅先检查候选源
能否完成 Docker Registry v2 鉴权和 manifest 下载；后续真实拉取、写入 Docker 配置、重启服务均会再次
询问确认。脚本只接受 Docker Hub 测试镜像，且只会改写 `/etc/docker/daemon.json` 的
`registry-mirrors` 字段，原文件会在应用前按时间戳备份；重启 Docker 后服务未能恢复 active 时会自动还原备份并再次重启。

在 TUI 外也可以直接运行：

```bash
# 仅检查 Registry API，不拉镜像、不改配置
sudo bash scripts/docker-mirror-switch.sh --check

# 真实拉取验证，但不改配置
sudo bash scripts/docker-mirror-switch.sh --verify-pull

# 验证后备份、写入镜像源并重启 Docker
sudo bash scripts/docker-mirror-switch.sh --apply
```

单个真实拉取默认 90 秒超时，拉取通过的测试标签会自动删除。`registry-mirrors` 只加速 Docker Hub
引用，例如 `alpine`、`nginx`、`mysql:5.7`；它不会代理明确写成 `ghcr.io/...` 或 `quay.io/...` 的镜像。

### Docker Hub Mirror 服务端

Docker 管理分类的 `17. 本机部署 Docker Hub Mirror` 部署官方 `registry:2` 的
pull-through cache。它适合放在国外 OVH 服务器：第一次拉取由 OVH 访问 Docker Hub，后续国内机器
从 OVH 读取已缓存层。脚本会在 `10305-10307` 中检查 TCP 和 UDP 后自动选择空闲端口，也可以用
`--port` 指定；缓存默认保存在 `/var/lib/dockerhub-mirror/data`。

服务端先准备域名和 HTTPS（或仅限源站 IP 的防火墙规则），然后执行：

```bash
sudo bash scripts/dockerhub-mirror.sh --server --port 10305 --public-url https://mirror.example.com
```

没有域名时可以临时使用 HTTP，但必须在国内客户端显式加 `--insecure`，例如：

```bash
sudo bash scripts/dockerhub-mirror.sh --client --mirror http://OVH_IP:10305 --insecure
```

HTTP 不加密且容易被滥用，建议只用于验证；公网长期运行应使用 HTTPS，并在 OVH 防火墙只放行
国内服务器 IP。服务端 `--status` 查看容器，`--uninstall` 只删除容器而保留缓存数据。Mirror 仅
处理 Docker Hub 引用，不会代理 `ghcr.io`、`quay.io` 等其他 Registry。客户端写入 daemon.json 后 Docker 重启失败时，脚本会自动还原原配置并再次重启。

#### 缓存管理

17 号菜单还提供缓存管理：可列出已缓存仓库、选择删除例如 `library/redis`，停止 Mirror 后运行
Registry 垃圾回收，再启动 Mirror。删除只影响 OVH 缓存，不影响任何客户端已拉取的镜像。缓存总目录是
`/var/lib/dockerhub-mirror/data`（可用 `DOCKERHUB_MIRROR_STATE_DIR` 覆盖，脚本会拒绝空值和系统目录）。自动策略按每天 04:25 执行：可设缓存总限额和保留天数；达到任一条件
时会短暂停止 Mirror、清空**整库**缓存、再重新启动。官方 pull-through cache 没有安全的按镜像 LRU 或
按镜像过期机制，所以这里明确使用整库轮换，不会假装只删除某个“最旧镜像”。

命令行也可使用：

```bash
# 查看缓存仓库和总占用
sudo bash scripts/dockerhub-mirror.sh --cache-list

# 删除 Redis 缓存仓库并回收无引用 layer
sudo bash scripts/dockerhub-mirror.sh --delete-repository library/redis

# 设为超过 1 GiB 或超过 14 天时，定时清空全部 Mirror 缓存
sudo bash scripts/dockerhub-mirror.sh --configure-policy --max-cache-gb 1 --expire-days 14
```

## 高级工具

### 猴哥 nft-forward 本地副本

“猴哥 nft-forward 端口转发（本地工具包）”执行仓库内
`tools/nft-forward/install.sh`。同一目录固定包含 Release `v0.68.0` 的 `nft-agent`、
`nft-server` 和 `SHA256SUMS`；本地克隆运行 TUI 时直接使用该目录。`launch.sh` 启动时仅下载
约 37 KB 的安装器；只有进入该菜单、且缓存内没有工具包时，安装器才从本仓库下载二进制和
`SHA256SUMS` 到缓存目录。默认安装会通过 `file://` 从该工具目录读取发布物并按校验文件验证，
不需要访问上游 install.sh 或该版本的 GitHub Release。

工具包的原始安装器快照来自 `xjetry/nft-forward` 的提交
`5c099fdd6000dbfb088387c8494fbbbfb1de5025`，其 Release `v0.68.0` 的 `nft-agent`、
`nft-server`、`SHA256SUMS` 已逐项校验并随本仓库保存。脚本的 `update-script` 和安装后生成的
`nft-forward-upgrade` 会从 `xiaofeixoa/dandan-tui/tools/nft-forward/install.sh` 更新自身，
而非上游的 `install.sh`。要升级到未打包的新 Release，可显式传入 `--release <tag>`；这时才会
从上游或通过 `NFTF_RELEASE_BASE_URL` 指定的发布源下载对应二进制。

### yjl-argo（Cloudflare Tunnel）

`yjl-argo/yjl-argo.sh` 是内置的 Cloudflare Tunnel 管理器，支持 systemd 与 OpenRC，`launch.sh`
会把脚本缓存到本地。独立安装与测试说明见 [`yjl-argo/README.md`](yjl-argo/README.md)。

Nodeseek 分类中的 TCP 窗口调优（`scripts/nekoneko-tools.sh`）等脚本同样由 `launch.sh` 一并缓存，`scripts/check-launch-manifest.sh` 保证清单里的每个本地脚本都会被下载。

### nodeseek合集（社区脚本）

「nodeseek合集」分类收录 NodeSeek 社区常用脚本合集帖的在线入口（DD 重装、综合/性能测试、流媒体与
IP 质量、测速、回程路由、SWAP/Fail2ban、Python/realm/gost/哪吒/Argo/PVE、科技lion、杜甫检测等
37 项），均为 `kind: online` 动作：执行时下载、bash 语法检查后进入脚本自身交互。说明：

- 锐速/BBRPLUS（tcpx）、TCP 窗口调优（nekoneko）、WARP、Docker 等与既有分类重复或本 TUI 已有
  更安全本地实现的，不在重复收录；
- 来源已失效的条目（`bench.im`、`git.io`、ghproxy 链接、DNS-Alice-Unlock）已剔除；
- `check.unlock.media`、`sick.onl`、`bash.icu` 等域名部分国内网络可能无法直连，海外 VPS 正常；
- 第三方脚本行为由各自上游决定，涉及重装/调优的条目请先读菜单描述再执行；
- 三个 DD 重装是交互式向导：分步选择目标系统/版本（leitbogioro 支持 Debian/Ubuntu/
  Windows/CentOS/RockyLinux/AlmaLinux，moeclub 支持 Debian/Ubuntu/CentOS）、输入新密码
  （getpass 不回显）和 SSH 端口，汇总展示（密码打码）后输入 `DD` 才开始重装；
  fcurrk 直接进入其自带交互菜单。
- 全部在线动作已声明交互形态并通过守门测试：自带菜单（哪吒面板引导、realm、gost、
  PVE、Argo、宝塔、233boy、融合怪等）、一键自动安装（Docker、chsrc、BBR v3 别名、
  lazydocker）、直跑检测/测速（bench、yabs、nws、回程等）、固定参数变体（yabs GB5、
  LemonBench --fast、speedtest --simple 等），需要参数的（哪吒 agent）有 `prompt_args`
  输入提示。新增在线动作时必须在
  `tests/test_smoke.py::test_online_actions_declare_interaction_model` 登记交互形态。

## 维护与开发

### 更新日志

版本间变更见 [CHANGELOG.md](CHANGELOG.md)。

### 运行数据

```text
root:     /var/lib/yjl-tui  /var/log/yjl-tui  /var/cache/yjl-tui
普通用户: ~/.local/state/yjl-tui              ~/.cache/yjl-tui
```

可临时覆盖：

```bash
YJL_TUI_CACHE_DIR=/tmp/yjl-cache YJL_TUI_LOG_DIR=/tmp/yjl-logs ./run.sh
```

### 安全模型与确认机制

- 所有下载优先 HTTPS 并做语法检查后才执行；launch.sh 额外用 `SHA256SUMS` 校验整个下载清单。
- 系统内核、GRUB、网络、SSH、Nginx、sing-box 配置的写入都遵循「备份 → 暂存校验 → 原子替换 → 失败回滚」。
- 三个不可逆/全局性内置动作（系统升级 `apt-get upgrade -y`、Swap 创建、Docker `system prune --all`）在执行前要求输入 `yes` 确认；其余动作不额外确认，上游脚本自身的交互保持原样。
- 动作内的 Ctrl-C 或异常只会中断当前动作并返回菜单，不会带走整个 TUI，也不会跳过动作自身的回滚逻辑。
- TUI 不显示风险标签；密码与 DNS Token 走 stdin/getpass/环境变量，不写入命令行日志。

### 测试与 CI

```bash
python -m unittest discover -s tests -v
bash scripts/check-launch-manifest.sh
./run.sh --check
```

`tests/` 覆盖：scripts.json 契约、launch.sh 下载清单与 SHA256SUMS 新鲜度、launch.sh 全链路端到端（真实 HTTP 下载 / 离线目录 / 缺文件拒绝）、快照版本与哈希固定、`bash -n` 语法检查、`py_compile`、内核规划纯函数、sing-box 状态/分片/交互路径、TUI 分发注册表与助手语义、nginx 解析/校验/写入守卫、`--doctor` 检查项。GitHub Actions（`.github/workflows/ci.yml`）在每次 push/PR 时运行同一套检查。新增本地脚本菜单时，务必把文件加入 `launch.sh` 的 `FILES` 清单并重新生成根目录 `SHA256SUMS`，否则测试会失败。

### 更新 fscarmen 快照的约定

以后只要提出“看看 README，更新工程的 sing-box 和 WARP 脚本”，按下面流程执行：先分别取上游 `main`
候选文件，记录提交号、版本、SHA-256 和 `diff`；语法检查通过后才用完整候选文件覆盖本地快照。WARP 覆盖后必须
重新应用上文 `YJL_WARP_UPDATE_URL` 与 `ver()` 下载地址两处定制，不能把它们带回 GitLab。sing-box 当前没有
本项目私有补丁，应保持逐字上游完整快照。

操作命令如下。临时文件只用于对比，不直接在服务器执行：

```bash
git ls-remote https://github.com/fscarmen/sing-box.git refs/heads/main
curl -fsSL https://raw.githubusercontent.com/fscarmen/sing-box/main/sing-box.sh -o /tmp/fscarmen-sing-box.sh

git ls-remote https://gitlab.com/fscarmen/warp.git refs/heads/main
curl -fsSL https://gitlab.com/fscarmen/warp/-/raw/main/menu.sh -o /tmp/fscarmen-warp.sh

bash -n /tmp/fscarmen-sing-box.sh
bash -n /tmp/fscarmen-warp.sh
sha256sum /tmp/fscarmen-sing-box.sh /tmp/fscarmen-warp.sh
diff -u scripts/fscarmen-sing-box.sh /tmp/fscarmen-sing-box.sh | less
diff -u scripts/fscarmen-warp.sh /tmp/fscarmen-warp.sh | less
```

确认变更后，完整覆盖 sing-box；完整覆盖 WARP 后再加回本仓库升级地址：

```bash
cp /tmp/fscarmen-sing-box.sh scripts/fscarmen-sing-box.sh
cp /tmp/fscarmen-warp.sh scripts/fscarmen-warp.sh
# 重新加入 YJL_WARP_UPDATE_URL 变量，并把 ver() 的下载地址改为 "$YJL_WARP_UPDATE_URL"
```

最后把新的提交号、版本和 SHA-256 写回本文对应小节，并执行：

```bash
python -m unittest discover -s tests -v
./run.sh --check
git add scripts/fscarmen-sing-box.sh scripts/fscarmen-warp.sh README.md tests/test_smoke.py
git commit -m "chore: refresh fscarmen script snapshots"
git push
```

正常的上游脚本更新不需要改 `yjl_tui/`、`scripts.json` 或 `launch.sh`，因为它们已固定引用两份本地快照；
但每次均须保留并运行针对两份快照的语法和冒烟测试。

### geosite 规则与仓库体积

`scripts/geosite/rule-set/` 内约 1900 个 `.srs` 二进制与 `rule-set.tar.gz` 是同一批数据的两份副本：松散文件充当 repo-as-CDN 回退源（`scripts/geosite/update.sh` 的 `MIRROR_RAW`），tar 包是 launch.sh 的离线回退。若要减小仓库体积，可把两者改为 GitHub Release 资产：`scripts/geosite/release-pack.sh` 会按 update.sh 的格式约定重新生成 `rule-set.tar.gz`、`SHA256SUMS` 与 `UPSTREAM.json`，上传后移除松散 `.srs` 即可。

### 许可证

本项目以 [MIT](LICENSE) 发布；`scripts/`、`tools/` 内的第三方快照沿用各自原始许可证（见 LICENSE 附录与各目录的 UPSTREAM/License 文件）。
