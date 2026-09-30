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


class ClaimBody(BaseModel):
    source_rev: int  # 读取时的原文修订号
    trans_rev: int   # 读取时的译文修订号


# 认领有效期：30 分钟
CLAIM_TTL = timedelta(minutes=30)


class WithdrawBody(BaseModel):
    status_rev: int  # 读取时的状态版本（乐观锁）
    reason: str = Field(min_length=1)


class RestoreBody(BaseModel):
    status_rev: int  # 读取时的状态版本（乐观锁）


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
        # 原文修订：该展签所有语言的认领一律失效
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
        # 撤展：该展签所有语言的认领一律失效
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
        # 复展：认领上下文（撤展状态）已变化，相关认领一律失效
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

    # 当前仍有效（未到期）的认领；到期行不展示，他人可直接接手
    claim_rows = conn.execute(
        "SELECT c.id, c.label_id, c.language_id, c.claimer_id, "
        "       u.display_name AS claimer_name, u.username AS claimer_username, "
        "       c.source_rev, c.trans_rev, c.claimed_at, c.expires_at "
        "FROM claims c JOIN users u ON u.id = c.claimer_id "
        "WHERE c.language_id = ? AND c.expires_at > ?",
        (language_id, now()),
    ).fetchall()
    claim_map = {c["label_id"]: dict(c) for c in claim_rows}

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
        items.append({
            "label": dict(label),
            "translation": t,
            "status": status,
            "reason": reason,
            "claim": claim_map.get(label["id"]),
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
        # 译文已提交/修订：原认领所基于的修订号已变化，一律失效
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


# ---------------------------- 确认 / 取消确认 ----------------------------


@app.post("/api/board/{language_id}/labels/{label_id}/confirm")
def confirm_translation(
    language_id: int, label_id: int, body: ConfirmBody,
    user: dict = Depends(require_roles("translator", "reviewer")),
):
    """校对确认。同一账号不得确认自己提交的译文；须携带读取时的双修订号。"""
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

        # 认领校验：该译文若被他人有效认领，其他账号一律拒绝确认
        claim = conn.execute(
            "SELECT c.*, u.display_name AS claimer_name FROM claims c "
            "JOIN users u ON u.id = c.claimer_id "
            "WHERE c.label_id = ? AND c.language_id = ? AND c.expires_at > ?",
            (label_id, language_id, now()),
        ).fetchone()
        if claim and claim["claimer_id"] != user["id"]:
            raise HTTPException(
                409,
                f"该译文已被 {claim['claimer_name']} 认领（到期时间 {claim['expires_at']} UTC），"
                "认领到期或被释放前请由认领人校对确认",
            )

        conn.execute(
            "UPDATE translations SET confirmed = 1, confirmer_id = ?, updated_at = ? WHERE id = ?",
            (user["id"], now(), t["id"]),
        )
        # 成功确认结束认领
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


# ---------------------------- 待审译文认领 / 释放 ----------------------------


def _get_active_claim(conn, language_id: int, label_id: int):
    """取该（语言, 展签）当前仍有效的认领；到期行视为不存在。"""
    return conn.execute(
        "SELECT c.*, u.display_name AS claimer_name, u.username AS claimer_username "
        "FROM claims c JOIN users u ON u.id = c.claimer_id "
        "WHERE c.label_id = ? AND c.language_id = ? AND c.expires_at > ?",
        (label_id, language_id, now()),
    ).fetchone()


@app.post("/api/board/{language_id}/labels/{label_id}/claim")
def claim_translation(
    language_id: int, label_id: int, body: ClaimBody,
    user: dict = Depends(require_roles("translator", "reviewer")),
):
    """认领一条待审译文 30 分钟，凭展签、语言及读取时的原文/译文修订号。

    - 提交该译文的账号不得认领（403）；
    - 已撤展 / 已确认 / 过期版本（原文或译文修订号不匹配）一律 409 拒绝，状态不变；
    - 他人有效认领中 → 409；自己已有效认领 → 幂等返回原认领；
    - 认领到期或已释放（含因修订/撤展失效）→ 接手成功；同一译文同一时刻仅一条有效认领。
    """
    with db._write_lock, get_conn() as conn:
        label = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
        if not label:
            raise HTTPException(404, "展签不存在")
        if not conn.execute(
            "SELECT 1 FROM languages WHERE id = ?", (language_id,)
        ).fetchone():
            raise HTTPException(404, "语言不存在")
        if label["status"] == "withdrawn":
            raise HTTPException(409, "该展签已撤展，不能认领译文")
        t = conn.execute(
            "SELECT * FROM translations WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        ).fetchone()
        if not t:
            raise HTTPException(404, "尚无译文，无法认领")
        if body.source_rev != label["source_rev"]:
            raise HTTPException(409, "原文已有新版本，该译文已过期，不能认领过期版本")
        if body.trans_rev != t["trans_rev"]:
            raise HTTPException(
                409,
                f"译文已被更新（当前译文修订号 {t['trans_rev']}，"
                f"你认领请求基于 {body.trans_rev}），请刷新后重试",
            )
        if t["source_rev_at_edit"] != label["source_rev"]:
            raise HTTPException(409, "译文基于旧版原文（过期），不能认领")
        if t["confirmed"]:
            raise HTTPException(409, "该译文已确认，无需认领")
        if t["translator_id"] == user["id"]:
            raise HTTPException(403, "提交该译文的账号不得认领，请由其他译者/校对认领校对")

        active = _get_active_claim(conn, language_id, label_id)
        if active:
            if active["claimer_id"] == user["id"]:
                # 幂等：重复认领自己仍有效持有的认领，直接返回
                return dict(_get_active_claim(conn, language_id, label_id))
            raise HTTPException(
                409,
                f"该译文已被 {active['claimer_name']} 认领"
                f"（到期时间 {active['expires_at']} UTC），"
                "到期前不能重复认领；到期后可接手",
            )

        # 无有效认领（含已到期的陈旧行）：清理陈旧行后写入新认领
        conn.execute(
            "DELETE FROM claims WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        )
        claimed_at = datetime.now(timezone.utc)
        expires_at = claimed_at + CLAIM_TTL
        claimed_s = claimed_at.strftime("%Y-%m-%d %H:%M:%S")
        expires_s = expires_at.strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "INSERT INTO claims (language_id, label_id, claimer_id, source_rev, "
            "trans_rev, claimed_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            (language_id, label_id, user["id"], body.source_rev, body.trans_rev,
             claimed_s, expires_s),
        )
        conn.commit()
        row = conn.execute(
            "SELECT c.*, u.display_name AS claimer_name, u.username AS claimer_username "
            "FROM claims c JOIN users u ON u.id = c.claimer_id WHERE c.id = ?",
            (cur.lastrowid,),
        ).fetchone()
    return dict(row)


@app.post("/api/board/{language_id}/labels/{label_id}/claim/release")
def release_claim(
    language_id: int, label_id: int,
    user: dict = Depends(require_roles("translator", "reviewer")),
):
    """认领人提前释放认领。

    - 自己持有有效认领 → 删除，返回释放结果；
    - 当前无有效认领（已到期 / 已被修订等失效 / 从未认领）→ 幂等成功，不改变状态；
    - 有效认领属于他人 → 403，不能代他人释放。
    """
    with db._write_lock, get_conn() as conn:
        active = _get_active_claim(conn, language_id, label_id)
        if active and active["claimer_id"] != user["id"]:
            raise HTTPException(
                403,
                f"该译文由 {active['claimer_name']} 持有有效认领，不能代为释放",
            )
        conn.execute(
            "DELETE FROM claims WHERE label_id = ? AND language_id = ?",
            (label_id, language_id),
        )
        conn.commit()
    return {"released": True, "language_id": language_id, "label_id": label_id}


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
