# Gold Engine Fleet · PWA piloto 1.0

Aplicación web instalable (PWA), **independiente de BodegaPro Desktop**, para mantener registros de vehículos, inspecciones de vehículos livianos, mantenimiento por kilometraje y bodega de repuestos vehiculares.

> **ESTADO: PROTOTIPO FUNCIONAL PARA PRUEBAS LOCALES. NO LISTO PARA OPERAR EN UNA RED CORPORATIVA NI PARA PUBLICARSE EN INTERNET.** Aún no implementa autenticación, permisos, HTTPS, restauración guiada, auditoría completa ni mecanismos de seguridad necesarios para la operación multiusuario. No uses información confidencial hasta que el responsable de TI autorice su despliegue.

## Requisitos

- Windows 10/11 o Linux.
- Python 3.11 o superior, descargado de https://www.python.org/downloads/.
- Conexión a Internet **solo la primera vez** para instalar las bibliotecas FastAPI y Uvicorn. Son gratuitas.
- Navegador Edge o Chrome. En PC, PWA instalable desde `localhost`.

## Iniciar en Windows

1. Descomprime `GoldEngineFleet.zip` en una carpeta de tu equipo (ejemplo: Escritorio).
2. Abre esa carpeta en **Visual Studio Code** si quieres editar el código.
3. Haz doble clic en **`INICIAR_WINDOWS.bat`**.
4. La primera vez, espera la instalación de dependencias y abre **http://127.0.0.1:8000**.
5. Para cargar las **nueve unidades de la fotografía**, ejecuta `CARGAR_VEHICULOS_WINDOWS.bat` (opcional) y actualiza el navegador.
6. Detén el servidor cerrando la terminal o con `Ctrl+C`.

**Los vehículos se crean con sus placas y nombres extraídos del listado recibido, marcados para verificar.** NO se precargan repuestos, existencias, horómetros, servicios, inspecciones ni compras inventadas.

### Abrir desde VS Code / terminal

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe seed_vehicles.py
.\.venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000
```

> La carga inicial de vehículos es **opcional y repetible sin crear duplicados**.

En Linux/macOS se sustituye la ruta al ejecutable por `.venv/bin/python` y se usan los comandos correspondientes.

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

Para una lectura errónea utiliza **Corregir kilometraje**, en la tarjeta del vehículo. Requiere lectura corregida, fecha, motivo y responsable. La corrección añade un evento **Corrección de kilometraje** al historial de mantenimiento existente, con lectura anterior y nueva; no reescribe servicios o inspecciones anteriores ni genera un plan preventivo. Esta fase no añade tablas, columnas ni migraciones.

Las lecturas nuevas deben ser enteros entre 0 y 10.000.000 km; los intervalos, entre 1 y 1.000.000 km. Las fechas deben existir en el calendario y usar `AAAA-MM-DD`. Las lecturas antiguas fuera de rango se conservan y pueden corregirse mediante la acción explícita.

Cada vehículo y tipo de servicio conserva su plan: se normalizan mayúsculas/minúsculas y espacios del nombre, por lo que conviene utilizar el mismo nombre para un servicio recurrente. Se selecciona el último servicio preventivo por fecha efectiva, después por kilometraje y finalmente por ID para desempatar. Un registro histórico cargado después no reemplaza al reciente. Los trabajos sin intervalo se consideran correctivos y no reinician planes, incluso si usan el mismo nombre de servicio.

El objetivo es `kilometraje del servicio + intervalo configurado`, sin intervalo universal. Se mantiene la advertencia de **500 km**: más de 500 = normal, entre 1 y 500 = próximo, cero o menos = vencido. Por ejemplo, `118539 + 4500 = 123039`. Con lectura actual de 122539 quedan 500 km; con 123039 el servicio está vencido.

El bootstrap incorpora `maintenance_plans` con todos los planes vigentes; los 200 registros de historial se consultan por separado. Fechas o cantidades heredadas inválidas se mantienen en el historial, pero se excluyen de los planes. Si el navegador conserva una copia anterior sin los planes completos, muestra un aviso para actualizarla al conectarse.

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

- La base se guarda en `data/fleet.db` (se crea automáticamente). Copia este archivo o usa **Historial → Descargar respaldo SQLite**. Realiza backups en una unidad o ubicación diferente al mismo equipo.
- Los datos de prueba usados para validar la app nunca se incluyen en el ZIP. `data/` viene vacío.
- Nunca publiques `fleet.db`, archivos exportados, fotografías o copias en GitHub.
- El archivo `.gitignore` ya excluye las bases de datos y entornos virtuales.
- Para revisar la base SQLite con un programa gratuito puedes usar **DB Browser for SQLite**.

## Pruebas automatizadas

Las pruebas son opcionales para el uso normal. En un entorno de desarrollo:

```powershell
.\.venv\Scripts\python.exe -m pip install pytest httpx
.\.venv\Scripts\python.exe -m pytest -q
```

Las pruebas utilizan bases temporales y verifican movimientos, stock, solicitudes, inspecciones idempotentes, mantenimiento, exportación, copia SQLite y preparación opcional del listado.

Las regresiones de idempotencia verifican concurrencia, claves repetidas o en conflicto, entregas legítimas, errores transaccionales, Kardex y migración con datos existentes. Los siete casos originales conservan sus comprobaciones; sus movimientos y entregas envían ahora el encabezado obligatorio.

Las pruebas de navegador usan Node.js 22 o superior y Chrome/Edge instalado, sin añadir bibliotecas al frontend. Arrancan servidor, SQLite y perfil de navegador temporales; no utilizan `data/fleet.db`:

```powershell
node --test tests/test_checklist_browser.mjs tests/test_inventory_browser.mjs tests/test_mileage_browser.mjs
```

### Verificación manual con base temporal

Abre una terminal nueva en el proyecto y ejecuta:

```powershell
$env:GEFLEET_DB = Join-Path $env:TEMP ('gefleet-manual-' + [guid]::NewGuid().ToString() + '.db')
.\.venv\Scripts\python.exe -B seed_vehicles.py
.\.venv\Scripts\python.exe -B -m uvicorn server:app --host 127.0.0.1 --port 8001
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

## Proyecto y licencia

Los archivos de código del proyecto se entregan para que puedas estudiarlos y modificarlos. Este paquete no contiene imágenes propietarias ni el logotipo oficial, ni credenciales de la empresa.
