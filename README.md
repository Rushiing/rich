# rich

A股盯盘与深度解析工具 — 内部使用，不超过 10 人共用。

> 详细的产品决策、阶段计划、架构说明都在 [CLAUDE.md](./CLAUDE.md)。
> 在新机器上首次接手开发，**先读 CLAUDE.md**。

## 当前状态

- ✅ Phase 0 — 骨架（Next.js + FastAPI + Postgres + 单密码登录）
- ✅ Phase 1 — 自选池（CRUD + 粘贴/Excel/CSV 导入 + akshare 校验）
- ✅ Phase 2 — 小时级抓取 + 信号引擎 + 盯盘视图
- ✅ Phase 3 — Claude 驱动的关键表 + 500 字深度解析（4h 缓存）
- ✅ Phase 4 — 移动端响应式 + PWA（可加桌面）

## 快速开始（本机）

依赖：Node 20+、Python 3.11+、Docker。

```bash
# 1. 复制环境变量
cp .env.example .env
# 然后改 .env 里的 APP_PASSWORD 和 AUTH_SECRET
#   AUTH_SECRET 推荐用：openssl rand -hex 32

# 2. 起 Postgres
docker compose up -d

# 3. 起后端
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# 4. 起前端（新终端）
cd frontend
npm install
npm run dev
```

打开 http://localhost:3000 → 跳到 `/login` → 输入 `APP_PASSWORD` → 登录后跳到 `/stocks`。

健康检查：`curl localhost:8000/health` → `{"status":"ok"}`

## 部署（Railway）

部署细节、env 配置、注意事项见 [CLAUDE.md](./CLAUDE.md#deployment-railway)。

## 目录

```
frontend/   Next.js 15 + React 19 + TypeScript
backend/    FastAPI + SQLAlchemy + Postgres
```

## 给 Claude 用户

每次在新终端开启 Claude Code 协作时，进入 repo 根目录后让 Claude 先读 `CLAUDE.md`，里面有完整的产品决策、阶段进度、约定和技术细节。
