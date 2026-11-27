<div align="center">

# AgentPulse（AI 智能体脉搏 · 工时 / 用量 / 额度一体看板）

**一款轻量的 macOS 原生桌面挂件 + 菜单栏应用，将分散在各个 AI 客户端的智能体统一汇总，100% 本地优先，一站式监控 AI 有效工时、并行杠杆率、Token 与估算费用、订阅额度及久坐提醒。**

[English](README.md) | [简体中文](README.zh-CN.md)

**最新版本 [v0.7.0](CHANGELOG.md#070---2026-11-28)**

[![macOS 13+](https://img.shields.io/badge/macOS-13.0%2B-000000?logo=apple&logoColor=white)](https://www.apple.com/macos/)
[![Swift 5.9+](https://img.shields.io/badge/Swift-5.9%2B-F05138?logo=swift&logoColor=white)](https://swift.org/)
[![Python 3.9+ 零外部依赖](https://img.shields.io/badge/Python-3.9%2B%20%E9%9B%B6%E4%BE%9D%E8%B5%96-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-22C55E.svg)](LICENSE)

</div>

---

## 为什么做 AgentPulse？

当你同时使用多个 AI 编程智能体——**T3 Code**、**Codex**、**Claude Code**、**Antigravity**、**WorkBuddy**、**Hermes**、**Pi**、**OpenCode**、**Gemini CLI**、**ZCode**——它们的工作时长、Token 消耗和订阅额度分散在各自的 JSONL 日志和 SQLite 数据库中。

**AgentPulse** 就像你 AI 团队的实时“体征与脉搏看板”，在 macOS 桌面和顶部菜单栏直接回答四个核心问题：

1. **AI 今天到底有效工作了多久？** —— 采用**步间心跳（5 分钟）纯 AI 执行口径**，自动剔除人类阅读、思考、等待审批的静默时间，同时不对连续自主长任务设 30 分钟硬封顶。
2. **多 Agent 并行杠杆率有多高？** —— 将“各任务累计 AI 工时”与“去重墙钟时长”对比（例如两个任务同时各跑 30 分钟 = **1.00h AI 工时**、**0.50h 墙钟**、**2.00× 并行杠杆**）。
3. **花了多少 Token、折合多少 API 费用？** —— 零依赖本地增量日志解析 + 600+ 模型定价基线 + 自有持久化账本（支持 `今日 / 本周 / 本月 / 今年` 与近 30 天趋势）。
4. **订阅额度还剩多少？该起身活动了吗？** —— 独立慢轨额度监控（不阻塞主刷新）+ 内置 macOS 原生久坐提醒。

---

## 核心特性

- **步间心跳 · 纯 AI 执行口径**：
  - 以 **5 分钟步间心跳窗口**识别真实执行段：连续自主运行 45 分钟或 2 小时的长任务全额计入，不设人为 30 分钟上限。
  - 轮内超过 5 分钟的静默断档直接剔除（计 0 秒）；T3 Code 进一步显式扣除 `approval_request` / `user_input_request` / `secret_request` 等人工等待区间，即使短于 5 分钟也会扣减，待审批时不会误报“在岗”。
- **自有增量 Token、费用与项目追踪账本**：
  - 仅使用 Python 3.9+ 标准库，工时和用量均采用 v5 增量缓存。来源健康检查和刷新保护会在采集不完整时保留已确认的历史记录。
  - 内置 `engine/pricing.json`（600+ 模型费率基线），并支持在 `~/.agent-pulse/pricing_overrides.json` 自定义模型价格与别名。
  - 自有持久化账本存于 `~/.agent-pulse/ledger.json`，支持 **7 档时间切页**（`今天 / 昨天 / 本周 / 上周 / 本月 / 今年 / 全部`）、**近 26 周活跃热力图与连续活跃天数**、跨工具模型消耗排行，以及按项目查看 AI 工时、Token 和估算费用。
- **T3 Code ↔ Codex 双向精准去重**：
  - 无论工时还是 Token/费用，Codex 日志中 `originator == "T3 Code"` 的会话均精准归属 `T3 · Codex`，与独立 `Codex` 零重复计数。
- **快慢双轨刷新架构**：
  - **快轨（15 秒，纯本地）**：AI 工时、实时在岗检测、增量 Token / 费用与项目账本更新。
  - **慢轨（5 分钟，异步非阻塞）**：Codex 默认读取本地日志，可在「额度」页启用联网查询。Claude 读取 CLI 状态栏或桌面端缓存，其他供应商展示本地额度快照。
- **macOS 原生视觉与双层交互**：
  - **桌面挂件卡片**提供完整和紧凑两种布局、三档窗口层级、可拖动并记忆位置的标题栏、可点击的近 7 天柱状图，以及 Markdown 日报复制。**24 小时昼夜脉搏图**显示智能体何时在工作。**桌面指挥哨兵**展示「运行中」「待拍板」「刚收工」三种任务状态，可在访达中直达通过校验的目录。
  - **菜单栏单图标 + 宽幅 5 页签原生 Popover**：实时展示工时·在岗数、今日估算费用与常用额度徽章；**左键**打开动态宽幅 5 页签面板（**明细 / 大屏 / 项目 / 额度 / 设置**），**右键**呼出快捷菜单：
    - **明细页**展示分工具的有效工时、会话数、缓存命中率、五项 Token 明细、5 小时和 7 天额度条，以及可展开的模型明细。可一键复制所选周期的 Markdown 战报。
    - **大屏页**：近 26 周 GitHub 风格交互热力图（点击任意日期查看当日分工具账单）、连续活跃天数（Streak）与跨工具模型消耗排行。
    - **项目页**将 Git 和 Orca worktree 归到母项目，可查看**今日 / 本周 / 本月 / 全部**的 AI 工时与用量，支持搜索、置顶及一键在访达或终端打开。
    - **额度页**展示支持的额度窗口的燃尽配速，估算额度何时用完。桌面挂件的可选额度预警用未舍入的百分比判断是否达到 70%。
    - **设置页**的**本月订阅回本仪**把符合条件的用量按 API 费率折算，与配置的订阅月费对比。有可核实的 API 实付时会单独展示。
  - **内置久坐提醒**：支持 30 / 45 / 60 / 90 分钟间隔、锁屏/休眠超 5 分钟自动重置计时、macOS 原生通知（支持「已起身重置 / 推迟 10 分钟」）。
- **严格只读与隐私安全**：
  - 绝不改写或锁住任何 AI 客户端数据库（SQLite 一律采用 WAL 安全的临时快照只读读取）。
  - 零遥测、零统计上报、零云端同步。详细审计见 [SECURITY.md](SECURITY.md)。

---

## 支持的 AI 客户端

| AI 客户端 | AI 工时 | 实时在岗 | Token 与费用 | 订阅额度 | 本地读取路径 |
|---|:---:|:---:|:---:|:---:|---|
| **T3 Code**（Antigravity / Codex / Claude） | ✅ | ✅ | ✅（与 Codex 双向去重） | — | `~/.t3/userdata/statev2.sqlite` |
| **Codex**（CLI / Desktop） | ✅ | ✅ | ✅ | ✅ 本地日志 / 可开启联网 | `~/.codex/sessions/**/*.jsonl` |
| **Claude Code** | ✅ | ✅ | ✅ | ✅ 状态栏 / 缓存 | `~/.claude/projects/**/*.jsonl` |
| **WorkBuddy** | ✅ | ✅ | ✅ | — | `~/.workbuddy/projects/**/*.jsonl` |
| **Hermes**（主库 + 多 Profiles） | ✅ | ✅ | ✅ | — | `~/.hermes/state.db` 及 `profiles/*/state.db` |
| **Antigravity**（桌面端 / CLI） | ✅ | ✅ | — | 🟡 缓存快照 | `~/.gemini/antigravity/brain/` 和 `~/.gemini/antigravity-cli/brain/` |
| **Pi Agent** | ✅ | — | ✅ | — | `~/.pi/agent/sessions/**/*.jsonl` |
| **OpenCode** | ✅ | — | ✅ | — | `~/.local/share/opencode/opencode.db` |
| **Gemini CLI** | — | — | ✅ | — | `~/.gemini/tmp/**/session-*.json` |
| **CodeBuddy** | — | — | ✅ | — | `~/.codebuddy/projects/**/*.jsonl` |
| **ZCode** | — | — | ✅ | — | `~/.zcode/cli/db/db.sqlite` |
| **Kimi / Grok** | — | — | — | 🟡 缓存快照 | `~/.agent-pulse/quota_cache.json` |

> 各客户端的详细路径、字段说明与接入指南请见 [docs/PROVIDERS.md](docs/PROVIDERS.md)。

---

## 快速开始

### 运行环境

- **macOS 13.0+**（Ventura 及以上，支持 Apple Silicon 与 Intel）
- **Xcode Command Line Tools**（`xcode-select --install`，提供 Swift 5.9+ 编译器）
- **Python 3.9+**（系统自带 `/usr/bin/python3` 即可，无需安装任何第三方 `pip` 包）

### 构建与安装

```bash
# 1. 克隆仓库
git clone https://github.com/hohoweiyao-cmyk/agent-pulse.git
cd agent-pulse

# 2. 运行隔离单元测试
python3 -m unittest discover -s tests -v

# 3. 编译并打包原生 macOS 应用（产物位于 dist/AgentPulse.app）
bash scripts/build_app.sh

# 4. 安装到 /Applications 并启动
cp -R dist/AgentPulse.app /Applications/AgentPulse.app
open /Applications/AgentPulse.app
```

> **使用提示**：`build_app.sh` 默认使用 `dev.agentpulse.app`（可通过环境变量 `AGENTPULSE_BUNDLE_ID` 自定义）并对本地产物执行 ad-hoc 签名。启动后，按住卡片左上角「AgentPulse」标题或顶部空白区即可拖动。左键点击菜单栏图标打开五页签面板，右键打开快捷菜单。

### 命令行使用（纯终端 / 脚本集成）

无需打开桌面 GUI，也可直接通过 CLI 输出 JSON 或 Markdown 日报：

```bash
# 输出今日融合 Markdown 日报（工时 + Token + 估算费用）
python3 engine/collector.py --markdown

# 输出指定日期的日报
python3 engine/collector.py --markdown --date 2026-11-25

# 输出本周战报
python3 engine/collector.py --markdown --period week

# 输出格式化 JSON 快照（Schema v3）
python3 engine/collector.py --days 14 --pretty

# 手动执行慢轨额度探测
python3 engine/quota_collector.py --pretty

# 明确允许一次 Codex 联网额度查询
python3 engine/quota_collector.py --pretty --live-codex --force
```

---

## 进阶配置

### Claude Code CLI 额度接入（状态栏入口）

Claude Code 的状态栏命令可以将官方状态栏 JSON（含 5 小时与 7 天窗口利用率）直接传给 AgentPulse：

```bash
/usr/bin/python3 /Applications/AgentPulse.app/Contents/Resources/quota_collector.py --claude-statusline
```

- **没有自定义状态栏**：直接将上述命令设为 Claude Code 的 `statusLine` 命令即可。
- **已有自定义状态栏**：在原命令中通过 `tee` 等方式把同一份标准输入额外转发给上述命令，即可保留原状态栏显示。
- AgentPulse 只保存额度利用率、重置时间和采集时间，不存储任何对话内容。完整配置示例见 [docs/PROVIDERS.md](docs/PROVIDERS.md)。

### 自定义模型定价覆盖

在 `~/.agent-pulse/pricing_overrides.json` 中可添加自定义模型价格（单位：美元 / 百万 Token）或模型别名：

```json
{
  "models": {
    "my-provider/custom-model": {
      "in": 1.0,
      "out": 4.0,
      "cache_read": 0.1,
      "cache_write": 1.25
    }
  },
  "aliases": {
    "custom-fast": "my-provider/custom-model"
  }
}
```

### 从 Tokei 无损迁移历史账本

如果你此前使用过 Tokei（`~/.tokei/`），可一键执行安全幂等迁移脚本：

```bash
python3 scripts/import_tokei.py
```

脚本会备份 Tokei 来源账本和受影响的 AgentPulse 文件至 `~/.agent-pulse/backups/`。当天 Codex 实采总量小于 Tokei 汇总时，继续展示导入汇总，避免历史用量突然变少。

如需恢复账本，可先退出 AgentPulse，再执行下方命令。CLI 会检查备份并另存当前账本。

```bash
python3 engine/collector.py --restore-ledger ~/.agent-pulse/ledger.previous.json
```

> **从早期 AgentClock（`~/.agent-clock/`）升级？** 首次运行 AgentPulse 时会自动只读拷贝旧目录下的账本、定价覆盖、额度缓存与偏好到 `~/.agent-pulse/`，原有 `~/.agent-clock/` 保持不动作为备份。

---

## 目录结构

```text
agent-pulse/
├── engine/
│   ├── collector.py          # 统一采集入口与步间心跳工时引擎（Schema v3）
│   ├── usage_collector.py    # 零依赖增量 Token & 费用多源解析器
│   ├── pricing.py            # 模型定价匹配与费用计算引擎
│   ├── pricing.json          # 内置 600+ 模型费率基线（OpenRouter 数据源）
│   ├── pricing_overrides.json# 内置模型别名与价格覆盖
│   ├── ledger.py             # ~/.agent-pulse/ledger.json 持久化与四周期聚合
│   └── quota_collector.py    # 独立慢轨订阅额度探测器
├── macos/
│   ├── Package.swift         # SwiftPM 清单（macOS 13+，零外部依赖）
│   └── Sources/AgentPulse/   # 原生 SwiftUI + AppKit 挂件、五页签面板与久坐提醒
├── scripts/
│   ├── build_app.sh          # Release 编译 + 矢量图标生成 + .app 组装 + ad-hoc 签名
│   ├── generate_icon.swift   # CoreGraphics 矢量 macOS 图标生成脚本
│   └── import_tokei.py       # 先备份后迁移的 Tokei 历史导入脚本
├── tests/                    # 隔离夹具单元测试（不读取真实用户目录）
└── docs/
    ├── ARCHITECTURE.md       # 步间心跳算法、去重规则、双轨架构与 Schema v3 详解
    └── PROVIDERS.md          # 全量客户端路径、状态栏配置与排查指南
```

---

## 已知边界与路线图

### 当前边界
- **界面语言**：macOS 前端与 Markdown 日报目前为简体中文。
- **统计时区**：按自然日切分默认显式采用 `Asia/Shanghai (UTC+8)`。
- **仅缓存快照的额度源**：Antigravity / Kimi / Grok 目前仅展示本地已有缓存快照，暂未实现独立实时拉取，过期快照不触发高额度预警。
- **Codex 边缘场景**：压缩归档和跨文件重复 `response_id` 去重暂未覆盖。
- **工时回溯**：首次刷新尝试从仍可读取的日志回溯最多 182 天，后续保留每日工时。已有用量历史可能比工时更早。
- **费用估算**：未计价用量会单独提示。价格变更会重算近 14 天内可读取的日志，较早账本保留当时记录的费用。

### 后续路线图
- [ ] 支持自定义业务时区（默认跟随系统本地时区，可显式覆盖）
- [ ] 英文界面（`en-US`）与双语日报切换
- [ ] 支持 GitHub Releases 预编译 `.dmg` / Homebrew Cask 分发
- [ ] 扩展更多 AI 编程客户端适配（Cursor / Windsurf / Cline / Roo Code 等，欢迎 PR）

---

## 更多文档

- [架构与算法详解](docs/ARCHITECTURE.md)
- [支持的 AI 客户端与配置手册](docs/PROVIDERS.md)
- [隐私与安全说明](SECURITY.md)
- [贡献指南](CONTRIBUTING.md)
- [更新日志](CHANGELOG.md)

---

## 致谢与开源协议

- 卡片式细分指标、两级模型展开、活跃热力图与按项目追踪等展示设计灵感借鉴自 MIT 开源项目 [cclank/tokei](https://github.com/cclank/tokei)。
- 本项目基于 [MIT License](LICENSE) 开源。
