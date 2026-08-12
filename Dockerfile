FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY .env* ./
COPY service_account.json* ./
COPY *.py ./
RUN mkdir -p logs
CMD ["python", "-u", "main.py"]