# sing-box 管理设计

## 目标和边界

本功能服务于由 `fscarmen/sing-box.sh` 安装、配置目录为
`/etc/sing-box/conf/` 的 sing-box 实例。它用于管理出站、服务分流、DNS
优先级、配置备份与恢复，并保留原来的 `sing-box(fsr)` 完整菜单处理协议、
证书、入站、订阅、Argo 和端口跳跃。

第一版不会修改非 fscarmen 的 sing-box、233boy、S-UI、3x-ui 或其他面板。
检测到服务启动参数不是 `/etc/sing-box/sing-box run -C /etc/sing-box/conf`
时，只显示诊断，不写入配置。

## 菜单结构

`sing-box(fsr)` 分类保持两个独立入口：

```text
1. bash <(wget ...) 完整菜单（本地克隆）
2. Geosite 规则更新（上游 -> 本仓库回退）
3. sing-box管理
```

规则更新不修改 `/etc/sing-box/conf`。`sing-box管理` 会调用同一个更新器，确保
选中的规则文件存在。

## Geosite 规则库

仓库维护端脚本为 `scripts/geosite/update.sh`。

```bash
# 在仓库根目录运行：下载 SagerNet rule-set 分支中的全部 .srs
bash scripts/geosite/update.sh --vendor

# 查看变化，确认后由维护者自行提交
git diff --stat -- scripts/geosite
git add scripts/geosite
git commit -m "chore: refresh geosite rule-set"
git push
```

`--vendor` 从 SagerNet `sing-geosite` 的 `rule-set` 分支下载完整快照，只有
提取到超过 100 个 `.srs` 文件、且已生成 `SHA256SUMS` 后才会原子替换仓库镜像。
`UPSTREAM.json` 记录上游提交、更新时间和规则数量。

服务器上的 TUI 动作执行：

```bash
bash scripts/geosite/update.sh --sync
```

它仅同步管理器内置的常用规则到 `/etc/sing-box/rules/`，不下载整个规则库：

```text
优先：SagerNet 官方 Raw
失败：dandan8511/dandan-tui GitHub Raw 镜像，并核对 SHA256SUMS
再失败：当前机器运行脚本旁的本地仓库镜像，并核对 SHA256SUMS
全部失败：保留已有本地 .srs，明确报告失败，不覆盖为错误页面或空文件
```

仓库同时维护 `scripts/geosite/rule-set.tar.gz`。`./run.sh` 直接从工作目录读取
解压后的镜像；`./launch.sh` 则缓存这个压缩镜像。只有上游和 GitHub 镜像都不可用时，
更新器才按需从压缩镜像提取所需 `.srs` 到临时目录，并继续做 SHA-256 校验。

目标服务器可用 `YJL_GEOSITE_RULE_DIR` 改写规则目录，默认保持为
`/etc/sing-box/rules/`。更新动作要求 root，但不重启、重载或修改 sing-box 服务。

## 规则集与服务名

服务名称和 SagerNet 的文件名并不总是一致。管理器展示服务名称，配置内部使用
经过确认的 rule set tag：

| 服务 | rule set tag |
| --- | --- |
| OpenAI / ChatGPT | `geosite-openai` |
| Claude | `geosite-anthropic` |
| Gemini | `geosite-google-gemini` |
| Google DeepMind | `geosite-google-deepmind` |
| Netflix | `geosite-netflix` |
| Disney+ | `geosite-disney` |
| HBO / Max | `geosite-hbo` |
| Prime Video | `geosite-primevideo` |
| YouTube | `geosite-youtube` |
| Spotify | `geosite-spotify` |
| Telegram | `geosite-telegram` |
| TikTok | `geosite-tiktok` |
| Bilibili | `geosite-bilibili` |

## sing-box 管理器

进入后先只读检索：安装器匹配、sing-box 版本、`sing-box check -C`、服务状态、
配置入站端口、出站 tag、DNS strategy、已安装规则、备份数量。只有同时发现
`01_outbounds.json`、`03_route.json`、`05_dns.json` 和 `/etc/sing-box/sing-box` 时才会允许写入。
通过检查后显示：

```text
1. 出站管理（direct、warp-ep、认证 SOCKS5）
2. AI 分流（OpenAI、Claude、Gemini、DeepMind）
3. 流媒体与常用服务分流
4. 独立 Geosite rule set 分流（例如 `geosite-github`）
5. DNS IPv4 / IPv6 优先级
6. 配置备份与还原
7. 重新应用当前受管配置
```

独立规则集只接受 SagerNet `geosite-*.srs` 名称，并与预置服务使用相同的下载、
SHA-256 回退和 staged `sing-box check` 流程。第一版不把任意域名列表伪装成
Geosite 规则集；需要按域名分流时仍应通过原 fscarmen 自定义路由菜单维护。

SOCKS5 支持粘贴完整 URL 或分字段输入。密码不输出到屏幕、日志或命令行；保存的
状态与生成的认证配置保持 root 可读写权限。

TUI 自己拥有的配置片段将避免覆盖 fscarmen 的基础文件：

```text
/etc/sing-box/conf/00_yjl_singbox_routes.json
/etc/sing-box/conf/99_yjl_singbox_dns.json
/etc/sing-box/yjl-tui-state.json
/etc/sing-box/yjl-tui-backups/
```

`00_` 规则优先于 fscarmen 默认 `03_route.json`，因此可以可靠实现
`geosite-openai -> ovh-openai`。受管分片会同时明确写入 `route.final: direct`：不论
SOCKS5 出站在合并后排在什么位置，未命中的流量都固定从本机直连，不会意外变成全局
SOCKS5 代理。

升级 TUI 后，已有受管配置的机器可直接选择 `7. 重新应用当前受管配置`。它会使用现有
SOCKS5、规则和 DNS 状态生成新分片、完成 staged `sing-box check` 后重载服务；不需要
重复填写 SOCKS5 信息或修改已有规则。

## 应用与验证

每一次出站、规则或 DNS 改动都使用相同流程：

```text
读取现状
-> 备份受管文件
-> 在临时目录生成 JSON
-> 检查 JSON 结构
-> sing-box check -C /etc/sing-box/conf
-> 原子替换受管文件
-> systemd reload；Alpine/OpenRC 优先向实际运行的 sing-box 进程发送 HUP，未发现进程才 zap + start
-> 确认服务仍运行；失败则自动恢复备份
```

OpenRC 的 pidfile 或 `rc-service status` 可能因旧的启动失败状态而失真。管理器不会仅凭
该状态报成功：它会精确匹配 `/etc/sing-box/sing-box run -C /etc/sing-box/conf` 进程，HUP 后
确认进程仍存在；若没有运行进程，才使用 OpenRC 启动并再次确认真实进程。

第一版结论只报告配置层面的成功或失败：SOCKS5 连接/认证、规则文件同步、规则写入、
sing-box 配置检查和服务状态。它不实施流媒体解锁检测，也不会把站点的 403、账号或
地区限制误判为 SOCKS5 配置失败。

可不进入菜单进行只读/连通检查：

```bash
python3 singbox_manager.py --status
printf '%s\n' 'socks5://user:password@example.com:1080' | python3 singbox_manager.py --probe-socks5 -
```

第二种形式从标准输入读取地址，不会把认证信息放到 shell 历史或终端命令行中。

自动化或临时目录做语法验证时可设置 `YJL_SINGBOX_SKIP_RELOAD=1`，它会在配置检查
通过后明确跳过服务重载。这是测试开关，正常从 TUI 进入时不会设置，仍会按 OpenRC
或 systemd 的实际服务状态完成应用验证。
