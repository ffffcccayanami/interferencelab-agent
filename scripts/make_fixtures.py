#!/usr/bin/env python
"""生成 fixtures/ 下的合成样例（可复现、不含任何公司数据）。

数字设计目标（与简历中的量级一致，但只是合成数据）：
* 并发 4、输入 2800、输出 128；
* baseline 吞吐 74.94 tok/s，apc 108.95 tok/s；
* TTFT P50 由 210ms 降到 133ms（降低 36.67%）。

实际跑：python scripts/make_fixtures.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
DOCS = FIXTURES / "docs"

CONCURRENCY = 4
INPUT_TOKENS = 2800
OUTPUT_TOKENS = 128
REQUESTS = 20
BASELINE_THROUGHPUT = 74.94
APC_THROUGHPUT = 108.95
BASELINE_TTFT = 210.0
APC_TTFT = 133.0


def _timeline(throughput: float, requests: int = REQUESTS, concurrency: int = CONCURRENCY) -> list[tuple[float, float]]:
    """构造并发窗口：max(end)-min(start) 恰好等于 sum(output)/throughput。"""

    total_tokens = requests * OUTPUT_TOKENS
    wall = total_tokens / throughput
    step = wall / (requests + concurrency - 1)
    window = concurrency * step
    return [(index * step, index * step + window) for index in range(requests)]


def _ttft_values(target: float, count: int = REQUESTS) -> list[float]:
    """20 个值的中位数恰好等于 target。"""

    offsets = [-10, -9, -8, -7, -6, -5, -4, -3, -2, -1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    values = [target + offsets[index % len(offsets)] for index in range(count)]
    assert count % 2 == 0
    ordered = sorted(values)
    assert abs((ordered[count // 2 - 1] + ordered[count // 2]) / 2 - target) < 1e-9
    return values


def build_requests(label: str, throughput: float, ttft_target: float, seed: int) -> list[dict]:
    rng = random.Random(seed)
    windows = _timeline(throughput)
    ttfts = _ttft_values(ttft_target)
    records = []
    for index, (start, end) in enumerate(windows, start=1):
        records.append(
            {
                "request_id": f"{label}-{index:02d}",
                "config_label": label,
                "concurrency": CONCURRENCY,
                "input_tokens": INPUT_TOKENS,
                "output_tokens": OUTPUT_TOKENS,
                "ttft_ms": round(ttfts[index - 1] + rng.uniform(-0.05, 0.05), 2),
                "start_ts": round(start, 4),
                "end_ts": round(end, 4),
                "status": "ok",
            }
        )
    return records


def write_benchmark() -> None:
    payload = {
        "schema_version": 1,
        "environment": {
            "note": "合成样例，仅用于验证分析链路；不是任何真实实验的原始记录。",
            "gpu": "RTX 4090 24GB",
            "model": "example-awq-4bit",
            "serving": "vLLM",
            "input_tokens": INPUT_TOKENS,
            "output_tokens": OUTPUT_TOKENS,
            "concurrency": CONCURRENCY,
        },
        "requests": build_requests("baseline", BASELINE_THROUGHPUT, BASELINE_TTFT, seed=7)
        + build_requests("apc", APC_THROUGHPUT, APC_TTFT, seed=11),
    }
    (FIXTURES / "bench_concurrency_sample.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )

    # 单组配置：只输出单组指标，不生成对比结论
    single = build_requests("apc", APC_THROUGHPUT, APC_TTFT, seed=13)[:10]
    lines = ["request_id,config_label,concurrency,input_tokens,output_tokens,ttft_ms,start_ts,end_ts,status"]
    for item in single:
        lines.append(
            "{request_id},{config_label},{concurrency},{input_tokens},{output_tokens},{ttft_ms},"
            "{start_ts},{end_ts},{status}".format(**item)
        )
    (FIXTURES / "bench_single_config.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 样本数不足：同一负载下 apc 只有 1 条记录 → 必须拒绝出对比
    uneven = build_requests("baseline", BASELINE_THROUGHPUT, BASELINE_TTFT, seed=17)[:4]
    uneven += build_requests("apc", APC_THROUGHPUT, APC_TTFT, seed=19)[:1]
    (FIXTURES / "bench_uneven_samples.json").write_text(
        json.dumps({"requests": uneven}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_prometheus() -> None:
    text = """# 合成样例：vLLM 风格的 Prometheus 文本（不是真实抓取）
