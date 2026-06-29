from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    AUTH_SECRET: str = "change-me-to-32-bytes-of-random-hex"
    DATABASE_URL: str = "postgresql+psycopg://rich:rich@localhost:5432/rich"
    FRONTEND_ORIGIN: str = "http://localhost:3000"

    # Claude / analysis-LLM API. ANTHROPIC_BASE_URL lets you point at any
    # Anthropic-compatible gateway — zenmux, dashscope coding plan, etc.
    # Empty = official api.anthropic.com.
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_BASE_URL: str = ""
    # Which model name to send to the gateway. Empty = use DEFAULT_MODEL
    # baked into services/analysis.py (currently kimi-k2.5 via dashscope).
    # Override per-deployment if the gateway shifts available models or you
    # want to A/B test (e.g., "claude-sonnet-4-6" on zenmux).
    ANALYSIS_MODEL: str = ""

    # --- Model A/B (6/10) ---------------------------------------------------
    # Route a deterministic slice of analyses to a second model so the
    # outcomes feedback loop can compare hit rates per model (anchors carry
    # a `model` column since 6/10). Bucketing is sha1(code + BJT date) % 100
    # — a stock stays on one model for the whole trading day, so intraday
    # re-analyses don't flap between models.
    #
    # CAVEAT: model B is called through the SAME ANTHROPIC_BASE_URL + API
    # key as model A — pick a name the current gateway serves (on dashscope
    # e.g. "MiniMax-M2.5" or "glm-4.7"). Cross-gateway A/B isn't supported.
    # Either value unset/0 = feature off.
    ANALYSIS_MODEL_B: str = ""
    ANALYSIS_AB_PCT: int = 0  # 0-100; % of (code, day) buckets sent to model B

    # --- 单股深挖档 (deep mode, 6/29) --------------------------------------
    # 详情页用户主动点「🔬 深度研究」时走的一条独立路径:OpenAI 兼容协议 +
    # thinking 模型(qwen3.7-max / deepseek-v4-pro)。~90s,reasoning 真生效。
    # 为什么不走 ANTHROPIC_BASE_URL:该 provider 的 Anthropic 兼容层对 thinking
    # 模型坏(force tool_choice 400 或静默吐损坏 JSON);只有 OpenAI 兼容协议
    # (/compatible-mode/v1) + stream + tool_choice="auto" 能跑通结构化输出。
    # batch/single/debate 路径不受影响(仍走 ANTHROPIC_*)。
    # **默认关闭**:ANALYSIS_DEEP_MODEL 为空 = 深挖档禁用(mode=deep 返回 503)。
    OPENAI_COMPAT_BASE_URL: str = ""   # 形如 https://.../compatible-mode/v1
    OPENAI_COMPAT_API_KEY: str = ""    # 为空时 adapter fallback 到 ANTHROPIC_API_KEY
    ANALYSIS_DEEP_MODEL: str = ""      # 推荐 qwen3.7-max;空 = 禁用深挖档
    # qwen/deepseek 在 stream 下默认就 thinking(实测 ~90s, reasoning 生效),
    # 显式传 enable_thinking=true 反而逼它多推理 → 慢到 ~118s。故默认 False。
    # 只有 kimi 系列需显式 true 才开 thinking —— 换 kimi 当 deep 模型时再开。
    ANALYSIS_DEEP_ENABLE_THINKING: bool = False

    # --- Replay eval gateway (火山 ARK coding plan) -------------------------
    # Separate from ANTHROPIC_* so the eval script can reach candidate models
    # without touching the production analysis path. Eval-only — runtime
    # generate() never reads these.
    VOLCENGINE_BASE_URL: str = ""
    VOLCENGINE_API_KEY: str = ""

    # Toggle the in-process APScheduler. Set False during local tests / when
    # running multiple replicas (only one should schedule).
    SCHEDULER_ENABLED: bool = True

    # Skip the password gate entirely. Intended ONLY for the testing window
    # before the tool gets handed to its real audience — anyone with the URL
    # gets full access while this is True. Default False so production stays
    # locked unless this is explicitly flipped on Railway.
    AUTH_DISABLED: bool = False

    # 6/24 安全收紧(codex 广审):
    # DIAG_TOKEN —— /api/_diag/*(含 eval)放行所需的 `X-Diag-Token: <值>` 头值
    # (见 main.py 中间件,常量时间比较)。生产在 Railway 设一个随机值;ops curl
    # 带上即可。**注意:默认 fail-closed —— DIAG_TOKEN 为空时 diag 全 403 锁死
    # (除非 DEV_DIAG_OPEN=true),不会裸奔。** 保护"能烧额度/删 eval/触发任务"的
    # 诊断面,未来新 diag 端点按路径前缀默认继承保护。
    DIAG_TOKEN: str = ""
    # DEV_DIAG_OPEN —— 本地 dev 显式放开 diag(不要 token)。**默认 False =
    # fail-closed**:生产只要不设这个,diag 就要求 token;万一 DIAG_TOKEN 也漏配,
    # diag 全 403(锁死),不会像以前那样裸奔(codex 安全审计 P1:别把"是否保护"
    # 交给人工记得配 env)。本地开发设 DEV_DIAG_OPEN=true。
    DEV_DIAG_OPEN: bool = False
    # COOKIE_SECURE —— 生产 HTTPS 下应 True(cookie 仅经 HTTPS 传)。本地 HTTP
    # 调试默认 False。Railway 设 COOKIE_SECURE=true。
    COOKIE_SECURE: bool = False

    # Admin user setup — used by the one-shot startup migration in
    # services/users.py. When set, lifespan ensures a User row with this
    # phone exists and assigns ALL watchlist rows with NULL user_id to it.
    # Re-running with the same value is idempotent.
    ADMIN_PHONE: str = ""


settings = Settings()
