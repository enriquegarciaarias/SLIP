# Horacio
```
sudo gpclient --fix-openssl connect guadarrama.uned.es --hip
lsi00270
ssh egarcia@horacio.lsi.uned.es
```

# Instalación de Ollama en el servidor `horacio.lsi.uned.es`

## Contexto

- **Servidor**: `horacio.lsi.uned.es` (Ubuntu Server 24.04), nodo de login del clúster del LSI-UNED.
- **Acceso**: sin privilegios de `sudo`. Todo se instala y ejecuta en el espacio de usuario.
- **Directrices del servidor**:
  - Todos los datos pesados (entornos, modelos, binarios grandes) deben guardarse en `/home/<user>/DATA`.
  - Evitar guardar en `/home/<user>` cualquier tipo de dato pesado.
  - El clúster GPU (`hermes`, `GPU-3`, `GPU-4`) se gestiona con **slurm** y se accede a través de `sirius`.
- **En este servidor**: `~/data` es un **symlink** a `/mnt/data/egarcia` (pool de 127 TB, 121 TB libres). Es el destino correcto para todo lo pesado.

---

## 1. Descarga del binario de Ollama

Se descargaron dos archivos en `~/data/download`:

```bash
# El .tgz resultó corrupto (9 bytes, ASCII text) → inservible
# El .tar.zst es válido (1.4 GB, Zstandard compressed data)
```

Verificación del tipo de archivo:

```bash
file ~/data/download/ollama-linux-amd64.tar.zst
# → Zstandard compressed data (v0.8+), Dictionary ID: None
```

Se eliminó el `.tgz` corrupto:

```bash
rm ~/data/download/ollama-linux-amd64.tgz
```

---

## 2. Extracción del programa en `~/data/.local`

Como `zstd` está disponible en el sistema (`/usr/bin/zstd`), se extrae directamente:

```bash
mkdir -p ~/.local
zstd -d -c ~/data/download/ollama-linux-amd64.tar.zst | tar -xf - -C ~/.local
```

Verificar la extracción:

```bash
ls ~/.local/bin/ollama
ls ~/.local/lib/ollama/ | head
```

Resultado esperado: binario `ollama` (~30 MB) y librerías (`libggml-*`, `cuda_v12/`, `cuda_v13/`, etc.).

---

## 3. Movimiento a `~/data` (cumplir directrices)

El contenido inicial estaba en `~/.local` (home), lo cual **viola las directrices**. Se mueve a `~/data/.local`:

```bash
mv ~/.local ~/data/.local
```

Verificar:

```bash
ls -la ~/data/.local/bin/ollama
du -shL ~/data/.local
ls -la ~/.local 2>/dev/null   # no debe existir
```

Resultado esperado:

- `/home/egarcia/data/.local/bin/ollama` existe
- `~/data/.local` ocupa ~1,8 GB
- `~/.local` ya no existe

---

## 4. Configuración de `~/.bashrc`

Se añade al final de `~/.bashrc` el siguiente bloque, **blindado contra acumulación** (el `source ~/.bashrc` se ejecuta varias veces y los `export` sin comprobación duplican rutas):

```bash
# Ollama: programa en ~/data (grande, no en home)
case ":$PATH:" in
  *":$HOME/data/.local/bin:"*) ;;
  *) export PATH="$HOME/data/.local/bin:$PATH" ;;
esac

# Solo añadir LD_LIBRARY_PATH si no está ya (evita acumulación)
case ":$LD_LIBRARY_PATH:" in
  *":$HOME/data/.local/lib/ollama:"*) ;;
  *) export LD_LIBRARY_PATH="$HOME/data/.local/lib/ollama${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
esac

# Ollama: modelos en ~/data
export OLLAMA_MODELS="$HOME/data/.ollama/models"
```

Recargar:

```bash
source ~/.bashrc
```

### Verificación del entorno

```bash
which ollama
# → /home/egarcia/data/.local/bin/ollama

echo $LD_LIBRARY_PATH
# → /home/egarcia/data/.local/lib/ollama

echo $OLLAMA_MODELS
# → /home/egarcia/data/.ollama/models

echo $PATH | tr ':' '\n' | grep -c "data/.local/bin"
# → 1 (una sola ocurrencia, sin duplicados)
```

### Comprobación anti-acumulación

