# syntax=docker/dockerfile:1.7

FROM python:3.14.0-slim-bookworm@sha256:d13fa0424035d290decef3d575cea23d1b7d5952cdf429df8f5542c71e961576 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    OPERATOR_HTTP_BIND=0.0.0.0 \
    OPERATOR_ALLOW_CONTAINER_BIND=true

WORKDIR /app

# Apply available operating-system security patches on top of the pinned base image.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --upgrade "pip==26.1.2" \
    && python -m pip install --require-hashes --only-binary=:all: -r requirements.txt

# The build context must contain the exact inspected release-candidate wheels.
ARG CLIENT_WHEEL
ARG OPERATOR_WHEEL
COPY wheelhouse/${CLIENT_WHEEL} /tmp/${CLIENT_WHEEL}
COPY wheelhouse/${OPERATOR_WHEEL} /tmp/${OPERATOR_WHEEL}
RUN python -m pip install --no-deps "/tmp/${CLIENT_WHEEL}" "/tmp/${OPERATOR_WHEEL}" \
    && rm "/tmp/${CLIENT_WHEEL}" "/tmp/${OPERATOR_WHEEL}"

COPY config ./config

RUN groupadd --system --gid 65532 operator-app \
    && useradd --system --uid 65532 --gid 65532 --home-dir /nonexistent operator-app \
    && chown -R 65532:65532 /app
USER 65532:65532

EXPOSE 8080
CMD ["python", "-m", "operator_app"]
