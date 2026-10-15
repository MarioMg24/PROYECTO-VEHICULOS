# Gold Engine Fleet · PWA piloto 1.0

Aplicación web instalable (PWA), **independiente de BodegaPro Desktop**, para mantener registros de vehículos, inspecciones de vehículos livianos, mantenimiento por kilometraje y bodega de repuestos vehiculares.

> **ESTADO: PROTOTIPO FUNCIONAL PARA PRUEBAS LOCALES. NO LISTO PARA OPERAR EN UNA RED CORPORATIVA NI PARA PUBLICARSE EN INTERNET.** Aún no implementa autenticación, permisos, HTTPS, restauración guiada, auditoría completa ni mecanismos de seguridad necesarios para la operación multiusuario. No uses información confidencial hasta que el responsable de TI autorice su despliegue.

## Requisitos

- Windows 10/11 o Linux.
- Para el arranque Windows validado: **Python oficial 3.15.0 x64 y SQLite 3.53.4**, con el entorno `.venv-sqlite-fixed` preparado. Consulta la [guía de instalación y vuelta atrás](ACTUALIZACION_SQLITE_2026-10-13.md). El código requiere Python 3.11 o superior en otras plataformas; allí debe comprobarse por separado la biblioteca SQLite realmente cargada.
- Conexión a Internet durante la preparación inicial del entorno y sus dependencias. **El lanzador no instala paquetes en cada arranque.**
- Navegador Edge o Chrome. En PC, PWA instalable desde `localhost`.

## Iniciar en Windows

1. Descomprime `GoldEngineFleet.zip` en una carpeta de tu equipo (ejemplo: Escritorio).
2. Abre esa carpeta en **Visual Studio Code** si quieres editar el código.
3. Prepara `.venv-sqlite-fixed` siguiendo la guía y ejecuta **`INICIAR_WINDOWS.bat --check`** desde la terminal para comprobarlo sin iniciar el servidor ni abrir la base.
4. Si es una instalación nueva sin base anterior, inicialízala explícitamente como se indica abajo. Si ya tienes `data/fleet.db`, consérvala y omite la inicialización.
5. Haz doble clic en **`INICIAR_WINDOWS.bat`** y abre **http://127.0.0.1:8000** cuando Uvicorn indique que está listo.
6. Si necesitas la carga opcional de las **nueve unidades de la fotografía**, ejecuta `seed_vehicles.py` con el intérprete corregido, como se indica abajo, y actualiza el navegador.
7. Detén el servidor con `Ctrl+C`.

El lanzador utiliza exclusivamente `.venv-sqlite-fixed\Scripts\python.exe`. Antes de iniciar comprueba **Python 3.15.0**, **SQLite 3.53.4**, los imports de FastAPI/Uvicorn y que **127.0.0.1:8000** esté disponible. Si falta el entorno, no coinciden las versiones o el puerto está ocupado, muestra un error y se detiene. No crea entornos ni instala dependencias; `.venv` permanece disponible para vuelta atrás.

`GEFLEET_DB` conserva la ruta absoluta definida en la terminal que ejecuta el lanzador; sin esa variable se utiliza `data/fleet.db`. El `.bat` desactiva la creación automática de bases, elimina `PYTHONPATH` externo únicamente para su proceso y mantiene el servidor en `127.0.0.1:8000`. Para una instalación nueva, usa `db-init` explícitamente. La opción `--check` comprueba solo el intérprete y sus dependencias; no valida la disponibilidad del puerto ni abre una base.

**Los vehículos se crean con sus placas y nombres extraídos del listado recibido, marcados para verificar.** NO se precargan repuestos, existencias, horómetros, servicios, inspecciones ni compras inventadas.

### Abrir desde VS Code / terminal

```powershell
.\INICIAR_WINDOWS.bat --check
# Tras preparar el entorno corregido; la instalación de paquetes se hace una sola vez.
.\.venv-sqlite-fixed\Scripts\python.exe server.py db-info
# Solo si no existe una base anterior y deseas una instalación nueva:
.\.venv-sqlite-fixed\Scripts\python.exe server.py db-init
# Carga opcional; omite esta línea si no necesitas el listado inicial.
.\.venv-sqlite-fixed\Scripts\python.exe seed_vehicles.py
.\.venv-sqlite-fixed\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000
```

