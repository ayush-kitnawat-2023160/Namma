FROM nvidia/cuda:12.1.1-devel-ubuntu22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG MAST3R_SLAM_COMMIT=e6f4e3d474fad0e11f561482012be864ba8c3f17
ARG SPLATSLAM_COMMIT=27454f58f27288cb3be828ec089521e62fab7b06
ARG GAUSSIAN_SPLATTING_COMMIT=54c035f7834b564019656c3e3fcc3646292f727d

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    CUDA_HOME=/usr/local/cuda \
    PATH=/opt/conda/envs/namma/bin:/opt/conda/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TORCH_CUDA_ARCH_LIST="7.5;8.6;8.9+PTX" \
    PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True,max_split_size_mb:128" \
    MALLOC_ARENA_MAX=4

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    cmake \
    curl \
    ffmpeg \
    git \
    git-lfs \
    libegl1 \
    libgl1 \
    libglib2.0-0 \
    ninja-build \
    pkg-config \
    unzip \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Fully isolated Python; the host never needs Python/Conda/CUDA toolkit.
RUN curl -fsSL --retry 5 --retry-delay 2 \
      https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
      -o /tmp/miniforge.sh \
    && bash /tmp/miniforge.sh -b -p /opt/conda \
    && rm /tmp/miniforge.sh \
    && /opt/conda/bin/conda create -y -n namma --override-channels -c conda-forge python=3.11 pip \
    && /opt/conda/bin/conda clean -afy

RUN python -m pip install --upgrade pip setuptools==70.0.0 wheel ninja \
    && python -m pip install \
      torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 \
      --index-url https://download.pytorch.org/whl/cu121

WORKDIR /opt

# -----------------------------------------------------------------------------
# MASt3R-SLAM
# -----------------------------------------------------------------------------
RUN git clone --recursive https://github.com/rmurai0610/MASt3R-SLAM.git /opt/MASt3R-SLAM \
    && cd /opt/MASt3R-SLAM \
    && git checkout "${MAST3R_SLAM_COMMIT}" \
    && git submodule sync --recursive \
    && git submodule update --init --recursive

# Two small build/runtime patches:
# 1) upstream setup.py checks torch.cuda.is_available() while building. A normal
#    Docker build does not expose a GPU, even though nvcc exists, so compile
#    against CUDA_HOME instead;
# 2) this deployment is always headless over SSH. Upstream imports its OpenGL
#    viewer unconditionally, which pulls an old pyimgui fork. Lazy-import it only
#    if visualization is actually requested. Our runner always passes --no-viz.
RUN cd /opt/MASt3R-SLAM \
    && python - <<'PY'
from pathlib import Path

p = Path('setup.py')
s = p.read_text()
s = s.replace(
    'from torch.utils.cpp_extension import BuildExtension, CppExtension',
    'from torch.utils.cpp_extension import BuildExtension, CppExtension, CUDA_HOME',
)
s = s.replace('has_cuda = torch.cuda.is_available()', 'has_cuda = CUDA_HOME is not None')
needle = '        "-gencode=arch=compute_86,code=sm_86",\n'
if 'compute_89' not in s and needle in s:
    s = s.replace(
        needle,
        needle
        + '        "-gencode=arch=compute_89,code=sm_89",\n'
        + '        "-gencode=arch=compute_89,code=compute_89",\n',
    )
p.write_text(s)

p = Path('main.py')
s = p.read_text()
s = s.replace('import argparse\n', 'import argparse\nimport dataclasses\n')
s = s.replace(
    'from mast3r_slam.visualization import WindowMsg, run_visualization\n',
    '@dataclasses.dataclass\n'
    'class WindowMsg:\n'
    '    is_terminated: bool = False\n'
    '    is_paused: bool = False\n'
    '    next: bool = False\n'
    '    C_conf_threshold: float = 1.5\n\n',
)
s = s.replace(
    '    if not args.no_viz:\n        viz = mp.Process(',
    '    if not args.no_viz:\n        from mast3r_slam.visualization import run_visualization\n        viz = mp.Process(',
)
p.write_text(s)
PY

