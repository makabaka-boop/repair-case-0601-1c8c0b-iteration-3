"""Pydantic v2 请求模型：求解之前完成全部整体校验。

任何类型错误、越界、start >= end、重复 id、多余字段、窗口数超限，
都会在进入动态规划之前被拒绝，不会产生任何部分结果（服务本身无状态）。

自定义违规使用 PydanticCustomError，其 type 即稳定错误码：
  - start_end_order_error
  - duplicate_window_id
  - too_many_windows
  - too_many_advertisers
  - duplicate_required_id（必播列表内部重复）
  - required_id_not_found（必播 id 不在窗口表中）
  - required_count_invalid（必播列表数量不在 1..3）
"""

from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    model_validator,
)
from pydantic_core import PydanticCustomError

from .solver import (
    AD_MAX_ADVERTISERS,
    AD_MAX_LIMIT,
    AD_MAX_WINDOWS,
    MAX_LIMIT,
    MAX_REQUIRED,
    MAX_TIME_MS,
    MAX_VALUE,
    MAX_WINDOWS,
)

ERR_START_END_ORDER = "start_end_order_error"
ERR_DUPLICATE_ID = "duplicate_window_id"
ERR_TOO_MANY_WINDOWS = "too_many_windows"
ERR_TOO_MANY_ADVERTISERS = "too_many_advertisers"
ERR_DUPLICATE_REQUIRED = "duplicate_required_id"
ERR_REQUIRED_NOT_FOUND = "required_id_not_found"
ERR_REQUIRED_COUNT = "required_count_invalid"

# 错误信息里重复 id 最多列出的数量，避免响应体无界
_REPORT_LIMIT = 100


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictStr = Field(min_length=1)
    start: StrictInt = Field(ge=0, le=MAX_TIME_MS)
    end: StrictInt = Field(ge=0, le=MAX_TIME_MS)
    value: StrictInt = Field(ge=0, le=MAX_VALUE)

    @model_validator(mode="after")
    def _validate_half_open(self):
        if self.start >= self.end:
            raise PydanticCustomError(
                ERR_START_END_ORDER,
                "start must be strictly smaller than end (half-open [start, end))",
                {"start": self.start, "end": self.end},
            )
        return self


class AdvertiserWindow(Window):
    """带广告主约束的窗口：沿用窗口/报价语义，额外要求非空广告主标识。"""

    model_config = ConfigDict(extra="forbid")

    advertiser_id: StrictStr = Field(min_length=1)


class Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: StrictInt = Field(ge=1, le=MAX_LIMIT)
    windows: list[Window]

    @model_validator(mode="after")
    def _validate_batch(self):
        if len(self.windows) > MAX_WINDOWS:
            raise PydanticCustomError(
                ERR_TOO_MANY_WINDOWS,
                "number of windows exceeds the maximum of {max_windows}",
                {"max_windows": MAX_WINDOWS, "actual": len(self.windows)},
            )

        seen: set[str] = set()
        dup: set[str] = set()
        for w in self.windows:
            if w.id in seen:
                dup.add(w.id)
            else:
                seen.add(w.id)
        if dup:
            dup_list = sorted(dup)
            shown = dup_list[:_REPORT_LIMIT]
            extra = len(dup_list) - len(shown)
            detail = ", ".join(shown)
            if extra:
                detail += f" (and {extra} more)"
            raise PydanticCustomError(
                ERR_DUPLICATE_ID,
                "duplicate window id(s): {ids}",
                {"ids": detail, "duplicate_count": len(dup_list)},
            )
        return self


class AdvertiserBatch(BaseModel):
    """带广告主约束排期批次：独立的窗口数、数量上限与广告主数规模上限。

    required_ids 为可选必播承诺：给定 1..3 个窗口 id（互不重复、必须存在），
    求解器在搜索过程中强制纳入它们；承诺之间无解时求解器返回不可排期。
    缺省（不传）时旧请求的结果与响应格式完全不变。
    """

    model_config = ConfigDict(extra="forbid")

    limit: StrictInt = Field(ge=1, le=AD_MAX_LIMIT)
    windows: list[AdvertiserWindow]
    # 必播 id 与窗口 id 一致：非空严格字符串（bool 不被接受）。
    required_ids: list[Annotated[StrictStr, Field(min_length=1)]] | None = (
        Field(default=None)
    )

    @model_validator(mode="after")
    def _validate_batch(self):
        if len(self.windows) > AD_MAX_WINDOWS:
            raise PydanticCustomError(
                ERR_TOO_MANY_WINDOWS,
                "number of windows exceeds the maximum of {max_windows}",
                {"max_windows": AD_MAX_WINDOWS, "actual": len(self.windows)},
            )

        seen: set[str] = set()
        dup: set[str] = set()
        advertisers: set[str] = set()
        for w in self.windows:
            if w.id in seen:
                dup.add(w.id)
            else:
                seen.add(w.id)
            advertisers.add(w.advertiser_id)
        if dup:
            dup_list = sorted(dup)
            shown = dup_list[:_REPORT_LIMIT]
            extra = len(dup_list) - len(shown)
            detail = ", ".join(shown)
            if extra:
                detail += f" (and {extra} more)"
            raise PydanticCustomError(
                ERR_DUPLICATE_ID,
                "duplicate window id(s): {ids}",
                {"ids": detail, "duplicate_count": len(dup_list)},
            )

        if len(advertisers) > AD_MAX_ADVERTISERS:
            raise PydanticCustomError(
                ERR_TOO_MANY_ADVERTISERS,
                "number of distinct advertisers exceeds the maximum of "
                "{max_advertisers}",
                {
                    "max_advertisers": AD_MAX_ADVERTISERS,
                    "actual": len(advertisers),
                },
            )

        if self.required_ids is not None:
            required = self.required_ids
            if not 1 <= len(required) <= MAX_REQUIRED:
                raise PydanticCustomError(
                    ERR_REQUIRED_COUNT,
                    "required_ids must contain between 1 and {max_required} "
                    "items",
                    {
                        "min_required": 1,
                        "max_required": MAX_REQUIRED,
                        "actual": len(required),
                    },
                )
            # 必播列表内部重复：参数错误（不是不可排期）。
            req_seen: set[str] = set()
            req_dup: set[str] = set()
            for rid in required:
                if rid in req_seen:
                    req_dup.add(rid)
                else:
                    req_seen.add(rid)
            if req_dup:
                dup_list = sorted(req_dup)
                raise PydanticCustomError(
                    ERR_DUPLICATE_REQUIRED,
                    "duplicate required id(s): {ids}",
                    {"ids": ", ".join(dup_list), "duplicate_count": len(dup_list)},
                )
            # 必播 id 不存在于窗口表：参数错误。
            missing = sorted(rid for rid in required if rid not in seen)
            if missing:
                shown = missing[:_REPORT_LIMIT]
                extra = len(missing) - len(shown)
                detail = ", ".join(shown)
                if extra:
                    detail += f" (and {extra} more)"
                raise PydanticCustomError(
                    ERR_REQUIRED_NOT_FOUND,
                    "required id(s) not found in windows: {ids}",
                    {"ids": detail, "missing_count": len(missing)},
                )
        return self
