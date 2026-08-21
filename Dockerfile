FROM python:3.12-slim

WORKDIR /app

# Set default timezone fallback
ENV TZ=America/Los_Angeles

# Install system dependencies, tzdata, Git, and GitHub CLI (gh)
RUN apt-get update && apt-get install -y \
    curl \
    git \
    tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Pre-install Python dependencies for fast boot
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Copy runner entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Bake private credentials and config into persistent /root/config/
RUN mkdir -p /root/config
COPY .env* service_account.json* /root/config/

# Install Google Pro session into /root/.gemini if synced during build
COPY .gemini_auth* /tmp/gemini_auth/
RUN if [ -d "/tmp/gemini_auth" ] && [ "$(ls -A /tmp/gemini_auth 2>/dev/null)" ]; then \
        mkdir -p /root/.gemini && cp -R /tmp/gemini_auth/* /root/.gemini/ && rm -rf /tmp/gemini_auth; \
    fi

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-u", "main.py"]