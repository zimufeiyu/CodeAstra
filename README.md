# CodeAstra（星鉴）

CodeAstra 是一个面向团队代码审查场景的智能审查平台。它将静态分析、本地 Qwen 模型、DeepSeek API、证据校验、审查历史、追问和 GitLab 导入整合到统一的网页工作区。

## 核心能力

- Python/C++ 代码、文件和项目审查
- Qwen3-8B、Qwen3-32B 与 DeepSeek API 多模型路由
- 静态规则与大模型结论合并，并校验文件、行号和证据
- 按用户隔离审查记录、文件、追问和本地集成配置
- 登录认证、单设备会话、密码修改和管理员用户管理
- GitLab 文件/合并请求导入及本地版本对比
- 审查历史、问题处理、修订记录、上下文追问和 SSE 进度

## 技术结构

- 后端：FastAPI、Pydantic、SQLite、HTTPX
- 前端：React、TypeScript、Vite
- 模型：SGLang/OpenAI 兼容接口、本地 Qwen、DeepSeek API
- 架构：领域模型、应用服务、基础设施适配器和 API 层分离

详细设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 本地开发

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cd frontend
npm install
npm run build
```

启动后端：

```bash
.venv/bin/python -m uvicorn code_review.api.main:app --app-dir src --host 127.0.0.1 --port 8081
```

配置项请参考源码中的 `code_review.config.settings.Settings`。不要把真实 API Key、数据库、模型权重或 `.env` 提交到仓库。

## 安全说明

本仓库只包含核心源码、测试和设计说明，不包含生产数据库、用户数据、API Key、SSH 私钥、模型权重、日志、构建产物或服务器运行配置。

