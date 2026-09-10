#!/usr/bin/env python3
"""Pruebas sin placa de sonido para el núcleo FreeDV 2400B."""

import unittest

import numpy as np
from gnuradio import blocks, filter, gr, vocoder
from gnuradio.fft import window
from gnuradio.filter import firdes
from gnuradio.vocoder import freedv_api


SPEECH_RATE = 8_000
MODEM_RATE = 48_000
TEST_SECONDS = 4


def synthetic_speech():
    """Señal determinista con energía dentro de la banda de voz."""
    time = np.arange(SPEECH_RATE * TEST_SECONDS) / SPEECH_RATE
    signal = 0.65 * np.sin(2 * np.pi * 440 * time)
    signal += 0.35 * np.sin(2 * np.pi * 700 * time)
    return np.asarray(12_000 * signal, dtype=np.int16)


def run_modem(channel):
    """Ejecuta Codec2/FreeDV y devuelve las muestras de voz decodificadas."""
    flowgraph = gr.top_block()
    source = blocks.vector_source_s(synthetic_speech().tolist(), False)
    transmitter = vocoder.freedv_tx_ss(
        freedv_api.MODE_2400B, "TEST 2400B", 1
    )
    receiver = vocoder.freedv_rx_ss(freedv_api.MODE_2400B, -100.0, 1)
    receiver.set_squelch_en(False)
    sink = blocks.vector_sink_s()

    flowgraph.connect(source, transmitter)
    channel(flowgraph, transmitter, receiver)
    flowgraph.connect(receiver, sink)
    flowgraph.run()
    return np.asarray(sink.data(), dtype=np.int16)


def ideal_channel(flowgraph, transmitter, receiver):
    flowgraph.connect(transmitter, receiver)


def filtered_noisy_channel(flowgraph, transmitter, receiver):
    short_to_float = blocks.short_to_float(1, 32_768)
    band_pass = filter.fir_filter_fff(
        1,
        firdes.band_pass(
            0.8,
            MODEM_RATE,
            300,
            3_000,
            150,
            window.WIN_HAMMING,
            6.76,
        ),
    )
    # La longitud cubre exactamente los cuatro segundos de salida del módem.
    noise = np.random.default_rng(42).normal(
        0.0, 0.002, MODEM_RATE * TEST_SECONDS
    )
    noise_source = blocks.vector_source_f(noise.astype(np.float32).tolist(), False)
    add = blocks.add_ff()
    float_to_short = blocks.float_to_short(1, 32_767)

    flowgraph.connect(transmitter, short_to_float, band_pass, (add, 0))
    flowgraph.connect(noise_source, (add, 1))
    flowgraph.connect(add, float_to_short, receiver)


class FreeDV2400BTest(unittest.TestCase):
    def assert_audio_was_decoded(self, decoded):
        self.assertGreater(len(decoded), SPEECH_RATE * (TEST_SECONDS - 1))
        self.assertGreater(np.max(np.abs(decoded)), 500)
        self.assertGreater(np.mean(np.abs(decoded)), 100)

    def test_ideal_loopback(self):
        self.assert_audio_was_decoded(run_modem(ideal_channel))

    def test_filtered_noisy_loopback(self):
        self.assert_audio_was_decoded(run_modem(filtered_noisy_channel))


if __name__ == "__main__":
    unittest.main()
