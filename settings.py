import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str = os.environ.get("APP_ENV", "development").strip() or "development"
    database_url: str = (os.environ.get("DATABASE_URL") or "").strip()
    auth_token_secret: str = (os.environ.get("AUTH_TOKEN_SECRET") or "").strip()
    api_key_pepper: str = (os.environ.get("API_KEY_PEPPER") or "").strip()
    cookie_secure: bool = _as_bool(os.environ.get("COOKIE_SECURE"), False)
    redis_url: str = (os.environ.get("REDIS_URL") or "").strip()
    upstash_redis_rest_url: str = (os.environ.get("UPSTASH_REDIS_REST_URL") or "").strip()
    upstash_redis_rest_token: str = (os.environ.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()
    r2_account_id: str = (os.environ.get("R2_ACCOUNT_ID") or "").strip()
    r2_bucket: str = (os.environ.get("R2_BUCKET") or "").strip()
    r2_access_key_id: str = (os.environ.get("R2_ACCESS_KEY_ID") or "").strip()
    r2_secret_access_key: str = (os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip()
    r2_endpoint: str = (os.environ.get("R2_ENDPOINT") or "").strip()
    job_queue_name: str = (os.environ.get("JOB_QUEUE_NAME") or "afe-default").strip() or "afe-default"
    api_key_rotation_days: int = int((os.environ.get("API_KEY_ROTATION_DAYS") or "90").strip() or "90")


settings = Settings()
