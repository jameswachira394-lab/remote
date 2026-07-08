FROM python:3.12-slim
RUN python.main
WORKDIR /remote

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]