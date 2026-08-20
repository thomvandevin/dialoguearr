FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY normalize.py .

# Must match PUID/PGID so rewritten media keeps its homelab:media ownership.
USER 1000:999

CMD ["python", "-u", "normalize.py"]
