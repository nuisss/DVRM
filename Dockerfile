FROM python:3.12-slim

# ffmpeg (müzik çalma için) ve ses kütüphaneleri için gereken sistem paketleri
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libffi-dev \
    libnacl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
