"""FastAPI 端点测试：正确响应、稳定错误码、非法批次不留部分结果。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_happy_path_and_number_order():
    payload = {
        "limit": 2,
        "windows": [
            {"id": "late", "start": 50, "end": 60, "value": 9},
            {"id": "early", "start": 0, "end": 10, "value": 9},
        ],
    }
    r = client.post("/api/v1/schedules", json=payload)
    assert r.status_code == 200, r.text
    assert r.json() == {"profit": 18, "ids": ["early", "late"]}


def test_touching_endpoints_via_api():
    payload = {
        "limit": 2,
        "windows": [
            {"id": "a", "start": 0, "end": 10, "value": 10},
            {"id": "b", "start": 10, "end": 20, "value": 20},
        ],
    }
    r = client.post("/api/v1/schedules", json=payload)
    assert r.status_code == 200
    assert r.json() == {"profit": 30, "ids": ["a", "b"]}


def test_empty_windows_allowed():
    r = client.post("/api/v1/schedules", json={"limit": 1, "windows": []})
    assert r.status_code == 200
    assert r.json() == {"profit": 0, "ids": []}


def _codes(payload):
    r = client.post("/api/v1/schedules", json=payload)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    return r, [(d["code"], tuple(d["loc"])) for d in body["error"]["details"]]


def test_duplicate_id_rejected_before_solving():
    r, codes = _codes(
        {
            "limit": 1,
            "windows": [
                {"id": "x", "start": 0, "end": 1, "value": 1},
                {"id": "x", "start": 2, "end": 3, "value": 1},
            ],
        }
    )
    assert ("DUPLICATE_ID", ("windows",)) in codes
    d = r.json()["error"]["details"][0]
    assert d["params"]["duplicate_count"] == 1


def test_out_of_range_fields_rejected():
    cases = [
        ({"id": "a", "start": 5, "end": 5, "value": 1}, "INVALID_INTERVAL"),
        ({"id": "a", "start": -1, "end": 5, "value": 1}, "OUT_OF_RANGE"),
        ({"id": "a", "start": 0, "end": 86400001, "value": 1}, "OUT_OF_RANGE"),
        ({"id": "a", "start": 0, "end": 1, "value": 10**9 + 1}, "OUT_OF_RANGE"),
        ({"id": "a", "start": 0, "end": 1, "value": -1}, "OUT_OF_RANGE"),
    ]
    for w, expected in cases:
        _, codes = _codes({"limit": 1, "windows": [w]})
        assert any(code == expected for code, _ in codes), (w, codes)


def test_limit_bounds():
    for bad in (0, 51, -3):
        _, codes = _codes({"limit": bad, "windows": []})
        assert codes == [("OUT_OF_RANGE", ("limit",))], (bad, codes)


def test_type_errors_including_boolean():
    # bool 不得被当作 int 接受
    _, codes = _codes({"limit": True, "windows": []})
    assert codes == [("TYPE_ERROR", ("limit",))]
    _, codes = _codes(
        {"limit": 1, "windows": [{"id": 1, "start": 0, "end": 1, "value": 1}]}
    )
    assert ("TYPE_ERROR", ("windows", 0, "id")) in codes


def test_missing_and_extra_fields():
    _, codes = _codes({"limit": 1, "windows": [{"id": "a", "start": 0, "end": 1}]})
    assert ("MISSING_FIELD", ("windows", 0, "value")) in codes
    _, codes = _codes({"limit": 1, "windows": [], "unexpected": 7})
    assert ("EXTRA_FIELD", ("unexpected",)) in codes
    _, codes = _codes(
        {
            "limit": 1,
            "windows": [{"id": "a", "start": 0, "end": 1, "value": 0, "x": 1}],
        }
    )
    assert ("EXTRA_FIELD", ("windows", 0, "x")) in codes


def test_empty_id_rejected():
    _, codes = _codes(
        {"limit": 1, "windows": [{"id": "", "start": 0, "end": 1, "value": 0}]}
    )
    assert codes == [("INVALID_VALUE", ("windows", 0, "id"))]


def test_malformed_json_returns_400_stable_code():
    r = client.post(
        "/api/v1/schedules",
        content="{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    details = r.json()["error"]["details"]
    assert details[0]["code"] == "INVALID_JSON"


def test_method_and_path_errors():
    assert client.get("/api/v1/schedules").status_code == 405
    assert client.post("/api/v1/nope", json={}).status_code == 404


def test_invalid_batch_leaves_no_trace():
    # 服务无状态：非法请求 422 后，另一个合法请求结果不受任何影响
    bad = {
        "limit": 1,
        "windows": [
            {"id": "a", "start": 0, "end": 1, "value": 1},
            {"id": "a", "start": 1, "end": 2, "value": 1},
        ],
    }
    assert client.post("/api/v1/schedules", json=bad).status_code == 422
    good = {
        "limit": 1,
        "windows": [{"id": "a", "start": 0, "end": 1, "value": 42}],
    }
    r = client.post("/api/v1/schedules", json=good)
    assert r.status_code == 200
    assert r.json() == {"profit": 42, "ids": ["a"]}


def test_maximum_size_structured_batch():
    """最大输入：20 万窗口，已知唯一最优 50 * 1e9。"""
    windows = []
    for j in range(50):
        base = j * 1_000_000
        windows.append(
            {"id": f"champ{j}", "start": base + 1000, "end": base + 1_000_000,
             "value": 1_000_000_000}
        )
        for d in range(3999):
            windows.append(
                {"id": f"d{j}_{d}", "start": base, "end": base + 999_999,
                 "value": 999_999_999}
            )
    assert len(windows) == 200_000

    r = client.post("/api/v1/schedules", json={"limit": 50, "windows": windows})
    assert r.status_code == 200, r.text[:500]
    body = r.json()
    assert body["profit"] == 50_000_000_000
    assert body["ids"] == [f"champ{j}" for j in range(50)]
