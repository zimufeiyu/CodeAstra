# 远程前端访问与接口预留说明

当前部署形态：FastAPI、PPU 上的 Qwen3-8B 模型和 React 前端都运行在远程 Linux 服务器。
浏览器在本地电脑打开页面时，推荐使用 SSH 本地端口转发，不把 8080 直接暴露到公网。

## 1. 远程服务

构建前端：

```bash
cd /home/lixun/check-code/frontend
export PATH=/home/lixun/check-code/.tools/node-v20.20.2-linux-x64/bin:$PATH
npm run build
```

启动 FastAPI，绑定远程本机地址：

```bash
cd /home/lixun/check-code
/usr/local/bin/python3 -m uvicorn code_review.api.main:app --host 127.0.0.1 --port 8080
```

同一个服务会提供 `GET /`、`GET /assets/*`、`GET /health`、`GET /health/instances` 和 `POST /v1/review`。

## 2. 本地电脑连接

在本地电脑打开终端并保持运行：

```bash
ssh -p 5122 -N -L 8080:127.0.0.1:8080 root@172.30.61.58
```

然后在本地浏览器访问：

```text
http://127.0.0.1:8080
```

请求链路：

```text
本地浏览器 -> 本地 127.0.0.1:8080 -> SSH tunnel -> 远程 127.0.0.1:8080 -> FastAPI -> PPU 模型服务
```

## 3. Vite 开发模式

构建进 FastAPI 是验收默认方式；如果单独跑 Vite，可以设置 `VITE_API_BASE_URL=http://127.0.0.1:8080`。
远程 Vite 常用端口是 5173，本地可以再开一条 tunnel：

```bash
ssh -p 5122 -N -L 5173:127.0.0.1:5173 root@172.30.61.58
```

## 4. 已接入与预留接口

已接入：`POST /v1/review`、`GET /health/instances`。

下一阶段预留：`POST /v1/review/files`、`GET /v1/reviews`、`GET /v1/reviews/{review_id}`、`POST /v1/reviews/{review_id}/followups`、`POST /v1/reports`。
当前前端已把上传、保存记录、导出报告、持久化接口、二次追问接口做成禁用按钮，留给下一阶段接线。

## 5. 安全边界

当前建议 FastAPI 只监听 `127.0.0.1:8080`，由 SSH tunnel 给本地电脑使用。
多人访问时再增加 HTTPS 反向代理、鉴权和访问审计。
