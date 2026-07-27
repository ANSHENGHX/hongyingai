FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 hongying \
    && useradd --uid 10001 --gid hongying --create-home hongying

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY apps ./apps
COPY prompts ./prompts
COPY schemas ./schemas
COPY resources ./resources
RUN pip install .

RUN mkdir -p /work && chown -R hongying:hongying /app /work
USER hongying

ENV APP_WORK_DIR=/work
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "hongying_ai.api:app", "--host", "0.0.0.0", "--port", "8080"]

