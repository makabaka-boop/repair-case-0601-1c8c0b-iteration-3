"""带广告主约束求解器单元测试。

覆盖：端点相接不算重叠、相邻已选广告主不得相同（空档/未选窗口不隔断）、
零报价空清单、前驱跨越多条未选窗口、数量上限截断、同分选择掩码裁决，
以及小输入子集对枚举参考实现的穷举核对（最优值与平局唯一清单）。
"""

import random

import pytest

from app.solver import solve_with_advertisers


def brute_force(limit, windows):
    """枚举子集的参考实现（仅用于小 n 交叉校验）。

    windows: [(id, start, end, value, advertiser), ...]
    返回 (最大收益, 规范 selections)：
      - 半开区间不冲突：相邻已选窗口前项 end <= 后项 start；
      - 相邻已选窗口广告主必须不同（未选窗口不隔断）；
      - 选中数 <= limit；
      - 同收益按“按编号的选择掩码数值最小”裁决，空清单掩码 0 最小。
    """
    n = len(windows)
    order = sorted(range(n), key=lambda j: (windows[j][2], windows[j][1], j))
    best_profit = 0
    best_mask = 0
    found_any = False
    for mask in range(1 << n):
        if mask.bit_count() > limit:
            continue
        chosen = [order[i] for i in range(n) if mask >> i & 1]
        ok = True
        for prev_idx, cur_idx in zip(chosen, chosen[1:]):
            prev_w = windows[prev_idx]
            cur_w = windows[cur_idx]
            # chosen 已按编号（end 升序）排列；半开区间相接不冲突
            if prev_w[2] > cur_w[1]:
                ok = False
                break
            # 相邻两条实际播出的广告不得同主，未选窗口不隔断
            if prev_w[4] == cur_w[4]:
                ok = False
                break
        if not ok:
            continue
        profit = sum(windows[j][3] for j in chosen)
        if not found_any or profit > best_profit or (
            profit == best_profit and mask < best_mask
        ):
            best_profit = profit
            best_mask = mask
            found_any = True
    selections = [
        (windows[order[i]][0], windows[order[i]][4])
        for i in range(n)
        if best_mask >> i & 1
    ]
    return best_profit, selections


def test_empty_batch():
    assert solve_with_advertisers(1, []) == (0, [])
    assert solve_with_advertisers(20, []) == (0, [])


def test_touching_endpoints_different_owners_both_selected():
    # [0,10) X 与 [10,20) Y：端点相接不算重叠，广告主也不同 -> 同时选
    windows = [("a", 0, 10, 10, "X"), ("b", 10, 20, 20, "Y")]
    assert solve_with_advertisers(2, windows) == (30, [("a", "X"), ("b", "Y")])


def test_touching_endpoints_same_owner_rejected_together():
    # 端点相接不冲突，但相邻同主不允许 -> 只能选报价高者
    windows = [("a", 0, 10, 10, "X"), ("b", 10, 20, 20, "X")]
    assert solve_with_advertisers(2, windows) == (20, [("b", "X")])
    assert solve_with_advertisers(1, windows) == (20, [("b", "X")])


def test_true_overlap_still_conflicts_even_with_different_owners():
    # [0,10) 与 [9,10) 真正重叠：不同广告主也不能同选
    assert solve_with_advertisers(
        2, [("a", 0, 10, 10, "X"), ("b", 9, 10, 100, "Y")]
    ) == (100, [("b", "Y")])
    assert solve_with_advertisers(
        2, [("a", 0, 10, 100, "X"), ("b", 9, 10, 10, "Y")]
    ) == (100, [("a", "X")])


def test_three_touching_same_owner_picks_best_single():
    windows = [
        ("w0", 0, 10, 5, "X"),
        ("w1", 10, 20, 9, "X"),
        ("w2", 20, 30, 7, "X"),
    ]
    assert solve_with_advertisers(3, windows) == (9, [("w1", "X")])


