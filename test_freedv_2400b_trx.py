#!/usr/bin/env python3
"""Unit tests for DTR and half-duplex sequencing; no radio is required."""

import os
import termios
import unittest

from freedv_2400b_trx import DtrPtt, PttStateMachine


class FakeTimer:
    def __init__(self, callback):
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class FakeScheduler:
    def __init__(self):
        self.calls = []

    def call_later(self, delay_ms, callback):
        timer = FakeTimer(callback)
        self.calls.append((delay_ms, timer))
        return timer

    def fire_last(self):
        self.calls[-1][1].fire()


class FakeDtr:
    def __init__(self):
        self.connected = False
        self.device = None
        self.events = []
        self.fail_on_tx = False

    def connect(self, device):
        self.connected = True
        self.device = device
        self.events.append(("connect", device))

    def set_tx(self, enabled):
        if enabled and self.fail_on_tx:
            raise OSError("USB desconectado")
        self.events.append(("dtr", enabled))

    def close(self):
        if self.connected:
            self.events.append(("close", False))
        self.connected = False
        self.device = None


class DtrBackendTest(unittest.TestCase):
    def test_connect_key_release_and_close(self):
        calls = []

        def fake_open(device, flags):
            calls.append(("open", device, flags))
            return 17

        def fake_ioctl(fd, request, _arg):
            calls.append(("ioctl", fd, request))

        def fake_close(fd):
            calls.append(("close", fd))

        dtr = DtrPtt(fake_open, fake_close, fake_ioctl)
        dtr.connect("/dev/ttyACM9")
        dtr.set_tx(True)
        dtr.set_tx(False)
        dtr.close()

        self.assertEqual(calls[0][0:2], ("open", "/dev/ttyACM9"))
        self.assertTrue(calls[0][2] & os.O_NOCTTY)
        requests = [call[2] for call in calls if call[0] == "ioctl"]
        self.assertEqual(
            requests,
            [
                termios.TIOCMBIC,
                termios.TIOCMBIS,
                termios.TIOCMBIC,
                termios.TIOCMBIC,
            ],
        )
        self.assertEqual(calls[-1], ("close", 17))


class PttStateMachineTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.dtr = FakeDtr()
        self.scheduler = FakeScheduler()
        self.ptt = PttStateMachine(
            self.dtr,
            self.scheduler,
            250,
            150,
            lambda enabled: self.events.append(("tx_audio", enabled)),
            lambda enabled: self.events.append(("rx_audio", enabled)),
            lambda state, detail="": self.events.append(("state", state)),
        )
        self.ptt.connect("/dev/ttyACM0")
        self.events.clear()
        self.dtr.events.clear()

    def test_normal_press_and_release_order(self):
        self.ptt.press()
        self.assertEqual(self.ptt.state, PttStateMachine.PREKEY)
        self.assertEqual(self.dtr.events[0], ("dtr", True))
        self.assertEqual(self.scheduler.calls[-1][0], 250)

        self.scheduler.fire_last()
        self.assertEqual(self.ptt.state, PttStateMachine.TX)
        self.assertIn(("tx_audio", True), self.events)

        self.events.clear()
        self.ptt.release()
        self.assertEqual(self.events[0], ("tx_audio", False))
        self.assertEqual(self.ptt.state, PttStateMachine.TAIL)
        self.assertEqual(self.scheduler.calls[-1][0], 150)
        self.scheduler.fire_last()
        self.assertEqual(self.dtr.events[-1], ("dtr", False))
        self.assertEqual(self.ptt.state, PttStateMachine.RX)

    def test_release_during_prekey_never_enables_audio(self):
        self.ptt.press()
        lead_timer = self.scheduler.calls[-1][1]
        self.ptt.release()
        lead_timer.fire()
        self.assertNotIn(("tx_audio", True), self.events)
        self.scheduler.fire_last()
        self.assertEqual(self.ptt.state, PttStateMachine.RX)

    def test_repress_during_tail_starts_a_new_lead(self):
        self.ptt.press()
        self.scheduler.fire_last()
        self.ptt.release()
        tail_timer = self.scheduler.calls[-1][1]
        self.ptt.press()
        tail_timer.fire()
        self.assertNotEqual(self.dtr.events[-1], ("dtr", False))
        self.assertEqual(self.scheduler.calls[-1][0], 250)
        self.scheduler.fire_last()
        self.assertEqual(self.ptt.state, PttStateMachine.TX)

    def test_usb_error_is_fail_safe(self):
        self.dtr.fail_on_tx = True
        self.ptt.press()
        self.assertEqual(self.ptt.state, PttStateMachine.ERROR)
        self.assertFalse(self.dtr.connected)
        self.assertIn(("tx_audio", False), self.events)

    def test_close_cancels_timer_and_releases_backend(self):
        self.ptt.press()
        timer = self.scheduler.calls[-1][1]
        self.ptt.close()
        timer.fire()
        self.assertEqual(self.ptt.state, PttStateMachine.CLOSED)
        self.assertFalse(self.dtr.connected)
        self.assertNotIn(("tx_audio", True), self.events)


if __name__ == "__main__":
    unittest.main()
