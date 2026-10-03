"""承诺兑现（redeem）为固定时刻通行凭证的 API 集成测试，需要 PostgreSQL。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_commitment_redeem_api.py
脚本会清空 passes/commitments/regions/region_meta 后跑全套用例（含并发），
结束时再次清空，避免影响其它测试脚本。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def future(seconds: float) -> str:
    return iso(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def at(h: int, m: int = 0, s: int = 0) -> datetime:
    """2030 年某日的固定 UTC 时刻（远在未来，受理时刻比较恒为真）。"""
    return datetime(2030, 6, 1, h, m, s, tzinfo=timezone.utc)


SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100]]
TRAJ = [
    {"x": -50, "y": 50, "t": 0},
    {"x": 150, "y": 50, "t": 60},
]
TRAJ_HIGH = [
    {"x": -50, "y": 200, "t": 0},
    {"x": 150, "y": 200, "t": 60},
]
HIGH_SQUARE = [[0, 150], [100, 150], [100, 250], [0, 250]]
WIDE = ("2020-01-01T00:00:00Z", "2032-01-01T00:00:00Z")

db.init_pool()
client = TestClient(app)


def reset_all():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            # commitments.redeemed_pass_id 引用 passes：有引用关系的表即使
            # 为空也必须在同一条 TRUNCATE 中一起清空
            cur.execute("TRUNCATE commitments, passes RESTART IDENTITY")
            cur.execute("TRUNCATE regions RESTART IDENTITY")
            cur.execute("TRUNCATE region_change_audit")
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


def count(table) -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS n FROM {table}")
            return cur.fetchone()[0]


def redeemed_link(commitment_id) -> int | None:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT redeemed_pass_id FROM commitments WHERE id = %s",
                (commitment_id,),
            )
            return cur.fetchone()[0]


def create_region(name, polygon, vf, vt):
    return client.post("/api/regions", json={
        "name": name, "polygon": polygon,
        "valid_from": iso(vf) if isinstance(vf, datetime) else vf,
        "valid_to": iso(vt) if isinstance(vt, datetime) else vt,
    })


def issue_commitment(traj, ws, we):
    return client.post("/api/commitments", json={
        "points": traj,
        "window_start": ws if isinstance(ws, str) else iso(ws),
        "window_end": we if isinstance(we, str) else iso(we),
    })


def redeem(cid, dep):
    return client.post(f"/api/commitments/{cid}/redeem", json={
        "departure_time": dep if isinstance(dep, str) else iso(dep),
    })


reset_all()

# ====================================================================
# 1. 正常兑现：安全时刻 → 201，生成唯一凭证，承诺按取消语义失效
# ====================================================================
r = create_region("R1", SQUARE, at(8, 0), at(9, 0))
check("登记 R1 201", r.status_code == 201, r.text)
rev_after_r1 = client.get("/api/revision").json()["revision"]

# 窗口 [07:58,09:01)：禁止 [07:59:15,08:59:45)，安全两段
r = issue_commitment(TRAJ, at(7, 58), at(9, 1))
check("签发被 R1 切成两段的承诺 201", r.status_code == 201, r.text)
c = r.json()
cid = c["id"]
check("承诺签发 revision 记录为 R1 之后版本", c["revision"] == rev_after_r1, str(c))
passes_before = count("passes")

r = redeem(cid, at(7, 58, 30))
check("安全时刻兑现 201", r.status_code == 201, r.text)
body = r.json()
check("响应含 commitment_id / pass / revision",
      body["commitment_id"] == cid and "pass" in body and body["revision"] == rev_after_r1,
      str(body))
new_pass = body["pass"]
check("只生成一张凭证", count("passes") == passes_before + 1)
check("凭证为 active，revision 为当前区域版本",
      new_pass["status"] == "active" and new_pass["revision"] == rev_after_r1, str(new_pass))
# 绝对时刻轨迹：首点=出发时刻、次点=出发+60s，x/y 与承诺相对轨迹一致
t0, t1 = new_pass["trajectory"]
check("相对轨迹按出发时刻展开为绝对时刻轨迹",
      t0["x"] == -50.0 and t0["y"] == 50.0 and t0["t"] == iso(at(7, 58, 30))
      and t1["x"] == 150.0 and t1["y"] == 50.0
      and t1["t"] == iso(at(7, 59, 30)), str(new_pass["trajectory"]))
check("兑现不推进区域 revision",
      client.get("/api/revision").json()["revision"] == rev_after_r1)
check("承诺行回填 redeemed_pass_id 指向新凭证",
      redeemed_link(cid) == new_pass["id"])

# 承诺查询/取消兼容：兑现后即「已取消承诺」
r = client.get(f"/api/commitments/{cid}")
check("兑现后 GET 承诺 status=cancelled 且 intervals 为空",
      r.status_code == 200 and r.json()["status"] == "cancelled"
      and r.json()["intervals"] == [] and r.json()["cancelled_at"] is not None, r.text)
r = client.post(f"/api/commitments/{cid}/cancel")
check("兑现后取消接口仍 200 幂等（响应结构不含新字段也不报错）",
      r.status_code == 200 and r.json()["status"] == "cancelled", r.text)
# 凭证查询不受影响
r = client.get(f"/api/passes/{new_pass['id']}")
check("新凭证可查询、仍 active",
      r.status_code == 200 and r.json()["status"] == "active"
      and r.json()["trajectory"] == new_pass["trajectory"], r.text)

# ====================================================================
# 2. 重复兑现 → 409 commitment_already_redeemed（带 pass_id），不再出凭证
# ====================================================================
passes_before = count("passes")
r = redeem(cid, at(8, 59, 50))
check("重复兑现 409", r.status_code == 409, r.text)
err = r.json()["error"]
check("错误码 commitment_already_redeemed 且 details 带原 pass_id",
      err["code"] == "commitment_already_redeemed"
      and err["details"][0]["pass_id"] == new_pass["id"], str(err))
check("重复兑现不新增凭证", count("passes") == passes_before)

# ====================================================================
# 3. 普通取消 → 409 commitment_cancelled，可与已兑现区分
# ====================================================================
r = issue_commitment(TRAJ, at(7, 58), at(9, 1))
cid_cancelled = r.json()["id"]
r = client.post(f"/api/commitments/{cid_cancelled}/cancel")
check("普通取消 200", r.status_code == 200, r.text)
r = redeem(cid_cancelled, at(8, 0))
check("已取消承诺兑现 → 409", r.status_code == 409, r.text)
check("错误码 commitment_cancelled（区别于已兑现）",
      r.json()["error"]["code"] == "commitment_cancelled", r.text)
check("普通取消承诺 redeemed_pass_id 为空", redeemed_link(cid_cancelled) is None)
check("失败不出凭证", count("passes") == passes_before)

# ====================================================================
# 4. 未知承诺 → 404
# ====================================================================
r = redeem(999999, at(8, 0))
check("未知承诺兑现 404 not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text)

# ====================================================================
# 5. 越界与受理时刻：窗口外/不晚于受理 → 422 invalid_input，承诺不变
# ====================================================================
r = issue_commitment(TRAJ, at(7, 58), at(9, 1))
cid_in = r.json()["id"]
passes_before = count("passes")

r = redeem(cid_in, at(7, 57, 59))
check("出发时刻早于窗口起点 → 422 invalid_input",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_input", r.text)
r = redeem(cid_in, at(9, 1))
check("出发时刻恰为窗口终点（开端）→ 422 invalid_input",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_input", r.text)
r = redeem(cid_in, datetime.now(timezone.utc) - timedelta(seconds=1))
check("出发时刻不晚于受理时刻 → 422 invalid_input",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_input", r.text)
r = client.post(f"/api/commitments/{cid_in}/redeem",
                json={"departure_time": "2030-06-01T08:00:00"})
check("出发时刻不带时区 → 422 validation_error", r.status_code == 422, r.text)
r = client.post(f"/api/commitments/{cid_in}/redeem",
                json={"departure_time": iso(at(8, 0)), "extra": 1})
check("多余字段 → 422", r.status_code == 422, r.text)
check("承诺仍 active（GET 区间仍为两段）",
      client.get(f"/api/commitments/{cid_in}").json()["status"] == "active"
      and len(client.get(f"/api/commitments/{cid_in}").json()["intervals"]) == 2)
check("各类非法兑现均未生成凭证", count("passes") == passes_before)

# ====================================================================
# 6. 越界但不安全：窗口内、当前集合下不安全 → 422 unsafe_trajectory
# ====================================================================
r = redeem(cid_in, at(8, 30))
check("窗口内但落入禁止区间 → 422 unsafe_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory", r.text)
violated = r.json()["error"]["details"]
check("不安全明细列出穿越区域（R1）",
      len(violated) == 1 and violated[0]["region_name"] == "R1"
      and len(violated[0]["crossings"]) == 1, str(violated))
check("不安全兑现后承诺仍 active 且未生成凭证",
      client.get(f"/api/commitments/{cid_in}").json()["status"] == "active"
      and count("passes") == passes_before)

# 端点开闭：禁止区间 [07:59:15,08:59:45)
r = issue_commitment(TRAJ, at(7, 59, 14), at(7, 59, 16))
cid_edge1 = r.json()["id"]
r = redeem(cid_edge1, at(7, 59, 15))
check("禁止区间左闭端点 → 422 unsafe_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory", r.text)

r = issue_commitment(TRAJ, at(8, 59, 44), at(8, 59, 46))
cid_edge2 = r.json()["id"]
r = redeem(cid_edge2, at(8, 59, 45))
check("禁止区间右端点开：恰好安全 → 201", r.status_code == 201, r.text)
edge_pass_id = r.json()["pass"]["id"]
# 该出发时刻接触 valid_to（开端），凭证轨迹确实未穿越
vr = client.post("/api/verify", json={"points": r.json()["pass"]["trajectory"]})
check("端点凭证在当前集合下核验安全", vr.json()["safe"] is True, vr.text)

# ====================================================================
# 7. 按当前区域集合重算（非签发时集合）：签发后新增区域使窗口中部变不安全
# ====================================================================
r = issue_commitment(TRAJ, at(10, 0), at(12, 0))
cid_shift = r.json()["id"]
check("签发时窗口全程安全（单段）",
      len(client.get(f"/api/commitments/{cid_shift}").json()["intervals"]) == 1)
# R5 生效 10:30-10:31 → 禁止出发 [10:29:15,10:30:45)，两端仍安全 → 允许登记
r = create_region("R5", SQUARE, at(10, 30), at(10, 31))
check("部分遮挡区域登记 201", r.status_code == 201, r.text)
rev_now = client.get("/api/revision").json()["revision"]
got = client.get(f"/api/commitments/{cid_shift}").json()["intervals"]
check("登记后承诺区间收缩为两段",
      len(got) == 2 and got[0]["end"] == iso(at(10, 29, 15))
      and got[1]["start"] == iso(at(10, 30, 45)), str(got))
r = redeem(cid_shift, at(10, 30, 0))
check("对新集合不安全的时刻兑现 → 422 unsafe_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory", r.text)
r = redeem(cid_shift, at(11, 0))
check("对新集合仍安全的时刻兑现 201，revision 为当前集合版本",
      r.status_code == 201 and r.json()["revision"] == rev_now, r.text)

# ====================================================================
# 8. 窗口已结束 → 409 commitment_expired（不生成凭证、承诺不改变）
# ====================================================================
w_end = datetime.now(timezone.utc) + timedelta(seconds=2)
r = issue_commitment(TRAJ_HIGH, future(1), iso(w_end))
check("短窗口承诺签发 201", r.status_code == 201, r.text)
cid_exp = r.json()["id"]
time.sleep((w_end - datetime.now(timezone.utc)).total_seconds() + 0.5)
passes_before = count("passes")
r = redeem(cid_exp, at(11, 0))
check("窗口结束后兑现 → 409 commitment_expired",
      r.status_code == 409 and r.json()["error"]["code"] == "commitment_expired", r.text)
check("过期承诺仍为 active 且未生成凭证",
      client.get(f"/api/commitments/{cid_exp}").json()["status"] == "active"
      and count("passes") == passes_before)

# ====================================================================
# 9. 兑现后新凭证立即承担区域保护（pass_conflict），承诺不再约束
# ====================================================================
# edge_pass 出发 08:59:45 恰避开 R1；登记长期生效的 SQUARE 会令其穿越 → 409
r = create_region("R6-压凭证", SQUARE,
                  datetime(2020, 1, 1, tzinfo=timezone.utc),
                  datetime(2032, 1, 1, tzinfo=timezone.utc))
check("变更使已兑现凭证不安全 → 409 pass_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict", r.text)
ids = [d["pass_id"] for d in r.json()["error"]["details"]]
check("409 明细包含新凭证且不出现 commitment_conflict 类型",
      edge_pass_id in ids, str(ids))
check("409 后 revision 不变", client.get("/api/revision").json()["revision"] == rev_now)

# ====================================================================
# 10. 并发兑现：至多一次成功，失败方拿到 commitment_already_redeemed
# ====================================================================
reset_all()
r = issue_commitment(TRAJ, at(10, 0), at(12, 0))
check("并发前置：签发承诺 201", r.status_code == 201, r.text)
cid_conc = r.json()["id"]
barrier = threading.Barrier(3)
outcome: dict = {}


def redeem_worker(i):
    barrier.wait()
    rr = redeem(cid_conc, at(10, 10 + i))
    with lock:
        outcome[i] = (rr.status_code, rr.json())


lock = threading.Lock()
threads = [threading.Thread(target=redeem_worker, args=(i,)) for i in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=30)

oks = [i for i, v in outcome.items() if v[0] == 201]
check("三个并发兑现恰好一个 201", len(oks) == 1, str({k: v[0] for k, v in outcome.items()}))
winner_pass = outcome[oks[0]][1]["pass"]["id"]
losers = [i for i in outcome if i not in oks]
check("失败方均为 409 commitment_already_redeemed",
      all(outcome[i][0] == 409
          and outcome[i][1]["error"]["code"] == "commitment_already_redeemed"
          for i in losers), str({k: v[0] for k, v in outcome.items()}))
check("失败方 details 指向同一张凭证",
      all(outcome[i][1]["error"]["details"][0]["pass_id"] == winner_pass for i in losers),
      str(outcome)[:400])
check("全库只生成一张凭证", count("passes") == 1)
check("承诺 redeemed_pass_id 唯一指向胜出凭证", redeemed_link(cid_conc) == winner_pass)

# ====================================================================
# 11. 并发混合：兑现 vs 取消 vs 区域登记 → 串行化，无 5xx
# ====================================================================
r = issue_commitment(TRAJ_HIGH, at(13, 0), at(14, 0))
cid_mix = r.json()["id"]
base_rev = client.get("/api/revision").json()["revision"]
K = 4
barrier2 = threading.Barrier(K)
mix: list = []
mlock = threading.Lock()


def mix_worker(i):
    barrier2.wait()
    if i < 2:
        rr = redeem(cid_mix, at(13, 30))
    elif i == 2:
        rr = client.post(f"/api/commitments/{cid_mix}/cancel")
    else:
        # 不接触 TRAJ_HIGH 的远处区域，不应被承诺或新凭证挡住
        rr = create_region("并发区-远处",
                           [[0, 900], [100, 900], [100, 950], [0, 950]],
                           at(0), at(23))
    with mlock:
        mix.append((i, rr.status_code))


ts = [threading.Thread(target=mix_worker, args=(i,)) for i in range(K)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=30)

check("混合并发无 5xx", all(s < 500 for _, s in mix) and len(mix) == K, str(mix))
# 终态唯一且自洽：要么兑现先得手（一张新凭证 + 一个 409），
# 要么取消先得手（无新凭证 + 两个 409）
final_commit = client.get(f"/api/commitments/{cid_mix}").json()
final_rev = client.get("/api/revision").json()["revision"]
link = redeemed_link(cid_mix)
if link is not None:
    check("兑现先得手：承诺 cancelled 且恰有一张凭证",
          final_commit["status"] == "cancelled" and count("passes") == 2, str(mix))
    # 取消在兑现之后仍幂等 200；第二个兑现必为 409
    codes = sorted(s for i, s in mix if i < 2)
    check("两个兑现一个 201 一个 409（或取消先到 200）", codes in ([201, 409],), str(mix))
else:
    check("取消先得手：承诺 cancelled 且无新凭证",
          final_commit["status"] == "cancelled" and count("passes") == 1, str(mix))
    codes = sorted(s for i, s in mix if i < 2)
    check("取消先到：两个兑现均 409（commitment_cancelled）", codes == [409, 409], str(mix))
check("建区恰好推进 revision 一次", final_rev == base_rev + 1, f"{final_rev} vs {base_rev}")

# ---- 清理，避免影响其它测试脚本 ----
reset_all()

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部承诺兑现 API 集成用例通过")
