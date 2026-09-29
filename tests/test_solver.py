"""求解器单元测试：端点相接、同分裁决、暴力交叉校验、确定性。"""

import itertools
import random

import pytest

from app.solver import solve


def brute_force(limit, windows):
    """枚举子集的参考实现（仅用于小 n 交叉校验）。

    返回 (最大收益, 唯一规范 id 列表)：规范裁决为
    “按编号的选择掩码中数值最小者”，与 DP 的同分固定排除等价。
    """
    n = len(windows)
    order = sorted(range(n), key=lambda j: (windows[j][2], windows[j][1], j))
    best = (0, (1 << n) - 1)  # 掩码最小者为规范解；初始取不可能超过的全选掩码
    found_any = False
    for mask in range(1 << n):
        if mask.bit_count() > limit:
            continue
        chosen = [order[i] for i in range(n) if mask >> i & 1]
        chosen_sorted = sorted(chosen, key=lambda j: (windows[j][2], windows[j][1]))
        ok = True
        for a, b in zip(chosen_sorted, chosen_sorted[1:]):
            if windows[a][2] > windows[b][1]:
                ok = False
                break
        if not ok:
            continue
        profit = sum(windows[j][3] for j in chosen)
        if not found_any or profit > best[0] or (profit == best[0] and mask < best[1]):
            best = (profit, mask)
            found_any = True
    mask = best[1]
    ids = [windows[order[i]][0] for i in range(n) if mask >> i & 1]
    return best[0], ids


def test_empty_batch():
    assert solve(1, []) == (0, [])
    assert solve(50, []) == (0, [])


def test_touching_endpoints_do_not_conflict():
    # [0,10) 与 [10,20) 端点相接，limit=2 可同时选择
    assert solve(2, [("a", 0, 10, 10), ("b", 10, 20, 20)]) == (30, ["a", "b"])
    # 半开：[0,10) 与 [9,10) 重叠；与 [10,11) 不重叠
    assert solve(2, [("a", 0, 10, 10), ("b", 9, 10, 100)])[1] == ["b"]
    assert solve(2, [("a", 0, 10, 100), ("b", 9, 10, 10)])[1] == ["a"]


def test_overlapping_greedy_misfire_case():
    # 最高报价贪心会误排：大单 100 < 两个小单 60+60
    windows = [("big", 0, 100, 100), ("x", 0, 50, 60), ("y", 50, 100, 60)]
    assert solve(2, windows) == (120, ["x", "y"])
    # limit=1 时取大单
    assert solve(1, windows) == (100, ["big"])


def test_tie_excludes_higher_number_end_order():
    # 互斥同分：按编号保留编号最小（同分固定排除当前编号）
    assert solve(1, [("a", 0, 5, 10), ("b", 5, 10, 10)]) == (10, ["a"])
    # 交换输入顺序不影响编号：编号按 (end, start, 下标)
    assert solve(1, [("b", 5, 10, 10), ("a", 0, 5, 10)]) == (10, ["a"])


def test_tie_same_end_start_order():
    # 同 end：start 较小者编号小，同分保留它
    assert solve(1, [("a", 0, 10, 5), ("b", 5, 10, 5)]) == (5, ["a"])
    assert solve(1, [("b", 5, 10, 5), ("a", 0, 10, 5)]) == (5, ["a"])


def test_tie_same_end_and_start_uses_input_index():
    # end、start 都相同：编号按输入下标，同分保留先出现者
    assert solve(1, [("first", 0, 10, 7), ("second", 0, 10, 7)]) == (7, ["first"])
    # 反转输入顺序后编号也反转："second" 成为下标 0（编号小），同分保留它
    assert solve(1, [("second", 0, 10, 7), ("first", 0, 10, 7)]) == (
        7,
        ["second"],
    )


def test_limit_truncation_tie_keeps_low_numbers():
    windows = [(f"w{c}", c * 10, c * 10 + 10, 10) for c in range(5)]
    assert solve(2, windows) == (20, ["w0", "w1"])
    assert solve(5, windows) == (50, [f"w{c}" for c in range(5)])


def test_zero_values_are_valid():
    windows = [("z", 0, 10, 0), ("y", 10, 20, 0)]
    profit, ids = solve(2, windows)
    assert profit == 0
    # 全零：所有纳入/排除收益相等，固定排除 -> 空清单（唯一）
    assert ids == []


def test_result_ids_sorted_by_number():
    # 乱序输入，输出按编号（end,start,下标）升序
    windows = [
        ("late", 50, 60, 9),
        ("mid", 20, 30, 9),
        ("early", 0, 10, 9),
    ]
    profit, ids = solve(3, windows)
    assert profit == 27
    assert ids == ["early", "mid", "late"]


@pytest.mark.parametrize("seed", range(200))
def test_brute_force_random_small(seed):
    rng = random.Random(seed)
    n = rng.randint(0, 6)
    limit = rng.randint(1, 4)
    windows = []
    for j in range(n):
        s = rng.randint(0, 12)
        e = rng.randint(s + 1, 15)
        windows.append((f"id{j}", s, e, rng.randint(0, 5)))
    assert solve(limit, windows) == brute_force(limit, windows)


def test_determinism_across_repeated_runs():
    rng = random.Random(7)
    windows = [
        (f"i{j}", rng.randint(0, 900), 0, rng.randint(0, 100)) for j in range(300)
    ]
    windows = [(i, s, s + rng.randint(1, 100), v) for (i, s, _, v) in windows]
    first = solve(10, windows)
    for _ in range(3):
        assert solve(10, windows) == first
