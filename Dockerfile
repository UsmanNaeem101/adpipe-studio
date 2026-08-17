# AdPipe Studio as a service.
#
# Python and a browser, and nothing else — this app has no pip dependencies, so
# the whole image is the standard library plus the Chromium that `render.py`
# screenshots ads with.

FROM python:3.11-slim

# Chromium is not an optimisation here: rendering an ad *is* taking a screenshot
# of an HTML template, so without a browser the render stage cannot run at all.
# The fonts matter as much — a headless browser with no fonts renders every ad
# in tofu, and the failure looks like a bad template rather than a bad image.
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium \
      fonts-liberation \
      fonts-dejavu-core \
      fonts-noto-color-emoji \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # `find_chrome()` looks here before falling back to PATH.
    CHROME=/usr/bin/chromium \
    # Reachable from outside the container; see the note in app.py.
    STUDIO_HOST=0.0.0.0 \
    STUDIO_PORT=8765 \
    STUDIO_NO_BROWSER=1 \
    # Keys arrive as environment variables. `credentials.resolve()` already
    # prefers those over its stored file, so nothing needs a home directory —
    # but the Settings tab writes, and this is where that write lands: on the
    # volume, so a key saved in the browser survives the next deploy.
    ADPIPE_CREDENTIALS_PATH=/app/projects/.credentials.json

WORKDIR /app
COPY . /app

# The volume mounts exactly where the app already looks for project state, so
# `paths.py` needs no knowledge of it. Mounting elsewhere and pointing the store
# at it would leave two ideas of where things live — one in paths.py and one in
# the store — which is the kind of split that loses a project's work quietly.
#
# This is what makes the difference between "running online" and "running online
# until the next push". The store migration replaces it with Postgres; until
# that lands, this is the whole persistence story.
VOLUME ["/app/projects"]

EXPOSE 8765

# No health endpoint of its own, so ask for the page it always serves.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ.get('STUDIO_PORT','8765'), timeout=4).status == 200 else 1)"

CMD ["python3", "pipeline/app.py"]