# MASt3R's curope local dependency imports torch while building, hence
# --no-build-isolation is required. in3d is deliberately not installed because
# its only use in MASt3R-SLAM is the disabled GUI viewer path.
RUN cd /opt/MASt3R-SLAM \
    && MAX_JOBS=8 python -m pip install --no-build-isolation -e thirdparty/mast3r \
    && MAX_JOBS=8 python -m pip install --no-build-isolation -e .

# Bake checkpoints into the image so inference is repeatable/offline after build.
RUN mkdir -p /opt/MASt3R-SLAM/checkpoints \
    && cd /opt/MASt3R-SLAM/checkpoints \
    && wget --continue --tries=5 --timeout=60 \
      https://download.europe.naverlabs.com/ComputerVision/MASt3R/MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric.pth \
    && wget --continue --tries=5 --timeout=60 \
      https://download.europe.naverlabs.com/ComputerVision/MASt3R/MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric_retrieval_trainingfree.pth \
    && wget --continue --tries=5 --timeout=60 \
      https://download.europe.naverlabs.com/ComputerVision/MASt3R/MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric_retrieval_codebook.pkl \
    && test -s MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric.pth \
    && test -s MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric_retrieval_trainingfree.pth \
    && test -s MASt3R_ViTLarge_BaseDecoder_512_catmlpdpt_metric_retrieval_codebook.pkl

# -----------------------------------------------------------------------------
# SplatSLAM bridge: MASt3R-SLAM -> COLMAP-style 3DGS input
# -----------------------------------------------------------------------------
RUN export GIT_LFS_SKIP_SMUDGE=1 \
    && git clone https://github.com/jessielijc/SplatSLAM.git /opt/SplatSLAM \
    && cd /opt/SplatSLAM \
    && git checkout "${SPLATSLAM_COMMIT}" \
    && python -m pip install -r requirements.txt

# -----------------------------------------------------------------------------
# Official Graphdeco 3D Gaussian Splatting - training path only, no SIBR viewer
# -----------------------------------------------------------------------------
RUN git clone https://github.com/graphdeco-inria/gaussian-splatting.git /opt/gaussian-splatting \
    && cd /opt/gaussian-splatting \
    && git checkout "${GAUSSIAN_SPLATTING_COMMIT}" \
    && git submodule sync --recursive \
    && git submodule update --init --recursive \
      submodules/diff-gaussian-rasterization \
      submodules/simple-knn \
      submodules/fused-ssim \
    && python -m pip install plyfile tqdm joblib opencv-python \
    && MAX_JOBS=8 python -m pip install --no-build-isolation ./submodules/diff-gaussian-rasterization \
    && MAX_JOBS=8 python -m pip install --no-build-isolation ./submodules/simple-knn \
    && MAX_JOBS=8 python -m pip install --no-build-isolation ./submodules/fused-ssim


# Patch MASt3R timestamps for subsampled video
RUN python - <<'PY2'
from pathlib import Path

p = Path("/opt/MASt3R-SLAM/mast3r_slam/dataloader.py")
s = p.read_text()

old = "timestamp = idx / self.fps"
new = "timestamp = (idx * self.stride) / self.fps"

if old in s:
    s = s.replace(old, new)
    p.write_text(s)
    print("Patched MASt3R video timestamp handling")
elif new in s:
    print("Timestamp patch already applied")
else:
    raise RuntimeError(
        "Expected timestamp line not found. "
        "Inspect /opt/MASt3R-SLAM/mast3r_slam/dataloader.py"
    )
PY2

COPY scripts /opt/namma/scripts
COPY config /opt/namma/config
RUN chmod +x /opt/namma/scripts/*.sh

ENV PYTHONPATH=/opt/MASt3R-SLAM:/opt/SplatSLAM:/opt/gaussian-splatting
WORKDIR /workspace
ENTRYPOINT ["/opt/namma/scripts/entrypoint.sh"]
CMD ["help"]

