# FreeDV 2400B con dos HT VHF

Este ejemplo transporta voz digital entre dos radios portátiles FM mediante sus
conectores de micrófono y parlante. Usa los bloques FreeDV incluidos en GNU
Radio 3.10.12, sin módulos externos.

## Velocidades y modulación

FreeDV 2400B combina Codec2 a 1300 bit/s, sincronismo, un canal de texto y
protección Golay (23,12) dentro de un módem de 2400 bit/s. Cuatro estados
transportan dos bits por símbolo, por lo que la velocidad equivalente es:

```text
2400 bit/s / 2 bit/símbolo = 1200 símbolos/s (1200 baudios)
```

No son 600 baudios. Para transportar 2400 bit/s a 600 baudios harían falta
cuatro bits por símbolo, es decir, 16 estados.

Hay una diferencia física importante entre los modos FreeDV:

- **2400A** genera 4-FSK de banda ancha para un transmisor SDR.
- **2400B** genera banda base para la entrada de micrófono de una radio FM. El
  modulador FM del HT convierte sus cuatro niveles en cuatro desviaciones de
  frecuencia de RF. Este es el modo previsto para radios FM comerciales.

Por eso la señal 2400B que circula por el cable de audio no debe interpretarse
como cuatro tonos sinusoidales independientes, aunque el enlace de radio sea
4-FSK.

## Archivos

- `freedv_2400b_loopback.grc` y `.py`: prueba local con micrófono y parlante.
- `freedv_2400b_tx.grc` y `.py`: estación transmisora.
- `freedv_2400b_rx.grc` y `.py`: estación receptora.
- `freedv_2400b_trx.py`: transceptor half-duplex con PTT por DTR.
- `test_freedv_2400b.py`: prueba automática sin dispositivos de audio.
- `test_freedv_2400b_trx.py`: pruebas del backend DTR y su temporización.

Los `.py` se generan desde GNU Radio Companion. Para regenerarlos:

```bash
grcc freedv_2400b_tx.grc freedv_2400b_rx.grc freedv_2400b_loopback.grc -o .
```

## 1. Prueba de loopback

Primero lista los dispositivos de audio disponibles, por ejemplo con:

```bash
aplay -L
arecord -L
```

Luego ejecuta el loopback. Una cadena vacía usa el dispositivo predeterminado:

```bash
python3 freedv_2400b_loopback.py \
  --audio-in 'default' \
  --audio-out 'default'
```

Habla de forma continua durante algunos segundos. El receptor necesita adquirir
sincronismo antes de entregar voz. El control **Ruido del canal** permite
degradar la señal y **Ganancia del canal** simula atenuación. El filtro interno
limita el trayecto a 300–3000 Hz, como una cadena de audio de radio típica.

Conviene usar auriculares para evitar realimentación acústica.

## 2. Conexión de dos estaciones

Cada estación necesita una computadora o proceso GNU Radio y una interfaz de
audio conectada al HT correspondiente.

```text
Estación A                              Estación B
micrófono -> TX -> salida de audio -> entrada MIC del HT A
salida SPK del HT B -> entrada de audio -> RX -> auriculares
```

En la estación transmisora:

```bash
python3 freedv_2400b_tx.py \
  --audio-in 'DISPOSITIVO_MICROFONO' \
  --audio-out 'DISPOSITIVO_HACIA_HT'
```

En la receptora:

```bash
python3 freedv_2400b_rx.py \
  --audio-in 'DISPOSITIVO_DESDE_HT' \
  --audio-out 'DISPOSITIVO_AURICULARES'
```

Los mismos parámetros aparecen al ejecutar cada programa con `--help`.

## Niveles y configuración del HT

1. Usa aislamiento de audio y el cableado correcto para los niveles e
   impedancias de micrófono/parlante de cada equipo.
2. Comienza con **Nivel TX hacia el HT = 0.10** y volumen bajo. Aumenta sólo
   hasta conseguir decodificación estable; una entrada saturada destruye la
   forma de onda.
3. Ajusta el volumen de recepción y luego **Ganancia de entrada RX** sin llevar
   la señal a los límites de ±1 mostrados en la gráfica.
4. Desactiva compander, ecualización, reducción de ruido y otros procesamientos
   de voz si el HT lo permite. No uses CTCSS/DCS durante la primera prueba.
5. Mantén ambos equipos en el mismo ancho de canal y comienza la prueba a corta
   distancia o con carga artificial/atenuación apropiada.
6. El ejemplo no controla PTT. Acciona PTT manualmente o usa VOX; deja un breve
   margen antes de hablar para que se establezcan el transmisor y el sincronismo.

