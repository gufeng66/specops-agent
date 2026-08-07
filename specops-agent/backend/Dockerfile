FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir -e ".[agent]"

ENV DATABASE_PATH=/data/specops.db
ENV CHECKPOINT_PATH=/data/specops_checkpoints.db
ENV LLM_PROVIDER=none

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
