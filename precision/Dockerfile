ARG BASE_IMAGE=namma-nextgen:latest
FROM ${BASE_IMAGE}

# A pip pycolmap wheel alone does not provide CUDA PatchMatch stereo.
# Build the matching CLI explicitly, for the RTX 3090's sm_86 architecture.
ARG COLMAP_COMMIT=682ea9ac4020a143047758739259b3ff04dabe8d
# Last HLoc revision before its PyCOLMAP 3.12 rig/frame migration. The inherited
# NextGen HLoc requires >=3.13 and cannot use our deliberately pinned 3.11 model.
ARG PRECISION_HLOC_COMMIT=3bdf494c852f157db57a1cf2039a6c826d52e702
ARG BUILD_JOBS=2
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      libboost-program-options-dev libboost-graph-dev libboost-system-dev \
      libeigen3-dev libflann-dev libfreeimage-dev libmetis-dev \
      libgoogle-glog-dev libgtest-dev libgmock-dev libsqlite3-dev \
      libglew-dev libceres-dev \
    && rm -rf /var/lib/apt/lists/*

# Use the system compiler/dependencies, not Conda's CMake package search paths.
RUN git clone --no-checkout https://github.com/colmap/colmap.git /opt/colmap-src \
    && cd /opt/colmap-src && git checkout ${COLMAP_COMMIT} \
    && /usr/bin/cmake -S . -B build -GNinja \
       -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES=86 \
       -DCMAKE_INSTALL_PREFIX=/usr/local -DGUI_ENABLED=OFF \
       -DCUDA_ENABLED=ON -DCGAL_ENABLED=OFF -DTESTS_ENABLED=OFF \
    && /usr/bin/cmake --build build --parallel ${BUILD_JOBS} \
    && /usr/bin/cmake --install build && ldconfig \
    && rm -rf /opt/colmap-src

RUN git -C /opt/Hierarchical-Localization checkout "${PRECISION_HLOC_COMMIT}" \
    && python -m pip install --no-cache-dir --no-deps -e /opt/Hierarchical-Localization \
    && python -m pip install --no-cache-dir 'pycolmap==3.11.1' \
    && colmap -h \
    && colmap patch_match_stereo -h 2>&1 | tee /tmp/colmap-mvs-help.txt \
    && grep -q PatchMatchStereo.geom_consistency /tmp/colmap-mvs-help.txt \
    && rm /tmp/colmap-mvs-help.txt

COPY precision/scripts /opt/namma-precision/scripts
RUN chmod +x /opt/namma-precision/scripts/*.sh \
    && python /opt/namma-precision/scripts/sfm_compatibility.py
ENV OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 \
    QT_QPA_PLATFORM=offscreen
WORKDIR /workspace
ENTRYPOINT ["/opt/namma-precision/scripts/entrypoint.sh"]
CMD ["help"]