> `db-init` no borra ni reemplaza archivos existentes. Si la base existe, verifica su esquema y aplica únicamente migraciones pendientes. No lo uses para resolver una ruta equivocada: localiza primero la base anterior. La carga inicial de vehículos es **opcional y repetible sin crear duplicados**.

En Linux/macOS utiliza el intérprete de un entorno compatible y comprueba su SQLite; las versiones verificadas aquí corresponden al instalador oficial Windows.

### Vuelta atrás del arranque Windows

Detén primero el servidor corregido y conserva la misma base. Para una contingencia, inicia explícitamente el entorno anterior desde la terminal, sin editar ni reemplazar ninguno de los dos entornos:

```powershell
# Mantén GEFLEET_DB si utilizas una ruta distinta de data/fleet.db.
.\.venv\Scripts\python.exe -B -m uvicorn server:app --host 127.0.0.1 --port 8000
```

La vuelta atrás reintroduce SQLite 3.45.3 y su riesgo WAL-reset; utiliza ese entorno únicamente para contingencia o diagnóstico. No requiere restaurar respaldos ni borrar registros. Consulta el [procedimiento completo](ACTUALIZACION_SQLITE_2026-10-13.md#volver-al-entorno-anterior).

## Funciones incluidas

- **Vehículos:** catálogo, edición de datos, tipo y kilometraje actual.
- **Inspecciones:** plantilla basada en el checklist **rellenado del Kia Soluto** (37 verificaciones en Niveles, Pedales, Luces, Equipo de carretera y Varios). Bueno/Malo/No aplica, notas, tres fotos JPEG reducidas, vista imprimible.
- **Offline parcial:** la interfaz PWA puede quedar almacenada en el navegador y los borradores de inspección se guardan localmente; el envío sin conexión se conserva en IndexedDB y requiere sincronización posterior. **Debe abrirse primero en línea.** El almacenamiento local del navegador puede eliminarse y no sustituye un respaldo.
- **Mantenimiento:** registrar servicios reales y configurar el intervalo de kilómetros hasta el siguiente mantenimiento. No se asignan intervalos universales ni se mezclan fechas estimadas con kilometraje real.
- **Repuestos:** bodega vehicular exclusiva con códigos, número de parte, existencias, mínimo, categorías, compatibilidad declarada, entradas y salidas. Cantidades exactas de hasta tres decimales almacenadas como milésimas enteras.
- **Solicitudes:** solicitud de materiales, autorización nominal manual, entregas totales o parciales, stock descontado solo al confirmar entrega en transacción SQL.
- **Historial/Documentos:** impresión del checklist y solicitudes mediante el navegador (Guardar como PDF), exportaciones CSV (Excel) y respaldo descargable consistente de SQLite.

### Confirmaciones y reintentos de bodega

Las entradas, salidas y entregas generan una clave de operación antes del primer envío. El navegador conserva esa clave y los datos originales en `localStorage` hasta recibir confirmación. Durante el envío bloquea el botón y muestra **PROCESANDO**. Si se pierde la respuesta, aparece **RESULTADO DESCONOCIDO** y el botón **Reintentar operación** conserva la misma clave y contenido, incluso después de cerrar y reabrir el formulario o recargar. No se envían operaciones de bodega automáticamente al recuperar conexión.

Una confirmación muestra **Operación CONFIRMADA**. Si falta stock o hay un rechazo de validación, se informa que la operación no fue confirmada y se permite corregirla. Si hay una entrega pendiente de confirmar cuyo pedido ya aparece entregado, abre su detalle y usa **Resolver entrega pendiente**. No borres los datos del sitio ni cambies de perfil/origen mientras tengas un resultado desconocido: el comprobante de reintento pertenece a ese almacenamiento.

Para clientes de la API, **`POST /api/movements` y `POST /api/requests/{id}/deliver` requieren el encabezado `Idempotency-Key`**. Admite 1 a 128 caracteres de `A-Z`, `a-z`, `0-9`, punto, guion, guion bajo o dos puntos. Una clave ya confirmada con el mismo contenido devuelve exactamente su respuesta anterior sin agregar movimientos. Otra ruta, solicitud o contenido con esa clave devuelve HTTP 409 y `detail.code="idempotency_conflict"`. Cada operación legítima nueva usa una clave distinta. Para un reintento se conserva el cuerpo original; por ejemplo, cambiar `quantity` de `"1"` a `"1.0"` se considera contenido diferente.

Al iniciar, la aplicación migra el esquema SQLite de versión 0 a 1 y añade `inventory_operations`, con clave primaria única, alcance, huella del contenido y respuesta confirmada. La creación de esa tabla y el cambio de versión son transaccionales. No modifica movimientos anteriores ni sus cantidades; movimiento/entrega y comprobante nuevo se confirman en la misma transacción. La protección se aplica a operaciones nuevas identificadas por su clave; no asigna claves retroactivas al Kardex histórico.

### Kilometrajes y planes de mantenimiento

La edición del vehículo envía únicamente los campos modificados. Cambiar nombre, placa o marca conserva los demás datos. Una edición normal solo mantiene o aumenta el kilometraje; si la lectura cambió mientras el formulario estaba abierto, se rechaza su sustitución y se pide actualizar los datos.

Para una lectura errónea utiliza **Corregir kilometraje**, en la tarjeta del vehículo. Requiere lectura corregida, fecha, motivo y responsable. La corrección añade un evento **Corrección de kilometraje** al historial de mantenimiento existente, con lectura anterior y nueva; no reescribe servicios o inspecciones anteriores ni genera un plan preventivo. Esta acción utiliza los campos históricos existentes.

Las lecturas nuevas deben ser enteros entre 0 y 10.000.000 km; los intervalos, entre 1 y 1.000.000 km. Las fechas deben existir en el calendario y usar `AAAA-MM-DD`. Las lecturas antiguas fuera de rango se conservan y pueden corregirse mediante la acción explícita.

Cada vehículo y familia de servicio conserva su plan: se normalizan mayúsculas/minúsculas y espacios del nombre, por lo que conviene utilizar el mismo nombre para un servicio recurrente. Se selecciona el último servicio con intervalo por fecha efectiva, después por kilometraje y finalmente por ID para desempatar. Un registro histórico cargado después no reemplaza al reciente. Los trabajos sin intervalo no reinician planes, incluso si usan el mismo nombre de servicio; un trabajo clasificado como **Correctivo** rechaza un intervalo preventivo.

El objetivo es `kilometraje del servicio + intervalo configurado`, sin intervalo universal. Se mantiene la advertencia de **500 km**: más de 500 = normal, entre 1 y 500 = próximo, cero o menos = vencido. Por ejemplo, `118539 + 4500 = 123039`. Con lectura actual de 122539 quedan 500 km; con 123039 el servicio está vencido.

El bootstrap incorpora `maintenance_plans` con todos los planes vigentes y una vista reciente de hasta 200 registros. El historial completo se consulta mediante el endpoint paginado, sin depender de esa vista. Fechas o cantidades heredadas inválidas se mantienen en el historial, pero se excluyen de los planes. Si el navegador conserva una copia anterior sin los planes completos, muestra un aviso para actualizarla al conectarse.

### Registro e historial técnico por vehículo

En **Mantenimientos → Registrar servicio**, selecciona vehículo/placa, fecha efectiva y familia de servicio; registra kilometraje, tipo **Preventivo (MPG)** o **Correctivo**, trabajo realizado, responsable o mecánico, lugar y observaciones. La familia de servicio identifica el plan recurrente —por ejemplo, Cambio de aceite o Frenos— y el tipo clasifica el trabajo. El campo **Próximo mantenimiento** muestra el objetivo calculado con la lectura y el intervalo configurado; no se guarda un objetivo independiente ni se usa un intervalo universal.

Los campos existentes se reutilizan: `vehicle_id` relaciona la placa con el vehículo, `performed_on` guarda la fecha, `service_name` identifica la familia de servicio, `odometer` guarda la lectura, `interval_km` y `next_due_km` conservan la programación y `notes` contiene observaciones. La migración **1→2** añade exclusivamente `maintenance_type`, `work_performed`, `responsible` y `location`, además de un índice para consultar por vehículo y fecha. Todos los campos nuevos empiezan vacíos en registros anteriores; no se infieren tipos, responsables ni trabajos a partir de sus notas. Los clientes anteriores de la API siguen funcionando.

El historial permite seleccionar vehículo, fechas desde/hasta y tipo. Muestra cantidades y páginas de 20 registros, ordenados por fecha efectiva descendente, kilometraje e ID para desempatar. **Anterior/Siguiente** permiten recorrer todo el historial; **Detalle** consulta directamente el servicio aunque no esté entre los 200 registros del bootstrap. También puedes abrirlo desde **Vehículos → Ver ficha → Historial técnico**. El último mantenimiento y los planes próximos corresponden al vehículo seleccionado y son independientes de los filtros de fecha o tipo; las correcciones de odómetro siguen visibles en el historial, pero no se presentan como el último servicio realizado.

La consulta completa requiere conexión con el servidor local y muestra errores/reintentos de forma explícita. Los planes y el comportamiento offline de inspecciones mantienen su funcionamiento previo. El detalle y el CSV existente de mantenimiento incluyen los cuatro campos nuevos.

Las fechas deben existir y usar `AAAA-MM-DD`; los límites de kilometraje e intervalo siguen siendo los de la Fase 03. Trabajo y observaciones admiten hasta 4000 caracteres cada uno; responsable y lugar, hasta 150; familia de servicio, hasta 250. Los datos nuevos que exceden los límites se rechazan, sin truncarlos. Los campos adicionales son opcionales para conservar la compatibilidad; conviene completarlos al registrar servicios nuevos.

La API añade `GET /api/maintenance` con `vehicle_id`, `date_from`, `date_to`, `maintenance_type`, `page` y `page_size` (máximo 100), y `GET /api/maintenance/{id}` para el detalle. Los tipos de filtro son `MPG`, `CORRECTIVO` y `LEGACY` para registros sin clasificar. La respuesta paginada incluye `items`, `total`, `page`, `page_size`, `pages`, `latest` y `plans`. Los planes del historial incluyen la lectura actual del vehículo tomada en la misma transacción de consulta para calcular los kilómetros restantes.

## Qué NO está implementado todavía

- Checklist de vehículos pesados, mientras no se reciba el formato real.
- Formulario oficial de solicitud de repuestos; se incluye uno provisional.
- Autenticación, permisos, auditoría empresarial y publicación remota segura.
- Sincronización desde cualquier lugar por Internet, Google Drive automático o integración con BodegaPro.
- Firma digital/electrónica, certificado de inspección legal o decisión automática sobre aptitud de circulación.
- Carga y descarga avanzada por lotes/series, órdenes complejas o costo contable.
- Instalador Android APK o tienda de aplicaciones. Es una PWA web instalable.
- Servidor PostgreSQL/infraestructura de producción: este piloto emplea SQLite local para facilitar pruebas. Puede migrarse a una base central en una segunda fase.

## Uso offline y celulares

- **En la misma computadora**, Chrome/Edge pueden ofrecer "Instalar aplicación" al entrar en `http://localhost:8000` o `http://127.0.0.1:8000`.
- **En teléfonos o desde otras computadoras**, este paquete se inicia deliberadamente solo en `127.0.0.1`. No se puede acceder directamente desde otra máquina. Esto evita exponer registros empresariales sin autorización.
- Para desplegarla en móviles de la empresa se necesita un servidor accesible, **HTTPS válido/confiable**, cuentas de usuario, controles de acceso, copias externas, pruebas de sincronización y autorización de TI. Exponer Uvicorn en `0.0.0.0` o reenviar un puerto **NO** es un reemplazo seguro de ese trabajo.
- La lógica de almacenamiento offline ya está preparada para pruebas, pero no garantiza que todas las funciones operen sin Internet: **solo checklists y borradores offline**, no movimientos de stock ni aprobaciones.

## Datos, seguridad y respaldos

- Sin `GEFLEET_DB`, la aplicación utiliza `data/fleet.db` dentro del proyecto, independientemente de la carpeta desde la que se ejecute. Si defines `GEFLEET_DB`, debe contener una ruta absoluta no vacía. Una ruta inexistente, base vacía o esquema incompatible detiene el arranque con un mensaje; no se crea otra flota silenciosamente.
- Usa **Historial → Descargar respaldo SQLite** o el comando `db-backup`. Ambos utilizan `sqlite3.Connection.backup()` y comprueban esquema, `PRAGMA integrity_check` y `PRAGMA foreign_key_check` en la copia. El respaldo contiene las transacciones confirmadas, incluso las pendientes de checkpoint en WAL, y se entrega como un archivo independiente en modo `DELETE`. No copia solamente los bytes de `fleet.db`.
- Conserva respaldos con nombres distintos, también en una unidad diferente al equipo. Los comandos rechazan un destino existente; no hay opción de sobrescritura. No copies ni elimines manualmente los archivos `-wal`, `-shm` o `-journal` de una base activa. [Referencia de SQLite sobre WAL](https://sqlite.org/wal.html#the_wal_file).
- Los datos de prueba usados para validar la app nunca se incluyen en el ZIP. `data/` viene vacío.
- Nunca publiques `fleet.db`, archivos exportados, fotografías o copias en GitHub.
- El archivo `.gitignore` ya excluye las bases de datos y entornos virtuales.
- Para revisar la base SQLite con un programa gratuito puedes usar **DB Browser for SQLite**.

### Identificar la base y comprobar migraciones

En la misma terminal y con las mismas variables que utilizará Uvicorn:

```powershell
$env:GEFLEET_DB
.\.venv-sqlite-fixed\Scripts\python.exe -B server.py db-info
# Con el servidor local en funcionamiento:
Invoke-RestMethod http://127.0.0.1:8000/api/health | ConvertTo-Json -Depth 5
```

La consola de arranque y `/api/health` muestran la ruta absoluta y la versión realmente abiertas por el proceso. `db-info` también comprueba integridad y claves foráneas y presenta cantidades de registros y saldos por repuesto, sin crear ni migrar la base. Puede consultar otra ubicación con `--database "C:\ruta\base.db"`. Una terminal diferente puede tener otro valor de `GEFLEET_DB`; compara su salida con `/api/health` antes de cargar vehículos o registrar operaciones.

La versión actual es **2**. Se conserva la migración aditiva **0→1** que creó `inventory_operations` y se incorpora **1→2** para los detalles de mantenimiento; los registros históricos, las cantidades y las claves confirmadas permanecen intactos. `MIGRATIONS` registra versiones consecutivas y el ejecutor aplica todas las pendientes en orden, una vez, dentro de una sola transacción `BEGIN IMMEDIATE`. Verifica la base antes de confirmar. Si alguna falla, revierte datos, tablas y `PRAGMA user_version`; un esquema más reciente que el código o un registro de migraciones incompleto provoca rechazo. Los respaldos de versiones 0 y 1 siguen siendo compatibles: la restauración conserva su versión y el arranque migra únicamente la copia seleccionada.

Para una migración futura, añade su función al registro, aumenta `SCHEMA_VERSION` y actualiza las comprobaciones de esquema que correspondan. Las funciones ejecutan sentencias con `execute`/`executemany`; el ejecutor controla la transacción y la versión. Bloquea `COMMIT`, `ROLLBACK`, `executescript()` con commit implícito y cambios directos de versión dentro de las migraciones. Prueba el cambio sobre una copia y conserva un respaldo verificado antes de reiniciar la aplicación real.

La creación automática solo puede habilitarse explícitamente con `GEFLEET_ALLOW_CREATE_DB=1`, usada por las pruebas temporales. Para operación normal deja esa variable sin definir y utiliza `db-init` únicamente al preparar una instalación nueva. La carpeta de destino debe existir previamente.

### Respaldo y restauración con el servidor detenido

1. Antes de actualizar o restaurar, confirma la ruta activa y resuelve las operaciones de bodega con resultado desconocido y las inspecciones pendientes del navegador. La copia SQLite no incluye borradores, IndexedDB ni `localStorage`. Si necesitas recuperar desde un respaldo anterior, conserva los pendientes antes del cambio y verifica sus vehículos y repuestos antes de enviarlos a la base restaurada.
2. Crea un respaldo nuevo mediante Historial o por terminal. El siguiente ejemplo lo guarda en una carpeta temporal existente; para conservación elige una carpeta privada en otra unidad. Comprueba el código de salida y la información del archivo:

```powershell
$fleetBackup = Join-Path $env:TEMP ('gefleet-backup-' + [guid]::NewGuid().ToString() + '.db')
.\.venv-sqlite-fixed\Scripts\python.exe -B server.py db-backup --destination "$fleetBackup"
if ($LASTEXITCODE -ne 0) { throw 'No se confirmó el respaldo; revisa el error.' }
.\.venv-sqlite-fixed\Scripts\python.exe -B server.py db-info --database "$fleetBackup"
```

3. Detén Uvicorn con `Ctrl+C` y cierra cualquier editor SQLite que tenga abiertas las bases. Restaura primero a un archivo nuevo en una ubicación temporal:

```powershell
$fleetRestored = Join-Path $env:TEMP ('gefleet-restored-' + [guid]::NewGuid().ToString() + '.db')
.\.venv-sqlite-fixed\Scripts\python.exe -B server.py db-restore --source "$fleetBackup" --destination "$fleetRestored"
if ($LASTEXITCODE -ne 0) { throw 'Restauración rechazada; conserva la base anterior.' }
.\.venv-sqlite-fixed\Scripts\python.exe -B server.py db-info --database "$fleetRestored"
```

4. La restauración exige un respaldo independiente sin archivos auxiliares, valida versión/esquema, integridad y claves foráneas, crea una copia provisional en la carpeta de destino, vuelve a validarla y publica el archivo completo sin reemplazar otro archivo. Rechaza destinos existentes, el mismo origen/destino y `data/fleet.db`. Compara registros, saldos, movimientos recientes y claves de `inventory_operations` con la información del respaldo. Un respaldo antiguo contiene únicamente los datos confirmados hasta su creación; las operaciones posteriores deben revisarse antes de decidir el cambio.
5. Para conservar la restauración, repite el comando con **otro nombre nuevo** dentro de una carpeta privada persistente existente. Con el servidor aún detenido, selecciona esa ruta mediante `GEFLEET_DB`, ejecuta `db-info` y después inicia Uvicorn. Verifica `/api/health`, vehículos, Kardex, solicitudes e historial. La base anterior queda intacta para comparación o vuelta atrás; no reemplaces `fleet.db` ni borres sus archivos auxiliares. Define la misma variable en cada terminal de arranque, incluido cualquier script `.bat` que ejecutes desde ella.

```powershell
# Ejemplo: sustituye esta ruta por el archivo nuevo que acabas de restaurar.
$env:GEFLEET_DB = 'C:\ruta\privada\fleet-restaurada-verificada.db'
Remove-Item Env:GEFLEET_ALLOW_CREATE_DB -ErrorAction SilentlyContinue
.\.venv-sqlite-fixed\Scripts\python.exe -B server.py db-info
if ($LASTEXITCODE -ne 0) { throw 'La base seleccionada no superó la verificación.' }
.\.venv-sqlite-fixed\Scripts\python.exe -B -m uvicorn server:app --host 127.0.0.1 --port 8000
```

No existe endpoint de restauración ni se publica el respaldo por Internet. El comando no puede comprobar por sí mismo que todos los servidores externos estén detenidos: detenerlos y verificar la ruta antes de cambiar la base es parte del procedimiento. La publicación sin sobrescritura necesita enlaces físicos en el sistema de archivos; si el volumen no los admite, falla conservando los archivos existentes. Restaura en una carpeta local compatible, como NTFS; la base activa WAL debe permanecer en disco local.

Para verificar manualmente el Kardex, las cantidades entregadas y los comprobantes de reintento, ejecuta las siguientes consultas de solo lectura sobre **el respaldo y la copia restaurada** con un editor SQLite. Sus resultados deben coincidir. La última consulta corresponde a bases a partir de la versión 1; en versión 0 la tabla de operaciones todavía no existe.

```sql
SELECT * FROM movements ORDER BY id;
SELECT * FROM request_lines ORDER BY id;
SELECT * FROM inventory_operations ORDER BY operation_key;
```

**Respaldos descargados con la versión anterior:** `/api/backup` ya generaba una copia consistente, pero podía conservar el encabezado WAL. Si `db-restore` informa que el respaldo usa formato WAL, conserva el original y normaliza ese respaldo completo a otro archivo mediante la API de copia, después repite las verificaciones y la restauración:

```powershell
# fleetBackup debe apuntar al respaldo antiguo completo, no a una copia parcial de una base activa.
$fleetNormalized = Join-Path $env:TEMP ('gefleet-backup-normalized-' + [guid]::NewGuid().ToString() + '.db')
.\.venv-sqlite-fixed\Scripts\python.exe -B server.py db-backup --database "$fleetBackup" --destination "$fleetNormalized"
if ($LASTEXITCODE -ne 0) { throw 'El respaldo antiguo no pudo normalizarse.' }
.\.venv-sqlite-fixed\Scripts\python.exe -B server.py db-info --database "$fleetNormalized"
```

La normalización conserva tablas, valores y versión; no recupera transacciones de un `-wal` que se perdió al copiar manualmente una base activa. Para una copia incompleta se necesita recuperar el conjunto original y generar un respaldo consistente antes de restaurar. No elimines archivos auxiliares para sortear el rechazo.

El entorno anterior `.venv` utiliza Python **3.13.0 / SQLite 3.45.3**, afectado por WAL-reset. `.venv-sqlite-fixed` utiliza **Python oficial Windows 3.15.0 / SQLite 3.53.4**, confirmado mediante la biblioteca realmente cargada, con **234 pruebas Python y 57 casos Chrome aprobados**. Desde el **14 de octubre de 2026**, `INICIAR_WINDOWS.bat` selecciona el entorno corregido; un servidor que ya estuviera iniciado conserva su intérprete hasta que se detenga y vuelva a arrancar. `.venv`, el código de negocio y `fleet.db` se conservan. Consulta [versiones, evidencias oficiales, instalación, adopción y vuelta atrás](ACTUALIZACION_SQLITE_2026-10-13.md). [Aviso oficial de SQLite](https://sqlite.org/wal.html#walreset).

## Pruebas automatizadas

Las pruebas son opcionales para el uso normal. En un entorno de desarrollo:

```powershell
# Solo al preparar el entorno de pruebas, fuera del arranque habitual:
.\.venv-sqlite-fixed\Scripts\python.exe -m pip install --only-binary=:all: -r requirements-sqlite-fixed.lock
.\.venv-sqlite-fixed\Scripts\python.exe -m pytest -q
```

Las pruebas utilizan bases temporales y verifican movimientos, stock, solicitudes, inspecciones idempotentes, mantenimiento, exportación, copia SQLite y preparación opcional del listado.

Las regresiones de idempotencia verifican concurrencia, claves repetidas o en conflicto, entregas legítimas, errores transaccionales, Kardex y migración con datos existentes. Los siete casos originales conservan sus comprobaciones; sus movimientos y entregas envían ahora el encabezado obligatorio.

Las regresiones de persistencia prueban migraciones ordenadas/repetidas, rollback ante errores o commits indebidos, rutas inexistentes, respaldos con WAL, restauración temporal, integridad y claves foráneas, conservación de registros e idempotencia y rechazo de destinos existentes o respaldos incompatibles. Las bases de prueba se crean con autorización explícita; ninguna utiliza ni reemplaza `data/fleet.db`.

Las pruebas de navegador usan Node.js 22 o superior y Chrome/Edge instalado, sin añadir bibliotecas al frontend. Arrancan servidor, SQLite y perfil de navegador temporales; no utilizan `data/fleet.db`:

```powershell
$env:GEFLEET_TEST_PYTHON = (Resolve-Path '.venv-sqlite-fixed\Scripts\python.exe').Path
node --test tests/test_checklist_browser.mjs tests/test_inventory_browser.mjs tests/test_mileage_browser.mjs tests/test_maintenance_history_browser.mjs
```

### Verificación manual con base temporal

Abre una terminal nueva en el proyecto y ejecuta:

```powershell
$env:GEFLEET_DB = Join-Path $env:TEMP ('gefleet-manual-' + [guid]::NewGuid().ToString() + '.db')
.\.venv-sqlite-fixed\Scripts\python.exe -B server.py db-init
.\.venv-sqlite-fixed\Scripts\python.exe -B seed_vehicles.py
.\.venv-sqlite-fixed\Scripts\python.exe -B -m uvicorn server:app --host 127.0.0.1 --port 8001
```

1. Abre `http://127.0.0.1:8001`, registra un repuesto de prueba y confirma una entrada. Comprueba el mensaje de confirmación, el stock y el Kardex.
2. En las herramientas de desarrollo del navegador, pestaña Network, copia la solicitud `POST /api/movements` como `fetch`. Reenvíala sin cambiar `Idempotency-Key` ni el cuerpo: devuelve el mismo resultado y no cambia stock/Kardex. Cambia la cantidad conservando la clave: responde 409.
3. Registra otra entrada igual desde el formulario: utiliza una clave distinta y aumenta el stock una vez. Haz lo mismo con una salida.
4. Crea y aprueba una solicitud de varias unidades; entrega una unidad. Reenvía su petición `/deliver` con la misma clave: no vuelve a descontar. Otra entrega desde el formulario genera una clave nueva y descuenta una unidad legítima.
5. Para comprobar un resultado desconocido sin envío, abre un movimiento, activa Offline en Network y confirma. Debe conservar clave/datos y mostrar el reintento. Recupera conexión, recarga, abre el mismo tipo de movimiento y reintenta: debe confirmarse una sola vez. Las pruebas automatizadas también cubren una respuesta perdida **después** del commit.

Detén el servidor con `Ctrl+C` y cierra esa terminal de pruebas. La variable de base temporal queda limitada a esa terminal; el arranque normal vuelve a usar `data/fleet.db`. La migración de la base real se aplicará cuando se reinicie el servidor normal con esta versión.

Para comprobar kilometrajes y alertas en esa misma base temporal:

1. Edita un vehículo y establece 118539 km. Cambia después solo nombre, placa o marca: el kilometraje y los demás campos deben conservarse. Intenta reducir la lectura mediante Editar: debe impedirlo.
2. Registra **Cambio de aceite**, fecha 2026-10-12, lectura 118539 e intervalo 4500. Comprueba el objetivo 123039. Añade después un servicio de aceite con fecha 2025-04-01 y lectura 100000: el plan vigente y el odómetro actual no deben retroceder.
3. Aumenta la lectura a 122538, 122539 y 123039: deben quedar 501 km normales, 500 km de advertencia y luego Vencido. Registra Frenos con otro intervalo y verifica que ambos planes son independientes.
4. Registra un trabajo de aceite sin intervalo: debe añadirse al historial sin reemplazar el plan. Usa **Corregir kilometraje** únicamente para una lectura errónea e indica motivo y responsable; comprueba su evento de historial.
5. Deja abierto Editar o Corregir kilometraje, actualiza la lectura desde otra ventana y vuelve a intentar guardar una lectura en el formulario antiguo: debe informar del conflicto y conservar la más reciente. Las regresiones automatizadas también verifican más de 200 mantenimientos, fechas inexistentes, límites y rollback.

Para comprobar el historial técnico de la Fase 05, prepara otra base temporal vacía con `db-init`, omitiendo la carga opcional de vehículos. Registra **Chevrolet NLR**, placa **PDX-2511**, y un servicio con fecha **30/09/2026**, kilometraje **118539**, intervalo **4500**, tipo **Preventivo (MPG)**, responsable **Samir Lobato**, lugar **Base Coca** y trabajo **cambio de aceite y filtros**. Comprueba el objetivo **123039**, filtra por vehículo/fecha/tipo y abre Detalle. Registra después un servicio histórico anterior: debe aparecer en su fecha sin reemplazar el plan vigente. Las pruebas automatizadas recorren más de 200 servicios y verifican registros de páginas anteriores. El ejemplo se utiliza únicamente en bases temporales.

## Proyecto y licencia

Los archivos de código del proyecto se entregan para que puedas estudiarlos y modificarlos. Este paquete no contiene imágenes propietarias ni el logotipo oficial, ni credenciales de la empresa.
