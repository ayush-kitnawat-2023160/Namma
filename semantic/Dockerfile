FROM namma-nextgen:latest

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# namma-nextgen already contains the geometry stack we need for the first
# semantic milestones (Open3D, trimesh, scikit-image, scikit-learn, networkx).
# Keep the initial semantic image deliberately minimal; stage-specific packages
# will be added only when that stage is implemented and validated.

COPY semantic/scripts /opt/namma-semantic/scripts
COPY semantic/config /opt/namma-semantic/config

RUN chmod +x /opt/namma-semantic/scripts/*.sh

ENTRYPOINT ["/opt/namma-semantic/scripts/entrypoint.sh"]
