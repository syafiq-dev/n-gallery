FROM alpine:3.21
RUN apk add --no-cache python3 py3-pillow ffmpeg
WORKDIR /app
COPY app.py index.html ./
ENV DATA_DIR=/data PORT=8088
EXPOSE 8088
CMD ["python3", "-u", "app.py"]
