# GestiónPersonal KIMTEK

Sistema local de control de asistencia y boletas de permiso. Centraliza marcaciones, solicitudes, aprobaciones, estructura organizacional y reportes para trabajadores, encargados, Recursos Humanos y administración.

## Objetivo y estado

Preservar la evidencia original de asistencia y permitir la gestión trazable de permisos y decisiones. Es un prototipo funcional local; el código lo identifica expresamente como entorno de desarrollo, no como despliegue de producción. No existe conexión configurada con una base oficial de RR. HH.

La versión revisada incluye 16 migraciones. Las responsabilidades y referencias cargadas por `local_setup.py` son provisionales: requieren validación institucional de adscripciones, encargados, vigencias laborales, horarios y calendarios. Una marcación no demuestra por sí sola una jornada completa. No se inició ninguna migración a Laravel.

## Funcionalidades

- Inicio/cierre de sesión, cambio de contraseña y validación CSRF.
- Capacidades por función y alcance, además de roles `employee`, `manager`, `hr` y `admin`.
- Solicitudes de permiso y corrección, aprobación por etapas, historial y evidencia de autoridad.
- Boletas PDF individuales y agrupadas en papel carta.
- Asistencia personal, vista de equipo, personal y configuración organizacional.
- Empresas, sucursales, áreas, encargados, adscripciones y horarios con vigencias.
- Importación de marcaciones CSV/XLSX y archivos biométricos Zlink, previsualización, confirmación y control de duplicados/cobertura.
- Padrón biométrico, enlaces de identidad y referencias de RR. HH.
- Agrupación de marcaciones, conciliación, regímenes, calendario, resoluciones y recálculo incremental.
- Reportes Excel, cierres/reaperturas mensuales, auditoría y notificaciones internas.

## Tecnologías y versiones

Versiones observadas en el entorno revisado; los requisitos declaran rangos, no un bloqueo exacto de versiones.

| Componente | Versión local | Declaración |
|---|---|---|
| Python | 3.12.10 | Sin versión mínima declarada; usar 3.12 para reproducir este entorno |
| FastAPI | 0.162.2 | `>=0.115,<1` |
| Uvicorn | 0.54.0 | `>=0.30,<1` |
| Jinja2 | 3.1.6 | `>=3.1,<4` |
| python-multipart | 0.0.33 | `>=0.0.12,<1` |
| itsdangerous | 2.2.0 | `>=2.2,<3` |
| openpyxl | 3.1.5 | `>=3.1,<4` |
| python-dateutil | 2.9.0.post0 | `>=2.9,<3` |
| ReportLab | 4.5.1 | `>=4,<5` |
| tzdata | 2026.6 | `>=2025.2` |

SQLite 3.49.1 se utiliza mediante `sqlite3` de Python, sin servidor externo ni ORM. Starlette se incorpora como dependencia de FastAPI. Las herramientas de prueba se declaran en `requirements-dev.txt`: pytest, httpx y Beautiful Soup.

## Arquitectura

Aplicación monolítica modular: el navegador envía solicitudes HTTP y formularios a FastAPI; el servidor ejecuta reglas Python, consulta SQLite y renderiza plantillas Jinja2. Las descargas se generan con ReportLab/openpyxl. CSS y JavaScript propios agregan estilo e interacción; no existe una aplicación React/Vue ni un frontend desacoplado.

`core.db()` administra conexiones, transacciones, claves foráneas y procesos de actualización de asistencia. Algunas consultas pueden disparar recálculos; no debe usarse esa función para una inspección estrictamente de solo lectura. `core.initialize()` crea el esquema y aplica migraciones pendientes, con copia previa cuando corresponde. No reconstruye el padrón operativo al arrancar.

Las sesiones están firmadas mediante middleware de Starlette; duran ocho horas y utilizan cookie `asis_session`. Las contraseñas se derivan con scrypt y sal individual. Las políticas de acceso y aprobación incluyen alcance organizacional e historial, no solamente el campo `role`.

La zona horaria es `America/La_Paz`. Hay tareas internas asíncronas de precierre/notificación y cierre automático opcional. No hay un servicio de colas externo configurado.

## Estructura

```text
app.py                         Rutas HTTP, formularios, sesiones y vistas
core.py                        SQLite, reglas comunes, auditoría y cierres
migrations.py                  Migraciones de esquema versionadas
access_control.py              Capacidades y acceso por función
organization.py                Organización, vigencias y autoridad
approval_authority.py          Planes y evidencia de aprobación
attendance_*.py                Motor, agrupación, recálculo y presentación de asistencia
reconciliation.py              Importación biométrica y conciliación
roster.py / hr_reference.py     Identidades y referencias de RR. HH.
documents.py / boleta_layout.py Exportación Excel/PDF y composición de boletas
*_presenter.py / ui_presenters.py Ayudas de presentación
local_setup.py                 Preparación explícita del entorno local
local_credentials.py           Administración interactiva de claves locales
templates/                     Plantillas Jinja2
static/                        CSS, JavaScript y logotipo utilizado
tests/                         Pruebas y datos sintéticos
tools/visual_stage3b.py        Revisión visual sobre copia temporal
verify_grouping_october.py      Comprobación específica del archivo fuente de octubre
ejemplos/marcaciones_ejemplo.csv Ejemplo de importación
instance/                      Base, staging y respaldos privados; excluido de Git
INICIAR_KIMTEK.bat              Arranque local en Windows
```

