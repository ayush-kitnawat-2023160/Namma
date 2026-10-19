FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG PANOVGGT_COMMIT=556bb7d2ec2d02bd3ee4ed535542e74290ba22cf
ARG SPHR_COMMIT=9ba9424face778da660ea5e0283af162d1698b4f

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git wget ffmpeg jq tini \
      python3 python3-pip python3-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python3 -m pip install --no-cache-dir \
      torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 xformers==0.0.28.post3 \
      --index-url https://download.pytorch.org/whl/cu124

RUN git clone https://github.com/YijingGuo-June/PanoVGGT.git /opt/PanoVGGT \
    && cd /opt/PanoVGGT \
    && git checkout "${PANOVGGT_COMMIT}" \
    # Upstream currently hard-pins its own torch/torchvision versions. Keep a
    # single CUDA stack in this image instead of downloading/replacing PyTorch
    # a second time. All other upstream requirements remain authoritative. \
    && sed -E '/^(torch|torchvision|torchaudio|xformers)([<>=~! ].*)?$/d' requirements.txt \
         > /tmp/panovggt-requirements.txt \
    && cat /tmp/panovggt-requirements.txt \
    && python3 -m pip install --no-cache-dir -r /tmp/panovggt-requirements.txt \
    && python3 -m pip check

# PanoVGGT's aggregator uses torch.hub for its DINOv2 ViT-L/14 encoder. Cache
# both the repo snapshot and pretrained weight in the image so inference never
# performs a surprise ~1.1 GB download on the shared GPU server.
ENV TORCH_HOME=/opt/torch_cache
RUN python3 - <<'PY'
import torch

model = torch.hub.load("facebookresearch/dinov2", "dinov2_vitl14")
print("Cached PanoVGGT DINOv2 backbone:", type(model).__name__)
del model
PY

RUN python3 -m pip install --no-cache-dir \
      "huggingface_hub>=0.27" hf_xet \
      numpy scipy pillow pyyaml psutil

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version \
    && git clone https://github.com/lukehollis/sphr.git /opt/sphr \
    && cd /opt/sphr \
    && git checkout "${SPHR_COMMIT}" \
    && npm ci \
    && npm run typecheck \
    && npm run build

COPY panograph/scripts /opt/namma-panograph/scripts
COPY panograph/config /opt/namma-panograph/config
RUN chmod +x /opt/namma-panograph/scripts/*.sh /opt/namma-panograph/scripts/*.py

ENV PYTHONPATH=/opt/PanoVGGT
ENV TORCH_HOME=/opt/torch_cache
ENV HF_HOME=/workspace/hf_cache
ENV HUGGINGFACE_HUB_CACHE=/workspace/hf_cache/hub
ENV TOKENIZERS_PARALLELISM=false
ENV PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128

WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/namma-panograph/scripts/entrypoint.sh"]
CMD ["help"]
