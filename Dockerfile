# --- Frontend build stage: compiles the dashboard SPA from source inside the
# image, never from a possibly-stale host-built web/dist (which is dockerignored).
FROM node:20-alpine AS web

WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- Python runtime ---
FROM python:3.11-slim

# Set system variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV WORKDIR=/app

# Set work directory
WORKDIR ${WORKDIR}

# Install system-level dependencies for utility and sqlite inspection
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements.txt first to leverage Docker build cache
COPY requirements.txt .

# Install python dependencies explicitly targeting standard PyPI repository simple index
RUN pip install --no-cache-dir -r requirements.txt --index-url https://pypi.org/simple/

# Copy the rest of the application code
COPY . .

# Built dashboard SPA -- see the `web` stage above; web/dist is dockerignored
# from the COPY . . above precisely so this is always the freshly-built copy.
COPY --from=web /web/dist ./web/dist
ENV EMAIL_TRIAGE_WEB_DIST=/app/web/dist

# Expose an optional port for HTTP/SSE transports (if running MCP over SSE)
EXPOSE 8000

# Default command: Runs the MCP server.
CMD ["python", "mcp_server.py"]
