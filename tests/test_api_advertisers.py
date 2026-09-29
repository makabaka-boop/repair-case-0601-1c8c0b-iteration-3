"""带广告主约束排期接口测试：正确响应、独立规模上限、稳定错误码、
非法批次整批拒绝不留部分结果，以及旧接口行为与规模上限逐项不变。
"""

import time

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PATH = "/api/v1/advertiser-schedules"
LEGACY_PATH = "/api/v1/schedules"


# ---------------------------------------------------------------------------
# 正确响应
# ---------------------------------------------------------------------------

def test_happy_path_touching_different_owners():
    payload = {
        "limit": 2,
        "windows": [
            {"id": "a", "start": 0, "end": 10, "value": 10, "advertiser_id": "X"},
            {"id": "b", "start": 10, "end": 20, "value": 20, "advertiser_id": "Y"},
        ],
    }
    r = client.post(PATH, json=payload)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "profit": 30,
        "selections": [
            {"id": "a", "advertiser_id": "X"},
            {"id": "b", "advertiser_id": "Y"},
        ],
    }


def test_touching_same_owner_only_higher_bid_chosen():
    payload = {
        "limit": 2,
        "windows": [
            {"id": "a", "start": 0, "end": 10, "value": 10, "advertiser_id": "X"},
            {"id": "b", "start": 10, "end": 20, "value": 20, "advertiser_id": "X"},
        ],
    }
    r = client.post(PATH, json=payload)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "profit": 20,
        "selections": [{"id": "b", "advertiser_id": "X"}],
    }


def test_empty_windows_allowed():
    r = client.post(PATH, json={"limit": 1, "windows": []})
    assert r.status_code == 200
    assert r.json() == {"profit": 0, "selections": []}


def test_zero_bids_return_empty_selection():
    payload = {
        "limit": 3,
        "windows": [
            {"id": "z", "start": 0, "end": 10, "value": 0, "advertiser_id": "X"},
            {"id": "y", "start": 10, "end": 20, "value": 0, "advertiser_id": "Y"},
        ],
    }
    r = client.post(PATH, json=payload)
    assert r.status_code == 200
    assert r.json() == {"profit": 0, "selections": []}


def test_predecessor_spans_unselected_windows_via_api():
    # w1 与 w0 真正重叠，w0 与 w2 时间相容但同主；未选的 w1 不隔断，
    # w0+w2 禁止；最优 w1+w2 = 11。
    payload = {
        "limit": 2,
        "windows": [
            {"id": "w0", "start": 0, "end": 10, "value": 10, "advertiser_id": "X"},
            {"id": "w1", "start": 9, "end": 11, "value": 1, "advertiser_id": "Y"},
            {"id": "w2", "start": 11, "end": 20, "value": 10, "advertiser_id": "X"},
        ],
    }
    r = client.post(PATH, json=payload)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "profit": 11,
        "selections": [
            {"id": "w1", "advertiser_id": "Y"},
            {"id": "w2", "advertiser_id": "X"},
        ],
    }


# ---------------------------------------------------------------------------
# 校验：整批拒绝
# ---------------------------------------------------------------------------

def _codes(payload):
    r = client.post(PATH, json=payload)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    return r, [(d["code"], tuple(d["loc"])) for d in body["error"]["details"]]


def test_missing_advertiser_field_rejected():
    _, codes = _codes(
        {
            "limit": 1,
            "windows": [{"id": "a", "start": 0, "end": 1, "value": 1}],
        }
    )
    assert ("MISSING_FIELD", ("windows", 0, "advertiser_id")) in codes


def test_empty_and_typed_advertiser_id_rejected():
    _, codes = _codes(
        {
            "limit": 1,
            "windows": [
                {"id": "a", "start": 0, "end": 1, "value": 1, "advertiser_id": ""}
            ],
        }
    )
    assert codes == [("INVALID_VALUE", ("windows", 0, "advertiser_id"))]
    _, codes = _codes(
        {
            "limit": 1,
            "windows": [
                {"id": "a", "start": 0, "end": 1, "value": 1, "advertiser_id": 7}
            ],
        }
    )
    assert ("TYPE_ERROR", ("windows", 0, "advertiser_id")) in codes
    # bool 不得被当作字符串
    _, codes = _codes(
        {
            "limit": 1,
            "windows": [
                {"id": "a", "start": 0, "end": 1, "value": 1, "advertiser_id": True}
            ],
        }
    )
    assert ("TYPE_ERROR", ("windows", 0, "advertiser_id")) in codes


