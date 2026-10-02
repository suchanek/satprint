FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY satprint ./satprint
RUN pip install --no-cache-dir --root-user-action=ignore .
# Run unprivileged. The cache directory is created here so a named volume
# mounted on it inherits the satprint user's ownership.
RUN useradd --create-home --uid 1000 satprint \
    && mkdir -p /home/satprint/.cache/satprint \
    && chown -R satprint:satprint /home/satprint/.cache
USER satprint
ENV SATPRINT_HOST=0.0.0.0 SATPRINT_PORT=7417
EXPOSE 7417
VOLUME ["/home/satprint/.cache/satprint"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"SATPRINT_PORT\"]}/api/health', timeout=4)"
CMD ["sh", "-c", "satprint serve --host $SATPRINT_HOST --port $SATPRINT_PORT"]