def test_predecessor_spans_unselected_conflicting_window():
    # w1 与 w0 真正重叠：不可能成为两者间的“已选隔断”。
    # w0 与 w2 时间相容但同主，w1 未选时不隔断 -> w0+w2 禁止；
    # 最优为 w1+w2 = 11（Y、X）。
    windows = [
        ("w0", 0, 10, 10, "X"),
        ("w1", 9, 11, 1, "Y"),
        ("w2", 11, 20, 10, "X"),
    ]
    assert solve_with_advertisers(2, windows) == (
        11,
        [("w1", "Y"), ("w2", "X")],
    )


def test_predecessor_spans_multiple_unselected_windows():
    # 四条相接窗口编号 w0..w3：X,Y,Y,X。limit=2 时
    # {w0,w3} 时间相容、跨越多条未选窗口，但相邻已选同主 -> 禁止。
    windows = [
        ("w0", 0, 5, 10, "X"),
        ("w1", 5, 6, 5, "Y"),
        ("w2", 6, 7, 5, "Y"),
        ("w3", 7, 10, 10, "X"),
    ]
    assert solve_with_advertisers(2, windows) == (
        15,
        [("w0", "X"), ("w1", "Y")],
    )


def test_tie_break_choice_mask_with_advertiser_constraint():
    # 同 25 收益有 {w0,w1,w3}(X,Y,X) 与 {w0,w2,w3}(X,Y,X) 两种，
    # 按编号选择掩码取小者 -> 保留编号较小的 w1（掩码 1011 < 1101）。
    windows = [
        ("w0", 0, 5, 10, "X"),
        ("w1", 5, 6, 5, "Y"),
        ("w2", 6, 7, 5, "Y"),
        ("w3", 7, 10, 10, "X"),
    ]
    assert solve_with_advertisers(4, windows) == (
        25,
        [("w0", "X"), ("w1", "Y"), ("w3", "X")],
    )


def test_tie_break_across_predecessor_owner_states():
    # 回归：纳入某窗口时，多个前驱广告主状态给出相同收益、不同规范掩码，
    # 必须取前驱掩码较小者（终掩码恒再 OR 当前编号位，序关系不变）。
    # 编号（按 end,start）：id1, id2, id3, id4, id5, id0, id6。
    # 收益 10 的两个最优清单：
    #   {id1,id3,id6} 掩码 1000101（小，规范解）
    #   {id1,id5,id6} 掩码 1010001
    windows = [
        ("id0", 6, 9, 4, "o1"),
        ("id1", 2, 3, 3, "o4"),
        ("id2", 0, 8, 0, "o0"),
        ("id3", 6, 8, 4, "o2"),
        ("id4", 6, 8, 3, "o2"),
        ("id5", 7, 8, 4, "o0"),
        ("id6", 8, 9, 3, "o1"),
    ]
    assert solve_with_advertisers(4, windows) == (
        10,
        [("id1", "o4"), ("id3", "o2"), ("id6", "o1")],
    )


def test_zero_values_yield_empty_canonical_list():
    # 全零报价：空清单与任何非空清单收益相同，空清单掩码 0 最小 -> 空
    windows = [
        ("z", 0, 10, 0, "X"),
        ("y", 10, 20, 0, "Y"),
        ("q", 20, 30, 0, "Z"),
    ]
    assert solve_with_advertisers(3, windows) == (0, [])


def test_zero_value_bridge_not_selected_keeps_owner_adjacency():
    # w1（Y，报价 0）是唯一可能的同主隔断；全零隔断不会被选入最优，
    # 故 w0 与 w2（同为 X）即便时间相容也不能相邻入选。
    windows = [
        ("w0", 0, 10, 10, "X"),
        ("w1", 10, 20, 0, "Y"),
        ("w2", 20, 30, 10, "X"),
    ]
    # 选 w0+w1+w2 收益 20 且广告主交替合法 —— 零报价但能解锁两条 X
    assert solve_with_advertisers(3, windows) == (
        20,
        [("w0", "X"), ("w1", "Y"), ("w2", "X")],
    )
    # limit=1 时无法借助零价隔断，只能取一条 X（收益 10）
    assert solve_with_advertisers(1, windows) == (10, [("w0", "X")])


