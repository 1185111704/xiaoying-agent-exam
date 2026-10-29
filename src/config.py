"""运行配置：路径、上限、模型接入。只从环境变量和 .env 读取，不依赖第三方库。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """把 .env 中的 KEY=VALUE 读入环境变量；已存在的环境变量优先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


@dataclass
class Settings:
    data_dir: Path = ROOT / "data"
    db_path: Path = ROOT / "runtime" / "state.db"
    max_steps: int = 15  # 模型决策轮数上限
    max_tool_calls: int = 30  # 工具调用总次数上限
    llm_base_url: str = field(
        default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
    )
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "deepseek-flash"))
    llm_timeout: float = 60.0
