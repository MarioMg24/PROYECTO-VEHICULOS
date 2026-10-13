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

## Proyecto y licencia

Los archivos de código del proyecto se entregan para que puedas estudiarlos y modificarlos. Este paquete no contiene imágenes propietarias ni el logotipo oficial, ni credenciales de la empresa.
