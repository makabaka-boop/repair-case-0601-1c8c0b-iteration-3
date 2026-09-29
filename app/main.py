"""FastAPI 应用：接收普通 JSON 批次，返回唯一最优清单。

错误响应统一形如
    {"error": {"code": <稳定错误码>, "details": [ ... ]}}
求解之前的 Pydantic 校验失败返回 422（非法 JSON 返回 400），
求解器不会被调用，因此非法批次不会留下任何部分结果。
"""

import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .schemas import (
    AdvertiserBatch,
    Batch,
    ERR_DUPLICATE_ID,
    ERR_TOO_MANY_ADVERTISERS,
    ERR_TOO_MANY_WINDOWS,
)
from .solver import solve, solve_with_advertisers

logger = logging.getLogger("scheduling")

# Pydantic v2 错误类型 -> 对外稳定错误码
_CODE_MAP = {
    ERR_DUPLICATE_ID: "DUPLICATE_ID",
    ERR_TOO_MANY_WINDOWS: "TOO_MANY_WINDOWS",
    ERR_TOO_MANY_ADVERTISERS: "TOO_MANY_ADVERTISERS",
    "start_end_order_error": "INVALID_INTERVAL",
    "missing": "MISSING_FIELD",
    "value_error": "INVALID_VALUE",
    "int_type": "TYPE_ERROR",
    "string_type": "TYPE_ERROR",
    "list_type": "TYPE_ERROR",
    "model_type": "TYPE_ERROR",
    "greater_than_equal": "OUT_OF_RANGE",
    "less_than_equal": "OUT_OF_RANGE",
    "string_too_short": "INVALID_VALUE",
    "extra_forbidden": "EXTRA_FIELD",
    "json_invalid": "INVALID_JSON",
    "json_type": "INVALID_JSON",
}


def _stable_code(error_type: str) -> str:
    return _CODE_MAP.get(error_type, error_type.upper())


def _detail(error: dict) -> dict:
    d = {
        "loc": [p if isinstance(p, int) else str(p) for p in error.get("loc", ())],
        "code": _stable_code(error.get("type", "unknown")),
        "message": error.get("msg", ""),
    }
    ctx = error.get("ctx")
    if ctx:
        # PydanticCustomError 的 ctx 携带结构化参数；跳过重复冗长的 ids 文本
        compact = {k: v for k, v in ctx.items() if k != "ids"}
        if compact:
            d["params"] = compact
            if error.get("type") == "json_invalid" and "error" in compact:
                inner = compact["error"]
                d["message"] = "request body is not valid JSON: " + str(inner)
    return d


app = FastAPI(title="weighted-interval-scheduling", version="1.0.0")


@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError):
    details = [_detail(e) for e in exc.errors()]

    # FastAPI 给 body 错误统一加 "body" 定位前缀，去掉后暴露字段路径。
    for d in details:
        loc = d["loc"]
        if d["code"] == "INVALID_JSON":
            # 非法 JSON 来自 body 本身，参数位 loc（如 [1]）没有字段意义。
            d["loc"] = ["body"]
            continue
        if loc and loc[0] == "body":
            loc = loc[1:]
            d["loc"] = loc
        # 自定义模型级错误（重复 id、窗口数超限）默认只定位到 body，补上稳定定位。
        if d["code"] == "DUPLICATE_ID" and not loc:
            d["loc"] = ["windows"]
        if d["code"] == "TOO_MANY_WINDOWS" and not loc:
            d["loc"] = ["windows"]
        if d["code"] == "TOO_MANY_ADVERTISERS" and not loc:
            d["loc"] = ["windows"]

    status = 400 if any(d["code"] == "INVALID_JSON" for d in details) else 422
    return JSONResponse(
        status_code=status,
        content={"error": {"code": "VALIDATION_FAILED", "details": details}},
    )


@app.exception_handler(StarletteHTTPException)
async def _on_http_error(request: Request, exc: StarletteHTTPException):
    code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(
        exc.status_code, "HTTP_ERROR"
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "details": [{"message": str(exc.detail)}]}},
    )


@app.exception_handler(Exception)
async def _on_unexpected(request: Request, exc: Exception):
    logger.exception("unexpected error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "details": []}},
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/api/v1/schedules")
async def create_schedule(batch: Batch):
    windows = [
        (w.id, w.start, w.end, w.value) for w in batch.windows
    ]
    # 纯 CPU 且最坏 1e7 次递推，放到线程池避免阻塞事件循环。
    profit, ids = await asyncio.get_running_loop().run_in_executor(
        None, solve, batch.limit, windows
    )
    return {"profit": profit, "ids": ids}


@app.post("/api/v1/advertiser-schedules")
async def create_advertiser_schedule(batch: AdvertiserBatch):
    windows = [
        (w.id, w.start, w.end, w.value, w.advertiser_id)
        for w in batch.windows
    ]
    # 纯 CPU：排序 O(n log n) + DP O(n * limit * A)（n<=2000, limit<=20, A<=8），
    # 放到线程池避免阻塞事件循环。
    profit, selections = await asyncio.get_running_loop().run_in_executor(
        None, solve_with_advertisers, batch.limit, windows
    )
    return {
        "profit": profit,
        "selections": [
            {"id": wid, "advertiser_id": advertiser}
            for wid, advertiser in selections
        ],
    }
