# One Python image for the mandate service, the agent, and the demo runner.
# Credentials are NOT baked in: compose passes PayPal secrets to the mandate
# service only. The agent container gets an LLM key and a mandate token, nothing else.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir uv==0.13.7

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY services ./services
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 app && mkdir -p /app/data && chown app /app/data
USER app

EXPOSE 8000
CMD ["uvicorn", "--factory", "mandate.main:app_from_env", "--host", "0.0.0.0", "--port", "8000"]
