FROM namma-nextgen:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# namma-nextgen already contains the geometry stack we need for the first
# semantic milestones (Open3D, trimesh, scikit-image, scikit-learn, networkx).
# Stage 1 uses the Hugging Face implementations of Mask2Former, Grounding DINO
# and SAM2. This avoids compiling a second custom CUDA extension stack inside
# the already validated NextGen image.
RUN python -m pip install --no-cache-dir \
    transformers==4.56.2 \
    accelerate==1.10.1

COPY semantic/scripts /opt/namma-semantic/scripts
COPY semantic/config /opt/namma-semantic/config

RUN chmod +x /opt/namma-semantic/scripts/*.sh

ENTRYPOINT ["/opt/namma-semantic/scripts/entrypoint.sh"]
