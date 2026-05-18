---
name: create-skill
description: 指导如何创建新的 Skill，包括目录结构、SKILL.md 格式规范和最佳实践
---

# 创建 Skill 指南

## 什么是 Skill

Skill 是一个放在 `skills/<skill-name>/SKILL.md` 的 Markdown 文件，为 Agent 提供特定领域的专业指引。Agent 启动时自动扫描所有 Skill，需要时通过 `load_skill` 工具按需加载完整内容。

## 目录结构

```
skills/
  <skill-name>/
    SKILL.md          ← 必须，Skill 定义文件
    examples/         ← 可选，示例文件
    templates/        ← 可选，模板文件
```

## SKILL.md 格式

文件由 **frontmatter**（YAML 元数据）和 **正文**（Markdown）组成：

```markdown
---
name: my-skill
description: 一句话描述该 skill 的用途（显示在可用列表中）
---

# Skill 标题

正文内容：详细的指引、步骤、规则、示例等。
```

### frontmatter 字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | Skill 唯一标识，建议用 kebab-case |
| `description` | 是 | 一句话描述，Agent 据此判断是否需要加载 |

## 创建步骤

1. 在 `skills/` 下新建目录：`mkdir -p skills/<skill-name>`
2. 创建 `skills/<skill-name>/SKILL.md`
3. 编写 frontmatter（name + description）
4. 编写正文指引
5. 重启 Agent 或新会话生效

## 正文编写最佳实践

### 结构清晰
- 用标题分层：`#` 概述、`##` 各步骤、`###` 细节
- 用列表和表格组织规则

### 指令明确
- 用祈使句："执行 X"、"确保 Y"、"不要 Z"
- 避免模糊表述："可以考虑" → "必须"

### 提供示例
- 每个关键操作给出代码示例
- 用 `<good-example>` 和 `<bad-example>` 标签对比

### 控制篇幅
- 保持在 200-500 行以内
- 过长时拆分为多个 Skill

### 避免重复
- 不要重复 System Prompt 已有的通用规则
- 只写该领域特有的专业知识

## 示例：一个完整的 Skill

```markdown
---
name: api-design
description: RESTful API 设计规范和最佳实践
---

# API 设计规范

## URL 命名
- 使用名词复数：`/users`、`/articles`
- 嵌套资源最多两层：`/users/{id}/posts`

## 状态码
| 操作 | 成功 | 失败 |
|------|------|------|
| GET | 200 | 404 |
| POST | 201 | 400/409 |
| PUT | 200 | 400/404 |
| DELETE | 204 | 404 |

## 示例

<good-example>
GET /api/v1/users?page=1&limit=20
响应：{ "data": [...], "total": 100, "page": 1 }
</good-example>

<bad-example>
GET /api/getUsers
响应：[...] （缺少分页元数据）
</bad-example>
```
