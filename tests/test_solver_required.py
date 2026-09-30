"""承诺必播求解器单元测试。

必播 id 列表在搜索过程中强制纳入（不是先求旧最优再过滤）：
覆盖交叠承诺、同广告主必播需插入其他广告主过渡片段、零收益过渡片段的
正确裁决、名额耗尽、同收益规范掩码裁决、承诺无解返回 None，以及与
“短片段全集枚举预言机”的穷举对照（含顺序无关性与可重算性）。
"""

import random

import pytest

from app.solver import RequiredWindowError, solve_with_advertisers


def brute_force(limit, windows, required_ids=None):
    """全集枚举预言机：枚举全部子集，过滤合法者后取含全部必播的最优。

    与生产 DP 完全独立的参考实现：
      - 半开区间不冲突：相邻已选前项 end <= 后项 start；
      - 相邻已选广告主必须不同（未选窗口不隔断）；
      - 选中数 <= limit；
      - 必播 id 必须全部入选；
      - 同收益按“按编号的选择掩码数值最小”裁决，空清单掩码 0 最小；
      - 无任何合法含承诺子集时返回 None（不可排期，不给部分方案）。
    """
    n = len(windows)
    order = sorted(range(n), key=lambda j: (windows[j][2], windows[j][1], j))
    pos_of = {windows[order[i]][0]: i for i in range(n)}
    required_bits = 0
    for wid in required_ids or ():
        required_bits |= 1 << pos_of[wid]

    best_profit = 0
    best_mask = 0
    found = False
    for mask in range(1 << n):
        if mask.bit_count() > limit:
            continue
        if mask & required_bits != required_bits:
            continue
        chosen = [order[i] for i in range(n) if mask >> i & 1]
        ok = True
        for prev_idx, cur_idx in zip(chosen, chosen[1:]):
            prev_w = windows[prev_idx]
            cur_w = windows[cur_idx]
            if prev_w[2] > cur_w[1] or prev_w[4] == cur_w[4]:
                ok = False
                break
        if not ok:
            continue
        profit = sum(windows[j][3] for j in chosen)
        if not found or profit > best_profit or (
            profit == best_profit and mask < best_mask
        ):
            best_profit = profit
            best_mask = mask
            found = True
    if not found:
        return None
    selections = [
        (windows[order[i]][0], windows[order[i]][4])
        for i in range(n)
        if best_mask >> i & 1
    ]
    return best_profit, selections


# ---------------------------------------------------------------------------
# 强制纳入：不是先求旧最优再过滤
# ---------------------------------------------------------------------------

def test_required_window_forced_into_schedule():
    # limit=1 时旧最优只取报价 100 的 big；强制低价的 x 后必须包含它，
    # 联合重排（不是先求旧最优再过滤）。limit=2 下 x+y=120 才是旧最优。
    windows = [
        ("big", 0, 100, 100, "Z"),
        ("x", 0, 50, 60, "X"),
        ("y", 50, 100, 60, "Y"),
    ]
    assert solve_with_advertisers(1, windows) == (100, [("big", "Z")])
    assert solve_with_advertisers(1, windows, ["x"]) == (60, [("x", "X")])
    assert solve_with_advertisers(2, windows, ["x"]) == (
        120,
        [("x", "X"), ("y", "Y")],
    )
    # 强制 big 后，x、y 均与其重叠，无法再加入。
    assert solve_with_advertisers(2, windows, ["big"]) == (
        100,
        [("big", "Z")],
    )


def test_required_zero_value_window_must_appear():
    # 必播本身报价为 0：全零环境下旧最优是空清单，强制后必播仍必须入选。
    windows = [("a", 0, 10, 0, "X"), ("b", 10, 20, 0, "Y")]
    assert solve_with_advertisers(2, windows) == (0, [])
    assert solve_with_advertisers(2, windows, ["a"]) == (
        0,
        [("a", "X")],
    )
    assert solve_with_advertisers(2, windows, ["a", "b"]) == (
        0,
        [("a", "X"), ("b", "Y")],
    )


def test_same_owner_required_pair_needs_other_advertiser_bridge():
    # 两条 X 主必播时间相容但相邻同主；必须插入 Y 主片段才能合法。
    windows = [
        ("a", 0, 10, 100, "X"),
        ("z", 10, 20, 7, "Y"),
        ("b", 20, 30, 100, "X"),
    ]
    assert solve_with_advertisers(3, windows, ["a", "b"]) == (
        207,
        [("a", "X"), ("z", "Y"), ("b", "X")],
    )


