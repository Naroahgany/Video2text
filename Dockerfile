FROM python:3.12-slim-bookworm

ARG APT_MIRROR=
ARG APT_SECURITY_MIRROR=
ARG PIP_INDEX_URL=

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

WORKDIR /app

RUN if [ -n "$APT_SECURITY_MIRROR" ]; then \
        find /etc/apt -type f \( -name "*.list" -o -name "*.sources" \) \
          -exec sed -i \
            -e "s|URIs: http://deb.debian.org/debian-security|URIs: ${APT_SECURITY_MIRROR}|g" \
            -e "s|URIs: https://deb.debian.org/debian-security|URIs: ${APT_SECURITY_MIRROR}|g" \
            -e "s|URIs: http://security.debian.org/debian-security|URIs: ${APT_SECURITY_MIRROR}|g" \
            -e "s|URIs: https://security.debian.org/debian-security|URIs: ${APT_SECURITY_MIRROR}|g" {} +; \
    fi \
    && if [ -n "$APT_MIRROR" ]; then \
        find /etc/apt -type f \( -name "*.list" -o -name "*.sources" \) \
          -exec sed -i \
            -e "s|URIs: http://deb.debian.org/debian|URIs: ${APT_MIRROR}|g" \
            -e "s|URIs: https://deb.debian.org/debian|URIs: ${APT_MIRROR}|g" {} +; \
    fi \
    && apt-get update -o Acquire::Retries=5 -o Acquire::http::Timeout=30 \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt backend/requirements.txt
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir -i "$PIP_INDEX_URL" -r backend/requirements.txt; \
    else \
        pip install --no-cache-dir -r backend/requirements.txt; \
    fi

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
