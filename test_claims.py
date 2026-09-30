"""待审译文认领功能端到端测试（stdlib + FastAPI TestClient）。"""
import os
import sys
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["SEED_LIBRARIAN_PW"] = "lib123"
os.environ["SEED_TRANSLATOR_PW"] = "tra123"
os.environ["SEED_REVIEWER_PW"] = "rev123"

from fastapi.testclient import TestClient

from app.main import app
from app import db
from app.db import get_conn, hash_password, now

db.init_db()
client = TestClient(app)

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


def login(username, password):
    c = TestClient(app)
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def expect_status(name, r, code):
    check(name, r.status_code == code, f"got {r.status_code}: {r.text}")


lib = login("librarian", "lib123")
tra = login("translator", "tra123")
rev = login("reviewer", "rev123")

# 预置额外译者账号，用于并发测试
with get_conn() as conn:
    for i in range(8):
        uname = f"worker{i}"
        salt = f"salt{i}"
        conn.execute(
            "INSERT INTO users (username, display_name, role, salt, pw_hash, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (uname, f"并发译者{i}", "translator", salt, hash_password("pw", salt), now()),
        )
    conn.commit()

EN = 1  # 初始化语言 en 的 id
labels = lib.get("/api/labels").json()
L1, L2, L3 = (x["id"] for x in labels[:3])


def submit(c, label, lang=EN, text="译文", source_rev=1, trans_rev=None):
    body = {"text": text, "source_rev": source_rev}
    if trans_rev is not None:
        body["trans_rev"] = trans_rev
    return c.post(f"/api/board/{lang}/labels/{label}/translation", json=body)


def claim(c, label, source_rev, trans_rev, lang=EN):
    return c.post(f"/api/board/{lang}/labels/{label}/claim",
                  json={"source_rev": source_rev, "trans_rev": trans_rev})


def release(c, label, lang=EN):
    return c.post(f"/api/board/{lang}/labels/{label}/claim/release")


def confirm(c, label, source_rev, trans_rev, lang=EN):
    return c.post(f"/api/board/{lang}/labels/{label}/confirm",
                  json={"source_rev": source_rev, "trans_rev": trans_rev})


def board_claim(c, label, lang=EN):
    items = {i["label"]["id"]: i for i in c.get(f"/api/board/{lang}").json()["items"]}
    return items[label]["claim"]


def active_claim_count(label, lang=EN):
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM claims WHERE label_id=? AND language_id=? AND expires_at > ?",
            (label, lang, now()),
        ).fetchone()["c"]


print("== 1. 提交人不得认领 ==")
submit(tra, L1, text="blue vase")
expect_status("提交者认领自己的译文 → 403", claim(rev if False else tra, L1, 1, 1), 403)

print("== 2. 认领修订号校验 ==")
expect_status("错误原文修订号 → 409", claim(rev, L1, 2, 1), 409)
expect_status("错误译文修订号 → 409", claim(rev, L1, 1, 2), 409)
r = claim(rev, L1, 1, 1)
expect_status("校对正确认领 → 200", r, 200)
c1 = r.json()
check("认领返回认领人/到期时间", c1["claimer_id"] == 3 and "expires_at" in c1 and "claimed_at" in c1, c1)
check("认领有效期 30 分钟",
      c1["expires_at"][-8:] != c1["claimed_at"][-8:])  # 时间不同即可，下面精确校验
from datetime import datetime, timedelta, timezone
ca = datetime.strptime(c1["claimed_at"], "%Y-%m-%d %H:%M:%S")
ea = datetime.strptime(c1["expires_at"], "%Y-%m-%d %H:%M:%S")
check("到期时间恰为 30 分钟后", ea - ca == timedelta(minutes=30), f"{ea-ca}")

print("== 3. 认领期间他人确认被拒；认领人可确认 ==")
# L1 由译者提交、校对认领；用第三个账号（并发译者 worker0）验证他人确认被拒
w0 = login("worker0", "pw")
expect_status("其他账号在他人有效认领期间确认 → 409", confirm(w0, L1, 1, 1), 409)
expect_status("认领人确认成功 → 200", confirm(rev, L1, 1, 1), 200)
check("成功确认后认领结束（无有效认领）", active_claim_count(L1) == 0)
expect_status("已确认译文禁止认领 → 409", claim(tra, L1, 1, 1), 409)

