# OpenBench Vercel Deployment Guide

OpenBench is a Django server plus standalone workers. Vercel hosts the server only.
Workers run on separate machines and connect to the server URL.

## Prerequisites
- A Postgres database (Neon/Supabase/etc.) and its connection string.
- A place to store Media files (PGNs, uploaded networks). Vercel is ephemeral.

## Environment variables
Set these in the Vercel project settings:
- OPENBENCH_SECRET_KEY: strong random string
- OPENBENCH_DEBUG: false
- OPENBENCH_ALLOWED_HOSTS: yourdomain.vercel.app,custom.domain
- DATABASE_URL: postgres connection string
- OPENBENCH_MEDIA_ROOT: optional path if you mount persistent storage (otherwise use external storage)

## Vercel configuration
Create a `vercel.json` at the repo root:
```
{
  "builds": [
    { "src": "OpenSite/wsgi.py", "use": "@vercel/python" }
  ],
  "routes": [
    { "src": "/static/(.*)", "dest": "/static/$1" },
    { "src": "/(.*)", "dest": "OpenSite/wsgi.py" }
  ]
}
```

If you use Django `collectstatic`, add a build step in Vercel:
- Build Command: `python manage.py collectstatic --noinput`
- Output Directory: `static`

## Database migrations
Run migrations once after first deploy:
```
python manage.py migrate
python manage.py createsuperuser
```

## Media storage
Vercel file storage is ephemeral. For persistent Media:
- Use an object store (S3/R2) with a Django storage backend.
- Or host Media on another server and point OPENBENCH_MEDIA_ROOT there.

## Worker setup
Workers run outside Vercel. Point them at your server URL:
```
set OPENBENCH_SERVER=https://yourdomain.vercel.app
set OPENBENCH_USERNAME=<user>
set OPENBENCH_PASSWORD=<pass>
python Client\client.py --no-client-downloads --threads 4 --nsockets 1
```

## Notes
- Variantfishtest is embedded in the Client for variant tests.
- Fastchess is still supported for standard and Fischer random tests.
