from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "local"
    app_log_level: str = "INFO"
    app_service_name: str = "ai-api"
    app_work_dir: Path = Path("./work")

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = "ai_platform"
    mysql_username: str = "root"
    mysql_password: str = ""

    redis_host: str = "10.211.55.15"
    redis_port: int = 6379
    redis_password: str = ""
    redis_database: int = 0

    rabbitmq_host: str = "10.211.55.15"
    rabbitmq_port: int = 5672
    rabbitmq_username: str = "guest"
    rabbitmq_password: str = ""
    rabbitmq_vhost: str = "/"

    minio_endpoint: str = "http://127.0.0.1:9000"
    minio_console_endpoint: str = "http://127.0.0.1:9003"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "hongying"
    minio_secure: bool = False

    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    model_provider: str = "deepseek"
    kimi_base_url: str = "https://api.moonshot.ai/v1"
    kimi_api_key: str = ""
    kimi_model: str = "moonshot-v1-8k"

    baidu_tts_api_key: str = ""
    baidu_tts_short_url: str = "https://tsn.baidu.com/text2audio"
    baidu_tts_voice: int = 5003
    baidu_tts_speed: int = 5
    baidu_tts_pitch: int = 5
    baidu_tts_volume: int = 8

    ark_media_enabled: bool = False
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_api_key: str = ""
    ark_text_model: str = "doubao-seed-1-6-flash-250828"
    ark_image_model: str = ""
    ark_video_model: str = ""
    ark_timeout_seconds: int = 900
    ark_poll_interval_seconds: float = 3.0
    ai_video_clip_duration_seconds: int = 5
    ai_video_min_clip_count: int = 3
    ai_video_max_clip_count: int = 6
    ai_video_require_motion: bool = False

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    ffmpeg_preset_version: str = "v1"
    internal_service_allowlist: str = "video-task-service,material-service,work-service,ops-console"
    worker_kind: str = "parser"

    max_media_bytes: int = 2 * 1024 * 1024 * 1024
    max_work_bytes: int = 8 * 1024 * 1024 * 1024
    render_timeout_seconds: int = 1800
    lease_seconds: int = 60
    heartbeat_seconds: int = 15
    max_attempts: int = 3
    environment_object_prefix: str = "prod"
    studio_direct_execution: bool = True
    studio_max_upload_bytes: int = 2 * 1024 * 1024 * 1024

    @field_validator("worker_kind")
    @classmethod
    def valid_worker_kind(cls, value: str) -> str:
        allowed = {"parser", "planner", "composer", "quality"}
        if value not in allowed:
            raise ValueError(f"WORKER_KIND must be one of {sorted(allowed)}")
        return value

    @property
    def mysql_url(self) -> str:
        user = quote_plus(self.mysql_username)
        password = quote_plus(self.mysql_password)
        return f"mysql+asyncmy://{user}:{password}@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"

    @property
    def redis_url(self) -> str:
        auth = f":{quote_plus(self.redis_password)}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_database}"

    @property
    def rabbitmq_url(self) -> str:
        user = quote_plus(self.rabbitmq_username)
        password = quote_plus(self.rabbitmq_password)
        vhost = quote_plus(self.rabbitmq_vhost, safe="")
        return f"amqp://{user}:{password}@{self.rabbitmq_host}:{self.rabbitmq_port}/{vhost}"

    @property
    def allowed_services(self) -> frozenset[str]:
        return frozenset(item.strip() for item in self.internal_service_allowlist.split(",") if item.strip())

    @property
    def minio_host(self) -> str:
        return self.minio_endpoint.removeprefix("http://").removeprefix("https://").rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
