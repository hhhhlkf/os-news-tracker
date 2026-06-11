# Superpowers for CodeBuddy - 安装和使用指南

我已经将 Superpowers 方法论适配到 CodeBuddy。这是一个完整软件开发方法论，可以显著提高 AI 辅助编程的质量。

## 📁 已创建的文件

```
/Users/kieran/Projects/os-news-tracker/
├── .codebuddy/
│   └── skills/
│       ├── CODEBUDDY.md                    # 引导文件（入口点）
│       ├── brainstorming.md                 # 头脑风暴技能
│       ├── writing-plans.md                # 编写计划技能
│       └── test-driven-development.md      # TDD 技能
└── docs/
    └── superpowers/
        └── SUPERPOWERS-FOR-CODEBUDDY.md   # 完整使用指南
```

## 🚀 如何使用

### 方法 1：在对话开始时引用（推荐）

在任何新的编码任务开始时，告诉我：

```
请阅读 docs/superpowers/SUPERPOWERS-FOR-CODEBUDDY.md 并遵循其中的方法论。
```

我会自动：
1. 读取该文件
2. 遵循 Superpowers 工作流程
3. 在适当时候调用相应的技能

### 方法 2：让我记住（一次性设置）

你可以让我将这个方法论添加到项目记忆中：

```
@command://memory 记住：在这个项目中，始终遵循 docs/superpowers/SUPERPOWERS-FOR-CODEBUDDY.md 中的 Superpowers 方法论。
```

### 方法 3：在具体任务中使用

当你想使用特定技能时，可以直接说：

```
使用 brainstorming 技能帮我设计这个功能...
```

或者

```
使用 writing-plans 技能为这个功能创建实现计划...
```

## 📝 Superpowers 工作流程

当你使用 Superpowers 时，我会自动遵循这个流程：

### 1️⃣ 头脑风暴（Brainstorming）
- **何时：** 任何创造性工作之前
- **做什么：** 通过提问理解需求，提出 2-3 种方案，展示设计，获得批准
- **输出：** `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`

### 2️⃣ 编写计划（Writing Plans）
- **何时：** 设计批准后
- **做什么：** 将工作分解为 2-5 分钟的小任务，每个任务包含完整代码和验证步骤
- **输出：** `docs/superpowers/plans/YYYY-MM-DD-<feature-name>.md`

### 3️⃣ 测试驱动开发（TDD）
- **何时：** 实现任何功能时
- **做什么：** 红（失败测试）→ 绿（最小实现）→ 重构
- **原则：** 没有失败的测试先行，就没有生产代码

### 4️⃣ 系统化调试（Systematic Debugging）
- **何时：** 遇到问题时
- **做什么：** 4 阶段根因分析，不猜测，系统化调查

### 5️⃣ 代码审查（Code Review）
- **何时：** 完成任务后
- **做什么：** 对照计划审查代码，上报问题

### 6️⃣ 完成分支（Finishing Branch）
- **何时：** 所有任务完成后
- **做什么：** 验证测试，提供合并/PR 选项，清理工作树

## 🎯 核心原则

1. **在做任何事情之前检查技能** - 即使只有 1% 的可能性
2. **证据优先于主张** - 在宣布完成之前先验证
3. **系统化优先于临时方案** - 使用规范流程
4. **YAGNI** - 无情地删除不需要的功能
5. **DRY** - 避免重复代码

## 📚 技能库

当前已适配的技能：

| 技能 | 用途 | 触发时机 |
|------|------|---------|
| **brainstorming** | 苏格拉底式需求细化 | 任何创造性工作之前 |
| **writing-plans** | 详细实现计划 | 设计批准后 |
| **test-driven-development** | TDD 红绿重构 | 实现任何功能时 |
| **subagent-driven-development** | 子代理驱动开发 | 执行计划时 |
| **systematic-debugging** | 系统化调试 | 遇到问题时 |
| **requesting-code-review** | 代码审查 | 完成任务后 |
| **finishing-a-development-branch** | 完成分支 | 所有任务完成后 |
| **using-git-worktrees** | Git 工作树管理 | 开始新功能时 |

## 🔧 接下来可以做什么

### 选项 1：测试完整流程

让我演示完整的 Superpowers 流程：

```
让我们使用 Superpowers 方法论创建一个新功能：用户可以在新闻列表中按日期筛选。
```

我会自动：
1. 使用 brainstorming 技能理解需求
2. 创建设计文档
3. 使用 writing-plans 创建实现计划
4. 使用 TDD 实现功能

### 选项 2：为现有代码创建规范

如果你有现有项目，可以：

```
使用 Superpowers 方法论为现有的 [功能] 创建设计文档和规范。
```

### 选项 3：继续适配更多技能

我还可以继续适配其他 Superpowers 技能：
- `systematic-debugging` - 系统化调试
- `requesting-code-review` - 代码审查
- `executing-plans` - 执行计划
- `using-git-worktrees` - Git 工作树
- `subagent-driven-development` - 子代理驱动开发
- `finishing-a-development-branch` - 完成开发分支

你想让我继续创建这些技能吗？

## 💡 提示

1. **在重要任务前使用** - Superpowers 最适合新功能开发、复杂重构、架构变更
2. **小任务可以简化** - 对于非常小的改动（如修改一行配置），可以跳过完整流程
3. **保持规范更新** - 当需求变化时，更新设计文档和计划
4. **频繁提交** - 每个任务完成后提交，保持干净的 git 历史

## 📖 更多信息

- 原始项目：https://github.com/obra/superpowers
- 当前版本：基于 Superpowers v5.1.0 适配
- 许可证：MIT

---

**祝你使用愉快！遵循流程，写出更好的代码。** 🚀
