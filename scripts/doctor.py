#!/usr/bin/env python
"""配置自检：不需要启动服务，就能判断"模型端点能不能用"。

    python scripts/doctor.py            # 检查配置 + 探测端点（不消耗 token）
    python scripts/doctor.py --no-probe # 只检查配置

退出码：0 全部正常；1 配置或连通性有问题（脚本不会打印任何密钥）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.llm import LLMClient  # noqa: E402
from app.settings import ConfigError, load_settings, resolve_env_file  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="InferenceLab 配置自检")
    parser.add_argument("--no-probe", action="store_true", help="跳过端点探测")
    args = parser.parse_args(argv)

    env_file = resolve_env_file()
    print(f"[doctor] 配置文件: {env_file or '未找到（仅使用进程环境变量）'}")

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[doctor] 配置错误: {exc}", file=sys.stderr)
        return 1

    print(f"[doctor] 任务数据目录: {settings.data_dir}")
    print(f"[doctor] API 监听: http://{settings.infl_api_host}:{settings.infl_api_port}")
    print(f"[doctor] 模型: enabled={settings.llm_enabled} model={settings.llm_model_id}")
    print(
        f"[doctor] 接入: provider={settings.llm_provider} profile={settings.llm_profile or '-'} "
        f"deployment={settings.deployment}"
    )
    print(f"[doctor] 端点: {settings.llm_endpoint} auth={settings.llm_auth_mode}")
    print(f"[doctor] key 指纹: {settings.llm_key_fingerprint()}")

    if not settings.llm_configured:
        print("[doctor] 结论: 离线模式（任务仍可运行，只是没有模型解释）")
        return 0

    if args.no_probe:
        print("[doctor] 结论: 配置完整（未探测端点）")
        return 0

    client = LLMClient(settings)
    result = client.probe()
    print(f"[doctor] 探测 {settings.llm_health_url}")
    print(f"[doctor]   reachable={result['reachable']} status={result['status_code']} latency={result['latency_ms']}ms")
    if result["models"]:
        preview = ", ".join(result["models"][:5])
        print(f"[doctor]   可用模型（前 5）: {preview}")
    if result["model_present"] is False:
        print(f"[doctor] 警告: LLM_MODEL_ID={settings.llm_model_id!r} 不在 /models 列表中")
    if result["error"]:
        print(f"[doctor] 错误: {result['error']}", file=sys.stderr)
        return 1

    print("[doctor] 结论: 模型端点可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
