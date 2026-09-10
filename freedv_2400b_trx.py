#!/usr/bin/env python3
"""FreeDV 2400B half-duplex transceiver with USB CDC DTR PTT."""

import argparse
import array
import fcntl
import glob
import os
import signal
import sys
import termios

from PyQt5 import Qt, QtCore
from gnuradio import audio, blocks, filter, gr, qtgui, vocoder
from gnuradio.fft import window
from gnuradio.vocoder import freedv_api
import sip


AUDIO_RATE = 48_000
SPEECH_RATE = 8_000


def discover_serial_devices():
    """Return stable USB serial names first, followed by kernel device names."""
    patterns = (
        "/dev/serial/by-id/*",
        "/dev/ttyACM*",
        "/dev/ttyUSB*",
    )
    devices = []
    for pattern in patterns:
        for device in sorted(glob.glob(pattern)):
            if device not in devices:
                devices.append(device)
    return devices


class DtrPtt:
    """Small Linux DTR backend; it never writes data to the serial port."""

    def __init__(self, open_fn=os.open, close_fn=os.close, ioctl_fn=fcntl.ioctl):
        self._open = open_fn
        self._close = close_fn
        self._ioctl = ioctl_fn
        self._fd = None
        self.device = None
        self._dtr_arg = array.array("i", [termios.TIOCM_DTR])

    @property
    def connected(self):
        return self._fd is not None

    def connect(self, device):
        if not device:
            raise ValueError("Debe seleccionar un puerto PTT")
        self.close()
        fd = self._open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self._fd = fd
        self.device = device
        try:
            self.set_tx(False)
        except BaseException:
            self.close()
            raise

    def set_tx(self, enabled):
        if self._fd is None:
            raise RuntimeError("El puerto PTT no está conectado")
        request = termios.TIOCMBIS if enabled else termios.TIOCMBIC
        self._ioctl(self._fd, request, self._dtr_arg)

    def close(self):
        fd = self._fd
        self._fd = None
        self.device = None
        if fd is None:
            return
        try:
            try:
                self._ioctl(fd, termios.TIOCMBIC, self._dtr_arg)
            except OSError:
                # The USB device may already be gone. Closing the descriptor is
                # still mandatory and is the only remaining fail-safe action.
                pass
        finally:
            self._close(fd)


class _QtTimerHandle:
    def __init__(self, delay_ms, callback):
        self.timer = QtCore.QTimer()
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(callback)
        self.timer.start(max(0, int(delay_ms)))

    def cancel(self):
        self.timer.stop()


class QtScheduler:
    def call_later(self, delay_ms, callback):
        return _QtTimerHandle(delay_ms, callback)


