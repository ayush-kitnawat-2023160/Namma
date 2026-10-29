FROM nvidia/cuda:11.8.0-cudnn8-devel-ubuntu22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG PANSPLAT_COMMIT=05d134b

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git wget ffmpeg tini \
      python3 python3-pip python3-dev build-essential ninja-build \
      libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel \
 && python3 -m pip install --no-cache-dir \
      torch==2.4.0+cu118 torchvision==0.19.0+cu118 torchaudio==2.4.0+cu118 \
      --index-url https://download.pytorch.org/whl/cu118 \
 && python3 -m pip install --no-cache-dir \
      xformers==0.0.27.post2+cu118 \
      --index-url https://download.pytorch.org/whl/cu118

RUN git clone https://github.com/chengzhag/PanSplat.git /opt/PanSplat \
 && cd /opt/PanSplat \
 && git checkout "${PANSPLAT_COMMIT}" \
 && python3 -m pip install --no-cache-dir -r requirements.txt \
 && python3 -m pip install --no-cache-dir git+https://github.com/dcharatan/diff-gaussian-rasterization-modified \
 && python3 -m pip check

COPY pansplat/scripts /opt/namma-pansplat/scripts
COPY pansplat/config /opt/namma-pansplat/config
RUN chmod +x /opt/namma-pansplat/scripts/*.py /opt/namma-pansplat/scripts/*.sh

ENV PYTHONPATH=/opt/PanSplat
ENV TOKENIZERS_PARALLELISM=false
ENV PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128
ENV WANDB_MODE=disabled

WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/namma-pansplat/scripts/entrypoint.sh"]
CMD ["help"]
