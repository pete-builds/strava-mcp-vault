FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -r -m appuser && \
    mkdir -p /app/data && \
    chown appuser:appuser /app/data
USER appuser
EXPOSE 18201
CMD ["python", "server.py"]
