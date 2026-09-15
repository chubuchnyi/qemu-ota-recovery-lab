FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bc \
        bison \
        build-essential \
        ca-certificates \
        cpio \
        cryptsetup-bin \
        curl \
        file \
        flex \
        git \
        libelf-dev \
        libncurses-dev \
        libssl-dev \
        locales \
        openssl \
        patch \
        perl \
        python3 \
        python3-pip \
        rsync \
        shellcheck \
        unzip \
        wget \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