print("== 4. 看板展示认领信息 ==")
submit(rev, L2, text="prayer wheel")  # 校对提交，译者来认领
r = claim(tra, L2, 1, 1)
expect_status("译者认领校对提交的译文 → 200", r, 200)
b = board_claim(rev, L2)
check("看板条目含有效认领（认领人名）", b is not None and b["claimer_name"] == "译者小李", b)
# 自审禁令仍然有效：校对（提交者）确认 → 403
expect_status("提交者自审仍被拒 → 403", confirm(rev, L2, 1, 1), 403)
expect_status("认领人确认成功 → 200", confirm(tra, L2, 1, 1), 200)
check("确认后看板不再显示认领", board_claim(rev, L2) is None)

print("== 5. 提前释放 ==")
submit(tra, L3, text="wooden type")
r = claim(rev, L3, 1, 1)
expect_status("校对认领 → 200", r, 200)
expect_status("他人释放 → 403", release(tra, L3), 403)
r = release(rev, L3)
expect_status("认领人释放 → 200", r, 200)
check("释放返回 released", r.json().get("released") is True)
expect_status("释放后他人接手认领 → 200", claim(w0, L3, 1, 1), 200)
# 再次释放不存在的认领（幂等）
release(w0, L3)
r = release(w0, L3)
expect_status("无有效认领时释放幂等 → 200", r, 200)

print("== 6. 到期后他人可接手 ==")
submit(rev, L2, text="prayer wheel v2", trans_rev=1)  # L2 已确认，修订回待审 trans_rev=2
r = claim(tra, L2, 1, 2)
expect_status("译者认领修订版 → 200", r, 200)
expect_status("未到期他人重复认领 → 409", claim(w0, L2, 1, 2), 409)
# 把到期时间改到过去
with get_conn() as conn:
    conn.execute(
        "UPDATE claims SET expires_at = ? WHERE label_id=? AND language_id=?",
        ("2000-01-01 00:00:00", L2, EN),
    )
    conn.commit()
check("到期后看板不再显示该认领", board_claim(tra, L2) is None)
r = claim(w0, L2, 1, 2)
expect_status("到期后他人接手 → 200", r, 200)
check("接手后同一译文仅一条有效认领", active_claim_count(L2) == 1)
check("接手者为新认领人", r.json()["claimer_name"] == "并发译者0")
release(w0, L2)

print("== 7. 重复认领幂等 ==")
# L3: tra 提交（trans_rev=1），校对认领
r1 = claim(rev, L3, 1, 1)
expect_status("首次认领 → 200", r1, 200)
r2 = claim(rev, L3, 1, 1)
expect_status("自己重复认领 → 200（幂等）", r2, 200)
check("幂等返回同一条认领", r1.json()["id"] == r2.json()["id"])
check("仍只有一条有效认领", active_claim_count(L3) == 1)

print("== 8. 原文修订使认领失效，且过期版本禁止认领 ==")
# L3 被 rev 认领中；馆员修订原文
r = lib.put(f"/api/labels/{L3}", json={"source_text": "活字印刷木活字一套（修订）", "source_rev": 1})
expect_status("馆员修订原文 → 200", r, 200)
check("原文修订后认领失效", active_claim_count(L3) == 0)
expect_status("认领旧原文修订号 → 409（过期版本）", claim(rev, L3, 1, 1), 409)
# 译者基于新原文修订译文后可重新认领
submit(tra, L3, text="wooden type v2", source_rev=2, trans_rev=1)
expect_status("新译文可被认领 → 200", claim(rev, L3, 2, 2), 200)
release(rev, L3)

print("== 9. 译文修订使认领失效 ==")
claim(rev, L3, 2, 2)
r = submit(tra, L3, text="wooden type v3", source_rev=2, trans_rev=2)
expect_status("认领期间译者提交修订 → 200（沿用既有提交流程）", r, 200)
check("译文修订后认领失效", active_claim_count(L3) == 0)
expect_status("旧译文修订号认领 → 409", claim(rev, L3, 2, 2), 409)
expect_status("新译文修订号认领 → 200", claim(rev, L3, 2, 3), 200)
release(rev, L3)