class PttStateMachine:
    """Orders DTR and audio gating with configurable radio lead/tail times."""

    DISCONNECTED = "DESCONECTADO"
    RX = "RX"
    PREKEY = "PREKEY"
    TX = "TX"
    TAIL = "TAIL"
    ERROR = "ERROR"
    CLOSED = "CERRADO"

    def __init__(
        self,
        dtr,
        scheduler,
        lead_ms,
        tail_ms,
        set_tx_audio,
        set_rx_audio,
        state_changed,
    ):
        self.dtr = dtr
        self.scheduler = scheduler
        self.lead_ms = max(0, int(lead_ms))
        self.tail_ms = max(0, int(tail_ms))
        self.set_tx_audio = set_tx_audio
        self.set_rx_audio = set_rx_audio
        self.state_changed = state_changed
        self.state = self.DISCONNECTED
        self._timer = None
        self._held = False

    def _set_state(self, state, detail=""):
        self.state = state
        self.state_changed(state, detail)

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def connect(self, device):
        self.disconnect()
        try:
            self.dtr.connect(device)
            self.set_tx_audio(False)
            self.set_rx_audio(True)
            self._set_state(self.RX, device)
        except BaseException as exc:
            self._fail(exc)
            raise

    def disconnect(self):
        self._held = False
        self._cancel_timer()
        self.set_tx_audio(False)
        try:
            self.dtr.close()
        finally:
            self.set_rx_audio(True)
            if self.state != self.CLOSED:
                self._set_state(self.DISCONNECTED)

    def press(self):
        if self._held or not self.dtr.connected:
            return
        self._held = True
        self._cancel_timer()
        self.set_tx_audio(False)
        self.set_rx_audio(False)
        try:
            self.dtr.set_tx(True)
            self._set_state(self.PREKEY)
            self._timer = self.scheduler.call_later(
                self.lead_ms, self._finish_prekey
            )
        except BaseException as exc:
            self._fail(exc)

    def _finish_prekey(self):
        self._timer = None
        if self._held and self.dtr.connected:
            self.set_tx_audio(True)
            self._set_state(self.TX)

    def release(self):
        if not self._held:
            return
        self._held = False
        self._cancel_timer()
        self.set_tx_audio(False)
        if not self.dtr.connected:
            self.set_rx_audio(True)
            return
        self._set_state(self.TAIL)
        self._timer = self.scheduler.call_later(self.tail_ms, self._finish_tail)

    def _finish_tail(self):
        self._timer = None
        try:
            self.dtr.set_tx(False)
            self.set_rx_audio(True)
            self._set_state(self.RX, self.dtr.device or "")
        except BaseException as exc:
            self._fail(exc)

    def _fail(self, exc):
        self._held = False
        self._cancel_timer()
        self.set_tx_audio(False)
        try:
            self.dtr.close()
        except BaseException:
            pass
        self.set_rx_audio(True)
        self._set_state(self.ERROR, str(exc))

    def close(self):
        self._held = False
        self._cancel_timer()
        self.set_tx_audio(False)
        try:
            self.dtr.close()
        finally:
            self.set_rx_audio(False)
            self._set_state(self.CLOSED)


