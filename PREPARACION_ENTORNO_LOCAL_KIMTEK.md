# Preparación y recuperación del entorno local KIMTEK

Guía técnica conservada porque `INICIAR_KIMTEK.bat` la referencia y porque contiene procedimientos necesarios de mantenimiento. La descripción vigente del sistema está en [README.md](README.md). Esta guía reemplaza el antiguo informe histórico; sus cifras y pendientes de etapas no deben usarse como diagnóstico actual.

## Arranque habitual

No reconstruir ni importar datos para iniciar el sistema. Instalar las dependencias, disponer de la base privada autorizada `instance/kimtek.sqlite3` y ejecutar `INICIAR_KIMTEK.bat`. El servicio local utiliza `http://127.0.0.1:8000`.

El lanzador fija `ASISTENCIA_DB` y desactiva `AUTO_CLOSE`. El código aplica migraciones pendientes al arrancar; mantener una copia verificada antes de actualizar versiones. La aplicación no lee `.env` automáticamente.

## Recuperar el respaldo completo

1. Detener la instancia correspondiente del servidor.
2. Conservar por separado cualquier trabajo posterior al respaldo.
3. Verificar el SHA-256 del ZIP contra `manifest.json` del respaldo privado.
4. Extraer `proyecto_completo.zip` en una carpeta de recuperación separada. Incluye los archivos ocultos de Git, el código, las bases, los archivos ignorados y el entorno local.
5. Usar conjuntamente el código y la base del mismo respaldo. Las copias `*_consistent.sqlite3` adicionales fueron obtenidas mediante la API de respaldo SQLite y verificadas con `PRAGMA integrity_check`.
6. Recrear `.venv` si cambió la ruta o el equipo: los entornos virtuales no son portables. Instalar los requisitos y establecer el secreto privado de sesión.
7. Comprobar integridad y acceso sobre la copia restaurada antes de adoptarla como entorno operativo.

El manifiesto enumera los archivos originales con tamaño y SHA-256. La ruta concreta y el resultado de esta preservación se entregan al titular por separado; las bases y el ZIP no se agregan a Git. Conservar otra copia privada en almacenamiento institucional.

## Preparación nueva desde fuentes

`local_setup.py` es un preparador específico del conjunto fuente de octubre de 2026; valida cantidades e identidades exactas. No es una importación genérica ni una recuperación del trabajo posterior.

Requiere tres archivos privados: el biométrico de octubre y los reportes detallado y resumido de RR. HH. Las variables siguientes deben establecerse con las rutas autorizadas de esos archivos:

```powershell
$biometrico = 'RUTA_PRIVADA_DEL_BIOMETRICO.xlsx'
$detallado = 'RUTA_PRIVADA_DEL_REPORTE_DETALLADO.xlsx'
$resumido = 'RUTA_PRIVADA_DEL_REPORTE_RESUMIDO.xlsx'
$destinoLocal = Join-Path (Get-Location) 'instance\kimtek.sqlite3'
.\.venv\Scripts\python.exe local_setup.py --dry-run --biometric "$biometrico" --detailed "$detallado" --summary "$resumido"
```

El modo predeterminado es de inspección. Para aplicar en una instalación nueva, revisar el resultado y confirmar la ruta absoluta exacta:

```powershell
.\.venv\Scripts\python.exe local_setup.py --apply --confirm-target "$destinoLocal" --biometric "$biometrico" --detailed "$detallado" --summary "$resumido"
```

Si la base ya fue preparada con las mismas fuentes, devuelve `already_prepared` sin regenerarla. El único destino admitido es el del proyecto. No ejecutar `--rebuild` para arrancar: sustituye el estado activo por una preparación nueva y retira de él el trabajo posterior, aunque guarda una copia previa.

## Restauración con el preparador

Con el servidor detenido y las fuentes originales disponibles:

```powershell
.\.venv\Scripts\python.exe local_setup.py --apply --confirm-target "$destinoLocal" --restore 'RUTA_ABSOLUTA_DEL_RESPALDO_DENTRO_DE_instance_backups' --biometric "$biometrico" --detailed "$detallado" --summary "$resumido"
```

El preparador valida las fuentes antes de restaurar. Solo acepta respaldos íntegros dentro de `instance/backups` que contengan metadatos `local_preparation`, y guarda primero el estado actual. Un respaldo externo completo se recupera con el procedimiento del ZIP, no pasándolo directamente a este comando.

Para administrar acceso, utilizar `local_credentials.py --list` y `--code CODIGO_EXISTENTE`, que solicita la clave sin mostrarla. Ese comando modifica la contraseña; no ejecutarlo como prueba de arranque.
