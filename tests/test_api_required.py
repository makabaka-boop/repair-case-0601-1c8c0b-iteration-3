"""必播承诺（required_ids）接口测试。

覆盖：
  - 必播求解正确响应（强制纳入、同主隔断、零收益过渡片段）；
  - 必播互斥返回 422 UNSCHEDULABLE，且不给出部分方案；
  - 重复必播 id / 不存在必播 id / 数量越界 / 类型错误 -> 422 参数错误；
  - 不传 required_ids 时旧请求逐字节不变；旧接口 /api/v1/schedules
    收到 required_ids 视为多余字段拒绝；
  - 运营页面 GET / 可访问，并展示必播选择态与失败清空的脚本不变量。
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PATH = "/api/v1/advertiser-schedules"
LEGACY_PATH = "/api/v1/schedules"


# ---------------------------------------------------------------------------
# 正确响应
# ---------------------------------------------------------------------------


def test_required_pair_forced_with_zero_bridge():
    payload = {
        "limit": 3,
        "windows": [
            {"id": "x1", "start": 0, "end": 10, "value": 10, "advertiser_id": "acme"},
            {"id": "bridge", "start": 10, "end": 12, "value": 0,
             "advertiser_id": "globex"},
            {"id": "x2", "start": 12, "end": 22, "value": 10, "advertiser_id": "acme"},
            {"id": "big", "start": 0, "end": 22, "value": 25,
             "advertiser_id": "initech"},
        ],
        "required_ids": ["x1", "x2"],
    }
    r = client.post(PATH, json=payload)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "profit": 20,
        "selections": [
            {"id": "x1", "advertiser_id": "acme"},
            {"id": "bridge", "advertiser_id": "globex"},
            {"id": "x2", "advertiser_id": "acme"},
        ],
    }


def test_single_required_id():
    payload = {
        "limit": 2,
        "windows": [
            {"id": "a", "start": 0, "end": 10, "value": 10, "advertiser_id": "X"},
            {"id": "b", "start": 10, "end": 20, "value": 20, "advertiser_id": "Y"},
        ],
        "required_ids": ["a"],
    }
    r = client.post(PATH, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["id"] for s in body["selections"]] == ["a", "b"]
    assert body["profit"] == 30


def test_required_ids_order_independent():
    windows = [
        {"id": "r1", "start": 0, "end": 5, "value": 10, "advertiser_id": "X"},
        {"id": "g", "start": 5, "end": 7, "value": 1, "advertiser_id": "Y"},
        {"id": "r2", "start": 7, "end": 12, "value": 10, "advertiser_id": "X"},
    ]
    b1 = client.post(PATH, json={
        "limit": 3, "windows": windows, "required_ids": ["r1", "r2"]}).json()
    b2 = client.post(PATH, json={
        "limit": 3, "windows": windows, "required_ids": ["r2", "r1"]}).json()
    assert b1 == b2
    assert [s["id"] for s in b1["selections"]] == ["r1", "g", "r2"]


def test_response_selections_actually_contain_all_required():
    # 接口不变量：成功响应的 selections 必须包含全部必播 id。
    payload = {
        "limit": 5,
        "windows": [
            {"id": f"w{j}", "start": j * 4, "end": j * 4 + 3,
             "value": j, "advertiser_id": "ABC"[j % 3]}
            for j in range(6)
        ],
        "required_ids": ["w1", "w4"],
    }
    r = client.post(PATH, json=payload)
    assert r.status_code == 200, r.text
    ids_ = {s["id"] for s in r.json()["selections"]}
    assert {"w1", "w4"} <= ids_


# ---------------------------------------------------------------------------
# 不可排期：422 UNSCHEDULABLE，无部分方案
# ---------------------------------------------------------------------------


def test_overlapping_required_returns_unschedulable():
    payload = {
        "limit": 3,
        "windows": [
            {"id": "a", "start": 0, "end": 10, "value": 10, "advertiser_id": "X"},
            {"id": "b", "start": 5, "end": 15, "value": 10, "advertiser_id": "Y"},
        ],
        "required_ids": ["a", "b"],
    }
    r = client.post(PATH, json=payload)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "UNSCHEDULABLE"
    detail = body["error"]["details"][0]
    assert detail["code"] == "UNSCHEDULABLE"
    assert tuple(detail["loc"]) == ("required_ids",)
    assert detail["params"]["required_ids"] == ["a", "b"]
    # 不给出部分方案：响应里没有 selections/profit 字段。
    assert "selections" not in body
    assert "profit" not in body


def test_same_owner_touching_required_returns_unschedulable():
    payload = {
        "limit": 2,
        "windows": [
            {"id": "a", "start": 0, "end": 10, "value": 10, "advertiser_id": "X"},
            {"id": "b", "start": 10, "end": 20, "value": 10, "advertiser_id": "X"},
        ],
        "required_ids": ["a", "b"],
    }
    r = client.post(PATH, json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNSCHEDULABLE"


def test_limit_exhaustion_returns_unschedulable():
    payload = {
        "limit": 2,
        "windows": [
            {"id": "a", "start": 0, "end": 4, "value": 5, "advertiser_id": "X"},
            {"id": "b", "start": 4, "end": 8, "value": 5, "advertiser_id": "Y"},
            {"id": "c", "start": 8, "end": 12, "value": 5, "advertiser_id": "Z"},
        ],
        "required_ids": ["a", "b", "c"],
    }
    r = client.post(PATH, json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNSCHEDULABLE"


def test_unschedulable_then_retry_without_required_still_works():
    # 失败不残留状态：同一连接上随后的合法（无必播）请求正常返回。
    bad = {
        "limit": 2,
        "windows": [
            {"id": "a", "start": 0, "end": 10, "value": 10, "advertiser_id": "X"},
            {"id": "b", "start": 5, "end": 15, "value": 10, "advertiser_id": "Y"},
        ],
        "required_ids": ["a", "b"],
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


# ---------------------------------------------------------------------------
# 参数错误（求解前拒绝）
# ---------------------------------------------------------------------------


def _codes(payload):
    r = client.post(PATH, json=payload)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    return r, [(d["code"], tuple(d["loc"])) for d in body["error"]["details"]]


def test_duplicate_required_id_rejected():
    r, codes = _codes(
        {
            "limit": 2,
            "windows": [
                {"id": "a", "start": 0, "end": 1, "value": 1, "advertiser_id": "X"},
            ],
            "required_ids": ["a", "a"],
        }
    )
    assert ("DUPLICATE_REQUIRED_ID", ("required_ids",)) in codes
    params = r.json()["error"]["details"][0]["params"]
    assert params["duplicate_count"] == 1


def test_missing_required_id_rejected():
    r, codes = _codes(
        {
            "limit": 2,
            "windows": [
                {"id": "a", "start": 0, "end": 1, "value": 1, "advertiser_id": "X"},
            ],
            "required_ids": ["a", "ghost"],
        }
    )
    assert ("REQUIRED_ID_NOT_FOUND", ("required_ids",)) in codes
    params = r.json()["error"]["details"][0]["params"]
    assert params["missing_count"] == 1


def test_required_count_bounds():
    base_windows = [
        {"id": f"w{j}", "start": j, "end": j + 1, "value": 1,
         "advertiser_id": "X"}
        for j in range(4)
    ]
    # 空列表（0 个）拒绝
    _, codes = _codes({"limit": 2, "windows": base_windows, "required_ids": []})
    assert ("REQUIRED_COUNT_INVALID", ("required_ids",)) in codes
    # 超过 3 个拒绝
    _, codes = _codes(
        {"limit": 4, "windows": base_windows,
         "required_ids": ["w0", "w1", "w2", "w3"]}
    )
    assert ("REQUIRED_COUNT_INVALID", ("required_ids",)) in codes


def test_required_ids_type_errors():
    base = {
        "limit": 2,
        "windows": [
            {"id": "a", "start": 0, "end": 1, "value": 1, "advertiser_id": "X"},
        ],
    }
    _, codes = _codes({**base, "required_ids": "a"})
    assert ("TYPE_ERROR", ("required_ids",)) in codes
    _, codes = _codes({**base, "required_ids": [1]})
    assert ("TYPE_ERROR", ("required_ids", 0)) in codes
    # bool 不得冒充字符串
    _, codes = _codes({**base, "required_ids": [True]})
    assert ("TYPE_ERROR", ("required_ids", 0)) in codes


def test_required_empty_string_rejected():
    _, codes = _codes(
        {
            "limit": 2,
            "windows": [
                {"id": "a", "start": 0, "end": 1, "value": 1, "advertiser_id": "X"},
            ],
            "required_ids": [""],
        }
    )
    # 空字符串本身不是合法 StrictStr 内容 -> INVALID_VALUE
    assert ("INVALID_VALUE", ("required_ids", 0)) in codes


def test_extra_field_alongside_required_still_rejected():
    _, codes = _codes(
        {
            "limit": 1,
            "windows": [
                {"id": "a", "start": 0, "end": 1, "value": 1,
                 "advertiser_id": "X", "nope": 1},
            ],
            "required_ids": ["a"],
        }
    )
    assert ("EXTRA_FIELD", ("windows", 0, "nope")) in codes


# ---------------------------------------------------------------------------
# 向后兼容
# ---------------------------------------------------------------------------


def test_omitting_required_ids_byte_for_byte_unchanged():
    payload = {
        "limit": 2,
        "windows": [
            {"id": "a", "start": 0, "end": 10, "value": 10, "advertiser_id": "X"},
            {"id": "b", "start": 10, "end": 20, "value": 20, "advertiser_id": "Y"},
        ],
    }
    r = client.post(PATH, json=payload)
    assert r.status_code == 200
    assert r.json() == {
        "profit": 30,
        "selections": [
            {"id": "a", "advertiser_id": "X"},
            {"id": "b", "advertiser_id": "Y"},
        ],
    }
    # 显式 null 等价于不传
    r2 = client.post(PATH, json={**payload, "required_ids": None})
    assert r2.status_code == 200
    assert r2.json() == r.json()


def test_legacy_endpoint_rejects_required_ids_as_extra_field():
    # 旧接口不认识 required_ids：extra=forbid 整体拒绝，旧格式不受影响。
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


def test_legacy_responses_unchanged_shape():
    r = client.post(
        LEGACY_PATH,
        json={
            "limit": 1,
            "windows": [{"id": "a", "start": 0, "end": 1, "value": 7}],
        },
    )
    assert r.json() == {"profit": 7, "ids": ["a"]}


# ---------------------------------------------------------------------------
# 运营页面
# ---------------------------------------------------------------------------


def test_operations_page_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    html = r.text
    # 必播选择态与同一合法解提交的关键元素/脚本不变量
    assert 'id="solve-btn"' in html
    assert 'data-req' in html
    assert "/api/v1/advertiser-schedules" in html
    # 失败路径明确清空可提交旧排期
    assert "invalidateSolution" in html
    assert "UNSCHEDULABLE" in html
    # 提交按钮默认禁用，只有成功求解后才启用
    assert 'id="commit-btn"' in html


def test_healthz_still_ok():
    assert client.get("/healthz").json() == {"status": "ok"}
