# Actualización segura de SQLite — Gold Engine Fleet

El 13 de octubre se instaló un intérprete oficial y un entorno de validación nuevos, conservando `.venv`. El **14 de octubre de 2026** se actualizó `INICIAR_WINDOWS.bat` para utilizar `.venv-sqlite-fixed` y comprobar las versiones antes de Uvicorn, sin instalaciones automáticas. El arranque del `.bat` se verificó con una base temporal; no se reinició la aplicación real, no se modificó `fleet.db` ni se cambiaron `server.py`, `static/app.js` o `requirements.txt`.

## Versiones comprobadas

| Entorno | Python real | SQLite real | Estado WAL-reset |
|---|---|---|---|
| `.venv` anterior | 3.13.0, Windows x64 | 3.45.3 | Afectado |
| `.venv-sqlite-fixed` candidato | 3.15.0, Windows x64 | 3.53.4 | Corrección incorporada |

La comprobación se realizó con `sys.version`, `sys.executable`, `sqlite3.sqlite_version` y `SELECT sqlite_version(), sqlite_source_id()`. El binding nuevo se carga desde `C:\Users\PERSONAL\AppData\Local\Programs\Python\GoldEngineFleet-Python3150\DLLs\_sqlite3.pyd`. SQLite devuelve el identificador de fuente `2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc`.

