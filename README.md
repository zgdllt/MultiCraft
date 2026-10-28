<div align="center">
  <img src="assets/brand/logo.png" alt="MultiCraft logo" width="176" />
  <h1>MultiCraft</h1>
  <p><strong>一个客户端池，一张属于你的游戏画面。</strong></p>
  <p>Minecraft 客户端多开器与浏览器串流器 · Experimental alpha</p>
  <p><a href="docs/deployment.md">部署</a> · <a href="docs/architecture.md">架构</a> · <a href="docs/security.md">身份与安全</a> · <a href="docs/testing.md">验证</a> · <a href="docs/release-validation.md">发行实测</a> · <a href="THIRD_PARTY.md">third-party</a></p>
</div>

MultiCraft 在一台 GPU 主机上运行多个独立 Minecraft Java 客户端，把各自的画面、声音和输入接到浏览器。一个标签页登录一个身份，使用同一套简洁的单玩家界面；需要另一个角色时，再开一个身份标签页。

当前目标是把原版客户端的多开与远程游玩做好：会话数量由 OP 配置，每个角色拥有独立的连接、世界状态、菜单、输入、音频和编码流。同一运行时 JVM 调度这些客户端；正常 Minecraft 客户端仍然直接连接原服务器，服务器不需要安装 MultiCraft 插件。

> **版本状态：v0.1.0-alpha.1，面向实验部署。** 当前固定 Minecraft Java **26.3**、JDK **25**，GPU 宿主路径针对 Linux x86_64 / NVIDIA Vulkan。当前登录游戏使用受控离线身份，适用于有权管理的 `offline-mode` 测试服务器；Microsoft / `online-mode` 登录尚未实现。浏览器公钥登录管理的是 MultiCraft 访问权限。

## 能做什么

- **多开客户端池**：启动时配置 1–16 个槽位，默认生成 4 个；每个槽位在一个 JVM 内持有独立原版客户端。16 是配置边界，不是硬件容量保证。
- **一个标签页，一个身份**：公开界面沿用单玩家布局，账号由 OP 映射到一个原版客户端槽位。新身份入口创建独立标签页命名空间，避免同域 Cookie 覆盖登录。
- **鼠标捕获与全屏分开**：捕获用于第一人称转向，全屏由独立按钮控制。菜单、背包和容器使用绝对位置指针；F9 切换原版菜单，F8 释放输入捕获。
- **可调串流**：玩家可请求分辨率、帧率、视频码率和可选自适应码率；OP 管理允许的上限。界面区分请求、生效参数和实际接收统计。1000 fps 是允许请求的上限，实际帧率由游戏、GPU、编码、网络、解码和显示器共同决定。
- **独立音视频**：H.264 视频通过 HTTPS WebSocket 送到浏览器 WebCodecs；每个原版客户端的 OpenAL loopback 音频单独送入 WebAudio。alpha 音频是 48 kHz 双声道 s16le PCM，约 **1.536 Mbps / 活跃会话**，尚未使用 Opus。
- **保留服务端兼容边界**：MultiCraft 作为客户端接入协议服务器，不改变原服务器的客户端入口。公开发行的适配器仅校验并支持固定的官方 26.3 客户端；其他版本、Mod 和光影组合需要逐一适配、验证。

## 快速开始

先由 OP 准备已有 JDK 25、NVIDIA Vulkan 驱动、Python 3.12 与 [third-party 运行环境](THIRD_PARTY.md)。仓库不自动安装系统软件。以下从源码启动；完整账号、TLS 与后台运行步骤见[部署文档](docs/deployment.md)。

```sh
git clone https://github.com/zgdllt/MultiCraft.git
cd MultiCraft
python3 scripts/multicraft.py init
```

编辑生成的 `multicraft.json`，设置 `serverAddress`、JDK 路径、render node 和 `sessions`。没有配置 Microsoft 登录时，仅使用你获准管理的离线测试服务器。已有私有、可校验的游戏缓存可通过 `minecraftCache` 指定，prepare 将只读验证它；没有缓存时需显式请求官方下载：