def test_limit_truncation_keeps_low_numbers():
    # 四条相接、广告主交替、报价相同：limit=2 保留编号最小的两条
    windows = [
        (f"w{c}", c * 10, c * 10 + 10, 10, "XY"[c % 2]) for c in range(4)
    ]
    assert solve_with_advertisers(2, windows) == (
        20,
        [("w0", "X"), ("w1", "Y")],
    )
    assert solve_with_advertisers(4, windows) == (
        40,
        [
            ("w0", "X"),
            ("w1", "Y"),
            ("w2", "X"),
            ("w3", "Y"),
        ],
    )


def test_non_postprocessing_optimization_example():
    # 关键反例：不能先算无约束最优、再事后删连续同主条目。
    # 编号 1..4 相接：A(1), B(100), B(100), A(1)。
    # 无约束最优 202 = {B,B,A}（两条 B 相邻同主）；事后删同主只能得到
    # {B,A} 之类的残缺方案（101），而联合求解的合法最优是
    # {A(编号1), B(编号2), A(编号4)} = 102（广告主交替）。
    windows = [
        ("a1", 0, 10, 1, "A"),
        ("b", 10, 20, 100, "B"),
        ("b2", 20, 30, 100, "B"),
        ("a2", 30, 40, 1, "A"),
    ]
    # 同 102 收益还有 {a1,b2,a2}；掩码裁决取含小编号 b 的前者。
    assert solve_with_advertisers(4, windows) == (
        102,
        [("a1", "A"), ("b", "B"), ("a2", "A")],
    )


def test_output_sorted_by_window_number():
    # 乱序输入，输出仍按编号（end,start,下标）升序，并带广告主
    windows = [
        ("late", 50, 60, 9, "C"),
        ("mid", 20, 30, 9, "B"),
        ("early", 0, 10, 9, "A"),
    ]
    assert solve_with_advertisers(3, windows) == (
        27,
        [("early", "A"), ("mid", "B"), ("late", "C")],
    )


@pytest.mark.parametrize("seed", range(400))
def test_brute_force_random_small(seed):
    rng = random.Random(seed)
    n = rng.randint(0, 6)
    limit = rng.randint(1, 4)
    owner_pool = ["X", "Y", "Z"]
    windows = []
    for j in range(n):
        s = rng.randint(0, 12)
        e = rng.randint(s + 1, 15)
        windows.append(
            (f"id{j}", s, e, rng.randint(0, 5), rng.choice(owner_pool))
        )
    assert solve_with_advertisers(limit, windows) == brute_force(
        limit, windows
    )


@pytest.mark.parametrize("seed", range(400))
def test_brute_force_tie_heavy(seed):
    """小报价域 + 密集时间栅格：大量同收益平局，专门核对掩码裁决与
    “前驱跨越多条未选窗口”状态的组合。"""
    rng = random.Random(10_000 + seed)
    n = rng.randint(0, 8)
    limit = rng.randint(1, 5)
    pool = [f"o{i}" for i in range(rng.randint(1, 4))]
    windows = []
    for j in range(n):
        s = rng.randint(0, 7)
        e = rng.randint(s + 1, 8)
        windows.append(
            (f"id{j}", s, e, rng.randint(0, 2), rng.choice(pool))
        )
    assert solve_with_advertisers(limit, windows) == brute_force(
        limit, windows
    )


def test_determinism_across_repeated_runs():
    rng = random.Random(11)
    windows = []
    for j in range(500):
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
    first = solve_with_advertisers(20, windows)
    for _ in range(3):
        assert solve_with_advertisers(20, windows) == first
