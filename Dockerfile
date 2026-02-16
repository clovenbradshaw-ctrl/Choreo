FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY choreo_runtime.py .

RUN mkdir -p /data/instances

ENV CHOREO_PORT=8420
ENV CHOREO_DIR=/data/instances

EXPOSE 8420

VOLUME ["/data/instances"]

CMD ["python3", "choreo_runtime.py"]