def test_limit_bounds_new_mode():
    # 新模式数量上限为 20
    for bad in (0, 21, -3):
        _, codes = _codes({"limit": bad, "windows": []})
        assert codes == [("OUT_OF_RANGE", ("limit",))], (bad, codes)


def test_too_many_windows_new_mode():
    # 新模式最多 2000 个窗口
    windows = [
        {"id": f"w{j}", "start": 0, "end": 1, "value": 0, "advertiser_id": "X"}
        for j in range(2001)
    ]
    r, codes = _codes({"limit": 20, "windows": windows})
    assert ("TOO_MANY_WINDOWS", ("windows",)) in codes
    params = r.json()["error"]["details"][0]["params"]
    assert params == {"max_windows": 2000, "actual": 2001}


def test_too_many_distinct_advertisers():
    windows = [
        {
            "id": f"w{j}",
            "start": j * 10,
            "end": j * 10 + 5,
            "value": 1,
            "advertiser_id": f"adv{j}",
        }
        for j in range(9)
    ]
    r, codes = _codes({"limit": 20, "windows": windows})
    assert ("TOO_MANY_ADVERTISERS", ("windows",)) in codes
    params = r.json()["error"]["details"][0]["params"]
    assert params == {"max_advertisers": 8, "actual": 9}


def test_exactly_eight_advertisers_accepted():
    windows = [
        {
            "id": f"w{j}",
            "start": j * 10,
            "end": j * 10 + 5,
            "value": 1,
            "advertiser_id": f"adv{j}",
        }
        for j in range(8)
    ]
    r = client.post(PATH, json={"limit": 20, "windows": windows})
    assert r.status_code == 200, r.text
    assert r.json()["profit"] == 8


def test_invalid_interval_rejected_entire_batch():
    _, codes = _codes(
        {
            "limit": 2,
            "windows": [
                {"id": "a", "start": 5, "end": 5, "value": 1, "advertiser_id": "X"},
                {"id": "b", "start": 0, "end": 1, "value": 1, "advertiser_id": "Y"},
            ],
        }
    )
    assert ("INVALID_INTERVAL", ("windows", 0)) in codes


def test_duplicate_id_rejected_entire_batch():
    r, codes = _codes(
        {
            "limit": 2,
            "windows": [
                {"id": "x", "start": 0, "end": 1, "value": 1, "advertiser_id": "X"},
                {"id": "x", "start": 1, "end": 2, "value": 1, "advertiser_id": "Y"},
            ],
        }
    )
    assert ("DUPLICATE_ID", ("windows",)) in codes
    assert r.json()["error"]["details"][0]["params"]["duplicate_count"] == 1


def test_extra_and_missing_fields():
    _, codes = _codes(
        {
            "limit": 1,
            "windows": [],
            "unexpected": 7,
        }
    )
    assert ("EXTRA_FIELD", ("unexpected",)) in codes
    _, codes = _codes(
        {
            "limit": 1,
            "windows": [
                {
                    "id": "a",
                    "start": 0,
                    "end": 1,
                    "value": 0,
                    "advertiser_id": "X",
                    "x": 1,
                }
            ],
        }
    )
    assert ("EXTRA_FIELD", ("windows", 0, "x")) in codes


def test_invalid_batch_leaves_no_trace():
    # 整批拒绝后（9 个广告主），后续合法请求不受影响
    bad = {
        "limit": 20,
        "windows": [
            {
                "id": f"w{j}",
                "start": j,
                "end": j + 1,
                "value": 1,
                "advertiser_id": f"adv{j}",
            }
            for j in range(9)
        ],
    }
    assert client.post(PATH, json=bad).status_code == 422
    good = {
        "limit": 2,
        "windows": [
            {"id": "a", "start": 0, "end": 10, "value": 10, "advertiser_id": "X"},
            {"id": "b", "start": 10, "end": 20, "value": 20, "advertiser_id": "Y"},
        ],
    }
    r = client.post(PATH, json=good)
    assert r.status_code == 200
    assert r.json()["profit"] == 30


