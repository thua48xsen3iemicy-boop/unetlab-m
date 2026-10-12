FROM debian:bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/runner

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        genisoimage \
        iproute2 \
        mtools \
        python3 \
        qemu-system-x86 \
        qemu-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY runner/ /app/runner/
COPY profiles/ /app/profiles/
COPY assets/ /app/assets/
COPY LICENSE /app/LICENSE
RUN mkdir -p /run/puskalka

EXPOSE 8080
STOPSIGNAL SIGTERM
ENTRYPOINT ["python3", "-m", "puskalka"]
CMD ["run", "--ticket", "/ticket/ticket.json", "--listen", "0.0.0.0", "--port", "8080"]
