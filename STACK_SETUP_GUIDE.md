# FastAPI Stack Guide

## Adopted architecture

This project is now standardizing on the stack that fits the current codebase:

- Backend: FastAPI
- Database: Neon Postgres
- ORM: SQLAlchemy
- Auth: Python-native auth in FastAPI
- Rate limiting / queue backing: Redis or Upstash Redis
- Object storage: Backblaze B2 free tier or other S3-compatible storage
- Background jobs: Python worker backed by Redis + RQ
- Hosting: Railway

We are **not** building around Better Auth or Drizzle in this repo.

## What is already in the repo

The repo now contains the first layer of infrastructure for this stack:

- [settings.py](/abs/path/c:/Users/NAEHA/Desktop/projects/api/settings.py:1)
  Centralized environment-backed settings.
- [db.py](/abs/path/c:/Users/NAEHA/Desktop/projects/api/db.py:1)
  SQLAlchemy engine/bootstrap wiring.
- [models.py](/abs/path/c:/Users/NAEHA/Desktop/projects/api/models.py:1)
  Initial ORM models for users, sessions, tokens, API keys, analyses, and audit events.
- [services.py](/abs/path/c:/Users/NAEHA/Desktop/projects/api/services.py:1)
  Redis, object storage, and RQ client factories.
- [.env.example](/abs/path/c:/Users/NAEHA/Desktop/projects/api/.env.example:1)
  Environment template for the new stack.
- [app.py](/abs/path/c:/Users/NAEHA/Desktop/projects/api/app.py:377)
  Startup now reports DB/Redis/object storage/job config state and initializes SQLAlchemy tables if `DATABASE_URL` is present.

Important: the repo still uses JSON-backed auth and analysis storage at runtime today. The new DB layer is added so we can migrate cleanly without re-architecting again.

## Current migration status

### Done

- FastAPI remains the primary backend.
- Python-native auth exists in the app.
- SQLAlchemy foundation is added.
- Redis / object storage / RQ service factories are added.
- Environment template is added.
- Startup is wired for the new infra.

### Still to migrate

- Move auth persistence from JSON files to Postgres.
- Move API key storage from JSON files to Postgres.
- Move saved analysis metadata to Postgres.
- Move pattern cache away from local disk.
- Add Redis-backed rate limiting to login and API-key actions.
- Add real outbound email delivery for verification and reset flows.
- Add background workers for email, export, and cleanup jobs.
- Add S3-compatible artifact/report storage.

## Dependencies now used

The repo now expects these Python-side building blocks:

- `sqlalchemy`
- `psycopg[binary]`
- `redis`
- `boto3`
- `rq`
- `python-dotenv`

See [requirements.txt](/abs/path/c:/Users/NAEHA/Desktop/projects/api/requirements.txt:1).

## Environment setup

Copy [.env.example](/abs/path/c:/Users/NAEHA/Desktop/projects/api/.env.example:1) into `.env` and fill the required values.

Minimum values to get onto the new stack:

```env
APP_ENV=development
COOKIE_SECURE=false
AUTH_TOKEN_SECRET=replace-with-a-long-random-secret
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/DATABASE?sslmode=require
REDIS_URL=redis://localhost:6379/0
```

For production on Railway:

```env
APP_ENV=production
COOKIE_SECURE=true
AUTH_TOKEN_SECRET=<strong-random-secret>
DATABASE_URL=<neon-connection-string>
REDIS_URL=<redis-or-upstash-redis-url>
R2_BUCKET=<bucket-name>
R2_ACCESS_KEY_ID=<access-key>
R2_SECRET_ACCESS_KEY=<secret>
R2_ENDPOINT=<r2-endpoint>
```

## Provisioning checklist

### 1. Neon Postgres

What you do:

1. Create a Neon project.
2. Create a database for the app.
3. Copy the connection string.
4. Put it into `DATABASE_URL`.

Use this SQLAlchemy-compatible form:

```env
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/DATABASE?sslmode=require
```

What Postgres will own in this repo:

- users
- auth sessions
- auth tokens
- API keys
- analysis metadata
- audit events

