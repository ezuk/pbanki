FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb xauth libx11-6 libxext6 libxrandr2 libxcursor1 libxi6 libxss1 libxfixes3 \
    libxkbcommon0 libwayland-client0 libegl1 libgl1 libdbus-1-3 libudev1 locales ca-certificates \
    && sed -i 's/# en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen && locale-gen \
    && rm -rf /var/lib/apt/lists/*
ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
