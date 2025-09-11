FROM python:3.13-bullseye

# Define whether we're building a production or a development image
ARG DEVEL=no

# Configure apt to keep downloaded packages for caching
RUN set -eux; \
    rm -f /etc/apt/apt.conf.d/docker-clean; \
    echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache;

# Install system dependencies
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y netcat-traditional postgresql-client

# Set Python environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Create and set working directory
RUN mkdir /code
WORKDIR /code

# Copy and install Python dependencies
COPY requirements.txt /code/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Copy application code
COPY . /code/