def test_zero_value_bridge_participates_in_verdict():
    # 隔断片段报价为 0：它必须参与裁决并被选入，才能解锁两条 X 主必播。
    windows = [
        ("a", 0, 10, 100, "X"),
        ("z", 10, 20, 0, "Y"),
        ("b", 20, 30, 100, "X"),
    ]
    assert solve_with_advertisers(3, windows, ["a", "b"]) == (
        200,
        [("a", "X"), ("z", "Y"), ("b", "X")],
    )


def test_bridge_must_be_between_required_in_time():
    # 时间上真正重叠于必播 a 的片段不能充当隔断；无可插入的合法过渡 -> 无解。
    windows = [
        ("a", 0, 10, 100, "X"),
        ("bad", 5, 15, 1, "Y"),   # 与 a 真正重叠，不能夹在 a、b 之间
        ("b", 15, 25, 100, "X"),
    ]
    assert solve_with_advertisers(3, windows, ["a", "b"]) is None


def test_same_owner_required_pair_without_bridge_is_infeasible():
    windows = [
        ("a", 0, 10, 100, "X"),
        ("b", 10, 20, 100, "X"),
    ]
    assert solve_with_advertisers(2, windows, ["a", "b"]) is None


def test_overlapping_required_windows_are_infeasible():
    # 承诺片段互相重叠（即使广告主不同）也无法同播 -> 不可排期。
    windows = [("a", 0, 10, 10, "X"), ("b", 5, 15, 10, "Y")]
    assert solve_with_advertisers(2, windows, ["a", "b"]) is None


def test_three_overlapping_required_returns_none_not_partial():
    windows = [
        ("a", 0, 10, 10, "X"),
        ("b", 5, 15, 10, "Y"),
        ("c", 8, 20, 10, "Z"),
    ]
    assert solve_with_advertisers(3, windows, ["a", "b", "c"]) is None


def test_limit_exhaustion_makes_promises_infeasible():
    # 三条时间相容、广告主交替的必播，名额上限 2 -> 无法同时履行。
    windows = [
        ("a", 0, 1, 10, "X"),
        ("b", 2, 3, 10, "Y"),
        ("c", 4, 5, 10, "Z"),
    ]
    assert solve_with_advertisers(2, windows, ["a", "b", "c"]) is None
    assert solve_with_advertisers(3, windows, ["a", "b", "c"]) == (
        30,
        [("a", "X"), ("b", "Y"), ("c", "Z")],
    )


def test_bridge_consumes_limit_slot():
    # 两条 X 主必播需要 1 条隔断，共 3 个名额；limit=2 时无解。
    windows = [
        ("a", 0, 10, 100, "X"),
        ("z", 10, 20, 0, "Y"),
        ("b", 20, 30, 100, "X"),
    ]
    assert solve_with_advertisers(2, windows, ["a", "b"]) is None


def test_three_required_with_inserted_bridges():
    # 必播 X、X、Y：前两条 X 之间需要一条 Z 过渡，共 4 条。
    windows = [
        ("a", 0, 10, 10, "X"),
        ("g", 10, 11, 3, "Z"),
        ("b", 11, 20, 10, "X"),
        ("c", 20, 30, 10, "Y"),
    ]
    assert solve_with_advertisers(4, windows, ["a", "b", "c"]) == (
        33,
        [("a", "X"), ("g", "Z"), ("b", "X"), ("c", "Y")],
    )
    assert solve_with_advertisers(3, windows, ["a", "b", "c"]) is None


# ---------------------------------------------------------------------------
# 同收益规范掩码裁决在带必播时继续生效
# ---------------------------------------------------------------------------

def test_tie_break_prefers_lower_numbered_bridge():
    # 两条 X 必播之间有两个互斥的同分零价 Y 隔断 z1、z2（编号 z1 小），
    # 规范掩码取含小编号 z1 的方案。
    windows = [
        ("a", 0, 10, 100, "X"),
        ("z1", 10, 20, 0, "Y"),
        ("z2", 10, 20, 0, "Y"),
        ("b", 20, 30, 100, "X"),
    ]
    assert solve_with_advertisers(3, windows, ["a", "b"]) == (
        200,
        [("a", "X"), ("z1", "Y"), ("b", "X")],
    )


def test_tie_break_higher_value_bridge_wins():
    windows = [
        ("a", 0, 10, 100, "X"),
        ("z1", 10, 20, 1, "Y"),
        ("z2", 10, 20, 50, "Z"),
        ("b", 20, 30, 100, "X"),
    ]
    assert solve_with_advertisers(3, windows, ["a", "b"]) == (
        250,
        [("a", "X"), ("z2", "Z"), ("b", "X")],
    )


