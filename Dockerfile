FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy every module; an explicit list silently omits new ones.
COPY *.py ./
COPY templates ./templates

# Must match the media owner so rewritten files keep their uid:gid.
USER 1000:999

ARG VERSION=dev
ENV DIALOGUEARR_VERSION=$VERSION

EXPOSE 8080
CMD ["python", "-u", "main.py"]
