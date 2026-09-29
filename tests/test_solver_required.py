"""必播承诺（required_ids）求解器单元测试。

以“短片段全集枚举”作为**独立预言机**：对小输入枚举全部 2^n 个子集，
先过滤出包含全部必播片段的合法子集（半开区间不冲突 + 相邻已选广告主
不同 + 数量 <= limit），再取收益最大、平局取按编号选择掩码最小者；
无任何合法子集即不可排期。求解器结果必须与预言机逐项一致。

覆盖必播特有情形：
  - 必播片段互相时间重叠（不可排期）；
  - 必播同主、必须插入其他广告主片段（含零收益过渡片段）才合法；
  - 跨间隙过渡：间隙中多个互斥隔断里挑收益最优 / 平局挑编号最小；
  - 名额耗尽（必播本身或必播+隔断超过 limit，不可排期）；
  - 同收益裁决与无必播模式完全一致（含必播改变空清单最优的场景）；
  - 承诺无解时抛 UnschedulableError，不返回部分方案；
  - 不传必播列表时与旧行为完全一致（另由旧测试套件保证）。
"""

import random

import pytest

from app.solver import MAX_REQUIRED, UnschedulableError, solve_with_advertisers


def oracle(limit, windows, required):
    """子集枚举预言机。

    windows: [(id, start, end, value, advertiser), ...]（输入顺序）
    required: 必播 id 的集合；空集合表示无承诺（与求解器旧模式等价）。
    返回 (profit, selections)；无可行解时返回 None。
    平局规则：按编号（end, start, 输入下标）的选择掩码数值最小。
    """
    n = len(windows)
    order = sorted(range(n), key=lambda j: (windows[j][2], windows[j][1], j))
    src_to_pos = {windows[order[i]][0]: i for i in range(n)}
    req_bits = 0
    for rid in required:
        req_bits |= 1 << src_to_pos[rid]

    best_profit = None
    best_mask = None
    for mask in range(1 << n):
        if mask.bit_count() > limit:
            continue
        if (mask & req_bits) != req_bits:
            continue
        chosen = [order[i] for i in range(n) if mask >> i & 1]
        ok = True
        for prev_idx, cur_idx in zip(chosen, chosen[1:]):
            prev_w = windows[prev_idx]
            cur_w = windows[cur_idx]
            if prev_w[2] > cur_w[1]:  # 真正时间重叠（相接允许）
                ok = False
                break
            if prev_w[4] == cur_w[4]:  # 相邻已选同主
                ok = False
                break
        if not ok:
            continue
        profit = sum(windows[j][3] for j in chosen)
        if best_profit is None or profit > best_profit or (
            profit == best_profit and mask < best_mask
        ):
            best_profit = profit
            best_mask = mask

    if best_mask is None:
        return None
    selections = [
        (windows[order[i]][0], windows[order[i]][4])
        for i in range(n)
        if best_mask >> i & 1
    ]
    return best_profit, selections


def solve_or_none(limit, windows, required):
    try:
        return solve_with_advertisers(limit, windows, list(required))
    except UnschedulableError:
        return None


def make_windows(rng, n):
    pool = [f"o{i}" for i in range(rng.randint(1, 4))]
    windows = []
    for j in range(n):
        s = rng.randint(0, 8)
        e = rng.randint(s + 1, 10)
        windows.append((f"id{j}", s, e, rng.randint(0, 3), rng.choice(pool)))
    return windows


# ---------------------------------------------------------------------------
# 固定场景
# ---------------------------------------------------------------------------


def test_required_pair_overlapping_is_unschedulable():
    # 两条必播真正重叠（即便广告主不同也无法同时播出）。
    windows = [
        ("a", 0, 10, 10, "X"),
        ("b", 5, 15, 10, "Y"),
        ("c", 15, 20, 10, "Z"),
    ]
    with pytest.raises(UnschedulableError) as exc:
        solve_with_advertisers(3, windows, ["a", "b"])
    assert exc.value.required_ids == ["a", "b"]
    assert oracle(3, windows, {"a", "b"}) is None
    # 只承诺其中一条则有解，且与预言机一致。
    assert solve_or_none(3, windows, {"a"}) == oracle(3, windows, {"a"})


def test_required_same_owner_needs_other_advertiser_bridge():
    # 两条 X 必播端点相接；中间没有任何其他广告主窗口 -> 无解。
    windows = [
        ("r1", 0, 10, 10, "X"),
        ("r2", 10, 20, 10, "X"),
    ]
    with pytest.raises(UnschedulableError):
        solve_with_advertisers(2, windows, ["r1", "r2"])
    assert oracle(2, windows, {"r1", "r2"}) is None

    # 加入一条其他广告主的过渡片段 -> 必须插入它，哪怕它在间隙外不可替换。
    windows = [
        ("r1", 0, 10, 10, "X"),
        ("g", 10, 12, 0, "Y"),
        ("r2", 12, 20, 10, "X"),
    ]
    assert solve_with_advertisers(3, windows, ["r1", "r2"]) == (
        20,
        [("r1", "X"), ("g", "Y"), ("r2", "X")],
    )
    assert solve_or_none(3, windows, {"r1", "r2"}) == oracle(
        3, windows, {"r1", "r2"}
    )


