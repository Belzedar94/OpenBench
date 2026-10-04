#!/opt/openbench/.venv/bin/python
"""Manifiesto de chunks COMPLETED para el archivador de la torre.
Salida: lineas "ruta_relativa|sha256|bytes" de ficheros presentes en Media."""
import os, sys, django
sys.path.insert(0, "/opt/openbench")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "OpenSite.settings")
django.setup()
from OpenBench.models import DatagenChunk
for c in DatagenChunk.objects.filter(status="COMPLETED").select_related("test"):
    rel = f"datagen/{c.test_id}/chunk_{c.idx}.bz2"
    full = os.path.join("/opt/openbench/Media", rel)
    if os.path.exists(full):
        print(f"{rel}|{c.sha256}|{c.bytes}")
