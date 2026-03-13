"""Mouse service for hints.

This service is an independent application that hints calls using a Unix
Domain Socket to perform mouse movements by writing to uinput. We use
custom uinput devices to support X11 and Wayland. This is separate from
the main hints application to prevent slowing down the main hints
process when creating virutal devices.
"""

from __future__ import annotations

import socket
from os import path, remove
from pickle import dumps, loads
from signal import SIGINT, signal
from time import sleep, time
from typing import TYPE_CHECKING, Any, Iterable

# from evdev import AbsInfo, UInput, ecodes
import subprocess

from gi import require_version

from hints.constants import SOCKET_MESSAGE_SIZE, UNIX_DOMAIN_SOCKET_FILE
from hints.mouse_enums import MouseButton, MouseMode
from hints.utils import load_config

require_version("Gdk", "3.0")
require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

if TYPE_CHECKING:
    from hints.mouse_enums import MouseButtonState

MOUSE_SERVICE_LOOP_MS_INTERVAL = 10
config = load_config()


_BUTTON_MAP = {
    272: 1,  # BTN_LEFT
    273: 3,  # BTN_RIGHT
    274: 2,  # BTN_MIDDLE
    275: 8,
    276: 9,
}

class Mouse:
    """Mouse class for performing mouse actions (click, hover, move, etc).

    This uses xdotool
    """

    def __init__(self, abs_max_width=10000, abs_max_height=10000, write_pause=0.03):
        self.write_pause = write_pause
        self.abs_max_width = abs_max_width
        self.abs_max_height = abs_max_height

    def scroll(self, x: int, y: int, *_args, **_kwargs):
         """Scroll event.

        :param x: X scroll direction.
        :param y: Y scroll direction. :param *_args: Extra args to use
            the same interface as move. :param **_kwargs: Extra kwargs
            to use the same interface as move.
        """
        if y < 0:
            subprocess.run(["xdotool", "click", "4"])  # scroll up
        elif y > 0:
            subprocess.run(["xdotool", "click", "5"])  # scroll down
        if x < 0:
            subprocess.run(["xdotool", "click", "6"])  # scroll left
        elif x > 0:
            subprocess.run(["xdotool", "click", "7"])  # scroll right

    def move(self, x: int, y: int, absolute: bool = True):
        """Move event.

        :param X: X move direction.
        :param y: Y move direction.
        :param absolute: Whether to move the mouse using an absolute
            position.
        """
        if absolute:
            subprocess.run(["xdotool", "mousemove", str(int(x)), str(int(y))])
        else:
            subprocess.run(["xdotool", "mousemove_relative", "--", str(int(x)), str(int(y))])
        sleep(self.write_pause)

    def click(
        self,
        x: int,
        y: int,
        button: MouseButton,
        button_states: Iterable[MouseButtonState],
        repeat: int = 1,
        absolute: bool = True,
    ):
        """Click event.

        :param x: X position to click.
        :param y: Y position to click.
        :param button: Button to use for click.
        :param actions: Actions to use for the click button (button down
            / button up).
        :param repeat: Times to repeat a click.
        :param absolute: Whether the click position is absolute.
        """
        self.move(x, y, absolute=absolute)
        btn = _BUTTON_MAP.get(button, 1)
        for _ in range(repeat):
            for state in button_states:
                # state 1 = press, 0 = release (evdev KEY_DOWN/KEY_UP)
                cmd = "mousedown" if state == 1 else "mouseup"
                subprocess.run(["xdotool", cmd, str(btn)])
                sleep(self.write_pause)

    def do_mouse_action(
        self,
        key_press_state: dict[str, Any],
        key: str,
        mode: MouseMode,
    ):
        """Perform mouse action.

        :param key_press_state: State containing key press event data
            used for ramping up speeds.
        :param key: The key to perform a mouse action for.
        :param mode: The mouse mode.
        """
        sensitivity = 1
        rampup_time = 1
        mouse_navigation_action = self.move
        left, right, up, down = "h", "l", "k", "j"

        if mode == MouseMode.MOVE.value:
            sensitivity = config["mouse_move_pixel_sensitivity"]
            rampup_time = config["mouse_move_rampup_time"]
            left = config["mouse_move_left"]
            right = config["mouse_move_right"]
            up = config["mouse_move_down"]
            down = config["mouse_move_up"]
            mouse_navigation_action = self.move

        elif mode == MouseMode.SCROLL.value:
            sensitivity = config["mouse_scroll_pixel_sensitivity"]
            rampup_time = config["mouse_scroll_rampup_time"]
            left = config["mouse_scroll_left"]
            right = config["mouse_scroll_right"]
            up = config["mouse_scroll_up"]
            down = config["mouse_scroll_down"]
            mouse_navigation_action = self.scroll

        key_press_state.setdefault("sensitivity", sensitivity)
        if time() - key_press_state["start_time"] >= rampup_time:
            key_press_state["sensitivity"] += sensitivity

        if key == left:
            mouse_navigation_action(-key_press_state["sensitivity"], 0, absolute=False)
        if key == right:
            mouse_navigation_action(key_press_state["sensitivity"], 0, absolute=False)
        if key == up:
            mouse_navigation_action(0, key_press_state["sensitivity"], absolute=False)
        if key == down:
            mouse_navigation_action(0, -key_press_state["sensitivity"], absolute=False)

        return key_press_state


class MouseService:
    """Mouse Service.

    This is responsible for running the mouse service and detecting
    events requring the mouse devices to reload / be updated.
    """

    def __init__(self):
        """Mouse Service Constructor."""
        Gtk.init()

        self.screen = Gdk.Screen.get_default()
        self.mouse = Mouse(self.screen.get_width(), self.screen.get_height())

        if path.exists(UNIX_DOMAIN_SOCKET_FILE):
            remove(UNIX_DOMAIN_SOCKET_FILE)

        self.socket = socket.socket(
            socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_NONBLOCK
        )
        self.socket.bind(UNIX_DOMAIN_SOCKET_FILE)
        self.socket.listen(1)
        GLib.timeout_add(MOUSE_SERVICE_LOOP_MS_INTERVAL, self.socket_connection)

        self.screen.connect("size-changed", self.on_size_changed)
        signal(SIGINT, self.on_interrupt)

    def on_interrupt(self, *_):
        """Interrupt handler to clean up."""
        self.socket.close()
        Gtk.main_quit()

    def on_size_changed(self, screen: Gdk.Screen):
        """Screen size change event handler to update the mouse device min/max
        values for correct absolute position movement.

        :param screen: The screen object for the event.
        """
        self.mouse = Mouse(screen.get_width(), screen.get_height())

    def socket_connection(self):
        """Handle socket connection events.

        This is how the main hints process and the mouse service
        communicate.
        """
        try:
            connection, _ = self.socket.accept()
            payload = loads(connection.recv(SOCKET_MESSAGE_SIZE))
            method = payload.get("method", "")
            args = payload.get("args", ())
            kwargs = payload.get("kwargs", {})
            connection.send(
                dumps(
                    {
                        "click": self.mouse.click,
                        "move": self.mouse.move,
                        "scoll": self.mouse.scroll,
                        "do_mouse_action": self.mouse.do_mouse_action,
                    }[method](*args, **kwargs)
                )
            )
        except BlockingIOError:
            pass

        return GLib.SOURCE_CONTINUE

    def run(self):
        """Run the mouse service."""
        Gtk.main()


def main():
    """Mouse service entry point."""
    MouseService().run()


if __name__ == "__main__":
    main()
