#!/usr/bin/env python
"""固定验证脚本：对给定形状做一次确定性复算并打印 JSON。

它不是 benchmark 本身，只验证"审批 → 隔离执行 → 产物"链路；参数由
registry 校验后传入，不接受任意命令或路径。
"""

from __future__ import annotations

import argparse
import json
import math
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--params", required=True, help="JSON 参数")
    args = parser.parse_args()

    params = json.loads(args.params)
    input_tokens = int(params["input_tokens"])
    output_tokens = int(params["output_tokens"])
    concurrency = int(params["concurrency"])
    repeats = int(params["repeats"])

    if concurrency < 1 or repeats < 1:
        print("concurrency/repeats 必须 >= 1", file=sys.stderr)
        return 2

    # 纯计算：按固定形状估算一个可复现的请求数，不做任何 I/O 或网络调用。
    requests = concurrency * repeats
    total_tokens = requests * (input_tokens + output_tokens)
    summary = {
        "ok": True,
        "requests": requests,
        "total_tokens": total_tokens,
        "prefill_decode_ratio": round(input_tokens / max(1, output_tokens), 4),
        "sqrt_requests": round(math.sqrt(requests), 4),
        "note": "确定性复算，仅验证链路；不代表模型性能。",
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
