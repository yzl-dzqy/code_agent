# Coding Agent

Coding Agent 是一个可在终端运行的编程代理，支持多模型、多工具，以及 CLI/TUI 双交互模式，适合代码生成、重构、排错和自动化任务。

## 主要特性

- **多模型支持**：Gemini / OpenAI / Claude / Ollama。
- **双交互模式**：
  - **CLI**：适合脚本、管道、重定向。
  - **TUI**：基于 [Textual](https://textual.readthedocs.io/) 的交互界面。
- **工具系统**：支持工具元数据、延迟工具发现（`tool_search`）、子代理（`agent`）和网页工具（`web_search` / `web_fetch`）。
- **Hook 机制**：支持 `SessionStart` / `PreToolUse` / `PostToolUse`。
- **Skill 机制**：同时加载项目级和包内置 skill。

## 安装与依赖

```bash
pip install -r requirements.txt
```

## 配置

至少需要配置一个可用模型的密钥：

- 推荐使用 JSON 配置。复制 `agent_config.example.json` 为 `agent_config.json`，然后修改模型、密钥和中转站地址：

```json
{
  "llm": {
    "provider": "openai",
    "model": "gemini-3-flash-preview",
    "known_models": [
      "openai:gemini-3-flash-preview",
      "openai:gpt-4o",
      "gemini:gemini-2.5-flash"
    ]
  },
  "openai": {
    "api_key": "YOUR_RELAY_API_KEY",
    "base_url": "https://your-relay.example.com/v1"
  }
}
```

也可以用 `AGENT_CONFIG_FILE=/path/to/agent_config.json` 指定配置文件路径。优先级为：命令行参数 / 真实环境变量 > JSON 配置 > 代码默认值。

- 也可以使用临时环境变量：

```bash
export AGENT_PROVIDER="gemini"
export AGENT_MODEL="gemini-2.5-flash"
export GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
export OPENAI_API_KEY="YOUR_OPENAI_API_KEY"
export ANTHROPIC_API_KEY="YOUR_ANTHROPIC_API_KEY"
export OPENAI_BASE_URL="https://api.openai.com/v1"
```

模型引用统一使用 `provider:model` 格式，例如：

```bash
python -m coding_agent --provider openai --model gpt-4o
python -m coding_agent --provider claude --model claude-sonnet-4-20250514
python -m coding_agent --provider ollama --model qwen2.5-coder:7b
```

交互模式中可用 `/model` 查看当前模型和候选列表，用以下任一方式切换：

```text
/model openai:gpt-4o
/model claude claude-sonnet-4-20250514
/model gemini-2.5-flash
```

仅输入模型名时，会根据常见前缀自动推断 provider（如 `gemini-`、`gpt-`、`claude-`）；无法推断时沿用当前 provider。

## 使用

### 启动方式

推荐：

```bash
python -m coding_agent
```

兼容直接运行目录：

```bash
python coding_agent
```

### CLI 模式

```bash
python -m coding_agent --cli
```

### TUI 模式

交互式终端下默认进入 TUI（不加 `--cli` 且无 `-p`/stdin 输入）。

### 一次性执行（One-shot）

```bash
python -m coding_agent -p "请写一个两数之和的 Python 函数"
```

### 管道 / 重定向（Raw 输出）

```bash
python -m coding_agent -p "生成一个 Flask hello world" > app.py
cat src/api.py | python -m coding_agent -p "Review 这段代码，找出类型不安全的地方"
```

> 当 stdout 非 TTY（如重定向）会自动进入 Raw 模式；也可手动 `-r/--raw`。

### 常用参数

- `--cli`：强制使用纯文本 CLI
- `-p, --prompt`：一次性执行后退出
- `-r, --raw`：强制 Raw 输出
- `--provider`：指定模型提供商
- `--model`：指定模型名称

## TUI 快捷键与命令

- 快捷键：
  - `Ctrl+Q` 退出
  - `Ctrl+C` 中断生成
  - `Ctrl+M` 切换模型
  - `Ctrl+E` 导出会话
  - `Ctrl+L` 清屏
- 斜杠命令（支持 `/` 自动补全）：
  - `/help` `/model` `/workdir` `/hooks`
  - `/todo` `/tasks` `/bg` `/cron` `/worktree`
  - `/memories` `/skills` `/prompt` `/sections`
  - `/export` `/clear` `/clear_todo` `/metrics` `/stop`

## Hook 配置（严格模式）

- 配置文件固定为：
  - `coding_agent/.hooks/.hooks.json`
- 信任标记固定为：
  - `coding_agent/.agent/.agent_trusted`
- Hook 命令执行目录：
  - `coding_agent/`（包根目录）

示例（`coding_agent/.hooks/.hooks.json`）：

```json
{
  "hooks": {
    "SessionStart": [
      { "command": "echo '{\"additionalContext\": \"Hook 已激活\"}'" }
    ],
    "PreToolUse": [
      { "matcher": "bash", "command": "python .hooks/check_bash.py" }
    ],
    "PostToolUse": [
      { "matcher": "*", "command": "python .hooks/audit_log.py" }
    ]
  }
}
```

## Skill 加载规则

- 同时加载：
  - `CFG.pkg_dir / "skills"`（内置）
  - `CFG.workdir / "skills"`（项目自定义）
- 同名 skill 时，项目目录会覆盖内置版本。

## 目录概览

- `coding_agent/__main__.py`：统一入口（兼容 `python -m coding_agent` 与 `python coding_agent`）
- `coding_agent/agent.py`：核心代理循环
- `coding_agent/ui/tui.py`：TUI 交互界面
- `coding_agent/ui/cli.py`：CLI 交互界面
- `coding_agent/hooks.py`：Hook 系统
- `coding_agent/skills.py`：Skill 加载与注入
- `coding_agent/tools/`：工具定义与注册
