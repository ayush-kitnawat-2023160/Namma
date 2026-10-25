FROM nvidia/cuda:11.8.0-devel-ubuntu22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG SUGAR_COMMIT=7c10c4ae4a267dece512f5c7f40ed212a0a2ab44

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates cmake git libgl1 libglib2.0-0 libxext6 libxrender1 \
      ninja-build wget \
    && rm -rf /var/lib/apt/lists/*

RUN wget -q https://repo.anaconda.com/miniconda/Miniconda3-py39_24.11.1-0-Linux-x86_64.sh -O /tmp/miniconda.sh \
    && bash /tmp/miniconda.sh -b -p /opt/conda \
    && rm /tmp/miniconda.sh
ENV PATH=/opt/conda/bin:${PATH}

RUN git clone https://github.com/Anttwo/SuGaR.git /opt/SuGaR \
    && cd /opt/SuGaR \
    && git checkout "${SUGAR_COMMIT}"

# Follow SuGaR's published environment rather than mixing its PyTorch 2.0.1 /
# CUDA 11.8 stack into the NextGen reconstruction image.
RUN cd /opt/SuGaR \
    && conda env create -f environment.yml \
    && conda clean -afy

# Docker build does not expose the runtime GPU.  Pin the target architecture
# for the college RTX 3090 so PyTorch CUDA extensions compile deterministically.
ENV CUDA_HOME=/usr/local/cuda
ENV TORCH_CUDA_ARCH_LIST=8.6
ENV FORCE_CUDA=1

RUN cd /opt/SuGaR/gaussian_splatting/submodules/diff-gaussian-rasterization \
    && conda run -n sugar python -m pip install -e . \
    && cd /opt/SuGaR/gaussian_splatting/submodules/simple-knn \
    && conda run -n sugar python -m pip install -e .

RUN git clone --depth 1 https://github.com/NVlabs/nvdiffrast.git /opt/nvdiffrast \
    && conda run -n sugar python -m pip install /opt/nvdiffrast

WORKDIR /opt/SuGaR
CMD ["/bin/bash"]