SQLite documenta la corrección desde **3.51.3**, con backports **3.44.6** y **3.50.7** en sus respectivas series. La versión 3.52.0 fue retirada y no se selecciona. Los instaladores Windows de Python 3.13.16 y 3.14.8 todavía declaran SQLite 3.50.4, por lo que no resuelven este riesgo. [SQLite WAL-reset](https://www.sqlite.org/wal.html#walreset), [historial oficial de SQLite](https://www.sqlite.org/changes.html), [CPython 3.13.16](https://raw.githubusercontent.com/python/cpython/v3.13.16/PCbuild/python.props), [CPython 3.14.8](https://raw.githubusercontent.com/python/cpython/v3.14.8/PCbuild/python.props).

Python **3.15.0**, publicado como estable el 9 de octubre de 2026, incluye SQLite **3.53.4** en su compilación Windows. Se verificaron el tag exacto y el SBOM del instalador; después se confirmó la biblioteca realmente cargada. [Release oficial](https://www.python.org/downloads/release/python-3150/), [configuración de compilación](https://raw.githubusercontent.com/python/cpython/v3.15.0/PCbuild/python.props), [SBOM oficial](https://www.python.org/ftp/python/3.15.0/python-3.15.0-amd64.exe.spdx.json).

## Instalación realizada

El instalador `python-3.15.0-amd64.exe` se descargó de [python.org](https://www.python.org/ftp/python/3.15.0/python-3.15.0-amd64.exe). Se comprobó el SHA-256 oficial **67d9a63eb145a34aa939f02eadbf8c57f21fcdd6decb901ccac578d2d1f01d6f** y una firma Authenticode válida de **Python Software Foundation** antes de ejecutarlo.

El nuevo intérprete se instaló por usuario en `C:\Users\PERSONAL\AppData\Local\Programs\Python\GoldEngineFleet-Python3150`, separado de `Python313`. Las opciones desactivaron cambios en PATH, asociaciones, accesos directos e instalación del launcher. El instalador y sus registros quedan en `%LOCALAPPDATA%\GoldEngineFleet\RuntimeDownloads`. [Opciones oficiales de instalación Windows](https://docs.python.org/3.15/using/windows.html#installing-without-ui).

Se creó `.venv-sqlite-fixed` con ese intérprete, sin paquetes del sistema ni `PYTHONPATH` externo. Las 14 dependencias de la aplicación mantienen sus versiones anteriores: FastAPI 0.143.0, Pydantic 2.14.1 y Uvicorn 0.54.0, entre otras. Se instalaron pytest 9.1.1 y httpx 0.28.1 para ejecutar las pruebas. Todos los paquetes se instalaron mediante wheels compatibles; `pip check` aprobó.

`requirements-sqlite-fixed.lock` fija las dependencias del entorno probado. SQLite forma parte del intérprete oficial: **actualizar pip o instalar paquetes no cambia esta biblioteca**. No se reemplazaron DLL manualmente ni se instaló otro binding SQLite.

Para reproducir el candidato con el intérprete oficial ya instalado, utiliza una carpeta nueva. Estos comandos rechazan una carpeta existente; no se deben ejecutar sobre `.venv` ni sobre el candidato actual:

```powershell
$fleetPython315 = Join-Path $env:LOCALAPPDATA 'Programs\Python\GoldEngineFleet-Python3150\python.exe'
$fleetCandidateRebuild = '.venv-sqlite-fixed'
if (Test-Path -LiteralPath $fleetCandidateRebuild) { throw 'El entorno ya existe; conservarlo y elegir otra carpeta nueva.' }
& $fleetPython315 -B -m venv "$fleetCandidateRebuild"
if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear el entorno.' }
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$fleetRebuildPython = Join-Path $fleetCandidateRebuild 'Scripts\python.exe'
& $fleetRebuildPython -B -m pip install --only-binary=:all: -r requirements-sqlite-fixed.lock
if ($LASTEXITCODE -ne 0) { throw 'No se pudieron instalar las dependencias fijadas.' }
```

## Validación reproducible

La suite utiliza bases SQLite y perfiles Chrome temporales. No ejecutar una aplicación de prueba sin seleccionar explícitamente su base temporal. Desde la carpeta del proyecto, en una terminal nueva:

```powershell
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:GEFLEET_REQUIRE_FIXED_SQLITE = '1'
.\.venv-sqlite-fixed\Scripts\python.exe -B -c "import sys,sqlite3; print(sys.version); print(sys.executable); print(sqlite3.sqlite_version); assert sqlite3.sqlite_version == '3.53.4'"
if ($LASTEXITCODE -ne 0) { throw 'El intérprete no utiliza la biblioteca corregida esperada.' }
.\.venv-sqlite-fixed\Scripts\python.exe -B -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Las dependencias no superaron la comprobación.' }
$fleetRuntimeTests = Join-Path $env:TEMP ('gefleet-sqlite-fixed-' + [guid]::NewGuid().ToString())
.\.venv-sqlite-fixed\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --basetemp "$fleetRuntimeTests"
if ($LASTEXITCODE -ne 0) { throw 'Hay pruebas Python fallidas; no adoptar el entorno.' }
$env:GEFLEET_TEST_PYTHON = (Resolve-Path '.venv-sqlite-fixed\Scripts\python.exe').Path
$env:GEFLEET_TEST_BROWSER = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
node --test --test-reporter=spec tests/test_checklist_browser.mjs tests/test_inventory_browser.mjs tests/test_mileage_browser.mjs tests/test_maintenance_history_browser.mjs
if ($LASTEXITCODE -ne 0) { throw 'Hay pruebas Chrome fallidas; no adoptar el entorno.' }
```

`GEFLEET_REQUIRE_FIXED_SQLITE=1` activa la comprobación de versión en `tests/test_sqlite_runtime.py`; no introduce un cambio en el servidor. La misma suite sigue disponible en `.venv` para diagnóstico, retirando esa variable, aunque ese intérprete conserva la versión vulnerable.

La cobertura incluye migraciones **0→1→2**, repetición y rollback; integridad y claves foráneas; respaldos con operaciones WAL confirmadas y exclusión de cambios no confirmados; restauración temporal; conservación del Kardex, cantidades y claves; movimientos y entregas concurrentes e idempotencia. La nueva prueba combina dos escritores con checkpoints WAL y comprueba **200 entradas únicas, 200 reintentos, 200 movimientos, 200 claves y stock 200000 milésimas**, además de un respaldo y una restauración iguales.

Resultados confirmados en el entorno nuevo: **234 pruebas Python aprobadas, 0 fallidas**, incluidas las siete originales y las tres nuevas de runtime; **57 casos Chrome aprobados, 0 fallidos** (Node informa 61 al contar cuatro contenedores). Chrome real utilizado: 155.0.8059.79; Node: 22.1.0. Se conservan las tres advertencias conocidas de FastAPI/TestClient y las advertencias de WebSocket experimental de Node. `pip check` aprobó.

El informe JUnit con versión, ejecutable y `sqlite_source_id()` se guardó en `%LOCALAPPDATA%\GoldEngineFleet\RuntimeDownloads\pytest-python315-sqlite3534.xml`. La prueba de versión se ejecutó con `GEFLEET_REQUIRE_FIXED_SQLITE=1` y confirmó que el SQL y el binding cargan 3.53.4. El ensayo concurrente no reproduce de forma determinista el fallo infrecuente; la conclusión sobre la corrección depende de la versión oficial incorporada, además de las pruebas funcionales.

## Adoptar el entorno nuevo

1. Confirma que no hay operaciones pendientes de confirmar o inspecciones por sincronizar y genera un respaldo verificado de la base que realmente utiliza el servidor. Conserva el respaldo y ambos entornos. El procedimiento de respaldo/restauración está en el README.
2. Detén únicamente el servidor de este proyecto antes de cambiar de intérprete. Si está en una terminal, usa `Ctrl+C`; si se inició en segundo plano, identifica su PID y comando antes de detenerlo. Evita iniciar dos servidores sobre la misma base durante el cambio.
3. En una terminal nueva dentro del proyecto, selecciona la misma base existente y utiliza explícitamente el ejecutable candidato. Los siguientes comandos consultan la base seleccionada antes de iniciar; no crean otra flota:

```powershell
$env:GEFLEET_DB = (Resolve-Path -LiteralPath 'data\fleet.db').Path
Remove-Item Env:GEFLEET_ALLOW_CREATE_DB -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
.\.venv-sqlite-fixed\Scripts\python.exe -B -c "import sqlite3; print(sqlite3.sqlite_version); assert sqlite3.sqlite_version == '3.53.4'"
if ($LASTEXITCODE -ne 0) { throw 'No iniciar: biblioteca SQLite inesperada.' }
.\.venv-sqlite-fixed\Scripts\python.exe -B server.py db-info
if ($LASTEXITCODE -ne 0) { throw 'No iniciar: la base no superó la verificación.' }
.\.venv-sqlite-fixed\Scripts\python.exe -B -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Si el servidor usa otra ubicación mediante `GEFLEET_DB`, conserva esa ruta absoluta en vez del ejemplo `data\fleet.db`. Compara la ruta anterior y posterior con `/api/health`. El cambio de intérprete no necesita una migración de datos adicional: el esquema de la aplicación sigue siendo **2**. Los arranques conservan el ejecutor de migraciones existente.

En otra terminal comprueba `http://127.0.0.1:8000/api/health`, abre la app y consulta vehículos, historial y Kardex. Ejecuta `db-info` con el nuevo intérprete para confirmar `sqlite_version=3.53.4`, integridad y claves foráneas. En VS Code selecciona `.venv-sqlite-fixed\Scripts\python.exe` mediante **Python: Select Interpreter** si quieres usarlo para desarrollo.

**`INICIAR_WINDOWS.bat` utiliza `.venv-sqlite-fixed` desde el 14 de octubre de 2026.** Puedes utilizar el lanzador después de detener cualquier servidor anterior y comprobar la ruta de la base. `INICIAR_WINDOWS.bat --check` verifica Python 3.15.0, SQLite 3.53.4 y los imports de FastAPI/Uvicorn sin abrir la base ni iniciar Uvicorn. El arranque habitual también verifica la disponibilidad de `127.0.0.1:8000` y no instala dependencias. No renombres, reemplaces ni elimines `.venv`; para volver al entorno anterior utiliza el comando explícito de la sección siguiente. La instalación oficial registra un intérprete adicional, por lo que un comando genérico como `py -3` puede seleccionar otro Python; los comandos documentados utilizan rutas explícitas.

## Volver al entorno anterior

Detén el servidor candidato. Mantén la misma ruta de base existente; volver al intérprete anterior no requiere restaurar un respaldo ni deshacer registros creados mientras funcionó el candidato. Desde el proyecto:

```powershell
$env:GEFLEET_DB = (Resolve-Path -LiteralPath 'data\fleet.db').Path
Remove-Item Env:GEFLEET_ALLOW_CREATE_DB -ErrorAction SilentlyContinue
Remove-Item Env:GEFLEET_REQUIRE_FIXED_SQLITE -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -B -c "import sys,sqlite3; print(sys.version); print(sqlite3.sqlite_version)"
.\.venv\Scripts\python.exe -B server.py db-info
if ($LASTEXITCODE -ne 0) { throw 'La base no superó la verificación; no iniciar.' }
.\.venv\Scripts\python.exe -B -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Esta vuelta atrás sirve para contingencia o diagnóstico; reintroduce SQLite 3.45.3 y su riesgo WAL-reset. No mezcles paquetes o DLL de ambos intérpretes ni ejecutes `venv --upgrade` sobre el entorno anterior.

## Riesgos pendientes y alcance

Un servidor que ya estuviera iniciado conserva su intérprete anterior hasta que se detenga y vuelva a arrancar; el lanzador actualizado selecciona la biblioteca corregida. Python 3.15.0 es una versión mayor nueva; la validación demuestra compatibilidad con este proyecto y las dependencias fijadas, pero los cambios futuros requieren repetir las pruebas. Persisten las advertencias ya conocidas de FastAPI/TestClient y WebSocket experimental de Node.

La biblioteca corregida no sustituye los respaldos ni protege frente a errores de disco, pérdida de datos del navegador o un cambio equivocado de ruta. Los respaldos SQLite no incluyen IndexedDB ni `localStorage`. El entorno nuevo y el anterior se conservan; no se cambió código de negocio ni se avanzó a la Fase 06.
