"""展馆多语展签审校系统 — FastAPI 主应用。

角色：
  librarian  馆员：维护中文原文与语言，发布语言快照
  translator 译者：提交译文、可校对他人译文
  reviewer   校对：确认译文、也可提交译文
任何译者/校对账号均不得确认自己提交的译文。
"""
from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .db import get_conn, hash_password, now

# ---------------------------- 请求模型 ----------------------------


class LoginBody(BaseModel):
    username: str
    password: str


class LabelBody(BaseModel):
    source_text: str = Field(min_length=1)
    source_rev: int  # 读取时的原文修订号（乐观锁）


class LanguageBody(BaseModel):
    code: str = Field(min_length=1, max_length=10)
    name: str = Field(min_length=1, max_length=80)


class TranslationBody(BaseModel):
    text: str = Field(min_length=1)
    source_rev: int  # 译者读取时看到的原文修订号
    trans_rev: Optional[int] = None  # 修改既有译文时须携带，新建留空


class ConfirmBody(BaseModel):
    source_rev: int
    trans_rev: int


class WithdrawBody(BaseModel):
    status_rev: int  # 读取时的状态版本（乐观锁）
    reason: str = Field(min_length=1)


class RestoreBody(BaseModel):
    status_rev: int  # 读取时的状态版本（乐观锁）


class ClaimBody(BaseModel):
    source_rev: int  # 读取时的原文修订号
    trans_rev: int   # 读取时的译文修订号


# 认领有效期：30 分钟
CLAIM_TTL = timedelta(minutes=30)


# ---------------------------- 认领辅助 ----------------------------


def parse_ts(s: str) -> datetime:
    """解析 db.now() 生成的 UTC 时间字符串（无时区），按 UTC 处理。"""
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def get_claim_for(conn, label_id: int, language_id: int):
    """返回该译文当前有效的认厉行（未到期），无则 None。到期行原样保留、视为无效。"""
    return conn.execute(
        "SELECT * FROM claims WHERE label_id = ? AND language_id = ? AND expires_at > ?",
        (label_id, language_id, now()),
    ).fetchone()


def serialize_claim(conn, row) -> dict:
    """把认厉行组装为对外 JSON（带认领人姓名/账号、剩余秒数）。"""
    u = conn.execute(
        "SELECT id, username, display_name FROM users WHERE id = ?",
        (row["claimant_id"],),
    ).fetchone()
    remaining = max(0, int((parse_ts(row["expires_at"]) - datetime.now(timezone.utc)).total_seconds()))
    return {
        "claimant_id": row["claimant_id"],
        "claimant_name": u["display_name"] if u else f"#{row['claimant_id']}",
        "claimant_username": u["username"] if u else None,
        "source_rev_at_claim": row["source_rev_at_claim"],
        "trans_rev_at_claim": row["trans_rev_at_claim"],
        "claimed_at": row["claimed_at"],
        "expires_at": row["expires_at"],
        "remaining_seconds": remaining,
    }


# ---------------------------- 应用生命周期 ----------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="展馆多语展签审校系统", lifespan=lifespan)


# ---------------------------- 认证辅助 ----------------------------

SESSION_COOKIE = "exhibit_session"


def current_user(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="会话已失效，请重新登录")
    return dict(row)


def require_roles(*roles: str):
    def dep(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="当前角色无权执行该操作")
        return user
    return dep


def public_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------- 登录 / 登出 / 我 ----------------------------


@app.post("/api/login")
def login(body: LoginBody):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (body.username.strip(),)).fetchone()
        if not row or not secrets.compare_digest(
            row["pw_hash"], hash_password(body.password, row["salt"])
        ):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)",
            (token, row["id"], now()),
        )
        conn.commit()
    resp = JSONResponse({
        "id": row["id"], "username": row["username"],
        "display_name": row["display_name"], "role": row["role"],
    })
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {
        "id": user["id"], "username": user["username"],
        "display_name": user["display_name"], "role": user["role"],
    }


# ---------------------------- 展签（原文） ----------------------------


