"""限额加权区间调度（weighted interval scheduling with a cardinality limit）。

输入窗口已由上层（Pydantic 模型）校验：id 唯一、字段合法。

编号规则（题目要求）：窗口按 (end, start, 输入下标) 升序排列后编号 1..n。

递推式
------
令 p(i) = |{ j < i : end_j <= start_i }|，即编号 i 之前与 i 不冲突的窗口数
（半开区间 [start, end)，end == start 不算冲突）。

    dp[i][k] = max(
        dp[i - 1][k],            # 排除编号 i
        dp[p(i)][k - 1] + v_i,   # 纳入编号 i
    )

基例：dp[0][k] = 0，dp[i][0] = 0。
同分裁决：仅当“纳入”严格大于“排除”时才纳入，相等时固定排除编号 i，
从而总收益相同的方案有唯一清单（数值上等价于在最优子集中取编号掩码最小者）。

回溯：i = n, k = limit；若 dp[i][k] == dp[i-1][k] 则 i--，
否则选中 i、k--、i = p(i)。

复杂度：排序 O(n log n)，前驱 O(n log n)，动态规划 O(n * limit)，
回溯 O(n)；收益使用滚动 int64 层，选择决策使用位图。
最大总收益 50 * 10^9 = 5e10，int64 足够。
"""

from array import array
from bisect import bisect_right

MAX_LIMIT = 50
MAX_WINDOWS = 200_000
MAX_TIME_MS = 86_400_000
MAX_VALUE = 1_000_000_000

# 带广告主约束排期的独立规模上限。
AD_MAX_LIMIT = 20
AD_MAX_WINDOWS = 2_000
AD_MAX_ADVERTISERS = 8
# 不可撤销的承诺必播片段数量上限（1..AD_MAX_REQUIRED 个）。
AD_MAX_REQUIRED = 3


class RequiredWindowError(ValueError):
    """必播 id 重复或不存在：领域层的防御性校验，正常由接口层先行拒绝。"""