class FreeDV2400BTrx(gr.top_block, Qt.QWidget):
    def __init__(
        self,
        mic_in,
        speaker_out,
        radio_in,
        radio_out,
        ptt_device=None,
        ptt_lead_ms=250,
        ptt_tail_ms=150,
        dtr=None,
    ):
        gr.top_block.__init__(self, "FreeDV 2400B TRX", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("FreeDV 2400B — TRX")
        qtgui.util.check_set_qss()

        self.tx_gain = 0.10
        self.rx_gain = 1.0
        self.monitor_gain = 1.0
        self.squelch_thresh = 2.0
        self.squelch_enable = True
        self._tx_audio_enabled = False
        self._rx_audio_enabled = True
        self._shutdown_done = False

        self._build_layout()
        self._build_dsp(mic_in, speaker_out, radio_in, radio_out)

        self.dtr = dtr or DtrPtt()
        self.ptt = PttStateMachine(
            self.dtr,
            QtScheduler(),
            ptt_lead_ms,
            ptt_tail_ms,
            self._set_tx_audio,
            self._set_rx_audio,
            self._show_state,
        )

        self.refresh_serial_devices(ptt_device)
        Qt.QApplication.instance().installEventFilter(self)
        Qt.QApplication.instance().applicationStateChanged.connect(
            self._application_state_changed
        )
        if ptt_device:
            QtCore.QTimer.singleShot(0, self.toggle_serial_connection)

    def _build_layout(self):
        root = Qt.QVBoxLayout(self)

        serial_row = Qt.QHBoxLayout()
        serial_row.addWidget(Qt.QLabel("PTT USB CDC:"))
        self.serial_combo = Qt.QComboBox()
        self.serial_combo.setEditable(True)
        serial_row.addWidget(self.serial_combo, 1)
        refresh_button = Qt.QPushButton("Actualizar")
        refresh_button.clicked.connect(self.refresh_serial_devices)
        serial_row.addWidget(refresh_button)
        self.connect_button = Qt.QPushButton("Conectar")
        self.connect_button.clicked.connect(self.toggle_serial_connection)
        serial_row.addWidget(self.connect_button)
        root.addLayout(serial_row)

        self.state_label = Qt.QLabel("DESCONECTADO")
        self.state_label.setAlignment(QtCore.Qt.AlignCenter)
        self.state_label.setMinimumHeight(42)
        root.addWidget(self.state_label)

        self.ptt_button = Qt.QPushButton("MANTENER PARA TRANSMITIR\nPTT / ESPACIO")
        self.ptt_button.setMinimumHeight(100)
        self.ptt_button.setEnabled(False)
        self.ptt_button.pressed.connect(self.ptt_pressed)
        self.ptt_button.released.connect(self.ptt_released)
        root.addWidget(self.ptt_button)

        self._tx_gain_range = qtgui.Range(0.0, 1.0, 0.01, self.tx_gain, 200)
        self._tx_gain_widget = qtgui.RangeWidget(
            self._tx_gain_range,
            self.set_tx_gain,
            "Nivel TX hacia el HT",
            "counter_slider",
            float,
            QtCore.Qt.Horizontal,
        )
        root.addWidget(self._tx_gain_widget)

        self._rx_gain_range = qtgui.Range(0.05, 4.0, 0.05, self.rx_gain, 200)
        self._rx_gain_widget = qtgui.RangeWidget(
            self._rx_gain_range,
            self.set_rx_gain,
            "Ganancia desde el HT",
            "counter_slider",
            float,
            QtCore.Qt.Horizontal,
        )
        root.addWidget(self._rx_gain_widget)

        self._monitor_gain_range = qtgui.Range(
            0.0, 2.0, 0.05, self.monitor_gain, 200
        )
        self._monitor_gain_widget = qtgui.RangeWidget(
            self._monitor_gain_range,
            self.set_monitor_gain,
            "Volumen de escucha",
            "counter_slider",
            float,
            QtCore.Qt.Horizontal,
        )
        root.addWidget(self._monitor_gain_widget)

        squelch_row = Qt.QHBoxLayout()
        self.squelch_box = Qt.QCheckBox("Squelch FreeDV")
        self.squelch_box.setChecked(self.squelch_enable)
        self.squelch_box.toggled.connect(self.set_squelch_enable)
        squelch_row.addWidget(self.squelch_box)
        root.addLayout(squelch_row)

        self._squelch_range = qtgui.Range(
            -5.0, 20.0, 0.5, self.squelch_thresh, 200
        )
        self._squelch_widget = qtgui.RangeWidget(
            self._squelch_range,
            self.set_squelch_thresh,
            "Umbral de squelch (dB)",
            "counter_slider",
            float,
            QtCore.Qt.Horizontal,
        )
        root.addWidget(self._squelch_widget)

        self.main_layout = root
        self._show_state(PttStateMachine.DISCONNECTED)

    def _build_dsp(self, mic_in, speaker_out, radio_in, radio_out):
        # TX: operator microphone -> Codec2/FreeDV -> radio audio output.
        self.mic_source = audio.source(AUDIO_RATE, mic_in, True)
        self.tx_resampler = filter.rational_resampler_fff(
            interpolation=1, decimation=6, taps=[], fractional_bw=0.4
        )
        self.tx_float_to_short = blocks.float_to_short(1, 32_767)
        self.freedv_tx = vocoder.freedv_tx_ss(
            freedv_api.MODE_2400B, "GNU Radio 2400B", 1
        )
        self.tx_short_to_float = blocks.short_to_float(1, 32_768)
        self.tx_level = blocks.multiply_const_ff(self.tx_gain)
        self.tx_gate = blocks.multiply_const_ff(0.0)
        self.radio_sink = audio.sink(AUDIO_RATE, radio_out, True)

        self.connect(
            self.mic_source,
            self.tx_resampler,
            self.tx_float_to_short,
            self.freedv_tx,
            self.tx_short_to_float,
            self.tx_level,
            self.tx_gate,
            self.radio_sink,
        )

        # RX: radio audio input -> FreeDV/Codec2 -> operator headphones.
        self.radio_source = audio.source(AUDIO_RATE, radio_in, True)
        self.rx_level = blocks.multiply_const_ff(self.rx_gain)
        self.rx_float_to_short = blocks.float_to_short(1, 32_767)
        self.freedv_rx = vocoder.freedv_rx_ss(
            freedv_api.MODE_2400B, self.squelch_thresh, 1
        )
        self.freedv_rx.set_squelch_en(self.squelch_enable)
        self.rx_short_to_float = blocks.short_to_float(1, 32_768)
        self.rx_resampler = filter.rational_resampler_fff(
            interpolation=6, decimation=1, taps=[], fractional_bw=0.4
        )
        self.monitor_level = blocks.multiply_const_ff(self.monitor_gain)
        self.monitor_gate = blocks.multiply_const_ff(1.0)
        self.speaker_sink = audio.sink(AUDIO_RATE, speaker_out, True)

        self.connect(
            self.radio_source,
            self.rx_level,
            self.rx_float_to_short,
            self.freedv_rx,
            self.rx_short_to_float,
            self.rx_resampler,
            self.monitor_level,
            self.monitor_gate,
            self.speaker_sink,
        )

        self.spectrum = qtgui.freq_sink_f(
            2048,
            window.WIN_BLACKMAN_hARRIS,
            0,
            AUDIO_RATE,
            "Audio de radio — RX/TX",
            2,
            None,
        )
        self.spectrum.set_update_time(0.10)
        self.spectrum.set_y_axis(-120, 10)
        self.spectrum.set_line_label(0, "RX desde HT")
        self.spectrum.set_line_label(1, "TX hacia HT")
        self.spectrum.set_line_color(0, "blue")
        self.spectrum.set_line_color(1, "red")
        self.spectrum.enable_grid(True)
        spectrum_widget = sip.wrapinstance(self.spectrum.qwidget(), Qt.QWidget)
        self.main_layout.addWidget(spectrum_widget, 1)
        self.connect(self.rx_level, (self.spectrum, 0))
        self.connect(self.tx_gate, (self.spectrum, 1))

    def refresh_serial_devices(self, preferred=None):
        current = preferred or self.serial_combo.currentText().strip()
        devices = discover_serial_devices()
        self.serial_combo.blockSignals(True)
        self.serial_combo.clear()
        self.serial_combo.addItems(devices)
        if current:
            if self.serial_combo.findText(current) < 0:
                self.serial_combo.addItem(current)
            self.serial_combo.setCurrentText(current)
        self.serial_combo.blockSignals(False)

    def toggle_serial_connection(self):
        if self.dtr.connected:
            self.ptt.disconnect()
            return
        device = self.serial_combo.currentText().strip()
        try:
            self.ptt.connect(device)
        except BaseException:
            pass

    def ptt_pressed(self):
        self.ptt.press()

    def ptt_released(self):
        self.ptt.release()

    def _set_tx_audio(self, enabled):
        self._tx_audio_enabled = bool(enabled)
        if hasattr(self, "tx_gate"):
            self.tx_gate.set_k(1.0 if enabled else 0.0)

    def _set_rx_audio(self, enabled):
        self._rx_audio_enabled = bool(enabled)
        if hasattr(self, "monitor_gate"):
            self.monitor_gate.set_k(1.0 if enabled else 0.0)

    def set_tx_gain(self, value):
        self.tx_gain = float(value)
        if hasattr(self, "tx_level"):
            self.tx_level.set_k(self.tx_gain)

    def set_rx_gain(self, value):
        self.rx_gain = float(value)
        if hasattr(self, "rx_level"):
            self.rx_level.set_k(self.rx_gain)

    def set_monitor_gain(self, value):
        self.monitor_gain = float(value)
        if hasattr(self, "monitor_level"):
            self.monitor_level.set_k(self.monitor_gain)

    def set_squelch_enable(self, enabled):
        self.squelch_enable = bool(enabled)
        if hasattr(self, "freedv_rx"):
            self.freedv_rx.set_squelch_en(self.squelch_enable)

    def set_squelch_thresh(self, value):
        self.squelch_thresh = float(value)
        if hasattr(self, "freedv_rx"):
            self.freedv_rx.set_squelch_thresh(self.squelch_thresh)

    def _show_state(self, state, detail=""):
        colors = {
            PttStateMachine.DISCONNECTED: ("#555", "white"),
            PttStateMachine.RX: ("#176b2c", "white"),
            PttStateMachine.PREKEY: ("#b36b00", "white"),
            PttStateMachine.TX: ("#a00000", "white"),
            PttStateMachine.TAIL: ("#b36b00", "white"),
            PttStateMachine.ERROR: ("#700070", "white"),
            PttStateMachine.CLOSED: ("#333", "white"),
        }
        background, foreground = colors.get(state, ("#555", "white"))
        text = state if not detail else f"{state} — {detail}"
        self.state_label.setText(text)
        self.state_label.setStyleSheet(
            f"font-size: 18px; font-weight: bold; color: {foreground}; "
            f"background: {background}; padding: 6px;"
        )
        connected = state in {
            PttStateMachine.RX,
            PttStateMachine.PREKEY,
            PttStateMachine.TX,
            PttStateMachine.TAIL,
        }
        self.ptt_button.setEnabled(connected)
        self.connect_button.setText("Desconectar" if connected else "Conectar")

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.KeyPress:
            if event.key() == QtCore.Qt.Key_Space and not event.isAutoRepeat():
                self.ptt_pressed()
                return True
        elif event.type() == QtCore.QEvent.KeyRelease:
            if event.key() == QtCore.Qt.Key_Space and not event.isAutoRepeat():
                self.ptt_released()
                return True
        return Qt.QWidget.eventFilter(self, obj, event)

    def _application_state_changed(self, state):
        if state != QtCore.Qt.ApplicationActive:
            self.ptt_released()

    def shutdown(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.ptt.close()
        self.stop()
        self.wait()

    def closeEvent(self, event):
        self.shutdown()
        event.accept()


def argument_parser():
    parser = argparse.ArgumentParser(
        description="FreeDV 2400B half-duplex TRX with DTR PTT"
    )
    parser.add_argument("--mic-in", default="hw:CARD=Headset,DEV=0")
    parser.add_argument("--speaker-out", default="default:CARD=Headset")
    parser.add_argument("--radio-in", default="hw:CARD=Pro,DEV=0")
    parser.add_argument("--radio-out", default="default:CARD=Pro")
    parser.add_argument("--ptt-device")
    parser.add_argument("--ptt-lead-ms", type=int, default=250)
    parser.add_argument("--ptt-tail-ms", type=int, default=150)
    return parser


def main(options=None):
    options = options or argument_parser().parse_args()
    app = Qt.QApplication(sys.argv)
    top = FreeDV2400BTrx(
        mic_in=options.mic_in,
        speaker_out=options.speaker_out,
        radio_in=options.radio_in,
        radio_out=options.radio_out,
        ptt_device=options.ptt_device,
        ptt_lead_ms=options.ptt_lead_ms,
        ptt_tail_ms=options.ptt_tail_ms,
    )
    top.start()
    top.show()

    def stop_application(*_args):
        top.shutdown()
        app.quit()

    signal.signal(signal.SIGINT, stop_application)
    signal.signal(signal.SIGTERM, stop_application)
    heartbeat = QtCore.QTimer()
    heartbeat.start(500)
    heartbeat.timeout.connect(lambda: None)
    app.aboutToQuit.connect(top.shutdown)
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