@app.get("/api/labels")
def list_labels(user: dict = Depends(current_user)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT l.*, uw.display_name AS withdrawn_by_name, "
            "       ur.display_name AS restored_by_name "
            "FROM labels l "
            "LEFT JOIN users uw ON uw.id = l.withdrawn_by "
            "LEFT JOIN users ur ON ur.id = l.restored_by "
            "ORDER BY l.id"
        ).fetchall()
    return [dict(r) for r in rows]


def _get_label_with_names(conn, label_id: int):
    return conn.execute(
        "SELECT l.*, uw.display_name AS withdrawn_by_name, "
        "       ur.display_name AS restored_by_name "
        "FROM labels l "
        "LEFT JOIN users uw ON uw.id = l.withdrawn_by "
        "LEFT JOIN users ur ON ur.id = l.restored_by "
        "WHERE l.id = ?",
        (label_id,),
    ).fetchone()


@app.post("/api/labels")
def create_label(body: LabelBody, user: dict = Depends(require_roles("librarian"))):
    with db._write_lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO labels (source_text, source_rev, created_at, updated_at) VALUES (?,1,?,?)",
            (body.source_text.strip(), now(), now()),
        )
        conn.commit()
        label_id = cur.lastrowid
    return {"id": label_id, "source_rev": 1}


@app.put("/api/labels/{label_id}")
def update_label(label_id: int, body: LabelBody, user: dict = Depends(require_roles("librarian"))):
    """修改原文：原文修订号 +1，该展签全部语言的译文确认失效（confirmed=0）。

    即使新文本与旧文本完全相同也照样递增修订号，使所有旧确认失效。
    请求必须携带读取时的 source_rev，否则视为过期请求拒绝（保留现状）。
    """
    with db._write_lock, get_conn() as conn:
        row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not row:
            raise HTTPException(404, "展签不存在")
        if body.source_rev != row["source_rev"]:
            raise HTTPException(
                409,
                f"原文已被他人修改（服务器修订号 {row['source_rev']}，"
                f"请求基于 {body.source_rev}），请刷新后重试",
            )
        conn.execute(
            "UPDATE labels SET source_text = ?, source_rev = source_rev + 1, updated_at = ? WHERE id = ?",
            (body.source_text.strip(), now(), label_id),
        )
        # 旧译文确认全部失效（译文文本保留，状态变为“过期”）
        conn.execute(
            "UPDATE translations SET confirmed = 0, confirmer_id = NULL "
            "WHERE label_id = ? AND confirmed = 1",
            (label_id,),
        )
        # 原文修订使该展签各语言的认领全部失效
        conn.execute("DELETE FROM claims WHERE label_id = ?", (label_id,))
        conn.commit()
        new_row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
    return dict(new_row)


# ---------------------------- 撤展 / 复展 ----------------------------


@app.post("/api/labels/{label_id}/withdraw")
def withdraw_label(
    label_id: int, body: WithdrawBody,
    user: dict = Depends(require_roles("librarian")),
):
    """馆员撤展：按展签编号 + 读取时的状态版本 + 原因撤展。

    - 状态版本过期 / 已是撤展状态（重复变更）→ 409，数据不变；
    - 无权 → 403；编号无效 → 404；原因为空 → 422。
    - 立即对所有语言生效：游客看到的“最新发布内容”中该展签被隐藏
      （历史快照、已登录用户查询快照的内容均不改变）。
    - 译文文本与确认保留（看板不再展示待办，复展时另行清除确认）。
    """
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(422, "撤展原因不能为空")
    with db._write_lock, get_conn() as conn:
        row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not row:
            raise HTTPException(404, "展签编号无效")
        if body.status_rev != row["status_rev"]:
            raise HTTPException(
                409,
                f"展签状态已被他人变更（服务器状态版本 {row['status_rev']}，"
                f"请求基于 {body.status_rev}），请刷新后重试",
            )
        if row["status"] == "withdrawn":
            raise HTTPException(409, "该展签已撤展，请勿重复撤展")

        ts = now()
        conn.execute(
            "UPDATE labels SET status = 'withdrawn', status_rev = status_rev + 1, "
            "withdrawn_reason = ?, withdrawn_by = ?, withdrawn_at = ?, "
            "restored_by = NULL, restored_at = NULL WHERE id = ?",
            (reason, user["id"], ts, label_id),
        )
        # 对所有语言立即从游客最新发布内容中隐藏（历史快照 JSON 不动）
        conn.execute(
            "UPDATE translations SET public_hidden = 1 WHERE label_id = ?",
            (label_id,),
        )
        # 撤展使该展签各语言的认领全部失效
        conn.execute("DELETE FROM claims WHERE label_id = ?", (label_id,))
        conn.commit()
        new_row = _get_label_with_names(conn, label_id)
    return dict(new_row)


