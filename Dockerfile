FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

EXPOSE 8080

CMD ["gunicorn", "web:app", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8080", "--timeout", "120"]
