# 限额加权区间调度服务（Weighted Interval Scheduling with Cardinality Limit）

地方台广告窗大量重叠时，“按最高报价贪心”会误排（例如一个报价 100 的长窗不如
两个各报价 60 的相接短窗）。本服务用**限额加权区间动态规划**计算总收益最高、
且窗口数不超过 `limit` 的排期，并在总收益并列时给出**唯一、可重算**的清单。

- 框架：FastAPI + Pydantic v2（普通 JSON）
- 运行时依赖：仅 `fastapi` / `pydantic` / `uvicorn`
- 规模：`1 ≤ limit ≤ 50`，窗口数 `n ≤ 200 000`
- 时间单位：毫秒，半开区间 `[start, end)`；前项 `end` 等于后项 `start` **不冲突**
- 约束：`0 ≤ start < end ≤ 86 400 000`，`0 ≤ value ≤ 10^9`
- 禁止枚举子集；同收益方案有确定的唯一裁决

## 编号规则

窗口按三元组 **(end 升序, start 升序, 输入下标升序)** 编号为 `1..n`。
输出 `ids` 按该编号升序排列，且与请求体内的窗口顺序无关。

## 动态规划

设

- `v_i` = 编号 `i` 的报价；
- `p(i) = |{ j < i : end_j ≤ start_i }|`：编号 `i` 之前与 `i` 不冲突的窗口数。
  半开区间端点相接不冲突，故用 `bisect_right(ends, start_i, 0, i)` 求（`ends` 已随编号升序）。

状态 `dp[i][k]` 表示**前 `i` 个窗口中至多选 `k` 个**的最大收益：

```
dp[i][k] = max(
    dp[i-1][k],            # 排除编号 i
    dp[p(i)][k-1] + v_i,   # 纳入编号 i
)
```

基例：`dp[0][k] = 0`，`dp[i][0] = 0`。答案为 `dp[n][limit]`。

**同分裁决（唯一性）**：递推时仅当“纳入”收益**严格大于**“排除”收益才纳入；
两者相等时固定排除当前编号。等价地，在所有最优子集中取“按编号的选择掩码最小”者。

回溯：`i = n, k = limit`，

- 若 `dp[i][k] == dp[i-1][k]`（纳入不严格更优）→ 排除 `i`，`i -= 1`；
- 否则 → 选中 `i`，`k -= 1`，`i = p(i)`。

回溯与填表共用同一裁决，因此清单唯一且可重算。

### 复杂度

| 阶段 | 复杂度 |
| --- | --- |
| 排序 + 前驱 | `O(n log n)` |
| 动态规划 | `O(n × limit)` |
| 回溯 | `O(n)` |
| **总时间** | **`O(n log n + n × limit)`** |
| 空间 | `O(n × limit)`（int64 紧凑存储；200 000 × 51 ≈ 82 MB） |

最大总收益 `50 × 10^9 = 5×10^10`，64 位有符号整数足够，无溢出。

实测（200 000 窗口、limit=50）：约 2 秒、峰值 RSS ≈ 175 MB。

## 接口

### `POST /api/v1/schedules`

请求：

```json
{
  "limit": 2,
  "windows": [
    {"id": "a", "start": 0, "end": 10, "value": 10},
    {"id": "b", "start": 10, "end": 20, "value": 20}
  ]
}
```

响应 `200`（`ids` 按窗口编号升序）：

```json
{"profit": 30, "ids": ["a", "b"]}
```

`GET /healthz` → `{"status": "ok"}`。

### 校验（求解前整体拒绝）

