#!/usr/bin/env bash
# 承诺兑现（redeem）调用示例：把择时通行承诺按选定出发时刻兑现为
# 一张固定时刻通行凭证。
#
# 固定行程：(-50,50) 相对 0s → (150,50) 相对 60s，对 [0,100]×[0,100]
# 区域的几何接触为相对 [15s,45s]；区域生效 [vf,vt) 时，禁止出发区间恰为
# 左闭右开的 [vf-45s, vt-15s)。
#
# 演示：
#   1. 登记一分钟生效区域，禁止区间只覆盖候选窗口中段（两端安全）
#   2. 签发承诺 → 两段安全出发区间
#   3. 在安全段内选定出发时刻兑现 → 201，拿到唯一一张固定时刻凭证
#   4. 重复兑现 → 409 commitment_already_redeemed（带 pass_id，可取回原凭证）
#   5. 另一张承诺先普通取消再兑现 → 409 commitment_cancelled（与已兑现区分）
#   6. 在禁止段内选定出发时刻 → 422 unsafe_trajectory，承诺不变、不出凭证
#   7. 越出原窗口 / 出发时刻不带时区 → 422；未知承诺 → 404
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_commitment_redeem.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }
iso_after() { python3 -c "
from datetime import datetime,timedelta,timezone
print((datetime.now(timezone.utc)+timedelta(seconds=$1)).isoformat().replace('+00:00','Z'))"; }

WS=$(iso_after 600)   # 候选窗口起点：10 分钟后
WE=$(iso_after 900)   # 候选窗口终点：15 分钟后

echo "== 1. 登记一分钟生效区域：vf=窗口起点+90s，禁止区间只覆盖窗口中段 =="
VF=$(iso_after 690); VT=$(iso_after 750)
# 禁止出发 = [vf-45s, vt-15s) = [起+45s, 起+135s)，窗口 [起, 起+300s)
# 被切成两段：[起, 起+45s) 与 [起+135s, 起+300s)
REGION=$(curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d "{
        \"name\": \"临检区-兑现演示\",
        \"polygon\": [[0,0],[100,0],[100,100],[0,100]],
        \"valid_from\": \"$VF\",
        \"valid_to\":   \"$VT\"
      }")
echo "区域 id=$(echo "$REGION" | J "['region']['id']")，当前 revision=$(echo "$REGION" | J "['revision']")"

echo "== 2. 签发承诺：窗口两端各有一段安全出发区间 =="
COMMIT=$(curl -fsS -X POST "$BASE_URL/api/commitments" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": 0},
          {\"x\": 150, \"y\": 50, \"t\": 60}
        ],
        \"window_start\": \"$WS\",
        \"window_end\":   \"$WE\"
      }")
CID=$(echo "$COMMIT" | J "['id']")
echo "承诺 id=$CID"
echo "$COMMIT" | python3 -c "
import json,sys
for iv in json.load(sys.stdin)['intervals']:
    lb = '[' if iv['start_inclusive'] else '('
    rb = ']' if iv['end_inclusive'] else ')'
    print(f'  可出发：{lb}{iv[\"start\"]}, {iv[\"end\"]}{rb}')"

# 选第一段安全区间内的时刻（窗口起点 +10s）
DEP=$(python3 -c "
from datetime import datetime,timedelta
ws=datetime.fromisoformat('$WS'.replace('Z','+00:00'))
print((ws+timedelta(seconds=10)).isoformat().replace('+00:00','Z'))")

echo "== 3. 兑现：departure_time=$DEP → 201，只生成一张凭证 =="
REDEEM=$(curl -fsS -X POST "$BASE_URL/api/commitments/$CID/redeem" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$DEP\"}")
echo "$REDEEM" | python3 -m json.tool
PID=$(echo "$REDEEM" | J "['pass']['id']")
echo "已生成凭证 id=$PID；区域 revision 仍为：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")（兑现不推进）"
echo "承诺查询（兑现后按既有取消语义呈现）："
curl -fsS "$BASE_URL/api/commitments/$CID" | python3 -m json.tool

echo "== 4. 重复兑现 → 409 commitment_already_redeemed（带同一 pass_id） =="
curl -sS -o /tmp/redeem_again.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments/$CID/redeem" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$DEP\"}"
python3 -m json.tool /tmp/redeem_again.json

echo "== 5. 普通取消后再兑现 → 409 commitment_cancelled =="
COMMIT2=$(curl -fsS -X POST "$BASE_URL/api/commitments" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": 0},
          {\"x\": 150, \"y\": 50, \"t\": 60}
        ],
        \"window_start\": \"$WS\",
        \"window_end\":   \"$WE\"
      }")
CID2=$(echo "$COMMIT2" | J "['id']")
curl -fsS -X POST "$BASE_URL/api/commitments/$CID2/cancel" >/dev/null
curl -sS -o /tmp/redeem_cancelled.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments/$CID2/redeem" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$DEP\"}"
python3 -m json.tool /tmp/redeem_cancelled.json

echo "== 6. 新承诺在禁止段（起+60s，落在 [起+45s,起+135s)）兑现 → 422 =="
COMMIT3=$(curl -fsS -X POST "$BASE_URL/api/commitments" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [
          {\"x\": -50, \"y\": 50, \"t\": 0},
          {\"x\": 150, \"y\": 50, \"t\": 60}
        ],
        \"window_start\": \"$WS\",
        \"window_end\":   \"$WE\"
      }")
CID3=$(echo "$COMMIT3" | J "['id']")
DEP_UNSAFE=$(python3 -c "
from datetime import datetime,timedelta
ws=datetime.fromisoformat('$WS'.replace('Z','+00:00'))
print((ws+timedelta(seconds=60)).isoformat().replace('+00:00','Z'))")
curl -sS -o /tmp/redeem_unsafe.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments/$CID3/redeem" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$DEP_UNSAFE\"}"
python3 -m json.tool /tmp/redeem_unsafe.json
echo "承诺仍为：$(curl -fsS "$BASE_URL/api/commitments/$CID3" | J "['status']")（不安全不出凭证、承诺不变）"

echo "== 7. 越出原窗口 / 不带时区 / 未知承诺 =="
echo "-- 7a. 出发时刻在窗口终点（开端）之外 → 422 invalid_input --"
curl -sS -o /tmp/redeem_out.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments/$CID3/redeem" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$WE\"}"
python3 -m json.tool /tmp/redeem_out.json

echo "-- 7b. 出发时刻不带时区 → 422 validation_error --"
curl -sS -o /tmp/redeem_notz.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments/$CID3/redeem" \
  -H 'Content-Type: application/json' \
  -d '{"departure_time": "2030-06-01T08:00:00"}'
python3 -m json.tool /tmp/redeem_notz.json

echo "-- 7c. 未知承诺 → 404 --"
curl -sS -o /tmp/redeem_404.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/commitments/999999/redeem" \
  -H 'Content-Type: application/json' \
  -d "{\"departure_time\": \"$DEP\"}"
python3 -m json.tool /tmp/redeem_404.json

echo "完成。提示：示例区域与承诺会保留，可 DELETE /api/regions/{id} 清理。"
