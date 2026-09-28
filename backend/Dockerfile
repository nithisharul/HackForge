FROM python:3.11-slim

WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY backend/requirements.txt backend/requirements.txt
# CPU-only torch keeps the image small (~800MB instead of several GB)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY frontend frontend
COPY scripts scripts
COPY ml_research ml_research

# train models at build time if no trained artifacts were copied in
RUN mkdir -p /srv/data && if [ ! -f backend/artifacts/scaler.json ]; then \
      python scripts/generate_training_data.py && \
      cd backend && python -m app.ml.train --data ../data/train_normal.log; fi

WORKDIR /srv/backend
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