### 2. Redis / Upstash Redis

What you do:

1. Provision Redis or Upstash Redis.
2. Prefer a direct Redis URL when available.
3. Put it into `REDIS_URL`.

Example:

```env
REDIS_URL=redis://default:password@host:6379/0
```

What Redis will own:

- login throttling counters
- sensitive action throttling
- background queue transport
- short-lived cached state

### 3. Backblaze B2 or other S3-compatible storage

What you do:

1. Create a Backblaze B2 bucket, or another S3-compatible bucket with a free tier.
2. Create API credentials.
3. Fill the storage env vars.

Required values:

```env
R2_BUCKET=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_ENDPOINT=https://s3.<region>.backblazeb2.com
```

The code still uses the `R2_` env var names for compatibility, but the endpoint can point at Backblaze B2 or another S3-compatible provider.

What the storage bucket should store:

- exported reports
- uploaded files
- shareable artifacts
- large analysis payload exports

### 4. Railway

What you do:

1. Create a Railway service for this FastAPI app.
2. Add all env vars in Railway.
3. Ensure `COOKIE_SECURE=true` in production.
4. Deploy the API service.
5. Add a second Railway worker service for RQ when background jobs are enabled.
6. If Railway has a custom Start Command saved, clear it so the repo `Procfile` is used.

Recommended Railway services:

- `api`
  Runs `python -m uvicorn main:app --host 0.0.0.0 --port $PORT`
- `worker`
  Runs `rq worker afe-default`

### 5. Background jobs with Python

We are using a Python-side queue path instead of Trigger.dev.

Recommended first jobs:

- send verification email
- send password reset email
- export shareable analysis artifacts
- cleanup expired auth tokens
- recalculate pattern caches asynchronously

Queue backend:

- Redis
- RQ

See [services.py](/abs/path/c:/Users/NAEHA/Desktop/projects/api/services.py:1).

## Local development flow

### 1. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### 2. Create `.env`

Start from:

```powershell
Copy-Item .env.example .env
```

Then fill in:

- `AUTH_TOKEN_SECRET`
- `DATABASE_URL`
- `REDIS_URL`

### 3. Run Postgres and Redis

Use local Docker services or managed cloud services.

### 4. Start the app

```powershell
python -m uvicorn main:app --reload
```

### 5. Optional: start worker

```powershell
rq worker afe-default
```

## What the code still needs next

This is the concrete implementation order I recommend now.

### Phase 1: auth persistence cutover

Replace JSON-backed auth in [auth.py](/abs/path/c:/Users/NAEHA/Desktop/projects/api/auth.py:1) with SQLAlchemy-backed repositories for:

- users
- sessions
- auth tokens
- API keys

### Phase 2: analysis persistence cutover

Replace file-backed analysis persistence in [app.py](/abs/path/c:/Users/NAEHA/Desktop/projects/api/app.py:1150) and cache logic in [patterns.py](/abs/path/c:/Users/NAEHA/Desktop/projects/api/patterns.py:1) with Postgres-backed storage.

### Phase 3: Redis enforcement

Add Redis-backed throttles for:

- failed login attempts
- email verification requests
- re-auth attempts
- API key generation

### Phase 4: real email delivery

Move verification token handling out of the browser and into real outbound email jobs.

### Phase 5: S3-compatible artifact storage

Move large or shareable exports to the S3-compatible bucket instead of local files.

## Operational notes

- Do not rely on local JSON files in production.
- Do not rely on a generated local token secret in production.
- Set `AUTH_TOKEN_SECRET` explicitly in Railway.
- Set `COOKIE_SECURE=true` in production.
- Run the worker separately once queue-backed jobs are introduced.

## What I can do next

I can continue with one of these implementation steps immediately:

1. Migrate `auth.py` from JSON storage to SQLAlchemy/Postgres.
2. Migrate saved analyses from filesystem storage to Postgres.
3. Add Redis-backed rate limiting to the auth endpoints.
4. Add S3-compatible upload/export utilities.
5. Add an RQ worker entrypoint and first background jobs.

The most important next step is `1`, because the current auth system still persists to local JSON files.