## Base de datos

La base predeterminada es `instance/kimtek.sqlite3`; puede seleccionarse otra ruta con `ASISTENCIA_DB`. El lanzador Windows fija la ruta absoluta de esa base. Cada conexión activa `PRAGMA foreign_keys=ON` y espera hasta 15 segundos ante bloqueo.

El esquema incluye usuarios, estructura organizacional, marcaciones originales, solicitudes y decisiones, vigencias, resultados de conciliación, versiones diarias, cierres, notificaciones, auditoría y `schema_migrations`. No ejecutar migraciones de otro framework sobre esta base sin analizar previamente su esquema.

Los datos reales, credenciales, archivos importados y respaldos no se distribuyen por Git. Clonar el repositorio no entrega los registros operativos: para reproducir el sistema con sus datos se necesita una copia privada autorizada de la base y, para algunas verificaciones, los archivos fuente originales.

## Instalación y ejecución local

Desde la raíz del proyecto, en PowerShell con Python 3.12:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Con el servidor detenido, colocar una copia privada verificada de la base actual en `instance/kimtek.sqlite3`. No reemplazar una base existente para instalar dependencias. Consultar [la guía técnica de preparación y recuperación](PREPARACION_ENTORNO_LOCAL_KIMTEK.md) si se necesita preparar una instalación nueva a partir de los archivos fuente.

Establecer un secreto persistente de sesión mediante un mecanismo privado de entorno. La aplicación no carga archivos `.env` automáticamente. No escribir claves en Git ni en scripts compartidos.

```powershell
.\INICIAR_KIMTEK.bat
```

Abrir `http://127.0.0.1:8000/login`. Alternativamente, con la base y el entorno elegidos:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

El comando directo puede inicializar una base vacía si no existe: eso no recupera cuentas ni datos reales. El lanzador comprueba primero que exista el archivo de base.

Para listar cuentas locales y establecer una clave de forma interactiva:

```powershell
.\.venv\Scripts\python.exe local_credentials.py --list
.\.venv\Scripts\python.exe local_credentials.py --code OP_RRHH
```

Usar un código existente de la lista. El segundo comando cambia la contraseña de esa cuenta; no es necesario si ya tiene acceso. No hay contraseñas operativas publicadas. Las claves literales en fixtures y herramientas aisladas son exclusivamente sintéticas y no habilitan acceso a la base operativa.

## Variables de entorno

| Variable | Uso real |
|---|---|
| `ASISTENCIA_DB` | Ruta SQLite; predeterminado `instance/kimtek.sqlite3` dentro del proyecto |
| `SESSION_SECRET` | Secreto privado de firma; si falta se genera uno por proceso y las sesiones anteriores dejan de ser válidas al reiniciar |
| `HTTPS_ONLY` | `1` exige cookie por HTTPS; el entorno HTTP local usa otro valor |
| `AUTO_CLOSE` | Solo `1` activa la tarea automática de cierre; el lanzador fija `0` |

`.env.example` es una plantilla sin secretos. `INSTITUCION` y las opciones SMTP aparecen allí, pero no son consumidas actualmente por el código. La marca visual se define en el código/plantillas.

## Dependencias externas y mantenimiento

- Google Fonts suministra Inter y Plus Jakarta Sans. Una red sin acceso a esos dominios puede cambiar las tipografías visibles.
- Las importaciones biométricas se hacen mediante archivos; no se encontró conexión directa a un dispositivo ni API de Zlink.
- No hay integración SMTP activa; las notificaciones son internas.
- Los PDF usan ReportLab: el diseño de impresión es independiente de las plantillas HTML.
- No hay configuración productiva de contenedores, proxy institucional o SSO en este proyecto. Su diseño requiere conocer la infraestructura de destino.
- No ejecutar reconstrucciones, restablecimientos de claves ni herramientas de aceptación sobre datos operativos sin comprender sus efectos.

## Verificación

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Las pruebas emplean bases temporales. Parte de la suite necesita archivos privados específicos bajo Downloads: `Biometrico(20261001-20261031).xlsx` y los reportes detallado/resumido dentro de `reporte (35)`. No se incluyen en Git. Sin ellos no se reproduce toda la aceptación.

`verify_grouping_october.py` consulta la base elegida y el Excel original; escribe un resultado bajo `instance/stage2_continuous/`. `tools/visual_stage3b.py` usa una copia temporal, requiere Playwright y su navegador, y genera capturas privadas bajo `instance/`. Esas dependencias visuales son opcionales y no figuran en `requirements-dev.txt`.

## Preservación y revisión técnica

Esta organización conserva el código, las reglas, los recursos y el diseño existentes. La limpieza retira informes históricos del árbol activo; conserva esta guía principal y la guía técnica que referencia el lanzador. El respaldo privado completo anterior a la limpieza mantiene también documentos retirados, historial Git, base y entorno local. Debe custodiarse fuera del repositorio y copiarse a almacenamiento institucional autorizado.

El repositorio contiene referencias de personal incrustadas en código heredado y se mantiene privado por autorización del titular. No hacerlo público sin una revisión de anonimización. Las bases, secretos y fuentes privadas permanecen excluidos.
