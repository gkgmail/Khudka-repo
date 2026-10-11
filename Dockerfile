FROM python:3.10-slim

# Koyeb console par real-time live logs dekhne ke liye buffer disable
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# FFmpeg aur Pyrogram MTProto speed acceleration ke liye build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    python3-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Layer caching optimize karne ke liye pehle dependencies install karein
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Baaki project files copy karein
COPY . .

# Koyeb health check web server port
EXPOSE 8080

# Unbuffered mode me main script execute karein
CMD ["python", "-u", "main.py"]
