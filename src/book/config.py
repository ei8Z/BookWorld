"""项目配置——所有可变参数集中管理。"""

from pathlib import Path
from dataclasses import dataclass, field

# 项目根目录（基于本文件路径推导）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class Config:
    # 数据库路径
    db_path: Path = field(default_factory=lambda: _PROJECT_ROOT / "data/book.db")

    # LLM 配置（通过环境变量或直接配置）
    llm_api_base: str = "https://api.deepseek.com"
    llm_api_key: str = ""  # 从环境变量 DEEPSEEK_API_KEY 读取
    llm_model: str = "deepseek-flash"

    # 记忆系统阈值
    temp_memory_max: int = 10  # 临时记忆上限，达到后触发整合
    perm_memory_compress_at: int = 6  # 永久记忆达到此数时压缩为3条

    # 输出目录
    output_dir: Path = field(default_factory=lambda: _PROJECT_ROOT / "output")

    def __post_init__(self):
        import os
        if not self.llm_api_key:
            self.llm_api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        # 数据目录可以用 BOOK_HOME 整体搬走（部署时把数据与代码分离）
        book_home = os.environ.get("BOOK_HOME", "").strip()
        if book_home:
            home = Path(book_home)
            self.db_path = home / "book.db"
            self.output_dir = home / "output"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


# 全局配置实例
config = Config()
