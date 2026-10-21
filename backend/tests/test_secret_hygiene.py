"""密钥卫生：扫描器、模板生成器、仓库约定。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def load_script(name: str) -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        path = SCRIPTS / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"hygiene_{name}", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        # dataclass 依赖 sys.modules[cls.__module__]，必须先注册再执行
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


def test_scanner_flags_real_looking_key() -> None:
    scanner = load_script("secret_scan")
    findings = scanner.scan_text("LLM_API_KEY=sk-" + "a1b2c3d4" * 4, ".env")
    assert len(findings) == 1
    assert "sk-a" in findings[0].masked
    assert "a1b2c3d4a1b2c3d4" not in findings[0].masked


def test_scanner_allows_placeholders() -> None:
    scanner = load_script("secret_scan")
    for line in (
        "LLM_API_KEY=YOUR_LLM_API_KEY",
        "LLM_API_KEY=your-api-key-here",
        "TAVILY_API_KEY=${TAVILY_API_KEY}",
        "LLM_API_KEY=<set-me>",
    ):
        assert scanner.scan_text(line, ".env.example") == [], line


def test_scanner_flags_commented_out_key() -> None:
    """注释掉但仍是真 key 的行同样要拦截。"""

    scanner = load_script("secret_scan")
    fake = "# LLM_API_KEY=" + "sk-" + "abcdefghijklmnop"
    assert len(scanner.scan_text(fake, ".env.example")) == 1


def test_scanner_ignores_allow_comment() -> None:
    scanner = load_script("secret_scan")
    line = "SERPAPI_API_KEY=ac69...  # allow-secret: 单元测试样例"
    assert scanner.scan_text(line, "docs/demo.md") == []


def test_worktree_scan_skips_local_credential_files() -> None:
    """本地 .env / 私钥是真实值该待的地方，工作区扫描跳过；模板仍会被检查。"""

    scanner = load_script("secret_scan")
    for name in (".env", ".env.local", "id_rsa", "server.pem", "client.key"):
        assert scanner.is_local_secret_file(Path(name)) is True, name
    for name in (".env.example", "example.env", "env.sample", "README.md"):
        assert scanner.is_local_secret_file(Path(name)) is False, name


def test_template_generation_replaces_secret_values() -> None:
    env_template = load_script("env_template")
    # 运行时拼接，避免测试文件里出现"看起来像真 key"的字面量
    fake_llm = "sk-" + "a" * 24
    fake_tavily = "tvly-dev-" + "b" * 16
    source = (
        "# 注释保留\n"
        "LLM_MODEL_ID=deepseek-chat\n"
        f"LLM_API_KEY={fake_llm}\n"
        "LLM_TIMEOUT=120\n"
        f"TAVILY_API_KEY={fake_tavily}\n"
    )
    rendered = env_template.render_template(source)

    assert "YOUR_LLM_API_KEY" in rendered
    assert "YOUR_TAVILY_API_KEY" in rendered
    assert "deepseek-chat" in rendered
    assert "sk-abc" not in rendered
    assert "tvly-dev" not in rendered
    assert "# 注释保留" in rendered


def test_template_check_detects_leaked_value(workdir: Path) -> None:
    env_template = load_script("env_template")
    env_file = workdir / "backend.env"
    template = workdir / "example.env"
    secret = "sk-" + "z" * 24

    env_file.write_text(f"LLM_API_KEY={secret}\n", encoding="utf-8")
    template.write_text(f"LLM_API_KEY={secret}\n", encoding="utf-8")
    assert env_template.check(template, env_file)

    template.write_text("LLM_API_KEY=YOUR_LLM_API_KEY\n", encoding="utf-8")
    assert env_template.check(template, env_file) == []


def test_template_check_ignores_shared_placeholders(workdir: Path) -> None:
    """本地 .env 仍是占位符时，模板与之相同不算泄漏。"""

    env_template = load_script("env_template")
    env_file = workdir / "backend.env"
    template = workdir / "example.env"

    env_file.write_text("LLM_API_KEY=YOUR_LLM_API_KEY\nLLM_BASE_URL=http://127.0.0.1:8000/v1\n", encoding="utf-8")
    template.write_text("LLM_API_KEY=YOUR_LLM_API_KEY\nLLM_BASE_URL=http://127.0.0.1:8000/v1\n", encoding="utf-8")
    assert env_template.check(template, env_file) == []


def test_secret_key_detection_excludes_token_counts() -> None:
    """`*_TOKENS` 是数量而不是凭据，不能被模板生成器替换成占位符。"""

    env_template = load_script("env_template")
    for key in ("LLM_API_KEY", "TAVILY_API_KEY", "GITHUB_TOKEN", "QDRANT_API_KEY"):
        assert env_template.is_secret_key(key) is True, key
    for key in ("LLM_MAX_OUTPUT_TOKENS", "LLM_MAX_CALLS_PER_RUN", "INFL_MAX_UPLOAD_MB"):
        assert env_template.is_secret_key(key) is False, key


def test_template_generation_keeps_numeric_limits() -> None:
    env_template = load_script("env_template")
    source = "LLM_MAX_OUTPUT_TOKENS=2048\nLLM_API_KEY=" + "sk-" + "c" * 24 + "\n"
    rendered = env_template.render_template(source)

    assert "LLM_MAX_OUTPUT_TOKENS=2048" in rendered
    assert "YOUR_LLM_API_KEY" in rendered


def test_committed_template_is_clean() -> None:
    env_template = load_script("env_template")
    assert (ROOT / ".env.example").exists()
    assert env_template.check(ROOT / ".env.example", ROOT / "backend" / ".env") == []


def test_gitignore_protects_env_files() -> None:
    content = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in content
    assert "!.env.example" in content
    assert "backend/var/" in content
