from __future__ import annotations

from settings import settings

try:
    import boto3
except ModuleNotFoundError:  # pragma: no cover - optional runtime dependency
    boto3 = None

try:
    import redis
except ModuleNotFoundError:  # pragma: no cover - optional runtime dependency
    redis = None

try:
    from rq import Queue
except ModuleNotFoundError:  # pragma: no cover - optional runtime dependency
    Queue = None


def redis_enabled() -> bool:
    return bool(settings.redis_url or (settings.upstash_redis_rest_url and settings.upstash_redis_rest_token))


def get_redis_client():
    if not redis or not settings.redis_url:
        return None
    if settings.redis_url:
        return redis.from_url(settings.redis_url, decode_responses=True)
    return None


def get_job_queue():
    client = get_redis_client()
    if client is None or Queue is None:
        return None
    return Queue(settings.job_queue_name, connection=client)


def r2_enabled() -> bool:
    return bool(settings.r2_bucket and settings.r2_endpoint and settings.r2_access_key_id and settings.r2_secret_access_key)


def get_r2_client():
    if not r2_enabled() or boto3 is None:
        return None
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )
