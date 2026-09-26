# nginx-ui 快照来源与本地化改造

## 快照内容

本目录是 dandan-tui 为「nginx-ui 网页管理面板」做的自包含快照，运行时不再访问
`dandan8511/nginx-ui`：

| 文件 | 来源 |
| --- | --- |
| `install.sh` | `dandan8511/nginx-ui` `dev` 分支安装器快照（2026-09-26 抓取），按下列「本地化改造」修改 |
| `nginx-ui-linux-32.tar.gz` / `-64.tar.gz` / `-arm64-v8a.tar.gz` | `dandan8511/nginx-ui` Release `v2.5.7` 的三个 Linux 归档，逐字节原样保存 |
| `resources/services/nginx-ui.service` / `.rc` / `.openwrt` / `.init` | `dandan8511/nginx-ui` `dev` 分支 `resources/services/` |

上游项目：[0xJacky/nginx-ui](https://github.com/0xJacky/nginx-ui)（安装器与面板本体）。

## 本地化改造（相对上游安装器）

1. 版本固定为 `v2.5.7`，`get_latest_version()` 不再查询 GitHub API。
2. 面板归档与服务文件一律从本仓库 `tools/nginx-ui/`（raw）下载；不再访问
   GitHub Releases 和 `dandan8511/nginx-ui`。
3. 下载的归档会与本目录 `SHA256SUMS` 校验，不一致即中止。
4. 支持离线安装：`NGINX_UI_LOCAL_SOURCE=/path/to/dir bash install.sh`，
   目录内放置对应架构的 `nginx-ui-linux-<MACHINE>.tar.gz` 即跳过下载。

## 维护

升级 nginx-ui 版本时：替换三个架构归档与 `resources/services/*`，重新生成
`SHA256SUMS`（`sha256sum nginx-ui-linux-*.tar.gz resources/services/*`），并把
`install.sh` 里的 `RELEASE_PINNED` 改为新版本号。
