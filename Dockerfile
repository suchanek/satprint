FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY satprint ./satprint
RUN pip install --no-cache-dir .
ENV SATPRINT_HOST=0.0.0.0 SATPRINT_PORT=8000
EXPOSE 8000
VOLUME ["/root/.cache/satprint"]
CMD ["sh", "-c", "satprint serve --host $SATPRINT_HOST --port $SATPRINT_PORT"]