```bash
source ~/.bashrc
source ~/.bashrc
source ~/.bashrc
echo $PATH | tr ':' '\n' | grep -c "data/.local/bin"
# → 1 (sigue siendo 1 tras múltiples source)
```

---

## 5. Creación del directorio de modelos

```bash
mkdir -p ~/data/.ollama/models
ls -la ~/data/.ollama/
```

Resultado esperado:

```
drwxrwxr-x 2 egarcia egarcia 2 ... models
```

---

## 6. Verificación del estado del home

```bash
du -sh ~ 2>/dev/null
# → 2,1 MB (home limpio, sin datos pesados)
```

> **Nota**: `du -sh ~/data` devuelve `0` porque `~/data` es un symlink y `du` no lo sigue por defecto. Usar `du -shL ~/data` para ver el tamaño real.

---

## 7. Arranque del servidor Ollama (test breve en `horacio`)

Para pruebas cortas, se usa `tmux` para que el proceso sobreviva al cierre de la sesión SSH:

```bash
tmux new -s ollama-test
```

Dentro de tmux:

```bash
ollama serve
```

Salida esperada (fragmento):

```
msg="Ollama cloud disabled: false"
msg="Listening on 127.0.0.1:11434 (version 0.40.6)"
msg="inference compute" id=cpu library=cpu ... total="125.8 GiB" available="123.3 GiB"
```

Salir de tmux sin matar el proceso:

```
Ctrl+B, luego D
```

### Observaciones del log

- `OLLAMA_MODELS:/home/egarcia/data/.ollama/models` → los modelos van a `data` ✅
- `Listening on 127.0.0.1:11434` → servidor activo en localhost ✅
- `inference compute id=cpu` → corriendo en CPU (esperado en `horacio`, sin GPU) ⚠️
- `total_vram="0 B"` → sin VRAM (CPU) ⚠️
- Se ha creado `~/.ollama/id_ed25519` (clave SSH, unos KB en home; aceptable, no es "dato pesado") ✅

---

## 8. Descarga de un modelo y prueba de inferencia

En **otra terminal** (con el servidor corriendo en tmux):

```bash
ollama pull llama3.2:3b
```

Verificar que el modelo va a `data`:

```bash
du -shL ~/data/.ollama/models
ls ~/data/.ollama/models
```

Probar una inferencia:

```bash
ollama run llama3.2:3b "Explain in one sentence what a physiological signal is."
```

En CPU, un modelo 3B tarda ~2-10 s por respuesta corta.

### Parar el servidor al terminar

```bash
tmux attach -t ollama-test
# Ctrl+C para parar ollama serve
# exit para cerrar tmux
```

---

## 9. Producción en GPU vía slurm

Para trabajo real (SLIP, comparaciones, inferencias largas), **no usar `horacio`**. Crear un script de slurm:

```bash
nano ~/data/ollama-job.sh
```

Contenido:

```bash
#!/bin/bash
#SBATCH --job-name=ollama
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=04:00:00
#SBATCH --output=%x-%j.out

export PATH="$HOME/data/.local/bin:$PATH"
export LD_LIBRARY_PATH="$HOME/data/.local/lib/ollama:$LD_LIBRARY_PATH"
export OLLAMA_MODELS="$HOME/data/.ollama/models"

ollama serve
```

Lanzar:

```bash
sbatch ~/data/ollama-job.sh
squeue -u $USER
```

Ver el nodo asignado y abrir túnel SSH desde el equipo local (a través de `sirius`):

```bash
ssh -L 11434:localhost:11434 <nodo-asignado>
```

> **Nota**: los detalles del `#SBATCH` (nombre exacto de la partición, módulos CUDA si fueran necesarios) dependen de la configuración del clúster. Consultar con el administrador si no se conocen.

---

## 10. Resumen de la configuración final

| Elemento | Ruta / Valor |
| :--- | :--- |
| Binario `ollama` | `/home/egarcia/data/.local/bin/ollama` |
| Librerías | `/home/egarcia/data/.local/lib/ollama/` |
| Modelos | `/home/egarcia/data/.ollama/models/` |
| `PATH` | contiene `~/data/.local/bin` (1 ocurrencia) |
| `LD_LIBRARY_PATH` | `~/data/.local/lib/ollama` |
| `OLLAMA_MODELS` | `~/data/.ollama/models` |
| Home | ~2,1 MB (limpio, sin datos pesados) |
| Servidor | `127.0.0.1:11434` (localhost) |
| Inferencia en `horacio` | CPU (123 GB RAM disponibles) |
| Inferencia en GPU | slurm → `hermes`/`GPU-3`/`GPU-4` |
| Versión | 0.40.6 |

