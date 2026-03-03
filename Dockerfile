FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer-cached until requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY templates/ templates/

# Create the default upload directory
RUN mkdir -p /data/uploads

ENV UPLOAD_DIR=/data/uploads \
    AUTH_USERNAME=admin \
    AUTH_PASSWORD=changeme

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
