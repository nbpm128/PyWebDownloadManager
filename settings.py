import os


class Settings:
    """Configuration settings loaded from environment variables"""

    def __init__(self, env_file: str = None) -> None:
        """Initialize settings from environment variables"""
        if env_file and os.path.exists(env_file):
            from dotenv import load_dotenv
            load_dotenv(env_file)


        self.max_concurrent = int(os.getenv("MAX_CONCURRENT", 3))
        self.workspace_path = os.getenv("WORKSPACE_PATH", "./downloads")
        self.output_path = os.getenv("OUTPUT_PATH", self.workspace_path)
        self.presets_path = os.getenv("PRESETS_PATH", "./presets")

        self.log_level = os.getenv("LOG_LEVEL", "DEBUG")
        self.log_dir = os.getenv("LOG_DIR", "logs")
        self.log_max_bytes = int(os.getenv("LOG_MAX_BYTES", 10 * 1024 * 1024))
        self.log_backup_count = int(os.getenv("LOG_BACKUP_COUNT", 5))


settings = Settings(".env")