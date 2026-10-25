ARG BASE_IMAGE=namma-iitb:latest
FROM ${BASE_IMAGE}

ARG VGGT_OMEGA_COMMIT=48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6
ARG HLOC_COMMIT=c13273bd0ecc2917a35910fd843712a1c6243193
ARG LIGHTGLUE_COMMIT=eb42fee2d71449efb0aa5c10549752b5d75384d8
ARG GEOCALIB_COMMIT=97b8968e7798a66bf04fcf791fb535624241bda7

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# VGGT-Omega currently pins NumPy < 2. Keep OpenCV on a compatible release as
# well. The base image remains untouched; this only affects namma-nextgen.
RUN python -m pip install --no-cache-dir \
      "numpy==1.26.4" \
      "opencv-python==4.11.0.86" \
      "pycolmap>=3.10" \
      "open3d==0.19.0" \
      h5py scipy scikit-image trimesh

RUN git clone https://github.com/facebookresearch/vggt-omega.git /opt/vggt-omega \
    && cd /opt/vggt-omega \
    && git checkout "${VGGT_OMEGA_COMMIT}" \
    && python -m pip install --no-cache-dir -e ".[export]"

RUN git clone https://github.com/cvg/LightGlue.git /opt/LightGlue \
    && cd /opt/LightGlue \
    && git checkout "${LIGHTGLUE_COMMIT}" \
    && python -m pip install --no-cache-dir -e .

RUN git clone https://github.com/cvg/Hierarchical-Localization.git /opt/Hierarchical-Localization \
    && cd /opt/Hierarchical-Localization \
    && git checkout "${HLOC_COMMIT}" \
    && git submodule update --init third_party/SuperGluePretrainedNetwork \
    && python -m pip install --no-cache-dir -e .

RUN git clone https://github.com/cvg/GeoCalib.git /opt/GeoCalib \
    && cd /opt/GeoCalib \
    && git checkout "${GEOCALIB_COMMIT}" \
    && python -m pip install --no-cache-dir -e .

# Download the non-gated calibration and sparse-feature weights at build time.
# This keeps the remote GPU run deterministic and avoids surprise downloads in
# the middle of a reconstruction. VGGT-Omega itself remains a gated checkpoint
# and is mounted separately by the user.
RUN python - <<'PY'
from geocalib import GeoCalib
from hloc import extract_features, match_features, extractors, matchers
from hloc.utils.base_model import dynamic_load

GeoCalib(weights="distorted")

sp_conf = extract_features.confs["superpoint_max"]["model"]
sp_cls = dynamic_load(extractors, sp_conf["name"])
sp_cls(sp_conf)

lg_conf = match_features.confs["superpoint+lightglue"]["model"]
lg_cls = dynamic_load(matchers, lg_conf["name"])
lg_cls(lg_conf)
print("Cached GeoCalib, SuperPoint and LightGlue weights")
PY

COPY nextgen/scripts /opt/namma-nextgen/scripts
COPY nextgen/config /opt/namma-nextgen/config
RUN chmod +x /opt/namma-nextgen/scripts/*.sh

ENV PYTHONPATH=/opt/vggt-omega:/opt/LightGlue:/opt/Hierarchical-Localization:/opt/GeoCalib:/opt/MASt3R-SLAM:/opt/gaussian-splatting

WORKDIR /workspace
ENTRYPOINT ["/opt/namma-nextgen/scripts/entrypoint.sh"]
CMD ["help"]
