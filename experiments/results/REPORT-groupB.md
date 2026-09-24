# 对照实验统计报告

数据来源：groupB.jsonl
总记录数：120
配置：`single`(30)、`single_self_correct`(30)、`homogeneous`(30)、`heterogeneous`(30)

## 1. 总览

| 配置 | 题数 | 通过 | 通过率 | 平均耗时 | 平均尝试 | 说明 |
|---|---|---|---|---|---|---|
| `single` | 30 | 28 | **93.3%** | 2.2s | 1.00 | |
| `single_self_correct` | 30 | 30 | **100.0%** | 2.3s | 1.10 | |
| `homogeneous` | 30 | 26 | **86.7%** | 15.3s | 1.00 | |
| `heterogeneous` | 30 | 29 | **96.7%** | 92.9s | 1.00 | |

## 2. 按难度分层

| 配置 | easy | medium | hard |
|---|---|---|---|
| `single` | 9/9 (100.0%) | 12/12 (100.0%) | 7/9 (77.8%) |
| `single_self_correct` | 9/9 (100.0%) | 12/12 (100.0%) | 9/9 (100.0%) |
| `homogeneous` | 7/9 (77.8%) | 12/12 (100.0%) | 7/9 (77.8%) |
| `heterogeneous` | 9/9 (100.0%) | 12/12 (100.0%) | 8/9 (88.9%) |

## 3. 失败分类分布

| 配置 | 通过 | assertion_failed | interface_function_missing | runtime_error |
|---|---|---|---|---|
| `single` | 28 | 1 | 0 | 1 |
| `single_self_correct` | 30 | 0 | 0 | 0 |
| `homogeneous` | 26 | 2 | 2 | 0 |
| `heterogeneous` | 29 | 1 | 0 | 0 |

分类含义：`interface_*` = 未按题面接口写（**不算算法能力问题**）；`no_code`/`syntax_error` = 输出格式问题；`assertion_failed` = 算法真的错了。

## 4. 配对对比：`single` vs `homogeneous`（同一批题，30 道）

| 变化 | 题数 | 题号 |
|---|---|---|
| ✅ 讨论**救回来** | 0 | — |
| ❌ 讨论**弄坏了** | 2 | power_of_three, roman_to_integer |
| 两者都对 | 26 | candy, coin_change, daily_temperatures, decode_string, excel_sheet_column_title, first_missing_positive, hamming_weight, happy_number, has_cycle, jump_game, largest_rectangle_in_histogram, length_of_longest_substring, longest_valid_parentheses, majority_element, merge_intervals, min_subarray_len, minimum_window_substring, multiply_strings, product_except_self, rotate_array, search_in_rotated_sorted_array, single_number, split_array_largest_sum, trapping_rain_water, two_sum_ii, valid_parentheses |
| 两者都错 | 2 | binary_tree_max_path_sum, sliding_window_maximum |

**净收益：-2 题**（救回 0，弄坏 2）
（在 2 道出现差异的题里，讨论方向为正的比例 = 0%）
