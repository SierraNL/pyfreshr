FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		build-essential \
		gcc \
		git \
		curl \
		wget \
		ca-certificates \
		pkg-config \
		libxml2-dev \
		libxslt1-dev \
		zlib1g-dev \
		libffi-dev \
		libssl-dev \
		sudo \
	&& mkdir -p /etc/apt/keyrings \
	&& curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
	&& chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
	&& echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
	&& apt-get update \
	&& apt-get install -y --no-install-recommends gh \
	&& rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip setuptools wheel

# Create non-root user for development
ARG USERNAME=vscode
ARG USER_UID=1000
ARG USER_GID=1000

RUN groupadd --gid ${USER_GID} ${USERNAME} \
	&& useradd -m --uid ${USER_UID} --gid ${USER_GID} -s /bin/bash ${USERNAME} \
	&& echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME} \
	&& chmod 0440 /etc/sudoers.d/${USERNAME}

WORKDIR /workspace

# Copy requirements first to leverage Docker layer caching
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# Copy the rest of the repository
COPY . .

# Ensure the non-root user owns the workspace and switch to it
RUN chown -R ${USERNAME}:${USERNAME} /workspace
USER ${USERNAME}