def test_required_order_does_not_change_result():
    windows = [
        ("a", 0, 10, 100, "X"),
        ("z", 10, 20, 0, "Y"),
        ("b", 20, 30, 100, "X"),
        ("c", 30, 40, 5, "Y"),
    ]
    first = solve_with_advertisers(4, windows, ["b", "a", "c"])
    second = solve_with_advertisers(4, windows, ["c", "a", "b"])
    third = solve_with_advertisers(4, windows, ["a", "b", "c"])
    assert first == second == third
    assert [wid for wid, _ in first[1]] == ["a", "z", "b", "c"]


# ---------------------------------------------------------------------------
# 参数错误：重复 / 不存在的必播 id
# ---------------------------------------------------------------------------

def test_duplicate_required_id_raises():
    windows = [("a", 0, 10, 1, "X"), ("b", 10, 20, 1, "Y")]
    with pytest.raises(RequiredWindowError):
        solve_with_advertisers(2, windows, ["a", "a"])


def test_unknown_required_id_raises():
    windows = [("a", 0, 10, 1, "X")]
    with pytest.raises(RequiredWindowError):
        solve_with_advertisers(2, windows, ["ghost"])


def test_empty_required_list_equals_legacy():
    windows = [("a", 0, 10, 5, "X"), ("b", 10, 20, 5, "Y")]
    assert solve_with_advertisers(2, windows, []) == solve_with_advertisers(
        2, windows
    )


# ---------------------------------------------------------------------------
# 全集枚举预言机：穷举对照
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(500))
def test_brute_force_random_with_required(seed):
    rng = random.Random(seed)
    n = rng.randint(0, 7)
    limit = rng.randint(1, 5)
    pool = ["X", "Y", "Z"][: rng.randint(1, 3)]
    windows = []
    for j in range(n):
        s = rng.randint(0, 10)
        e = rng.randint(s + 1, 12)
        windows.append(
            (f"id{j}", s, e, rng.randint(0, 4), rng.choice(pool))
        )
    r = rng.randint(0, min(3, n))
    required = [f"id{j}" for j in rng.sample(range(n), r)]
    rng.shuffle(required)
    assert solve_with_advertisers(
        limit, windows, required or None
    ) == brute_force(limit, windows, required)


@pytest.mark.parametrize("seed", range(500))
def test_brute_force_tie_heavy_with_required(seed):
    """小报价域 + 密集时间栅格：大量同收益平局与零价隔断组合。"""
    rng = random.Random(20_000 + seed)
    n = rng.randint(1, 8)
    limit = rng.randint(1, 6)
    pool = [f"o{i}" for i in range(rng.randint(1, 4))]
    windows = []
    for j in range(n):
        s = rng.randint(0, 6)
        e = rng.randint(s + 1, 7)
        windows.append(
            (f"id{j}", s, e, rng.randint(0, 2), rng.choice(pool))
        )
    r = rng.randint(1, min(3, n))
    required = [f"id{j}" for j in rng.sample(range(n), r)]
    assert solve_with_advertisers(limit, windows, required) == brute_force(
        limit, windows, required
    )


@pytest.mark.parametrize("seed", range(200))
def test_brute_force_exactly_three_required(seed):
    """必给 3 个必播（n >= 3）：覆盖跨间隙过渡与无解裁决。"""
    rng = random.Random(30_000 + seed)
    n = rng.randint(3, 9)
    limit = rng.randint(1, 6)
    pool = ["X", "Y", "Z"]
    windows = []
    for j in range(n):
        s = rng.randint(0, 8)
        e = rng.randint(s + 1, 9)
        windows.append(
            (f"id{j}", s, e, rng.randint(0, 3), rng.choice(pool))
        )
    required = [f"id{j}" for j in rng.sample(range(n), 3)]
    assert solve_with_advertisers(limit, windows, required) == brute_force(
        limit, windows, required
    )


def test_determinism_with_required():
    rng = random.Random(99)
    windows = []
    for j in range(400):
        s = rng.randint(0, 900)
        windows.append(
            (
                f"i{j}",
                s,
                s + rng.randint(1, 100),
                rng.randint(0, 100),
                rng.choice(["X", "Y", "Z", "W"]),
            )
        )
    required = ["i3", "i150", "i399"]
    first = solve_with_advertisers(20, windows, required)
    for _ in range(3):
        assert solve_with_advertisers(20, windows, list(reversed(required))) == first