# HELP vllm:num_requests_running Number of requests currently running.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model="example-awq-4bit"} 4
# HELP vllm:gpu_cache_usage_perc GPU KV-cache usage.
# TYPE vllm:gpu_cache_usage_perc gauge
vllm:gpu_cache_usage_perc{model="example-awq-4bit"} 0.42
# HELP vllm:prefix_cache_hit_rate prefix cache hit rate.
# TYPE vllm:prefix_cache_hit_rate gauge
vllm:prefix_cache_hit_rate{model="example-awq-4bit"} 0.61
# HELP vllm:time_to_first_token_seconds_bucket TTFT histogram.
# TYPE vllm:time_to_first_token_seconds_bucket histogram
vllm:time_to_first_token_seconds_bucket{model="example-awq-4bit",le="0.05"} 2
vllm:time_to_first_token_seconds_bucket{model="example-awq-4bit",le="0.1"} 6
vllm:time_to_first_token_seconds_bucket{model="example-awq-4bit",le="0.2"} 12
vllm:time_to_first_token_seconds_bucket{model="example-awq-4bit",le="0.3"} 16
vllm:time_to_first_token_seconds_bucket{model="example-awq-4bit",le="0.5"} 19
vllm:time_to_first_token_seconds_bucket{model="example-awq-4bit",le="+Inf"} 20
vllm:time_to_first_token_seconds_sum{model="example-awq-4bit"} 3.4
vllm:time_to_first_token_seconds_count{model="example-awq-4bit"} 20
"""
    (FIXTURES / "prometheus_sample.prom").write_text(text, encoding="utf-8")


def write_docs() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "apc_notes.md").write_text(
        """# 前缀缓存（APC）排查笔记

## 什么情况下前缀会命中

前缀缓存按块（block）粒度匹配。只有当两次请求的 token 前缀在同一个块边界内完全一致，
并且该块仍被缓存保留时，才会命中。块大小由服务端配置决定，实际值必须从启动日志或
运行时配置读取，不能按默认值假设。

## 常见的不命中原因

1. 前缀在第一条用户消息之前就被注入（系统提示词每次不同，例如带时间戳）；
2. 混合注意力模型对可缓存的层有限制，只有部分层参与前缀复用；
3. 缓存保留比例过低，块在第二次请求到达前已被回收；
4. 请求被路由到不同的 worker 实例，缓存不共享。

## 验证方法

先固定提示词布局，再对比 `prefix_cache_hit_rate` 与 `gpu_cache_usage_perc` 两个指标；
如果命中率随并发升高而下降，优先怀疑块回收而不是提示词差异。
""",
        encoding="utf-8",
    )
    (DOCS / "chunked_prefill.md").write_text(
        """# Chunked Prefill 与 TTFT 的关系

Chunked prefill 把长提示词的 prefill 拆成多个小块，与 decode 请求交错调度。
它改善的是“长 prefill 阻塞其他请求”的问题，因此主要体现在尾延迟（P95/P99）
和并发吞吐上，而不必然降低单个请求的首 token 延迟。

## 何时应该打开

- 输入长度远大于输出长度（例如 2.8K 输入、128 输出）；
- 在线服务中短请求与长请求混跑，需要避免长请求长时间占住批次。

## 何时不该归因于它

如果 TTFT 的下降伴随着前缀命中率上升，那么收益更可能来自前缀复用，
而不是分块调度本身；两者需要分开测量。
""",
        encoding="utf-8",
    )
    (DOCS / "unrelated.md").write_text(
        """# 一周晚餐计划

周一：番茄鸡蛋面。
周二：清炒时蔬配米饭。
周三：豆腐汤与凉拌黄瓜。

这份文档与推理服务无关，用于验证检索不会把不相关文档当成证据。
""",
        encoding="utf-8",
    )


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    write_benchmark()
    write_prometheus()
    write_docs()
    print("[fixtures] 已生成：")
    for path in sorted(FIXTURES.rglob("*")):
        if path.is_file():
            print(f"  - {path.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