def test_zero_value_transitional_window_takes_part_in_adjudication():
    # 零收益隔断是唯一能解锁两条同主必播的片段；无必播时最优根本不选它
    # （big=25 独大），承诺后它必须被正确裁决进来。
    windows = [
        ("x1", 0, 10, 10, "acme"),
        ("bridge", 10, 12, 0, "globex"),
        ("x2", 12, 22, 10, "acme"),
        ("big", 0, 22, 25, "initech"),
    ]
    assert solve_with_advertisers(3, windows) == (25, [("big", "initech")])
    assert solve_with_advertisers(3, windows, ["x1", "x2"]) == (
        20,
        [("x1", "acme"), ("bridge", "globex"), ("x2", "acme")],
    )
    assert solve_or_none(3, windows, {"x1", "x2"}) == oracle(
        3, windows, {"x1", "x2"}
    )


def test_cross_gap_transition_picks_best_bridge():
    # 两条必播之间有间隙，间隙内两条互相重叠的 Y 隔断：选报价高者；
    # 同价时按编号掩码取小者。
    windows = [
        ("r1", 0, 5, 10, "X"),
        ("g1", 5, 9, 6, "Y"),
        ("g2", 6, 10, 7, "Y"),
        ("r2", 10, 15, 10, "X"),
    ]
    assert solve_with_advertisers(4, windows, ["r1", "r2"]) == (
        27,
        [("r1", "X"), ("g2", "Y"), ("r2", "X")],
    )
    windows_tie = [
        ("r1", 0, 5, 10, "X"),
        ("g1", 5, 9, 0, "Y"),
        ("g2", 6, 10, 0, "Y"),
        ("r2", 10, 15, 10, "X"),
    ]
    assert solve_with_advertisers(4, windows_tie, ["r1", "r2"]) == (
        20,
        [("r1", "X"), ("g1", "Y"), ("r2", "X")],
    )
    assert solve_or_none(4, windows, {"r1", "r2"}) == oracle(
        4, windows, {"r1", "r2"}
    )
    assert solve_or_none(4, windows_tie, {"r1", "r2"}) == oracle(
        4, windows_tie, {"r1", "r2"}
    )


def test_limit_exhaustion_is_unschedulable():
    # 三条互不冲突、广告主交替的必播，limit=2 -> 名额耗尽，无解。
    windows = [
        ("a", 0, 4, 5, "X"),
        ("b", 4, 8, 5, "Y"),
        ("c", 8, 12, 5, "Z"),
    ]
    with pytest.raises(UnschedulableError):
        solve_with_advertisers(2, windows, ["a", "b", "c"])
    # 必播自身放得下、但必需的隔断占用额外名额 -> 同样无解：
    # 两条同主必播之间唯一的合法过渡需要 1 个额外名额，limit=2 装不下 3 条。
    windows_bridge = [
        ("r1", 0, 4, 10, "A"),
        ("g", 4, 6, 0, "B"),
        ("r2", 6, 10, 10, "A"),
        ("r3", 10, 14, 10, "C"),
    ]
    with pytest.raises(UnschedulableError):
        solve_with_advertisers(2, windows_bridge, ["r1", "r2"])
    assert solve_or_none(2, windows_bridge, {"r1", "r2"}) is None
    assert solve_or_none(4, windows_bridge, {"r1", "r2"}) == oracle(
        4, windows_bridge, {"r1", "r2"}
    )


def test_three_required_same_owner_alternating_bridges():
    windows = [
        ("r1", 0, 4, 10, "A"),
        ("g1", 4, 6, 0, "B"),
        ("r2", 6, 10, 10, "A"),
        ("g2", 10, 12, 0, "B"),
        ("r3", 12, 16, 10, "A"),
    ]
    expected = (
        30,
        [("r1", "A"), ("g1", "B"), ("r2", "A"), ("g2", "B"), ("r3", "A")],
    )
    assert solve_with_advertisers(5, windows, ["r1", "r2", "r3"]) == expected
    assert solve_or_none(5, windows, {"r1", "r2", "r3"}) == oracle(
        5, windows, {"r1", "r2", "r3"}
    )


def test_required_changes_all_zero_canonical_empty_result():
    # 全零报价下无必播规范解为空清单；必播一条零收益窗口后必须包含它，
    # 且仍按掩码最小裁决其余片段。
    windows = [
        ("z", 0, 10, 0, "X"),
        ("y", 10, 20, 0, "Y"),
        ("q", 20, 30, 0, "Z"),
    ]
    assert solve_with_advertisers(3, windows) == (0, [])
    assert solve_with_advertisers(3, windows, ["z"]) == (0, [("z", "X")])
    assert solve_or_none(3, windows, {"z"}) == oracle(3, windows, {"z"})
    assert solve_or_none(3, windows, {"z", "q"}) == oracle(
        3, windows, {"z", "q"}
    )