print("== 10. 撤展 / 复展 ==")
submit(tra, L1, text="blue vase v2", trans_rev=1)  # L1 已确认过，修订 → trans_rev 2, 待审
claim(rev, L1, 1, 2)
r = lib.post(f"/api/labels/{L1}/withdraw", json={"status_rev": 1, "reason": "外借"})
expect_status("撤展 → 200", r, 200)
check("撤展使认领失效", active_claim_count(L1) == 0)
expect_status("已撤展译文认领 → 409", claim(rev, L1, 1, 2), 409)
expect_status("已撤展译文确认 → 409", confirm(rev, L1, 1, 2), 409)
r = lib.post(f"/api/labels/{L1}/restore", json={"status_rev": 2})
expect_status("复展 → 200", r, 200)
expect_status("复展后可重新认领（确认已清除）→ 200", claim(rev, L1, 1, 2), 200)
release(rev, L1)

print("== 11. 边界：无译文 / 不存在 / 非工作角色 ==")
expect_status("无译文认领 → 404", claim(rev, 9999, 1, 1, lang=EN), 404)
expect_status("馆员认领 → 403", claim(lib, L1, 1, 2), 403)
expect_status("未登录认领 → 401",
              client.post(f"/api/board/{EN}/labels/{L1}/claim",
                          json={"source_rev": 1, "trans_rev": 2}), 401)

print("== 12. 无人认领时确认请求仍只需双修订号 ==")
# L3 无人认领（已释放），trans_rev=3，source_rev=2
r = confirm(rev, L3, 2, 3)
expect_status("无人认领：非提交者凭双修订号确认 → 200", r, 200)

print("== 13. 并发：同一译文同一时刻仅一人持有有效认领 ==")
# L2: 当前 trans_rev=2, source_rev=1, 无确认（rev 确认的是 rev1；tra 提交 rev2 后未确认）
results = []
barrier = threading.Barrier(8)
clients = [login(f"worker{i}", "pw") for i in range(8)]


def race(c, out):
    barrier.wait()
    out.append(claim(c, L2, 1, 2).status_code)


# L2 当前译文由 rev 提交，故 rev 不能参与认领竞争（其应得 403，另行验证）
expect_status("提交者参与并发认领仍被拒 → 403", claim(rev, L2, 1, 2), 403)
threads = [threading.Thread(target=race, args=(c, results)) for c in clients]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("8 个并发认领恰好 1 个成功", results.count(200) == 1, f"results={results}")
check("其余均为 409", results.count(409) == 7, f"results={results}")
check("库内仅一条有效认领", active_claim_count(L2) == 1)

print("== 14. 认领与确认并发：结果与当前状态一致 ==")
# L2 译文由校对 rev 提交，确认者必须是第三方（worker2）
w2 = clients[2]
# 清场：定位当前持有人并释放
with get_conn() as conn:
    row = conn.execute(
        "SELECT claimer_id FROM claims WHERE label_id=? AND language_id=? AND expires_at > ?",
        (L2, EN, now()),
    ).fetchone()
if row:
    holder = {c.get("/api/me").json()["id"]: c for c in clients + [rev, tra, w0]}[row["claimer_id"]]
    release(holder, L2)

outcome = {}
barrier2 = threading.Barrier(2)


def do_claim():
    barrier2.wait()
    outcome["claim"] = claim(clients[1], L2, 1, 2).status_code


def do_confirm():
    barrier2.wait()
    outcome["confirm"] = confirm(w2, L2, 1, 2).status_code


t1 = threading.Thread(target=do_claim)
t2 = threading.Thread(target=do_confirm)
t1.start(); t2.start(); t1.join(); t2.join()
check("认领与确认并发：恰有一个成功（200），另一个被拒（409）",
      sorted(outcome.values()) == [200, 409], f"outcome={outcome}")
# 最终状态自洽：若确认成功则译文 confirmed=1 且无认领；若认领成功则未确认且有一条认领
with get_conn() as conn:
    confirmed = conn.execute(
        "SELECT confirmed FROM translations WHERE label_id=? AND language_id=?",
        (L2, EN),
    ).fetchone()["confirmed"]
n_claims = active_claim_count(L2)
check("最终状态一致：确认成功⇔无认领；认领成功⇔未确认",
      (confirmed == 1 and n_claims == 0) or (confirmed == 0 and n_claims == 1),
      f"confirmed={confirmed}, claims={n_claims}")

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
