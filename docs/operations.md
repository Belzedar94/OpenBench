# Operación de la instancia permanente


- **URL única para todo**: https://belzedar.duckdns.org (HTTPS, cert auto-renovado). El server local de la torre (127.0.0.1:8000) está RETIRADO — no lo levantes; su db es solo backup.
- **Workers** (cualquier motor): `python client.py -U <usuario> -P <password> -S https://belzedar.duckdns.org -T <hilos> -N 1` desde openbench-spell/Client. Las cuentas/máquinas existentes migraron intactas.
- **Crear tests**: vía web (login → Create Test / Create Tune / Create Datagen) o POST a `/scripts/` con action=CREATE_TEST como siempre, contra la URL nueva.
- **Datagen distribuido**: contrato del motor y checklist de adopción por variante (Atomic incluido) en `openbench-spell/docs/datagen-mode.md`. El binario del motor debe exponer un comando datagen (referencia: src/datagen.cpp de Spell-Stockfish).
- **Disco del server (40GB)**: los chunks de datagen completados se descargan a la torre y se PURGAN del server tras cada merge. Datasets archivados (p.ej. Atomic #68, 5GB) viven solo en la torre.
- **Cambios en el server**: editar en el repo local openbench-spell (torre), probar en el clon dev (:8001), y desplegar delta por scp + `systemctl restart openbench` (llave SSH: scratchpad sesión 90548ab1; server /opt/openbench en 178.104.66.19).
