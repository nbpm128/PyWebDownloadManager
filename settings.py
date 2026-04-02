import os


class Settings:
    """Configuration settings loaded from environment variables"""

    def __init__(self, env_file: str = None) -> None:
        """Initialize settings from environment variables"""
        if env_file and os.path.exists(env_file):
            from dotenv import load_dotenv
            load_dotenv(env_file)


        self.max_concurrent = int(os.getenv("DM_MAX_CONCURRENT", 3))
        self.workspace_path = os.getenv("DM_WORKSPACE_PATH", "./downloads")
        self.output_path = os.getenv("DM_OUTPUT_PATH", self.workspace_path)
        self.presets_path = os.getenv("DM_PRESETS_PATH", "./presets")

        self.log_level = os.getenv("DM_LOG_LEVEL", "DEBUG")
        self.log_dir = os.getenv("DM_LOG_DIR", "logs")
        self.log_max_bytes = int(os.getenv("DM_LOG_MAX_BYTES", 10 * 1024 * 1024))
        self.log_backup_count = int(os.getenv("DM_LOG_BACKUP_COUNT", 5))

        self.venv_info_path = os.environ.get("DM_VENV_INFO_PATH", "").strip()


settings = Settings(".env")