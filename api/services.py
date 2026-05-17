from __future__ import annotations

import boto3
import redis
from rq import Queue

from settings import settings


def redis_enabled() -> bool:
    return bool(settings.redis_url or (settings.upstash_redis_rest_url and settings.upstash_redis_rest_token))


def get_redis_client():
    if settings.redis_url:
        return redis.from_url(settings.redis_url, decode_responses=True)
    return None


def get_job_queue():
    client = get_redis_client()
    if client is None:
        return None
    return Queue(settings.job_queue_name, connection=client)


def r2_enabled() -> bool:
    return bool(settings.r2_bucket and settings.r2_endpoint and settings.r2_access_key_id and settings.r2_secret_access_key)


def get_r2_client():
    if not r2_enabled():
        return None
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )
