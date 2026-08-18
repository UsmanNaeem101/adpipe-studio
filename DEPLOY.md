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
| `STUDIO_HOST` | `0.0.0.0` | a container bound to loopback is unreachable, with no error to read |
| `STUDIO_NO_BROWSER` | `1` | there is no browser to open and nobody to look at it |
| `CHROME` | `/usr/bin/chromium` | names the browser installed above; `find_chrome()` looks in /Applications and on PATH otherwise |
| `ADPIPE_CREDENTIALS_FILE` | `/app/projects/.credentials.json` | puts the credential store **on the volume**; see below |
| `ADPIPE_SHARED_SECRET` | a long random string | must match Topic Atlas's; see below |
| `OPENROUTER_API_KEY` | your key | `credentials.resolve()` prefers the environment over its stored file |
| `OPENAI_API_KEY` | your key | only if you use it |
| `ANTHROPIC_API_KEY` | your key | only if you use it |

Keys can be pasted into the Settings tab instead of set here — but only if
`ADPIPE_CREDENTIALS_FILE` points at the volume. Its default is a user-level path
(`~/.config/adpipe/credentials.json`), which is right on a laptop and wrong here:
that is container filesystem, so a key typed into Settings works perfectly until
the next deploy silently discards it.

The startup log settles both questions before anyone spends money on them —

```
  API keys: openrouter · store /app/projects/.credentials.json
```

— naming which providers resolved and where a new key will land. A variable
spelled slightly wrong shows up as a missing provider here rather than as a 401
twenty minutes into a stage.

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

## 4. Supabase instead of a volume (optional)

Set `SUPABASE_URL` (or `NEXT_PUBLIC_SUPABASE_URL`, which is what Topic Atlas
already calls it — the two apps can share one project, their tables do not
collide) and `SUPABASE_SERVICE_ROLE_KEY`, and every project read and
write goes through Postgres and Supabase Storage instead of the disk — the volume
becomes optional and the service is no longer pinned to one machine. Leave them
unset and nothing changes.

Everything a project is made of goes there: the project's own `project.json` and
`facts.json`, the VOC corpora, the stage-06 evidence files (imported or produced),
every extraction, the PICC cards, the concepts, the briefs, the product sheets and
their segment sheets, and the rendered ads. What stays on the disk is what has to
be: the credential store, the page Chromium screenshots, and the templates and
skills that ship inside the image and are never written.

Text goes in a table, so a brief or a product sheet stays readable in the table
editor. Anything over **512KB** goes to the `adpipe-media` bucket instead, with a
marker row left behind so listings still answer from one place. That threshold
matters: a raw VOC dump is 16MB and its deduplicated twin nearly as big, and a
row that size is one PostgREST JSON body — a hosted API gateway refuses it long
before Postgres would care. It is also a quota question, since the database is
metered in hundreds of megabytes and the bucket in gigabytes. Override with
`ADPIPE_TEXT_MAX_BYTES` if you need to.

Both are needed. With only one set the app runs on the disk, and Settings →
**Where this is saved** says which of the two is missing. That line is the place
to check: it names the Supabase project when it is working, and names the remedy
when it is not — a rejected key, a missing table, an unreachable host.

**API keys do not move with it.** The credential store is a `0600` file written
straight to disk on purpose — a secret does not belong in a shared table or
bucket — so with no volume it is wiped on every deploy. Set `OPENROUTER_API_KEY`
(and the others) as service variables instead of typing them into Settings;
`credentials.resolve()` prefers the environment, and the startup log names which
providers resolved.

### Reclaiming the corpora

Stages 01–06 build several copies of the corpus, and stage 06 is the end of that
chain: everything after it — extract, picc, concepts, brief — reads the evidence
files and nothing earlier. Once those exist, the corpora are the largest thing in
a project and the least read.

Settings → Existing projects has **Download** (the whole project as a zip) and
**Free space** (removes only the spent corpora). The same pair on the command
line:

```bash
adpipe -p <project> archive          # whole project -> <project>.zip
adpipe -p <project> cleanup          # remove the spent corpora
```

`cleanup` refuses while stage 06 has produced no evidence files, because at that
point the corpora are still the only copy of the research. It leaves the
segmentation state (`candidate_segments`, `validated_segments`,
`segment_assignments`) alone, but removing the corpora does mean segmentation
cannot be re-run — `--reassign`, `--rediscover` and `--from` all read them — so
take the zip first.

## Known limits

- **Without Supabase, storage is a volume.** Work survives deploys but lives on
  one machine, so the service cannot be scaled to more than one instance.
- **Long stages hold a request open.** The pipeline tab streams output as it
  goes, but a stage that runs for many minutes depends on the connection staying
  up through Topic Atlas's proxy.
