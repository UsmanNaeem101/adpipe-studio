# Running AdPipe Studio online

AdPipe runs as its own service and is reached through Topic Atlas, which is what
checks that you are signed in. AdPipe has no login of its own, so it is never
published: put it on the private network and let Topic Atlas be the door.

```
you → topicatlas.digital/adpipe → /api/adpipe/* (session checked) → adpipe-studio:8765
```

## 1. The service

Railway → **New service** → deploy from this repository. Railway's own builder
handles it — there is no Dockerfile and no image to maintain, because the app has
no dependencies to install. Two settings do all the work:

| Setting | Value |
|---|---|
| Start command | `python3 pipeline/app.py` |
| `RAILPACK_DEPLOY_APT_PACKAGES` | `chromium fonts-liberation fonts-dejavu-core` |

Chromium is the only thing this app needs that Python does not bring: rendering
an ad *is* screenshotting an HTML template. The fonts matter as much — a headless
browser with no fonts renders every ad in tofu, and that looks like a broken
template rather than a missing font.

**Add a volume**, mounted at **`/app/projects`**. This is not optional. Without
it every project, every brief and every rendered ad is deleted on the next
deploy — the container filesystem does not survive one.

## 2. Its variables

| Variable | Value | Why |
|---|---|---|
| `STUDIO_HOST` | `0.0.0.0` | already set in the image; a container bound to loopback is unreachable |
| `OPENROUTER_API_KEY` | your key | `credentials.resolve()` prefers the environment over its stored file |
| `OPENAI_API_KEY` | your key | only if you use it |
| `ANTHROPIC_API_KEY` | your key | only if you use it |
| `ADPIPE_SHARED_SECRET` | a long random string | must match Topic Atlas's; see below |

Keys can also be pasted into the Settings tab — with the volume mounted, those
are written to `/app/projects/.credentials.json` and survive a deploy.

**Do not** give this service a public domain. If Railway has already generated
one, remove it.

## 3. Topic Atlas

On the Topic Atlas service:

| Variable | Value |
|---|---|
| `ADPIPE_URL` | `http://<service-name>.railway.internal:8765` |
| `ADPIPE_SHARED_SECRET` | the same string as above |

Redeploy, then open **topicatlas.digital/adpipe**. Signed in, you get the studio;
signed out, you get nothing at all.

## Known limits

- **Storage is a volume, not a database.** Work survives deploys but lives on one
  machine, so the service cannot be scaled to more than one instance. Moving
  project state into Postgres is in progress (`pipeline/store.py`); once every
  module reads and writes through it, set `SUPABASE_URL` and
  `SUPABASE_SERVICE_ROLE_KEY` and the volume becomes optional.
- **Long stages hold a request open.** The pipeline tab streams output as it
  goes, but a stage that runs for many minutes depends on the connection staying
  up through Topic Atlas's proxy.
