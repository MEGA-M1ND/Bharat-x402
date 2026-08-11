FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ledger lives on a mounted volume in compose; create the mountpoint anyway so
# a plain `docker run` still works.
RUN mkdir -p /data

EXPOSE 8402
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8402"]