@app.post("/api/labels/{label_id}/restore")
def restore_label(
    label_id: int, body: RestoreBody,
    user: dict = Depends(require_roles("librarian")),
):
    """馆员复展：按展签编号 + 状态版本复展。

    - 状态版本过期 / 本就在展（重复变更）→ 409；无权 → 403；编号无效 → 404。
    - 保留各语言译文文本，但清除该展签各语言的确认；
      游客在“译文按当前原文经他人重新确认并发布新快照”之前仍看不到它
      （public_hidden 维持为 1，随各语言下一次含该条的快照发布清除）。
    """
    with db._write_lock, get_conn() as conn:
        row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not row:
            raise HTTPException(404, "展签编号无效")
        if body.status_rev != row["status_rev"]:
            raise HTTPException(
                409,
                f"展签状态已被他人变更（服务器状态版本 {row['status_rev']}，"
                f"请求基于 {body.status_rev}），请刷新后重试",
            )
        if row["status"] == "on_display":
            raise HTTPException(409, "该展签当前在展，无需复展")

        ts = now()
        conn.execute(
            "UPDATE labels SET status = 'on_display', status_rev = status_rev + 1, "
            "restored_by = ?, restored_at = ? WHERE id = ?",
            (user["id"], ts, label_id),
        )
        # 保留译文，清除该展签各语言的确认（须重新经他人确认）
        conn.execute(
            "UPDATE translations SET confirmed = 0, confirmer_id = NULL WHERE label_id = ?",
            (label_id,),
        )
        # 复展同样使该展签各语言的认领全部失效
        conn.execute("DELETE FROM claims WHERE label_id = ?", (label_id,))
        conn.commit()
        new_row = _get_label_with_names(conn, label_id)
    return dict(new_row)


# ---------------------------- 语言 ----------------------------


@app.get("/api/languages")
def list_languages(user: dict = Depends(current_user)):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM languages ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/languages")
def create_language(body: LanguageBody, user: dict = Depends(require_roles("librarian"))):
    code = body.code.strip().lower()
    with db._write_lock, get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM languages WHERE code = ?", (code,)).fetchone()
        if exists:
            raise HTTPException(409, f"语言代码 {code} 已存在")
        cur = conn.execute(
            "INSERT INTO languages (code, name, created_at) VALUES (?,?,?)",
            (code, body.name.strip(), now()),
        )
        conn.commit()
        lid = cur.lastrowid
    return {"id": lid, "code": code, "name": body.name.strip()}


# ---------------------------- 看板核心：按语言聚合 ----------------------------