---

## 11. Problemas encontrados y soluciones

| Problema | Causa | Solución |
| :--- | :--- | :--- |
| `gzip: stdin: not in gzip format` | El `.tgz` descargado era HTML (9 bytes), no un gzip | Usar el `.tar.zst` válido (1,4 GB) |
| `~/.local` con 2,2 GB en home | Instalación inicial en home | `mv ~/.local ~/data/.local` |
| `LD_LIBRARY_PATH` duplicado y con ruta antigua | `export` sin comprobación + bloque antiguo en `.bashrc` | `unset` + bloque `case` condicional |
| `PATH` con 4 ocurrencias de `data/.local/bin` | `source ~/.bashrc` múltiple | `case` condicional + limpieza manual |
| `du -sh ~/data` → 0 | `~/data` es symlink, `du` no lo sigue | Usar `du -shL ~/data` |
| Servidor en CPU, no GPU | `horacio` es nodo de login sin GPU | Usar slurm para producción |
| `Couldn't find ~/.ollama/id_ed25519` | Primera ejecución de `ollama serve` | Aceptable: se genera automáticamente (KBs) |

---

## 12. Comandos rápidos de referencia

```bash
# Verificar entorno
which ollama
echo $LD_LIBRARY_PATH
echo $OLLAMA_MODELS

# Arrancar servidor (test breve)
tmux new -s ollama-test
ollama serve
# Ctrl+B, D para detach

# Descargar modelo
ollama pull llama3.2:3b

# Probar inferencia
ollama run llama3.2:3b "pregunta"

# Ver modelos descargados
ollama list
du -shL ~/data/.ollama/models

# Parar servidor
tmux attach -t ollama-test
# Ctrl+C, luego exit

# Producción en GPU
sbatch ~/data/ollama-job.sh
squeue -u $USER
```

# Trafico HTTP por la VPN
Para que tu tráfico HTTP (navegación web, curl, etc.) salga a través de la VPN de la UNED, primero hay que distinguir si el servidor ya te está enviando todo el tráfico por el túnel (full tunnel) o solo el de la red interna (split tunnel). La mayoría de las VPN corporativas usan split tunnel por defecto, lo que significa que tu navegación normal sigue saliendo por tu conexión local.

Aquí tienes las tres formas de forzarlo, de la más limpia a la más contundente.

🔍 1. Comprobar si ya está saliendo por la VPN
Antes de tocar nada, verifica tu IP pública con la VPN conectada:

bash
curl ifconfig.me
Si la IP que devuelve pertenece a la UNED (o a su rango), ya estás en full tunnel y no necesitas hacer nada más.

Si devuelve tu IP doméstica o de tu red local, estás en split tunnel y tu tráfico HTTP no pasa por la VPN.

🛡️ Opción A (Recomendada): Proxy HTTP/SOCKS sobre el túnel
Esta es la forma más segura porque no modifica la tabla de rutas global (no arriesgas perder la conexión SSH ni la propia VPN) y solo enruta el tráfico que tú decidas.

Puedes usar tunsocks, una herramienta que crea un proxy HTTP y SOCKS5 local que sale a través de la interfaz tun0.

Instala tunsocks (en tu máquina local, donde tienes sudo):

bash
sudo apt install tunsocks
Lanza el proxy en una terminal (mientras la VPN está conectada):

bash
tunsocks -H 8080
Esto crea un proxy HTTP escuchando en 127.0.0.1:8080.

Configura tu navegador o aplicación para usar 127.0.0.1:8080 como proxy HTTP y HTTPS.

En Firefox: Preferencias → Configuración de red → Configuración manual del proxy → Proxy HTTP: 127.0.0.1, Puerto: 8080. Marca también "Usar este proxy para todos los protocolos".

En curl:

bash
curl -x http://127.0.0.1:8080 ifconfig.me
Si la IP que devuelve es de la UNED, el proxy está funcionando.

Para detenerlo, simplemente pulsa Ctrl+C en la terminal donde corre tunsocks.