Pydantic 在调用求解器之前完成全部校验；`bool` 不会被当作整数接受，
多余字段、空 id 同样拒绝。错误响应统一为：

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "details": [
      {"loc": ["windows", 0], "code": "INVALID_INTERVAL", "message": "..."}
    ]
  }
}
```

| 稳定错误码 | HTTP | 含义 |
| --- | --- | --- |
| `VALIDATION_FAILED` | 422 | 统一校验失败包装（明细码位于 `details[].code`） |
| `DUPLICATE_ID` | 422 | 窗口 id 重复（定位 `windows`，含重复数） |
| `INVALID_INTERVAL` | 422 | `start >= end` |
| `OUT_OF_RANGE` | 422 | `limit`/`start`/`end`/`value` 越界 |
| `TYPE_ERROR` | 422 | 字段类型错误（含 `true/false` 冒充整数） |
| `MISSING_FIELD` | 422 | 缺字段 |
| `EXTRA_FIELD` | 422 | 多余字段 |
| `INVALID_VALUE` | 422 | 其它取值错误（如空 id） |
| `TOO_MANY_WINDOWS` | 422 | 窗口数超过 200 000 |
| `INVALID_JSON` | 400 | 请求体不是合法 JSON |

服务**完全无状态**：非法批次在求解前被拒绝，不会产生或残留任何部分结果；
合法大批次的清单由递推与裁决唯一确定，重复请求逐字节一致。

## 带广告主约束排期

### `POST /api/v1/advertiser-schedules`

合同要求**实际播出的相邻两条广告不能来自同一广告主**：约束只作用于实际选中的
窗口序列，空档与未选中窗口都不构成隔断（“上一个已选广告主”可跨越任意多条未选
窗口）。半开区间不冲突、数量上限与同收益裁决语义与普通模式完全一致。

请求沿用窗口、报价与数量上限语义，并为每个窗口提供非空 `advertiser_id`：

```json
{
  "limit": 2,
  "windows": [
    {"id": "a", "start": 0, "end": 10, "value": 10, "advertiser_id": "acme"},
    {"id": "b", "start": 10, "end": 20, "value": 20, "advertiser_id": "globex"}
  ]
}
```

响应 `200`（`selections` 按窗口编号升序，逐条带广告主标识）：

```json
{
  "profit": 30,
  "selections": [
    {"id": "a", "advertiser_id": "acme"},
    {"id": "b", "advertiser_id": "globex"}
  ]
}
```

规模上限（独立于普通模式）：窗口数 `n ≤ 2000`、不同广告主数 `≤ 8`、
`1 ≤ limit ≤ 20`。端点相接（前项 `end` == 后项 `start`）仍不算时间重叠；
但相接的两条若同主仍不能相邻入选。

广告主约束与区间/数量约束在动态规划中**联合求解**（不做“先算旧最优、再事后删
连续同主条目”的后处理）：状态在编号、已选数量之外再记录“上一个已选广告主”
（哨兵 0 表示尚无已选），纳入编号 `i` 时只允许从最后广告主不同于 `a_i` 的前驱
状态转移。新增稳定错误码：

| 稳定错误码 | HTTP | 含义 |
| --- | --- | --- |
| `TOO_MANY_ADVERTISERS` | 422 | 不同广告主数超过 8（定位 `windows`，含实际数量） |

非法标识、超限或无效窗口在求解前整批拒绝，不输出任何部分方案。
空批次与全零报价合法，返回 `profit = 0`、空 `selections`。

## 运行

### Docker Compose

宿主端口读取环境变量 `API_PORT`（默认 8000）：

```bash
cp .env.example .env          # 可修改 API_PORT
docker compose up --build -d  # 宿主机访问 http://localhost:${API_PORT}
```

### 验收服务 verify

```bash
docker compose --profile verify up --build verify
# api 健康检查通过后，verify 容器对其执行 verify.py；
# 全部检查通过输出 "ALL VERIFY CHECKS PASSED" 并以 0 退出
```

验收内容：健康检查、端点相接、重叠贪心误排场景、同分裁决（含输入顺序无关性）、
重复 id 与 `start>=end` 拒绝、以及 200 000 窗口最大批次的唯一最优
（`profit = 50 × 10^9`，恰为 50 个冠军窗口）与可重算性。

### 本地开发

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
pytest -q
python verify.py http://localhost:8000
```

## 测试

`tests/` 覆盖：

- 端点相接/真正重叠的区分；
- 同分裁决（end 序、(end,start) 序、输入下标序；全零值空清单）；
- 小随机用例（`n ≤ 6`）与**枚举子集参考实现**逐项对照 200 组；
- 最大输入 200 000 窗口的已知唯一最优与可重算；
- API 全部稳定错误码、非法 JSON、无部分结果残留。
