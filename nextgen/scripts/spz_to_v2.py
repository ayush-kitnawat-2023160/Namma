#!/usr/bin/env python3
"""Convert an SPZ file to legacy version 2 for GaussianSplats3D 0.4.7."""
import gzip
import os
from pathlib import Path
import struct
import sys

import spz

source, destination = map(Path, sys.argv[1:3])
cloud = spz.load_spz(str(source))
options = spz.PackOptions()
options.version = 2
temporary = destination.with_suffix('.v2.tmp')
try:
    if not spz.save_spz(cloud, options, str(temporary)):
        raise RuntimeError('SPZ conversion failed')
    with gzip.open(temporary, 'rb') as stream:
        magic, version, count = struct.unpack('<III', stream.read(12))
    if magic != 0x5053474e or version != 2 or count != cloud.num_points:
        raise ValueError('Invalid converted SPZ header')
    os.replace(temporary, destination)
finally:
    temporary.unlink(missing_ok=True)
