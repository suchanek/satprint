FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY satprint ./satprint
RUN pip install --no-cache-dir .
ENV SATPRINT_HOST=0.0.0.0 SATPRINT_PORT=7417
EXPOSE 7417
VOLUME ["/root/.cache/satprint"]
CMD ["sh", "-c", "satprint serve --host $SATPRINT_HOST --port $SATPRINT_PORT"]