def build_board(conn, language_id: int) -> dict:
    lang = conn.execute("SELECT * FROM languages WHERE id = ?", (language_id,)).fetchone()
    if not lang:
        raise HTTPException(404, "语言不存在")

    labels = conn.execute(
        "SELECT l.*, uw.display_name AS withdrawn_by_name, "
        "       ur.display_name AS restored_by_name "
        "FROM labels l "
        "LEFT JOIN users uw ON uw.id = l.withdrawn_by "
        "LEFT JOIN users ur ON ur.id = l.restored_by "
        "ORDER BY l.id"
    ).fetchall()
    trans_rows = conn.execute(
        "SELECT t.*, uu.display_name AS translator_name, "
        "       ur.display_name AS confirmer_name "
        "FROM translations t "
        "LEFT JOIN users uu ON uu.id = t.translator_id "
        "LEFT JOIN users ur ON ur.id = t.confirmer_id "
        "WHERE t.language_id = ?",
        (language_id,),
    ).fetchall()
    trans_map = {t["label_id"]: dict(t) for t in trans_rows}

    # 该语言当前有效（未到期、未释放）的认领，按展签索引；认领人姓名一并取出
    claim_rows = conn.execute(
        "SELECT c.*, u.display_name AS claimant_name, u.username AS claimant_username "
        "FROM claims c JOIN users u ON u.id = c.claimant_id "
        "WHERE c.language_id = ? AND c.expires_at > ?",
        (language_id, now()),
    ).fetchall()
    claim_map = {r["label_id"]: r for r in claim_rows}

    items = []
    withdrawn_items = []
    counts = {"missing": 0, "pending": 0, "stale": 0, "ready": 0}
    for label in labels:
        if label["status"] == "withdrawn":
            # 撤展展签：不计入待办统计与发布条件，也不写入后续快照
            withdrawn_items.append({
                "label": dict(label),
                "translation": trans_map.get(label["id"]),
                "status": "withdrawn",
                "reason": label["withdrawn_reason"] or "（未填写撤展原因）",
            })
            continue
        t = trans_map.get(label["id"])
        if t is None:
            status = "missing"           # 待译：尚无译文
            reason = "尚无译文"
        elif t["source_rev_at_edit"] != label["source_rev"]:
            status = "stale"             # 过期：译文基于旧版原文
            reason = f"译文基于原文修订 {t['source_rev_at_edit']}，当前为 {label['source_rev']}"
        elif not t["confirmed"]:
            status = "pending"           # 待审：当前原文 + 未确认
            reason = "译文待校对确认"
        else:
            status = "ready"
            reason = "已确认，可发布"
        counts[status] += 1
        claim_row = claim_map.get(label["id"])
        remaining = None
        if claim_row is not None:
            remaining = max(
                0, int((parse_ts(claim_row["expires_at"]) - datetime.now(timezone.utc)).total_seconds())
            )
        items.append({
            "label": dict(label),
            "translation": t,
            "status": status,
            "reason": reason,
            "claim": None if claim_row is None else {
                "claimant_id": claim_row["claimant_id"],
                "claimant_name": claim_row["claimant_name"] or f"#{claim_row['claimant_id']}",
                "claimant_username": claim_row["claimant_username"],
                "source_rev_at_claim": claim_row["source_rev_at_claim"],
                "trans_rev_at_claim": claim_row["trans_rev_at_claim"],
                "claimed_at": claim_row["claimed_at"],
                "expires_at": claim_row["expires_at"],
                "remaining_seconds": remaining,
            },
        })

    total = len(items)
    blocking = []
    if not labels:
        blocking.append("该语言下尚无任何展签")
    elif total == 0:
        blocking.append("当前没有在展展签（全部展签均已撤展），不能发布")
    if counts["missing"]:
        blocking.append(f"{counts['missing']} 条待译")
    if counts["stale"]:
        blocking.append(f"{counts['stale']} 条译文基于旧版原文（过期）")
    if counts["pending"]:
        blocking.append(f"{counts['pending']} 条待校对确认")
    publishable = total > 0 and counts["ready"] == total

    # 最新快照
    snap = conn.execute(
        "SELECT s.id, s.version, s.published_at, u.display_name AS published_by_name "
        "FROM snapshots s LEFT JOIN users u ON u.id = s.published_by "
        "WHERE s.language_id = ? ORDER BY s.version DESC LIMIT 1",
        (language_id,),
    ).fetchone()

    return {
        "language": dict(lang),
        "total": total,
        "withdrawn_total": len(withdrawn_items),
        "counts": counts,
        "items": items,
        "withdrawn_items": withdrawn_items,
        "publishable": publishable,
        "blocking_reasons": blocking,
        "latest_snapshot": dict(snap) if snap else None,
    }


