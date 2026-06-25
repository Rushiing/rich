"""③ 持仓决策漏斗 — 用户选择埋点(append-only)。

用户在详情页/列表页漏斗里每点一次"持仓 / 盈亏 / 风险",前端 fire-and-forget
POST 一条到这里。**不 upsert** —— 每条是"用户那一刻的处境"锚点,事后用来记分
(验证"已持仓情境建议含金量高")。anchor_close 由服务端取最新 snapshot 价,
不信客户端传值。owner 隔离,需登录。

Endpoint:
- POST /api/funnel/{code}   append 一条漏斗选择
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import require_auth
from ..db import get_db
from ..models import FunnelChoice, Snapshot
from ..services.users import resolve_owner

router = APIRouter(prefix="/api/funnel", tags=["funnel"])

_PNLS = {"盈", "平", "亏"}
_TIERS = {"aggressive", "neutral", "conservative"}


class FunnelChoiceIn(BaseModel):
    held: bool
    pnl: str | None = Field(default=None, max_length=4)
    tier: str = Field(max_length=16)


class FunnelStateOut(BaseModel):
    held: bool
    pnl: str | None
    tier: str


def _require_owner(user_id: int | None, db: Session) -> int:
    owner = resolve_owner(user_id, db)
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="漏斗埋点需要登录账号",
        )
    return owner


@router.post("/{code}")
def log_funnel_choice(
    code: str,
    body: FunnelChoiceIn,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Append 一条漏斗选择。校验枚举与代码;anchor_close 取服务端最新 snapshot
    价。返回 {ok:true} —— 前端 fire-and-forget,不读返回。"""
    owner = _require_owner(user_id, db)

    if not (code.isdigit() and len(code) == 6):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="非法代码")

    # 枚举兜底:脏值收敛到默认,不报错 —— 埋点不该因坏输入打断用户。
    tier = body.tier if body.tier in _TIERS else "neutral"
    pnl = body.pnl if (body.held and body.pnl in _PNLS) else None

    snap = (
        db.query(Snapshot)
        .filter(Snapshot.code == code)
        .order_by(Snapshot.ts.desc())
        .first()
    )
    anchor = snap.price if (snap and snap.price and snap.price > 0) else None

    db.add(FunnelChoice(
        user_id=owner, code=code,
        held=bool(body.held), pnl=pnl, tier=tier,
        anchor_close=anchor,
    ))
    db.commit()
    return {"ok": True}


@router.get("/mine")
def my_funnel_choices(
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """用户每只票的**最新**漏斗选择(给列表页 hydrate,让点选跟账号走、跨设备一致)。
    返回 {code: {held, pnl, tier}}。"""
    owner = _require_owner(user_id, db)
    rows = (
        db.query(FunnelChoice)
        .filter(FunnelChoice.user_id == owner)
        .order_by(
            FunnelChoice.code,
            FunnelChoice.created_at.desc(),
            FunnelChoice.id.desc(),
        )
        .all()
    )
    out: dict[str, dict] = {}
    for r in rows:
        if r.code not in out:  # 每 code 第一条 = 最新(已按 code, created_at desc 排)
            out[r.code] = {"held": r.held, "pnl": r.pnl, "tier": r.tier}
    return out


@router.get("/{code}/latest", response_model=FunnelStateOut | None)
def latest_funnel_choice(
    code: str,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """该票最新漏斗选择(详情页 hydrate)。null = 没记录过 → 前端退回 localStorage/默认。"""
    owner = _require_owner(user_id, db)
    row = (
        db.query(FunnelChoice)
        .filter(FunnelChoice.user_id == owner, FunnelChoice.code == code)
        .order_by(FunnelChoice.created_at.desc(), FunnelChoice.id.desc())
        .first()
    )
    if not row:
        return None
    return FunnelStateOut(held=row.held, pnl=row.pnl, tier=row.tier)
