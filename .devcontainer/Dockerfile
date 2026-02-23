FROM python:3.12-slim

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
		git \
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