def test_required_solution_still_profit_maximal_among_containing_sets():
    windows = [
        ("r", 0, 5, 1, "X"),
        ("a", 5, 9, 9, "Y"),
        ("b", 5, 9, 8, "Z"),  # 与 a 互斥；含 r 时选 a 更赚
        ("c", 9, 12, 4, "X"),
    ]
    assert solve_or_none(4, windows, {"r"}) == oracle(4, windows, {"r"})
    assert solve_with_advertisers(4, windows, ["r"]) == (
        14,
        [("r", "X"), ("a", "Y"), ("c", "X")],
    )


def test_required_ids_order_in_request_does_not_matter():
    windows = [
        ("r1", 0, 5, 10, "X"),
        ("g", 5, 7, 1, "Y"),
        ("r2", 7, 12, 10, "X"),
    ]
    expected = (21, [("r1", "X"), ("g", "Y"), ("r2", "X")])
    assert solve_with_advertisers(3, windows, ["r1", "r2"]) == expected
    assert solve_with_advertisers(3, windows, ["r2", "r1"]) == expected


def test_unschedulable_returns_no_partial_solution():
    # 异常对象本身不携带 selections；调用方只能重试或放弃。
    windows = [("a", 0, 10, 10, "X"), ("b", 5, 15, 10, "X")]
    with pytest.raises(UnschedulableError) as exc:
        solve_with_advertisers(2, windows, ["a", "b"])
    assert not hasattr(exc.value, "selections")
    # 同批窗口、去掉冲突承诺后仍可正常求解（求解器无残留状态）。
    assert solve_with_advertisers(2, windows, ["a"]) == (10, [("a", "X")])


# ---------------------------------------------------------------------------
# 独立预言机穷举
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(600))
def test_brute_force_random_with_required(seed):
    rng = random.Random(20_000 + seed)
    n = rng.randint(1, 7)
    limit = rng.randint(1, 5)
    windows = make_windows(rng, n)
    all_ids = [w[0] for w in windows]

    # 随机挑 1..3 个必播（可能重叠、可能同主——这正是要覆盖的情形）。
    k_req = rng.randint(1, min(MAX_REQUIRED, n))
    required = set(rng.sample(all_ids, k_req))

    expected = oracle(limit, windows, required)
    actual = solve_or_none(limit, windows, required)
    assert actual == expected, (limit, windows, sorted(required))


@pytest.mark.parametrize("seed", range(300))
def test_brute_force_dense_grid_tie_heavy(seed):
    """密集栅格 + 小报价域：大量平局与间隙过渡，核对必播下的掩码裁决。"""
    rng = random.Random(30_000 + seed)
    n = rng.randint(1, 8)
    limit = rng.randint(1, 5)
    pool = [f"o{i}" for i in range(rng.randint(1, 3))]
    windows = []
    for j in range(n):
        s = rng.randint(0, 6)
        e = rng.randint(s + 1, 8)
        windows.append((f"id{j}", s, e, rng.randint(0, 2), rng.choice(pool)))
    all_ids = [w[0] for w in windows]
    k_req = rng.randint(1, min(MAX_REQUIRED, n))
    required = set(rng.sample(all_ids, k_req))

    expected = oracle(limit, windows, required)
    actual = solve_or_none(limit, windows, required)
    assert actual == expected, (limit, windows, sorted(required))


@pytest.mark.parametrize("seed", range(200))
def test_brute_force_single_required_matches(seed):
    """对每条窗口分别单独设为必播，逐一与预言机核对（含不可排期）。"""
    rng = random.Random(40_000 + seed)
    n = rng.randint(1, 7)
    limit = rng.randint(1, 4)
    windows = make_windows(rng, n)
    for w in windows:
        required = {w[0]}
        expected = oracle(limit, windows, required)
        actual = solve_or_none(limit, windows, required)
        assert actual == expected, (limit, windows, w[0])


def test_determinism_with_required():
    # 构造一个保证可行的批次：三条互不冲突、广告主交替的必播，周围布满诱饵。
    rng = random.Random(55)
    windows = []
    for j in range(3):
        base = j * 100
        windows.append(
            (f"req{j}", base + 10, base + 50, 100, "AB"[j % 2])
        )
        for d in range(80):
            s = rng.randint(base, base + 60)
            windows.append(
                (
                    f"d{j}_{d}",
                    s,
                    s + rng.randint(1, 40),
                    rng.randint(0, 90),
                    rng.choice(["A", "B", "C"]),
                )
            )
    required = ["req0", "req1", "req2"]
    first = solve_with_advertisers(20, windows, required)
    assert all(sel[0] in required for sel in first[1] if sel[0] in required)
    assert [s[0] for s in first[1] if s[0] in required] == required
    for _ in range(3):
        assert solve_with_advertisers(20, windows, list(reversed(required))) == first