def test_malformed_json_returns_400():
    r = client.post(
        PATH,
        content="{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["details"][0]["code"] == "INVALID_JSON"


# ---------------------------------------------------------------------------
# 规模与性能：2000 窗口、8 广告主、limit=20
# ---------------------------------------------------------------------------

def test_maximum_size_batch():
    # 20 个互不冲突的冠军窗（广告主交替）各 1e9，前后布满同主诱饵；
    # 唯一最优：20 条冠军，总收益 20e9。
    windows = []
    for j in range(20):
        base = j * 1_000_000
        windows.append(
            {
                "id": f"champ{j}",
                "start": base + 1000,
                "end": base + 1_000_000,
                "value": 1_000_000_000,
                "advertiser_id": "AB"[j % 2],
            }
        )
        for d in range(99):
            windows.append(
                {
                    "id": f"d{j}_{d}",
                    "start": base,
                    "end": base + 999_999,
                    "value": 999_999_999,
                    # 与本段冠军同主：诱饵即使入选也会挤掉冠军
                    "advertiser_id": "AB"[j % 2],
                }
            )
    assert len(windows) == 2000

    t0 = time.perf_counter()
    r = client.post(PATH, json={"limit": 20, "windows": windows})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text[:500]
    body = r.json()
    assert body["profit"] == 20_000_000_000
    assert [s["id"] for s in body["selections"]] == [
        f"champ{j}" for j in range(20)
    ]
    assert [s["advertiser_id"] for s in body["selections"]] == [
        "AB"[j % 2] for j in range(20)
    ]
    assert elapsed < 10


# ---------------------------------------------------------------------------
# 旧接口 /api/v1/schedules 的请求、结果与规模上限逐项不变
# ---------------------------------------------------------------------------

def test_legacy_endpoint_shape_unchanged():
    payload = {
        "limit": 2,
        "windows": [
            {"id": "a", "start": 0, "end": 10, "value": 10},
            {"id": "b", "start": 10, "end": 20, "value": 20},
        ],
    }
    r = client.post(LEGACY_PATH, json=payload)
    assert r.status_code == 200
    # 旧响应不包含 selections / advertiser 字段
    assert r.json() == {"profit": 30, "ids": ["a", "b"]}


def test_legacy_endpoint_rejects_advertiser_field_as_extra():
    payload = {
        "limit": 1,
        "windows": [
            {
                "id": "a",
                "start": 0,
                "end": 1,
                "value": 1,
                "advertiser_id": "X",
            }
        ],
    }
    r = client.post(LEGACY_PATH, json=payload)
    assert r.status_code == 422
    codes = [
        (d["code"], tuple(d["loc"]))
        for d in r.json()["error"]["details"]
    ]
    assert ("EXTRA_FIELD", ("windows", 0, "advertiser_id")) in codes


def test_legacy_limit_bound_still_50():
    # 旧接口 limit=50 仍然合法、51 越界
    r = client.post(LEGACY_PATH, json={"limit": 50, "windows": []})
    assert r.status_code == 200
    r = client.post(LEGACY_PATH, json={"limit": 51, "windows": []})
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["code"] == "OUT_OF_RANGE"


def test_legacy_window_cap_still_200k():
    # 旧接口 2001 窗口不触发新模式的 2000 上限（窗口数校验仍按 200 000）；
    # 构造 2001 个端点相接窗口应被接受，旧模式无广告主约束，limit=50 截断
    # 为编号最小（报价相同）的前 50 条。
    windows = [
        {"id": f"w{j}", "start": j, "end": j + 1, "value": 1}
        for j in range(2001)
    ]
    r = client.post(LEGACY_PATH, json={"limit": 50, "windows": windows})
    assert r.status_code == 200, r.text[:500]
    body = r.json()
    assert body["profit"] == 50
    assert body["ids"] == [f"w{j}" for j in range(50)]