def solve(limit, windows):
    """求唯一最优清单。

    参数
    ----
    limit: 最多选择的窗口数（1..50，由上层校验）。
    windows: [(id, start, end, value), ...]，保持输入顺序。

    返回
    ----
    (profit, ids)：总收益，以及按窗口编号升序排列的 id 列表。
    """
    n = len(windows)

    # 编号：按 (end, start, 输入下标) 升序
    order = sorted(range(n), key=lambda j: (windows[j][2], windows[j][1], j))
    ids = [None] * n
    starts = [0] * n
    ends = [0] * n
    values = [0] * n
    for pos, src in enumerate(order):
        wid, st, en, va = windows[src]
        ids[pos] = wid
        starts[pos] = st
        ends[pos] = en
        values[pos] = va

    # p(i)：ends 已随编号升序，直接二分统计 end_j <= start_i 的前驱个数。
    # 半开端点相接不冲突，故用 bisect_right（<= 而非 <）。
    # 注意 starts 并不随编号单调，不能用单向游标批量扫描。
    pred = array("I", [0]) * n
    for i, start in enumerate(starts):
        pred[i] = bisect_right(ends, start, 0, i)

    # 按选择数量转置 DP；只保留上一收益层，并用位图保存回溯决策。
    # 每个窗口都要填：k > i 时 dp[i][k] = dp[i][i]，并非零值。
    stride = n + 1
    previous = array("q", [0]) * stride
    choices = bytearray((n * limit + 7) // 8)

    for k in range(1, limit + 1):
        current = array("q", [0]) * stride
        layer_offset = (k - 1) * n
        for i in range(1, n + 1):
            vi = values[i - 1]
            take = previous[pred[i - 1]] + vi
            skip = current[i - 1]
            bit_index = layer_offset + i - 1
            # 位图直接记录获胜分支，回溯无需保留全部收益层。
            # 同分裁决：仅严格更优才纳入，相等时固定排除编号 i。
            if take > skip:
                current[i] = take
                choices[bit_index >> 3] |= 1 << (bit_index & 7)
            else:
                current[i] = skip
        previous = current

    # 按位图回溯规范清单。
    chosen = []
    i = n
    k = limit
    while i > 0 and k > 0:
        bit_index = (k - 1) * n + i - 1
        took = choices[bit_index >> 3] & (1 << (bit_index & 7))
        if not took:
            i -= 1
            continue
        i -= 1
        chosen.append(i)
        k -= 1
        i = pred[i]
    chosen.reverse()

    return int(previous[n]), [ids[pos] for pos in chosen]


# ---------------------------------------------------------------------------
# 带广告主约束排期（advertiser-constrained scheduling）
# ---------------------------------------------------------------------------
#
# 在限额加权区间调度之外，再要求**实际播出的相邻两条广告不能来自同一广告主**：
# 只看实际选中的窗口序列，空档与未选中窗口都不隔断（前驱可跨越任意多条未选窗口）。
#
# 编号规则与普通模式一致：按 (end, start, 输入下标) 升序编号 1..n；半开区间
# [start, end)，end == start 不冲突（端点相接不算重叠）。
#
# 状态
# ----
# 令 dp_k[i][a][m] 表示前 i 个窗口中至多选 k 个、最后一个已选窗口的广告主为
# a、且已满足的承诺必播片段集合为 m 时的最大收益；a = 0 为“尚未选择任何窗口”
# 的哨兵（空清单收益 0），实际广告主编号 1..A；m 是 r 位覆盖掩码（r <= 3，
# 2^r <= 8），第 t 位为 1 表示第 t 个必播片段已在清单中。不可达状态以 -1 标记
# （所有报价非负，0 本身是合法收益）。
#
# 承诺必播在搜索过程中**强制纳入**：编号 i 若属于必播集合，处理它时只有“纳入”
# 分支（不存在排除继承）；纳入后覆盖掩码置上对应位。这不是“先求旧最优再过滤”，
# 而是在 DP 内部只允许覆盖承诺的路径存活。最终只接受 m == 2^r - 1 的状态；
# 该状态不可达即承诺之间确实无合法排期，返回 None 表示不可排期。
#
# 覆盖掩码维度还保证了“零收益过渡片段”不会被同分裁决误杀：若纳入一条 0 报价
# 的片段可以把末广告主状态（或必播覆盖掩码）推进到某个可达全掩码的状态，它与
# “排除”收益虽同为某个值，但落在不同的 (a, m) 格中被各自保留——只有同一个
# (a, m) 格内收益相等时才按规范掩码固定排除编号 i。
#
# 对编号 i（广告主 a_i、前驱 p(i)）：
#
#   排除 i（仅当 i 不是必播）：cur[i][a][m] = cur[i-1][a][m]
#   纳入 i：
#     cur[i][a_i][m | bit(i)] =
#         max_{b != a_i} prev[p(i)][b][m] + v_i   （含 b = 0）
#
# 其中 prev 是 k-1 数量层、cur 是 k 层的滚动收益表。
#
# 严格最大化收益后，同收益仍按“窗口编号选择掩码最小”裁决：收益相等时固定排除
# 编号 i；需要在多个前驱广告主状态间挑选时，取规范掩码较小者。规范掩码随收益
# 层一起滚动保存（本模式 n <= 2000，整层 Python 整数掩码代价可忽略）。
#
# 回溯：先在 dp_limit[n][a][全掩码] 中取收益最大、平局取掩码最小者（无全掩码
# 可达状态则不可排期），再按每格保存的单字节决策回溯（0=排除，否则记录纳入
# 时的前驱广告主状态）。
#
# 复杂度：排序/前驱 O(n log n)，动态规划 O(n * limit * A * 2^r)，回溯 O(n)；
# n <= 2000、limit <= 20、A <= 8、2^r <= 8。最大总收益 20 * 10^9 = 2e10，
# int64 足够。

# 不可达状态的收益标记；所有合法收益均 >= 0，故 -1 不会与合法值混淆。
_NEG_INF = -1


def solve_with_advertisers(limit, windows, required_ids=None):
    """求广告主约束（可带承诺必播）下的唯一最优清单。

    参数
    ----
    limit: 最多选择的窗口数（1..20，由上层校验）。
    windows: [(id, start, end, value, advertiser_id), ...]，保持输入顺序。
             advertiser_id 为非空字符串，不同广告主数 <= 8（由上层校验）。
    required_ids: 可选的承诺必播 id 列表（1..3 个、互不重复、必须全部存在于
             windows 中，由上层校验）。搜索过程强制包含全部必播片段；为 None
             或空列表时与旧行为完全一致。

    返回
    ----
    (profit, selections)：总收益，以及按窗口编号升序的
    [(id, advertiser_id), ...] 列表；承诺必播全部入选。
    若承诺之间确实不存在任何合法排期（互相重叠、名额不足或相邻规则无解），
    返回 None——明确不可排期，不给出部分方案。
    """
    n = len(windows)

    # 编号：按 (end, start, 输入下标) 升序（与普通模式完全一致）。
    order = sorted(range(n), key=lambda j: (windows[j][2], windows[j][1], j))
    ids = [None] * n
    starts = [0] * n
    ends = [0] * n
    values = [0] * n
    owner = [0] * n  # 每个编号窗口的广告主内部编号 1..A
    owner_label = {}  # 内部编号 -> 广告主标识
    for pos, src in enumerate(order):
        wid, st, en, va, advertiser = windows[src]
        ids[pos] = wid
        starts[pos] = st
        ends[pos] = en
        values[pos] = va
        code = owner_label.get(advertiser)
        if code is None:
            code = len(owner_label) + 1
            owner_label[advertiser] = code
        owner[pos] = code
    advertisers = len(owner_label)
    states = advertisers + 1  # 状态 0..A（0 为空清单哨兵）

    # 承诺必播：id -> 编号位置；按编号升序排列以确定覆盖掩码位序（与输入顺序
    # 无关，保证可重算）。重复/不存在是参数错误，接口层本应先行拦截。
    required = required_ids or []
    if len(required) > len(set(required)):
        raise RequiredWindowError("duplicate required window id")
    id_to_pos = {ids[pos]: pos for pos in range(n)}
    required_pos = []
    for wid in required:
        pos = id_to_pos.get(wid)
        if pos is None:
            raise RequiredWindowError(f"required window id not found: {wid!r}")
        required_pos.append(pos)
    required_pos.sort()
    required_count = len(required_pos)
    coverage = 1 << required_count
    full_mask = coverage - 1
    # 编号位置 -> 该必播片段对应的覆盖掩码位（非必播窗口为 0）。
    required_bit = [0] * n
    for t, pos in enumerate(required_pos):
        required_bit[pos] = 1 << t

    # p(i)：与普通模式相同，bisect_right 保证端点相接（end == start）不冲突。
    pred = array("I", [0]) * n
    for i, start in enumerate(starts):
        pred[i] = bisect_right(ends, start, 0, i)

    width = (n + 1) * states * coverage
    # prev：k-1 数量层。dp_0[i][0][0] = 0（空清单），其余不可达，
    # 且对所有 i 相同。
    prev_profit = array("q", [_NEG_INF]) * width
    prev_mask = [0] * width
    for i in range(n + 1):
        prev_profit[i * states * coverage] = 0

    # 决策字节：每个 (k, i, a, m) 一格。0 表示排除 i；
    # 否则为“纳入 i 且此前驱广告主状态获胜”时的前驱状态 + 1（取值 1..states）。
    decisions = bytearray(n * limit * states * coverage)

    for k in range(1, limit + 1):
        cur_profit = array("q", [_NEG_INF]) * width
        cur_mask = [0] * width
        layer_base = (k - 1) * n * states * coverage
        # “至多选 k 个”：空清单在任意前 i 个窗口上都可行，收益 0、状态为
        # 哨兵 0、必播覆盖掩码 0。它也是纳入首条窗口时（前驱 b = 0）的来源。
        for i in range(n + 1):
            cur_profit[i * states * coverage] = 0

        for i in range(1, n + 1):
            i0 = i - 1
            src = i * states * coverage
            up = (i - 1) * states * coverage

            ai = owner[i0]
            vi = values[i0]
            bit = required_bit[i0]
            ps = pred[i0] * states * coverage

            # 纳入 i：对每个前驱覆盖掩码 m 求一次最优前驱广告主状态
            # （b != a_i，b = 0 允许 i 作为首条）。收益相等时取规范掩码
            # 最小者：终掩码 = 前驱掩码 | 编号 i 的位，OR 的位恒定，
            # 故前驱掩码小者终掩码也小。
            best_by_coverage = [_NEG_INF] * coverage
            best_masks = [0] * coverage
            best_prev_states = [0] * coverage
            for m in range(coverage):
                best_take = _NEG_INF
                best_prev_mask = 0
                best_prev_state = 0
                for b in range(states):
                    if b == ai:
                        continue
                    cand_profit = prev_profit[ps + b * coverage + m]
                    if cand_profit == _NEG_INF:
                        continue
                    cand_profit += vi
                    cand_prev_mask = prev_mask[ps + b * coverage + m]
                    if (
                        cand_profit > best_take
                        or (
                            cand_profit == best_take
                            and cand_prev_mask < best_prev_mask
                        )
                    ):
                        best_take = cand_profit
                        best_prev_mask = cand_prev_mask
                        best_prev_state = b
                if best_take != _NEG_INF:
                    best_by_coverage[m] = best_take
                    best_masks[m] = best_prev_mask | (1 << i0)
                    best_prev_states[m] = best_prev_state

            for m in range(coverage):
                target = m | bit
                for a in range(states):
                    idx = src + a * coverage + target
                    if a == ai:
                        # 末条广告主为 a_i：纳入收益落在 (a_i, m|bit) 格。
                        take_profit = best_by_coverage[m]
                        if take_profit != _NEG_INF:
                            # 纳入掩码必含编号 i 的位、排除掩码必不含，
                            # 故收益相等时排除的掩码必然更小——仅严格更优
                            # 才纳入，与普通模式“同分固定排除当前编号”的
                            # 裁决一致。不可达标记为 -1：纳入可达
                            # （收益 >= 0）时自然严格更大。
                            # 必播窗口没有排除分支（禁止跳过承诺）。
                            if (
                                bit
                                or take_profit > cur_profit[up + a * coverage + target]
                            ):
                                cur_profit[idx] = take_profit
                                cur_mask[idx] = best_masks[m]
                                decisions[
                                    layer_base
                                    + i0 * states * coverage
                                    + a * coverage
                                    + target
                                ] = best_prev_states[m] + 1
                                continue
                        if bit:
                            # 必播窗口无法纳入（无可相容前驱）：该格不可达，
                            # 也不得从 i-1 继承——承诺不可被跳过。
                            continue
                        cur_profit[idx] = cur_profit[up + a * coverage + target]
                        cur_mask[idx] = cur_mask[up + a * coverage + target]
                    else:
                        # 末条广告主不是 a_i：i 不可能作为末条纳入，
                        # 仅排除继承。必播窗口则该格留为不可达。
                        if bit:
                            continue
                        cur_profit[idx] = cur_profit[up + a * coverage + target]
                        cur_mask[idx] = cur_mask[up + a * coverage + target]

        prev_profit = cur_profit
        prev_mask = cur_mask

    # 只在“全部承诺必播都已覆盖”的状态 dp_limit[n][a][full_mask] 中选收益
    # 最大者；平局取规范掩码最小者（空清单掩码 0 最小，故无必播且全零报价时
    # 与普通模式一样返回空清单）。全掩码无任何可达状态 -> 承诺不可排期。
    base = n * states * coverage
    best_profit = _NEG_INF
    best_state = 0
    best_mask = 0
    for a in range(states):
        profit = prev_profit[base + a * coverage + full_mask]
        if profit == _NEG_INF:
            continue
        mask = prev_mask[base + a * coverage + full_mask]
        if profit > best_profit or (profit == best_profit and mask < best_mask):
            best_profit = profit
            best_mask = mask
            best_state = a
    if best_profit == _NEG_INF:
        return None

    # 按决策字节回溯规范清单。
    chosen = []
    i = n
    k = limit
    state = best_state
    covered = full_mask
    while i > 0 and k > 0:
        i0 = i - 1
        decision = decisions[
            (k - 1) * n * states * coverage
            + i0 * states * coverage
            + state * coverage
            + covered
        ]
        if decision == 0:
            # 排除编号 i：末广告主与覆盖掩码均不变。
            i -= 1
            continue
        chosen.append(i0)
        prev_state = decision - 1
        bit = required_bit[i0]
        if bit:
            # 该位由本次纳入置上：前驱覆盖掩码必须去掉它（位一定存在）。
            covered ^= bit
        k -= 1
        i = pred[i0]
        state = prev_state
    chosen.reverse()

    code_to_label = {code: label for label, code in owner_label.items()}
    return int(best_profit), [
        (ids[pos], code_to_label[owner[pos]]) for pos in chosen
    ]
