"""承诺必播接口与运营页面测试。

覆盖：必播强制纳入的正确响应、承诺无解 409 NO_FEASIBLE_SCHEDULE 且无部分方案、
必播参数错误（重复/不存在/超过 3 个/类型错误）、空列表与缺省等价、
旧接口不收 required_ids；页面路由可访问且选择态只认服务端合法解、失败清空。
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PATH = "/api/v1/advertiser-schedules"
LEGACY_PATH = "/api/v1/schedules"


def _windows():
    return [
        {"id": "a", "start": 0, "end": 10, "value": 100, "advertiser_id": "X"},
        {"id": "z", "start": 10, "end": 20, "value": 0, "advertiser_id": "Y"},
        {"id": "b", "start": 20, "end": 30, "value": 100, "advertiser_id": "X"},
        {"id": "c", "start": 30, "end": 40, "value": 5, "advertiser_id": "Y"},
    ]


# ---------------------------------------------------------------------------
# 正确响应
# ---------------------------------------------------------------------------

def test_required_pair_forces_zero_value_bridge():
    # limit=3：两条 X 必播 + 零价 Y 隔断恰好占满，c 无法再排入。
    payload = {"limit": 3, "windows": _windows(), "required_ids": ["a", "b"]}
    r = client.post(PATH, json=payload)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "profit": 200,
        "selections": [
            {"id": "a", "advertiser_id": "X"},
            {"id": "z", "advertiser_id": "Y"},
            {"id": "b", "advertiser_id": "X"},
        ],
    }


def test_three_required_with_bridge():
    payload = {
        "limit": 4,
        "windows": _windows(),
        "required_ids": ["a", "b", "c"],
    }
    r = client.post(PATH, json=payload)
    assert r.status_code == 200, r.text
    assert [s["id"] for s in r.json()["selections"]] == ["a", "z", "b", "c"]
    assert r.json()["profit"] == 205


def test_required_response_shape_unchanged():
    # 带必播的成功响应与旧响应同形（不额外回显 required_ids）。
    payload = {"limit": 1, "windows": _windows()[:1], "required_ids": ["a"]}
    r = client.post(PATH, json=payload)
    assert r.status_code == 200
    assert set(r.json().keys()) == {"profit", "selections"}


def test_required_list_order_independent():
    base = {"limit": 4, "windows": _windows()}
    r1 = client.post(PATH, json=dict(base, required_ids=["b", "a", "c"]))
    r2 = client.post(PATH, json=dict(base, required_ids=["c", "a", "b"]))
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()


# ---------------------------------------------------------------------------
# 不可排期：409，明确拒绝、无部分方案
# ---------------------------------------------------------------------------

def test_overlapping_required_returns_409_no_partial_schedule():
    windows = [
        {"id": "a", "start": 0, "end": 10, "value": 10, "advertiser_id": "X"},
        {"id": "b", "start": 5, "end": 15, "value": 10, "advertiser_id": "Y"},
    ]
    r = client.post(PATH, json={"limit": 2, "windows": windows,
                                "required_ids": ["a", "b"]})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"]["code"] == "NO_FEASIBLE_SCHEDULE"
    detail = body["error"]["details"][0]
    assert tuple(detail["loc"]) == ("required_ids",)
    assert detail["params"]["required_ids"] == ["a", "b"]
    # 无任何部分方案字段
    assert "selections" not in body and "profit" not in body


def test_same_owner_required_without_bridge_returns_409():
    windows = [
        {"id": "a", "start": 0, "end": 10, "value": 10, "advertiser_id": "X"},
        {"id": "b", "start": 10, "end": 20, "value": 10, "advertiser_id": "X"},
    ]
    r = client.post(PATH, json={"limit": 2, "windows": windows,
                                "required_ids": ["a", "b"]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NO_FEASIBLE_SCHEDULE"


def test_limit_exhausted_returns_409():
    windows = [
        {"id": "a", "start": 0, "end": 1, "value": 10, "advertiser_id": "X"},
        {"id": "b", "start": 2, "end": 3, "value": 10, "advertiser_id": "Y"},
        {"id": "c", "start": 4, "end": 5, "value": 10, "advertiser_id": "Z"},
    ]
    r = client.post(PATH, json={"limit": 2, "windows": windows,
                                "required_ids": ["a", "b", "c"]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NO_FEASIBLE_SCHEDULE"


def test_infeasible_does_not_poison_followup_requests():
    bad = {
        "limit": 2,
        "windows": [
            {"id": "a", "start": 0, "end": 10, "value": 10, "advertiser_id": "X"},
            {"id": "b", "start": 5, "end": 15, "value": 10, "advertiser_id": "Y"},
        ],
        "required_ids": ["a", "b"],
    }
    assert client.post(PATH, json=bad).status_code == 409
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


# ---------------------------------------------------------------------------
# 参数校验：422
# ---------------------------------------------------------------------------

def _codes(payload):
    r = client.post(PATH, json=payload)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    return r, [(d["code"], tuple(d["loc"])) for d in body["error"]["details"]]


def test_duplicate_required_id_rejected():
    r, codes = _codes(
        {"limit": 2, "windows": _windows(), "required_ids": ["a", "a"]}
    )
    assert codes == [("REQUIRED_DUPLICATE_ID", ("required_ids",))]
    detail = r.json()["error"]["details"][0]
    assert "a" in detail["message"]


def test_unknown_required_id_rejected():
    r, codes = _codes(
        {"limit": 2, "windows": _windows(), "required_ids": ["a", "ghost"]}
    )
    assert codes == [("REQUIRED_UNKNOWN_ID", ("required_ids",))]
    detail = r.json()["error"]["details"][0]
    assert "ghost" in detail["message"]
    assert detail["params"]["unknown_count"] == 1


def test_too_many_required_rejected():
    windows = [
        {"id": f"w{j}", "start": j * 10, "end": j * 10 + 5,
         "value": 1, "advertiser_id": "ABC"[j % 3]}
        for j in range(4)
    ]
    r, codes = _codes(
        {"limit": 4, "windows": windows,
         "required_ids": ["w0", "w1", "w2", "w3"]}
    )
    assert codes == [("REQUIRED_TOO_MANY", ("required_ids",))]
    params = r.json()["error"]["details"][0]["params"]
    assert params == {"max_required": 3, "actual": 4}


def test_exactly_three_required_accepted():
    r = client.post(
        PATH,
        json={"limit": 4, "windows": _windows(),
              "required_ids": ["a", "z", "b"]},
    )
    assert r.status_code == 200, r.text


def test_required_wrong_type_rejected():
    _, codes = _codes(
        {"limit": 2, "windows": _windows(), "required_ids": "a"}
    )
    assert ("TYPE_ERROR", ("required_ids",)) in codes
    _, codes = _codes(
        {"limit": 2, "windows": _windows(), "required_ids": [7]}
    )
    assert ("TYPE_ERROR", ("required_ids", 0)) in codes
    _, codes = _codes(
        {"limit": 2, "windows": _windows(), "required_ids": [None]}
    )
    assert ("TYPE_ERROR", ("required_ids", 0)) in codes
    _, codes = _codes(
        {"limit": 2, "windows": _windows(), "required_ids": [""]}
    )
    # 空串是合法字符串类型，但不对应任何窗口 id -> 未知必播 id
    assert ("REQUIRED_UNKNOWN_ID", ("required_ids",)) in codes


def test_empty_required_list_means_no_constraint():
    payload = {"limit": 4, "windows": _windows(), "required_ids": []}
    r = client.post(PATH, json=payload)
    assert r.status_code == 200
    without = client.post(PATH, json={"limit": 4, "windows": _windows()})
    assert r.json() == without.json()


def test_null_required_ids_means_no_constraint():
    payload = {"limit": 4, "windows": _windows(), "required_ids": None}
    r = client.post(PATH, json=payload)
    assert r.status_code == 200
    # X,Y,X,Y 交替、端点相接：4 条全选收益 205
    assert [s["id"] for s in r.json()["selections"]] == ["a", "z", "b", "c"]


def test_required_field_validation_before_solving():
    # 必播 id 不存在是参数错误，而不是 409 不可排期。
    r = client.post(
        PATH,
        json={"limit": 2, "windows": _windows(),
              "required_ids": ["missing"]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["code"] == "REQUIRED_UNKNOWN_ID"


def test_legacy_endpoint_rejects_required_ids_as_extra():
    r = client.post(
        LEGACY_PATH,
        json={
            "limit": 1,
            "windows": [{"id": "a", "start": 0, "end": 1, "value": 1}],
            "required_ids": ["a"],
        },
    )
    assert r.status_code == 422
    codes = [
        (d["code"], tuple(d["loc"]))
        for d in r.json()["error"]["details"]
    ]
    assert ("EXTRA_FIELD", ("required_ids",)) in codes


def test_legacy_requests_byte_identical_without_required():
    payload = {
        "limit": 2,
        "windows": [
            {"id": "a", "start": 0, "end": 10, "value": 10},
            {"id": "b", "start": 10, "end": 20, "value": 20},
        ],
    }
    r = client.post(LEGACY_PATH, json=payload)
    assert r.json() == {"profit": 30, "ids": ["a", "b"]}


# ---------------------------------------------------------------------------
# 运营页面：接口、领域求解和页面选择态共用同一份合法解
# ---------------------------------------------------------------------------

def test_page_served_at_root():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    html = r.text
    # 承诺必播能力与“选择态只来自服务端解”的关键文案/钩子
    assert "承诺必播" in html
    assert "required_ids" in html
    assert "/api/v1/advertiser-schedules" in html
    assert "NO_FEASIBLE_SCHEDULE" in html


def test_page_script_clears_selection_on_failure():
    # 页面脚本中，失败分支必须清空旧解并禁用提交，不残留可提交的旧排期。
    html = client.get("/").text
    assert "schedule = null" in html
    assert "commitBtn.disabled" in html
    # 提交按钮只在 dirty=false 且存在服务端解时可用
    assert "commitBtn.disabled = dirty" in html
