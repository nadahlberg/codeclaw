# ClawCode Agent Container
# Runs Claude Agent SDK (Python) in isolated Linux VM with GitHub integration

FROM node:22-slim

# Install system dependencies for Chromium, GitHub CLI, and Python
RUN apt-get update && apt-get install -y \
    chromium \
    fonts-liberation \
    fonts-noto-color-emoji \
    libgbm1 \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libasound2 \
    libpangocairo-1.0-0 \
    libcups2 \
    libdrm2 \
    libxshmfence1 \
    curl \
    git \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Git config for bot identity
RUN git config --system user.name "ClawCode AI" \
    && git config --system user.email "clawcode[bot]@users.noreply.github.com"

# Set Chromium path for agent-browser
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

# Install agent-browser and claude-code globally (SDK requires the CLI)
RUN npm install -g agent-browser @anthropic-ai/claude-code

# Create app directory
WORKDIR /app

# Copy requirements first for better caching
COPY agent_runner/requirements.txt ./requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# Copy Python source code
COPY agent_runner/ ./

# Make source read-only (agents cannot modify the runner)
RUN chmod -R a-w /app/*.py

# Create workspace directories
RUN mkdir -p /workspace/group /workspace/global /workspace/extra /workspace/repo /workspace/ipc/messages /workspace/ipc/tasks /workspace/ipc/input \
    /workspace/store /workspace/data /workspace/groups

# Set ownership to node user (non-root) for writable directories
RUN chown -R node:node /workspace && chmod 777 /home/node

# Switch to non-root user (required for --dangerously-skip-permissions)
USER node

# Set working directory to group workspace
WORKDIR /workspace/group

# Entry point: secrets arrive via stdin JSON, piped directly to python (never written to disk)
# Follow-up messages arrive via IPC files in /workspace/ipc/input/
ENTRYPOINT ["python3", "/app/main.py"]
