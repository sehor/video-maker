FROM python:3.12.11-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY docs/licenses/uv-MIT.txt /usr/share/doc/uv/copyright

COPY apps/api/pyproject.toml apps/api/uv.lock ./
RUN uv sync --locked --extra dev --no-install-project
COPY apps/api .
RUN uv sync --locked --extra dev --no-editable

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
