# Instalación en la Raspberry Pi 5 (desde cero)

Al terminar, la Pi 5 hace todo sola al prenderse:

- **`smartgreenai-backend`**: la API y la página web en el puerto 8000.
- **`smartgreenai-gateway`**: lee el Arduino por USB y manda las lecturas a la
  API (MON-03). Si la API o la red fallan, guarda todo en su buffer.

El dashboard se abre desde cualquier laptop en la misma red:
`http://<ip-de-la-pi>:8000` (o `http://smartgreen.local:8000`).

```
Arduino ──USB──▶ Raspberry Pi 5 ─┬─ gateway  (lee, pone hora, buffer, envía)
                                 └─ backend  (API + BD + página)  ◀── laptop: http://<ip>:8000
```

## Qué necesitas

- Raspberry Pi 5 con su fuente USB-C oficial (27 W) y una microSD de 16 GB o más.
- El Arduino Uno con el firmware `vivero_sensoresV2.ino` y su cable USB.
- Una laptop con [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
- **Internet para la Pi**: la hora sale solo de internet (NTP), porque la Pi 5
  no trae pila de reloj. Recomendado: el **hotspot de un celular**. La WiFi de
  la uni puede pedir iniciar sesión en una página web, y la Pi no puede hacerlo
  sola (ver [Problemas comunes](#problemas-comunes)).

## 1. Grabar la microSD (en la laptop)

1. Abre Raspberry Pi Imager y elige:
   - **Dispositivo:** Raspberry Pi 5.
   - **Sistema operativo:** Raspberry Pi OS (64-bit). La versión con escritorio
     sirve de plan B (monitor HDMI); la "Lite" también funciona.
   - **Almacenamiento:** tu microSD.
2. Cuando te ofrezca **personalizar** el sistema, llena:
   - **Hostname:** `smartgreen`
   - **Zona horaria:** `America/Mexico_City`; teclado `latam` o `es`.
   - **Usuario y contraseña:** los que quieran (por ejemplo `radish`). Anótenlos.
   - **WiFi:** el nombre y la contraseña del hotspot (o de la red que usarán).
   - **SSH:** actívalo, con contraseña.
3. Graba y espera a que termine la verificación.

## 2. Primer arranque y conexión por SSH

1. Mete la microSD en la Pi, conecta el Arduino por USB y luego la fuente.
2. Espera unos 2 minutos. La laptop debe estar en **la misma red** (el hotspot).
3. En la laptop abre **CMD** y entra a la Pi (cambia `radish` por su usuario):

   ```bat
   ssh radish@smartgreen.local
   ```

   La primera vez pregunta `Are you sure you want to continue connecting?`:
   escribe `yes` y luego la contraseña (no se ve mientras escribes).
   Si `smartgreen.local` no responde, busca la IP de la Pi en la lista de
   dispositivos conectados del hotspot y usa `ssh radish@<ip>`.

Todo lo que sigue se escribe **dentro de esa sesión SSH** (la terminal de la Pi).

4. **Manda al equipo** la salida de estos comandos (con el Arduino conectado):

   ```bash
   cat /etc/os-release
   python3 --version
   ls /dev/serial/by-id/
   timedatectl
   ```

   En `timedatectl` debe decir `System clock synchronized: yes`. Si dice `no`,
   la Pi no tiene internet todavía.

## 3. Descargar el proyecto

```bash
git clone -b prueba https://github.com/EmiCas3/Arduino.git ~/Arduino
cd ~/Arduino
```

> Si el repo es privado, GitHub pide usuario y un **token** (no la contraseña
> de la cuenta): se crea en GitHub → Settings → Developer settings → Personal
> access tokens.

## 4. Instalar

```bash
cd ~/Arduino
SMARTGREENAI_SEED_PASSWORD='Rabano2026!' bash deploy/pi5/install.sh
```

Tarda unos minutos. Hace esto, en 7 pasos:

1. Instala `python3-venv`, `git` y `sqlite3` (pide tu contraseña de la Pi).
2. Te agrega al grupo `dialout` (permiso para leer el Arduino).
3. Crea el entorno de Python del backend (`backend/.venv`).
4. Crea el del gateway (`gateway/.venv`, solo `pyserial`).
5. Crea `backend/.env` con un secreto JWT **fijo** (las sesiones ya no se
   cierran al reiniciar), la ruta de la base de datos y
   `SMARTGREENAI_READING_STALE_MINUTES=2` (el dashboard marca un dato como
   viejo a los 2 min sin lecturas nuevas).
6. Corre el seed (usuarios `productor`, `admin` y `superadmin` con la
   contraseña que pusiste, el gateway `pi-vivero-01` y los actuadores) y
   **guarda la API key del gateway en `gateway/.env` automáticamente**.
7. Instala y arranca los dos servicios.

Al final debe decir:

```
  smartgreenai-backend: activo
  smartgreenai-gateway: activo
 Listo. Abre desde la laptop (misma red):
   http://192.168.x.x:8000        o   http://smartgreen.local:8000
 Arduino detectado: /dev/serial/by-id/usb-Arduino...
 Reloj sincronizado por internet: ...
```

> Sin `SMARTGREENAI_SEED_PASSWORD`, el seed genera contraseñas al azar y las
> muestra **una sola vez** en pantalla: cópialas.

## 5. Comprobar que funciona

1. Estado del gateway:

   ```bash
   cd ~/Arduino && gateway/.venv/bin/python -m gateway status
   ```

   Debe decir `Servicio: corriendo`, `Reloj: sincronizado`, `Última línea: hace
   N s` y `Enviadas` subiendo cada 10 s.

2. En el navegador de la laptop abre `http://smartgreen.local:8000` (o con la
   IP), entra como `productor` / `Rabano2026!`. En "Mi vivero" deben verse las
   5 tarjetas (temperatura, humedad del aire, humedad de la tierra, nivel del
   tanque y luz) con su valor, "hace N s" y su estado, y cambiar solas cada
   15 s. En `http://smartgreen.local:8000/docs` está la API.

> **Si la Pi se instaló antes de DASH-01**, su `backend/.env` no tiene el
> ajuste de "dato viejo" (`install.sh` no toca un `.env` que ya existe).
> Agrégalo una vez:
>
> ```bash
> echo 'SMARTGREENAI_READING_STALE_MINUTES=2' >> ~/Arduino/backend/.env
> sudo systemctl restart smartgreenai-backend
> ```

## 6. Demo de los criterios de MON-03 (corte de red)

```bash
sudo systemctl stop smartgreenai-backend          # "se cae" el backend
sleep 120                                         # esperar 2 minutos
cd ~/Arduino && gateway/.venv/bin/python -m gateway status
#   → Pendientes: ~60, Último error: sin conexión ...  (nada se perdió)
sudo systemctl start smartgreenai-backend         # vuelve
journalctl -u smartgreenai-gateway -f             # Ctrl+C para salir
#   → "Conexión con el backend recuperada" y "Lote enviado" hasta Pendientes: 0
```

El reintento espera cada vez más (5 s, 10 s, 20 s… hasta 5 min), así que puede
tardar un par de minutos en notar que el backend volvió. Para no esperar:
`sudo systemctl restart smartgreenai-gateway` (el buffer se conserva).

Las lecturas quedan guardadas con **la hora en que se leyeron**, no con la
del envío: en el dashboard no hay hueco en esos 2 minutos.

## Día a día

| Para | Comando (en la Pi) |
|---|---|
| Ver si los servicios corren | `systemctl status smartgreenai-backend smartgreenai-gateway` |
| Log del gateway en vivo | `journalctl -u smartgreenai-gateway -f` |
| Log del backend | `journalctl -u smartgreenai-backend -n 100` |
| Estado del buffer | `cd ~/Arduino && gateway/.venv/bin/python -m gateway status` |
| Reiniciar | `sudo systemctl restart smartgreenai-backend smartgreenai-gateway` |
| Apagar la Pi sin dañar la SD | `sudo shutdown now` (y espera a que se apague el LED verde) |
| **Actualizar** tras cambios en GitHub | `cd ~/Arduino && git pull && bash deploy/pi5/install.sh` |

Actualizar con `install.sh` es seguro: no toca los `.env`, la base de datos
ni el buffer.

Para probar el Arduino a mano (por ejemplo con el Monitor Serie), primero
detén el gateway: **solo un programa a la vez puede abrir el puerto**.
`sudo systemctl stop smartgreenai-gateway` y al terminar `start`.

## Problemas comunes

**`Reloj: SIN sincronizar` / no se envía nada.** La Pi no tiene internet, así
que no sabe la hora. Las lecturas se guardan como `sin_hora` y salen con su
hora correcta en cuanto sincroniza. Revisa con `timedatectl`. Si la WiFi de la
uni pide iniciar sesión en una página web, usa el hotspot del celular o un
cable Ethernet. Si la Pi se reinicia **sin** haber tenido internet, esas
lecturas ya no se pueden fechar: se quedan en el buffer y `status` lo dice.

**La laptop no abre `http://...:8000`.** Algunas WiFi (la de la uni, por
ejemplo) no dejan que los equipos se hablen entre sí. Usa el hotspot, o
conecta un monitor HDMI a la Pi y abre `http://localhost:8000` en su navegador.

**`No encuentro el Arduino`.** Revisa el cable (algunos solo cargan, no pasan
datos) y corre `ls /dev/serial/by-id/`. Si el Monitor Serie u otro programa
tiene el puerto abierto, ciérralo.

**`El backend rechazó la API key (HTTP 401)`.** La `SG_API_KEY` de
`gateway/.env` no corresponde a la BD. Ver "Perdí la API key".

**Perdí la API key.** El seed la muestra una sola vez. Para generar una nueva
(la BD vieja se conserva como respaldo):

```bash
cd ~/Arduino
sudo systemctl stop smartgreenai-gateway smartgreenai-backend
mv backend/smartgreenai.db backend/smartgreenai.db.respaldo
sed -i 's/^SG_API_KEY=.*/SG_API_KEY=/' gateway/.env
SMARTGREENAI_SEED_PASSWORD='Rabano2026!' bash deploy/pi5/install.sh
```

**Un servicio no arranca.** `journalctl -u smartgreenai-backend -n 50` (o
`-gateway`) muestra el error. Lo más común: se movió la carpeta `~/Arduino`
(vuelve a correr `install.sh`).

## Archivos de esta carpeta

| Archivo | Qué es |
|---|---|
| `install.sh` | La instalación de arriba. Se puede repetir |
| `smartgreenai-backend.service` | Plantilla del servicio del backend (`install.sh` pone tu usuario y ruta) |
| `smartgreenai-gateway.service` | Plantilla del servicio del gateway |

Los servicios corren con tu usuario (no como root) y se reinician solos si
fallan. Si algún día el backend se va a la nube, en la Pi solo queda el
gateway y se cambia `SG_API_URL` en `gateway/.env`.