@app.get("/api/board/{language_id}")
def board(language_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        return build_board(conn, language_id)


@app.get("/api/overview")
def overview(user: dict = Depends(current_user)):
    """各语言汇总 + 全局待办，界面顶部使用。"""
    with get_conn() as conn:
        langs = conn.execute("SELECT * FROM languages ORDER BY id").fetchall()
        result = []
        for lang in langs:
            b = build_board(conn, lang["id"])
            result.append({
                "language": b["language"],
                "total": b["total"],
                "withdrawn_total": b["withdrawn_total"],
                "counts": b["counts"],
                "publishable": b["publishable"],
                "blocking_reasons": b["blocking_reasons"],
                "latest_snapshot": b["latest_snapshot"],
            })
    return result


# ---------------------------- 提交译文 ----------------------------


@app.post("/api/board/{language_id}/labels/{label_id}/translation")
def submit_translation_for_label(
    language_id: int, label_id: int, body: TranslationBody,
    user: dict = Depends(require_roles("translator", "reviewer")),
):
    with db._write_lock, get_conn() as conn:
        label = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not label:
            raise HTTPException(404, "展签不存在")
        if label["status"] == "withdrawn":
            raise HTTPException(409, "该展签已撤展，不能提交或修订译文")
        lang = conn.execute("SELECT 1 FROM languages WHERE id = ?", (language_id,)).fetchone()
        if not lang:
            raise HTTPException(404, "语言不存在")

        # 乐观锁：读取后原文若已被修改，拒绝
        if body.source_rev != label["source_rev"]:
            raise HTTPException(
                409,
                f"原文已更新（当前修订号 {label['source_rev']}，你的请求基于 {body.source_rev}），"
                "请基于最新原文重新翻译",
            )

        existing = conn.execute(
            "SELECT * FROM translations WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        ).fetchone()

        if existing:
            # 乐观锁：译文也已被他人更新，拒绝
            if body.trans_rev is None or body.trans_rev != existing["trans_rev"]:
                raise HTTPException(
                    409,
                    f"译文已被更新（当前译文修订号 {existing['trans_rev']}），请刷新后重试",
                )
            conn.execute(
                "UPDATE translations SET text = ?, trans_rev = trans_rev + 1, "
                "source_rev_at_edit = ?, confirmed = 0, confirmer_id = NULL, "
                "translator_id = ?, updated_at = ? WHERE id = ?",
                (body.text.strip(), label["source_rev"], user["id"], now(), existing["id"]),
            )
        else:
            if body.trans_rev not in (None, 1):
                raise HTTPException(409, "译文尚不存在，不应携带译文修订号")
            cur = conn.execute(
                "INSERT INTO translations (label_id, language_id, text, trans_rev, "
                "source_rev_at_edit, confirmed, translator_id, updated_at) "
                "VALUES (?,?,?,1,?,0,?,?)",
                (label_id, language_id, body.text.strip(), label["source_rev"],
                 user["id"], now()),
            )
        # 译文修订（trans_rev 变化）使该译文上的认领失效
        conn.execute(
            "DELETE FROM claims WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        )
        conn.commit()
        t = conn.execute(
            "SELECT * FROM translations WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        ).fetchone()
    return dict(t)


# ---------------------------- 认领 / 释放（本轮新增） ----------------------------


@app.get("/api/board/{language_id}/labels/{label_id}/claim")
def get_claim(
    language_id: int, label_id: int, user: dict = Depends(current_user)
):
    """查询某译文当前的有效认领；无有效认领时 claim 为 null（任何人可查）。"""
    with get_conn() as conn:
        label = conn.execute("SELECT id, status FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not label:
            raise HTTPException(404, "展签编号无效")
        if not conn.execute("SELECT 1 FROM languages WHERE id = ?", (language_id,)).fetchone():
            raise HTTPException(404, "语言不存在")
        row = get_claim_for(conn, label_id, language_id)
        return {"claim": serialize_claim(conn, row) if row else None}


@app.post("/api/board/{language_id}/labels/{label_id}/claim")
def claim_translation(
    language_id: int, label_id: int, body: ClaimBody,
    user: dict = Depends(require_roles("translator", "reviewer")),
):
    """认领 30 分钟。凭展签 + 语言 + 读取时的双修订号；提交该译文的账号不得认领。

    拒绝且不改变状态的情形：
      404 展签/语言不存在、尚无译文；403 认领人是该译文提交人（禁止自领）；
      409 展签已撤展 / 译文已确认 / 双修订号过期 / 已有他人的有效认领。
    同一账号对自己仍持有的有效认领重复认领：幂等返回原认领（不延长、不重置到期）。
    """
    with db._write_lock, get_conn() as conn:
        label = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not label:
            raise HTTPException(404, "展签编号无效")
        if not conn.execute("SELECT 1 FROM languages WHERE id = ?", (language_id,)).fetchone():
            raise HTTPException(404, "语言不存在")
        t = conn.execute(
            "SELECT * FROM translations WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        ).fetchone()
        if not t:
            raise HTTPException(404, "尚无译文，无法认领")

        # 提交人自领始终 403（优先于其他冲突判断）
        if t["translator_id"] == user["id"]:
            raise HTTPException(403, "提交该译文的账号不得认领自己的译文")

        existing = get_claim_for(conn, label_id, language_id)
        if existing is not None:
            if existing["claimant_id"] == user["id"]:
                # 本人重复认领：幂等返回，到期时间不变
                return serialize_claim(conn, existing)
            other = conn.execute(
                "SELECT display_name FROM users WHERE id = ?",
                (existing["claimant_id"],),
            ).fetchone()
            raise HTTPException(409, {
                "message": "该译文已被他人认领，请等待对方释放或认领到期",
                "claim": serialize_claim(conn, existing),
                "held_by": other["display_name"] if other else f"#{existing['claimant_id']}",
            })

        # 以下均为“不可认领且不改变状态”的场景
        if label["status"] == "withdrawn":
            raise HTTPException(409, "该展签已撤展，不能认领译文")
        if t["confirmed"]:
            raise HTTPException(409, "该译文已确认，无需认领")
        if body.source_rev != label["source_rev"]:
            raise HTTPException(
                409,
                f"原文已更新（当前修订号 {label['source_rev']}，你的请求基于 {body.source_rev}），"
                "请刷新后再认领",
            )
        if body.trans_rev != t["trans_rev"]:
            raise HTTPException(
                409,
                f"译文已被更新（当前译文修订号 {t['trans_rev']}，你的请求基于 {body.trans_rev}），"
                "请刷新后再认领",
            )
        if t["source_rev_at_edit"] != label["source_rev"]:
            raise HTTPException(409, "译文基于旧版原文（已过期），不能认领")

        ts = now()
        expires = (datetime.now(timezone.utc) + CLAIM_TTL).strftime("%Y-%m-%d %H:%M:%S")
        # 清掉历史/到期/已释放行（唯一键 label_id+language_id 至多一行），再插入
        conn.execute(
            "DELETE FROM claims WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        )
        cur = conn.execute(
            "INSERT INTO claims (label_id, language_id, claimant_id, "
            "source_rev_at_claim, trans_rev_at_claim, claimed_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (label_id, language_id, user["id"], body.source_rev, body.trans_rev, ts, expires),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM claims WHERE id = ?", (cur.lastrowid,)).fetchone()
    return serialize_claim(conn, row)


@app.post("/api/board/{language_id}/labels/{label_id}/claim/release")
def release_claim(
    language_id: int, label_id: int, user: dict = Depends(current_user)
):
    """提前释放认领。仅持有该有效认领的账号可释放；
    无有效认领 404、非认领人 403，均不改变状态。"""
    with db._write_lock, get_conn() as conn:
        row = get_claim_for(conn, label_id, language_id)
        if row is None:
            raise HTTPException(404, "该译文当前没有有效认领")
        if row["claimant_id"] != user["id"]:
            raise HTTPException(403, "只有认领人本人可以提前释放该认领")
        conn.execute(
            "DELETE FROM claims WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
    return {"ok": True, "released": True}


# ---------------------------- 确认 / 取消确认 ----------------------------


@app.post("/api/board/{language_id}/labels/{label_id}/confirm")
def confirm_translation(
    language_id: int, label_id: int, body: ConfirmBody,
    user: dict = Depends(require_roles("translator", "reviewer")),
):
    """校对确认。同一账号不得确认自己提交的译文；须携带读取时的双修订号。

    存在他人的有效认领时，其他账号的确认一律拒绝（409，认领状态不变）；
    无人认领时沿用原有确认流程，请求仍只需双修订号；
    认领人本人可以在认领有效期内完成确认，成功后认领即结束。"""
    with db._write_lock, get_conn() as conn:
        label = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not label:
            raise HTTPException(404, "展签不存在")
        if label["status"] == "withdrawn":
            raise HTTPException(409, "该展签已撤展，不能确认译文")
        t = conn.execute(
            "SELECT * FROM translations WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        ).fetchone()
        if not t:
            raise HTTPException(404, "尚无译文，无法确认")
        if body.source_rev != label["source_rev"]:
            raise HTTPException(409, "原文已有新版本，该译文已过期，请退回译者更新")
        if body.trans_rev != t["trans_rev"]:
            raise HTTPException(409, "译文已被更新，你读取的是旧版本，请刷新后重试")
        if t["source_rev_at_edit"] != label["source_rev"]:
            raise HTTPException(409, "译文基于旧版原文，须先更新译文")
        if t["translator_id"] == user["id"]:
            raise HTTPException(403, "同一账号不得确认自己提交的译文")

        claim = get_claim_for(conn, label_id, language_id)
        if claim is not None and claim["claimant_id"] != user["id"]:
            other = conn.execute(
                "SELECT display_name FROM users WHERE id = ?",
                (claim["claimant_id"],),
            ).fetchone()
            raise HTTPException(409, {
                "message": "该译文正被他人认领，认领期间其他账号不能确认，请等待释放或到期",
                "claim": serialize_claim(conn, claim),
                "held_by": other["display_name"] if other else f"#{claim['claimant_id']}",
            })

        conn.execute(
            "UPDATE translations SET confirmed = 1, confirmer_id = ?, updated_at = ? WHERE id = ?",
            (user["id"], now(), t["id"]),
        )
        # 成功确认结束认领（认领人本人在认领期内确认，或此前无认领均安全）
        conn.execute(
            "DELETE FROM claims WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        )
        conn.commit()
        t = conn.execute(
            "SELECT t.*, ur.display_name AS confirmer_name FROM translations t "
            "LEFT JOIN users ur ON ur.id = t.confirmer_id WHERE t.id = ?",
            (t["id"],),
        ).fetchone()
    return dict(t)


# ---------------------------- 发布不可变快照 ----------------------------


@app.get("/api/snapshots")
def list_snapshots(user: dict = Depends(current_user)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.id, s.language_id, s.version, s.published_at, l.code, l.name, "
            "u.display_name AS published_by_name "
            "FROM snapshots s JOIN languages l ON l.id = s.language_id "
            "LEFT JOIN users u ON u.id = s.published_by "
            "ORDER BY s.id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: int, user: dict = Depends(current_user)):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if not row:
            raise HTTPException(404, "快照不存在")
    return {"id": row["id"], "language_id": row["language_id"], "version": row["version"],
            "published_at": row["published_at"], "payload": json.loads(row["payload"])}


@app.post("/api/board/{language_id}/publish")
def publish_snapshot(
    language_id: int, user: dict = Depends(require_roles("librarian"))
):
    """只有该语言全部展签译文均对应当前原文修订且已确认，才可发布。
    快照一经发布不可修改（应用层不提供任何变更/删除接口）。"""
    with db._write_lock, get_conn() as conn:
        b = build_board(conn, language_id)
        if not b["publishable"]:
            raise HTTPException(409, {
                "message": "当前语言尚不可发布",
                "reasons": b["blocking_reasons"],
            })

        last = conn.execute(
            "SELECT MAX(version) AS v FROM snapshots WHERE language_id = ?",
            (language_id,),
        ).fetchone()["v"]
        version = (last or 0) + 1

        entries = []
        for item in b["items"]:
            t = item["translation"]
            entries.append({
                "label_id": item["label"]["id"],
                "source_text": item["label"]["source_text"],
                "source_rev": item["label"]["source_rev"],
                "text": t["text"],
                "trans_rev": t["trans_rev"],
                "translator_id": t["translator_id"],
                "confirmer_id": t["confirmer_id"],
            })
        payload = {
            "language_id": language_id,
            "language_code": b["language"]["code"],
            "language_name": b["language"]["name"],
            "version": version,
            "entries": entries,
        }
        cur = conn.execute(
            "INSERT INTO snapshots (language_id, version, published_by, published_at, payload) "
            "VALUES (?,?,?,?,?)",
            (language_id, version, user["id"], now(), json.dumps(payload, ensure_ascii=False)),
        )
        # 本快照含在展展签：清除该语言的游客隐藏标记（撤展的展签不在 items 中，保持隐藏）
        on_display_ids = [item["label"]["id"] for item in b["items"]]
        if on_display_ids:
            placeholders = ",".join("?" for _ in on_display_ids)
            conn.execute(
                f"UPDATE translations SET public_hidden = 0 "
                f"WHERE language_id = ? AND label_id IN ({placeholders})",
                [language_id, *on_display_ids],
            )
        conn.commit()
        sid = cur.lastrowid
    return {"id": sid, "version": version}


# ---------------------------- 游客公开页（无需登录） ----------------------------


@app.get("/api/public/languages")
def public_languages():
    """游客页：每个语言展示最新已发布快照；未发布的语言标注暂无内容。

    已撤展（或复展后尚未随新快照重新发布）的展签，立即从最新内容中隐藏；
    隐藏通过 translations.public_hidden 按语言实现，快照 JSON 本身不修改，
    因此历史快照与已登录用户的快照查询字段/内容均保持不变。
    """
    with get_conn() as conn:
        langs = conn.execute("SELECT * FROM languages ORDER BY id").fetchall()
        result = []
        for lang in langs:
            snap = conn.execute(
                "SELECT * FROM snapshots WHERE language_id = ? ORDER BY version DESC LIMIT 1",
                (lang["id"],),
            ).fetchone()
            entry = {
                "language": {"id": lang["id"], "code": lang["code"], "name": lang["name"]},
                "published": snap is not None,
            }
            if snap:
                payload = json.loads(snap["payload"])
                hidden_rows = conn.execute(
                    "SELECT label_id FROM translations "
                    "WHERE language_id = ? AND public_hidden = 1",
                    (lang["id"],),
                ).fetchall()
                hidden = {r["label_id"] for r in hidden_rows}
                entry.update({
                    "snapshot_id": snap["id"],
                    "version": snap["version"],
                    "published_at": snap["published_at"],
                    "entries": [e for e in payload["entries"]
                                if e["label_id"] not in hidden],
                })
            result.append(entry)
    return result


# ---------------------------- 静态页面 ----------------------------


@app.get("/")
def root(request: Request):
    # 已登录进入工作台，否则进入登录页
    return FileResponse(
        "app/static/app.html" if public_user(request) else "app/static/login.html"
    )


@app.get("/login")
def login_page():
    return FileResponse("app/static/login.html")


@app.get("/app")
def app_page(user: dict = Depends(current_user)):
    return FileResponse("app/static/app.html")


@app.get("/visitor")
def visitor_page():
    return FileResponse("app/static/visitor.html")


app.mount("/static", StaticFiles(directory="app/static"), name="static")
