FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY assets/ ./assets/
COPY migrations/ ./migrations/
COPY main.py ./

CMD ["python", "main.py"]