```sh
# 使用已准备的缓存：不下载、不安装主机软件
python3 scripts/multicraft.py prepare --config multicraft.json

# 或显式下载固定版本的官方私有缓存
python3 scripts/multicraft.py prepare --config multicraft.json --download

python3 scripts/multicraft.py start --config multicraft.json
python3 scripts/multicraft.py status --config multicraft.json
```

按[部署文档](docs/deployment.md)准备 `.runtime/accounts.json` 与 TLS 证书后，启动独立 HTTPS 网关：

```sh
python3 -m multicraft.release_gateway --root . --config multicraft.json \
  --origin https://localhost:18443 --port 18443
```

浏览器打开配置的 HTTPS 入口。每个新身份从首页或“新身份标签页”进入；OP 登记的公钥只在本地对应私钥签名之后获得登录资格。同一公钥匹配多个身份时，由用户明确选择。

## 当前边界

同进程运行复用了部分进程级设施，但各会话仍有自己的 GPU device、场景、纹理和编码器。本版本没有实现跨视角场景几何共享、集中式光线追踪或能耗优化，不能把多开成功当作节能证据。

池中的槽位在启动时固定。普通断开、重新连接可在槽位中进行；终止原版槽位后，重新创建该槽位需要重启池。不同尺寸的热更新会调整对应的原生虚拟显示输出，不是把固定图像裁剪成新分辨率。

当前 Linux / NVIDIA / Wayland 组合存在原生全局退出兼容问题。显式 `defer-native` / `process-exit` 策略在所有客户端清理完成、真正释放已登记 Vulkan instance 和 SDL window 后，跳过有问题的最后一次进程级 `SDL_Quit`，再正常退出 Java。它不使用强制 `halt`，也不代表上游 SDL 退出问题已经修复。[架构文档](docs/architecture.md)说明清理条件与观察方法。

运行宿主、播放器解码、实际菜单/移动交互和可听音频分别验证。源码 CI 不创建 GPU 客户端；[验证文档](docs/testing.md)列出验收步骤，[发行实测记录](docs/release-validation.md)列出四会话功能检查、修正后的 4×60 / 单槽 30 fps 短测、真实四标签解码与单标签重新登录、OP 四路限额修改和正常关闭结果，并保留失败试验与尚未收敛的启动清理限制。当前内置浏览器不支持 Pointer Lock，本轮尚未完成外部浏览器的新版本捕获复测和四路声音的人耳确认。

## 开发与贡献

```sh
# 使用已经准备好测试依赖的 Python 环境
python3 -m unittest discover -s tests -v
npm test
npm run check:web
python3 scripts/release_source.py
```

无需 Node 打包工具或前端第三方库。`runtime-host/` 是固定版本的 Java 适配层，`multicraft/` 是鉴权、网关和媒体，`web/` 是产品界面，`scripts/` 是本地启动与检查工具。修改后请分别报告合同测试、真实原版客户端、浏览器收流及实际操作证据。

## 许可证与 third-party

自有源码和本仓库品牌资产使用 [MIT License](LICENSE)。**Minecraft、JDK、LWJGL、SDL、OpenAL、Selkies、pixelflux、PCMflux、驱动和系统音频组件均为 third-party**，不属于 MultiCraft 的 MIT 授权。版本、用途、来源与许可证见 [THIRD_PARTY.md](THIRD_PARTY.md)。游戏 JAR、资产、世界、认证材料和运行时日志不随源码发布。

MultiCraft 是独立社区实验项目，与 Mojang、Microsoft 或 NVIDIA 没有隶属或背书关系。使用游戏仍须遵守 [Minecraft EULA](https://www.minecraft.net/en-us/eula) 和适用的使用条款。

---

**English:** MultiCraft is an experimental Minecraft Java client pool and browser streamer. Multiple isolated native clients run in one JVM; each browser tab signs in to one OP-managed identity and receives its own view, input and audio. Pointer lock and fullscreen are independent. The current adapter is pinned to Minecraft 26.3 / Java 25 on a Linux NVIDIA Vulkan host, and currently supports authorized offline-mode test servers. It does not yet implement Microsoft account login, shared scene rendering, ray tracing or measured energy savings. See the linked deployment, architecture and third-party documents before running it.
