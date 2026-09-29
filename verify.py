"""验收脚本（verify 服务入口）：只依赖 Python 标准库。

用法:
    python verify.py [base_url]
默认 http://localhost:${API_PORT:-8000}

覆盖：
  1. 健康检查
  2. 端点相接不冲突
  3. 重叠广告窗贪心误排场景
  4. 同分裁决（编号最小，且与输入顺序无关）
  5. 重复 id 整体拒绝（稳定错误码 DUPLICATE_ID）
  6. 越界字段拒绝（start >= end）
  7. 最大输入 20 万窗口，唯一最优 50 * 1e9，并可重算

任一断言失败以非零码退出。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

MAX_WINDOWS = 200_000


def request(base_url, method, path, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(name, cond, extra=""):
    if not cond:
        print(f"FAIL: {name} {extra}")
        sys.exit(1)
    print(f"PASS: {name}")


def build_max_batch():
    # 50 个互不冲突的冠军区间 v=1e9；每段 3999 个相互重叠的诱饵 v=999_999_999。
    # 唯一最优：恰好 50 个冠军，总收益 50_000_000_000。
    windows = []
    for j in range(50):
        base = j * 1_000_000
        windows.append(
            {"id": f"champ{j}", "start": base + 1000,
             "end": base + 1_000_000, "value": 1_000_000_000}
        )
        for d in range(3999):
            windows.append(
                {"id": f"d{j}_{d}", "start": base,
                 "end": base + 999_999, "value": 999_999_999}
            )
    return windows


def main():
    port = os.environ.get("API_PORT", "8000")
    base_url = sys.argv[1] if len(sys.argv) > 1 else f"http://localhost:{port}"

    status, body = request(base_url, "GET", "/healthz")
    check("healthz 200", status == 200 and body.get("status") == "ok", str(body))

    # 端点相接 [0,10) + [10,20) 不冲突
    status, body = request(
        base_url, "POST", "/api/v1/schedules",
        {"limit": 2, "windows": [
            {"id": "a", "start": 0, "end": 10, "value": 10},
            {"id": "b", "start": 10, "end": 20, "value": 20},
        ]},
    )
    check("touching endpoints both selected",
          status == 200 and body == {"profit": 30, "ids": ["a", "b"]}, str(body))

    # 贪心误排场景
    status, body = request(
        base_url, "POST", "/api/v1/schedules",
        {"limit": 2, "windows": [
            {"id": "big", "start": 0, "end": 100, "value": 100},
            {"id": "x", "start": 0, "end": 50, "value": 60},
            {"id": "y", "start": 50, "end": 100, "value": 60},
        ]},
    )
    check("overlap beats highest single bid",
          status == 200 and body == {"profit": 120, "ids": ["x", "y"]}, str(body))

    # 同分裁决：互斥同分保留编号最小，且与输入顺序无关
    for order in (["a", "b"], ["b", "a"]):
        wins = [{"id": "a", "start": 0, "end": 5, "value": 10},
                {"id": "b", "start": 5, "end": 10, "value": 10}]
        wins.sort(key=lambda w: order.index(w["id"]))
        status, body = request(
            base_url, "POST", "/api/v1/schedules",
            {"limit": 1, "windows": wins},
        )
        check(f"tie keeps lowest number (input order {order})",
              status == 200 and body == {"profit": 10, "ids": ["a"]}, str(body))

    # 重复 id：整体拒绝
    status, body = request(
        base_url, "POST", "/api/v1/schedules",
        {"limit": 1, "windows": [
            {"id": "a", "start": 0, "end": 1, "value": 1},
            {"id": "a", "start": 1, "end": 2, "value": 1},
        ]},
    )
    codes = [d["code"] for d in body.get("error", {}).get("details", [])]
    check("duplicate ids rejected with stable code",
          status == 422 and "DUPLICATE_ID" in codes, str(body))

    # start >= end：越界拒绝
    status, body = request(
        base_url, "POST", "/api/v1/schedules",
        {"limit": 1, "windows": [
            {"id": "a", "start": 10, "end": 10, "value": 1},
        ]},
    )
    codes = [d["code"] for d in body.get("error", {}).get("details", [])]
    check("start>=end rejected with stable code",
          status == 422 and "INVALID_INTERVAL" in codes, str(body))

    # 最大输入
    windows = build_max_batch()
    assert len(windows) == MAX_WINDOWS
    t0 = time.perf_counter()
    status, body = request(
        base_url, "POST", "/api/v1/schedules",
        {"limit": 50, "windows": windows}, timeout=180,
    )
    elapsed = time.perf_counter() - t0
    expected_ids = [f"champ{j}" for j in range(50)]
    check(
        "maximum batch yields unique optimum 50e9",
        status == 200
        and body.get("profit") == 50_000_000_000
        and body.get("ids") == expected_ids,
        f"{status} {str(body)[:300]}",
    )
    print(f"   (maximum batch solved in {elapsed:.2f}s, {len(windows)} windows)")

    # 可重算：再请求一次结果完全一致
    status2, body2 = request(
        base_url, "POST", "/api/v1/schedules",
        {"limit": 50, "windows": windows}, timeout=180,
    )
    check("optimum is reproducible", status2 == 200 and body2 == body, str(body2)[:300])

    print("\nALL VERIFY CHECKS PASSED")


if __name__ == "__main__":
    main()
