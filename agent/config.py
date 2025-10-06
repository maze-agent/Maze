import os
import tomli
from pathlib import Path

class Config:
    """
    一个简单的单例类，用于加载和提供全局配置。
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self):
        """加载 config.toml 文件。"""
        try:
            project_root = Path(__file__).resolve().parent.parent
            config_path = project_root / "config" / "config.toml"
            print(f"Loading configuration from: {config_path}")
            if not config_path.exists():
                raise FileNotFoundError("Config file not found at 'config/config.toml'")

            with open(config_path, "rb") as f:
                self._config_data = tomli.load(f)
            print("Configuration from 'config.toml' loaded successfully.")

        except Exception as e:
            print(f"Error loading config.toml: {e}")
            self._config_data = {}

    def get(self, key, default=None):
        """获取一个顶层配置项。"""
        return self._config_data.get(key, default)

    @property
    def online_apis(self):
        """获取 'online_apis' 部分的配置。"""
        return self.get('online_apis', {})

    @property
    def local_models(self):
        """获取 'local_models' 部分的配置。"""
        return self.get('local_models', {})

# 创建一个全局实例供其他模块导入
config = Config()