No conectes directamente una salida de parlante de potencia a una entrada de
línea o micrófono sin atenuación y aislamiento adecuados.

## Verificación automática

La prueba usa voz sintética, primero con un canal ideal y después con filtro de
300–3000 Hz, atenuación y ruido:

```bash
python3 -m unittest -v test_freedv_2400b.py
```

La prueba confirma que el módem adquiere sincronismo y produce audio decodificado
con energía medible. La evaluación final de calidad debe hacerse escuchando el
loopback y luego mediante dos HT.

Respeta la normativa, identificación y bandas autorizadas para tu servicio de
radio.

## Transceptor half-duplex con PTT por DTR

`freedv_2400b_trx.py` combina recepción y transmisión en una sola ventana. Usa
cuatro dispositivos lógicos de audio:

```text
Headset MIC -> FreeDV TX -> salida de la interfaz de radio -> MIC del HT
SPK del HT -> entrada de la interfaz de radio -> FreeDV RX -> Headset SPK
```

Al abrir el transceptor, selecciona las cuatro interfaces ALSA antes de pulsar
**Iniciar audio**. Los selectores se llenan con `arecord -L` para las entradas y
`aplay -L` para las salidas e incluyen el nombre legible de cada placa. También
aceptan nombres ALSA escritos manualmente.
Mientras el audio está activo la selección queda bloqueada. Pulsa **Detener
audio** para liberar las interfaces y poder cambiarlas.

La ventana está organizada para pantallas de 1024x600: los controles quedan a
la izquierda y el espectro a la derecha; el divisor central permite ajustar el
ancho de ambos paneles. En pantallas más bajas, la columna de
controles permite desplazamiento y el ajuste del umbral de squelch se despliega
con el botón **Umbral**.

El panel derecho muestra dos espectros ajustables: la señal intercambiada con
el HT y la voz de micrófono junto con la voz FreeDV demodulada. El campo
**Texto TX FreeDV** transmite cíclicamente un texto ASCII de hasta 80 caracteres
(por ejemplo, indicativo y ubicación). El historial **Texto RX FreeDV** muestra
los mensajes recibidos. El texto TX se aplica al iniciar audio; para cambiarlo,
detén e inicia nuevamente el audio.

También se pueden preseleccionar desde la línea de comandos:

```bash
python3 freedv_2400b_trx.py \
  --mic-in 'hw:CARD=Headset,DEV=0' \
  --speaker-out 'default:CARD=Headset' \
  --radio-in 'hw:CARD=Pro,DEV=0' \
  --radio-out 'default:CARD=Pro' \
  --tx-text 'LU1ABC LOC GF00'
```

La lista **PTT USB CDC** busca primero nombres estables en
`/dev/serial/by-id/` y después `/dev/ttyACM*` y `/dev/ttyUSB*`. Selecciona el
puerto y pulsa **Conectar**. La aplicación limpia DTR al conectar; el botón PTT
permanece deshabilitado hasta que el puerto esté abierto.

También se puede indicar el puerto al arrancar:

```bash
python3 freedv_2400b_trx.py \
  --ptt-device '/dev/serial/by-id/USB_CDC_CORRESPONDIENTE'
```

El PTT es momentáneo: mantén presionado el botón o la barra espaciadora. La
secuencia predeterminada es:

```text
Pulsar:  silenciar RX -> activar DTR -> esperar 250 ms -> habilitar audio TX
Soltar:  cortar audio TX -> esperar 150 ms -> desactivar DTR -> habilitar RX
```

Los retardos pueden ajustarse con `--ptt-lead-ms` y `--ptt-tail-ms`. Al perder
el foco, cerrar la ventana, recibir SIGINT/SIGTERM o detectar un error del CDC,
la aplicación corta primero el audio TX e intenta liberar DTR.

Antes de conectar el PTT al HT, verifica con un LED o multímetro que DTR se
activa únicamente durante TX. Algunos controladores USB CDC pueden producir un
pulso corto al abrir el dispositivo; el programa limpia DTR inmediatamente,
pero no puede eliminar un transitorio generado dentro del controlador. Un
SIGKILL, corte de energía o fallo físico del USB tampoco permite ejecutar la
limpieza de software, por lo que el circuito debe liberar PTT al desaparecer
DTR.

Para ver todas las opciones:

```bash
python3 freedv_2400b_trx.py --help
```

Para ejecutar todas las pruebas sin radio ni puerto serie:

```bash
python3 -m unittest -v test_freedv_2400b.py test_freedv_2400b_trx.py
```
