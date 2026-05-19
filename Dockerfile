# Use an official slim Python runtime as a parent image
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

# Create SQLite database placeholder file and set permissive flags so the container can read/write database files
RUN touch email_cache.db token.json && chmod 666 email_cache.db token.json

# Expose an optional port for HTTP/SSE transports (if running MCP over SSE)
EXPOSE 8000

# Default command: Runs the main triage ingestion engine.
# To run the MCP server, override the container run command with: ["python", "mcp_server.py"]
CMD ["python", "main.py", "--human"]
