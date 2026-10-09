# CommitsTracker web app (webui.py) for Render or any Docker host.
# See DEPLOY.md. The app reads PORT, PUBLIC_URL / RENDER_EXTERNAL_URL,
# GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, SESSION_SECRET and DATABASE_URL
# from the environment.
# Debian 13 (trixie): the first with Chromium's headless shell packaged.
FROM python:3.12-slim-trixie

# PDF_NO_SANDBOX: Chromium can't use its sandbox inside a container (see
# pdfexport.py). MPLCONFIGDIR: a writable cache for matplotlib's font list.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PDF_NO_SANDBOX=1 \
    MPLCONFIGDIR=/tmp/matplotlib

# Chromium's headless shell renders the PDF with far less memory than the
# full browser (about 40% less); the fonts cover Latin text, symbols and
# emoji. tini reaps the browser processes a stopped PDF leaves behind.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium-headless-shell \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-color-emoji \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as an unprivileged user; reports are written to /app/output-web.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/output-web /tmp/matplotlib \
    && chown -R app:app /app/output-web /tmp/matplotlib
USER app

EXPOSE 10000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "webui.py", "--no-browser"]
