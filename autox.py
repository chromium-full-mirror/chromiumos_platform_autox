#!/usr/bin/python
#
# Copyright (c) 2010 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import re
import select
import time

import Xlib.display
import Xlib.protocol.request
from Xlib import X
from Xlib import Xatom
from Xlib import XK
from Xlib.ext import xtest

class AutoX(object):
    """AutoX provides an interface for interacting with X applications.

    This is done by using the XTEST extension to inject events into the X
    server.  Convenience methods are also provided to query information
    about windows and to wait for testable conditions to be met.

    Example usage:

        import autox

        ax = autox.AutoX()
        ax.move_pointer(200, 100)
        ax.press_button(1)
        ax.move_pointer(250, 150)
        ax.release_button(1)

        ax.send_hotkey("Ctrl+L")
        ax.send_text("http://www.example.org/\n")

        # Create a window and wait for it to get the focus.
        win = ax.create_and_map_window(width=200, height=200, title='test')
        info = ax.get_window_info(win.id)
        ax.await_condition(
            lambda: info.is_focused,
            desc='Waiting for window 0x%x to be focused' % win.id)
        win.destroy()

        # Create an override-redirect window and check that it appears in
        # the position that it requested.
        popup_win = ax.create_and_map_window(
            x=200, y=200, width=200, height=200,
            title='popup', override_redirect=True)
        popup_info = ax.get_window_info(popup_win.id)
        ax.await_condition(
            lambda: popup_info.get_geometry() == (200, 200, 200, 200),
            desc='Checking window 0x%x\'s geometry' % popup_win.id)
        popup_win.destroy()
    """

    # Map of characters that can be passed to send_text() that differ
    # from their X keysym names.
    __chars_to_keysyms = {
        ' ': 'space',
        '\n': 'Return',
        '\t': 'Tab',
        '~': 'asciitilde',
        '!': 'exclam',
        '@': 'at',
        '#': 'numbersign',
        '$': 'dollar',
        '%': 'percent',
        '^': 'asciicircum',
        '&': 'ampersand',
        '*': 'asterisk',
        '(': 'parenleft',
        ')': 'parenright',
        '-': 'minus',
        '_': 'underscore',
        '+': 'plus',
        '=': 'equal',
        '{': 'braceleft',
        '[': 'bracketleft',
        '}': 'braceright',
        ']': 'bracketright',
        '|': 'bar',
        ':': 'colon',
        ';': 'semicolon',
        '"': 'quotedbl',
        '\'': 'apostrophe',
        ',': 'comma',
        '<': 'less',
        '.': 'period',
        '>': 'greater',
        '/': 'slash',
        '?': 'question',
    }

    class Error(Exception):
        """Base exception class for AutoX."""
        pass

    class RuntimeError(Error):
        """Error caused by a (possibly temporary) condition at runtime."""
        pass

    class InputError(Error):
        """Error caused by invalid input from the caller."""
        pass

    class InvalidKeySymError(InputError):
        """Error caused by the caller referencing an invalid keysym."""
        def __init__(self, keysym):
            self.__keysym = keysym

        def __str__(self):
            return "Invalid keysym \"%s\"" % self.__keysym

    class ConditionTimeoutError(Error):
        """Error caused by a test condition timing out."""
        pass

    class WindowInfo:
        """Container for the latest information we've seen about a window."""
        def __init__(self, x, y, width, height, expose_callback=None):
            self.x = x
            self.y = y
            self.width = width
            self.height = height
            self.expose_callback = expose_callback
            self.was_exposed = False
            self.is_focused = False

        def get_geometry(self):
            """Get a tuple containing the window's position and dimensions.

            Returns:
                tuple of ints: (x, y, width, height)
            """
            return (self.x, self.y, self.width, self.height)

    def __init__(self, display_name=None):
        self.__display = Xlib.display.Display(display_name)
        self.__root = self.__display.screen().root
        self.__windows = {}

        # Make sure that we get notified about property changes on the root
        # window, since the caller may wait on conditions that use them.
        self.__root.change_attributes(event_mask=X.PropertyChangeMask)

    def __get_keycode_for_keysym(self, keysym):
        """Get the keycode corresponding to a keysym.

        Args:
            keysym: keysym name as str

        Returns:
            integer keycode

        Raises:
            InvalidKeySymError: keysym name isn't an actual keycode
            RuntimeError: unable to map the keysym to a keycode (maybe it
                isn't present in the current keymap)
        """
        keysym_num = XK.string_to_keysym(keysym)
        if keysym_num == XK.NoSymbol:
            raise self.InvalidKeySymError(keysym)
        keycode = self.__display.keysym_to_keycode(keysym_num)
        if not keycode:
            raise self.RuntimeError(
                'Unable to map keysym "%s" to a keycode' % keysym)
        return keycode

    def __keysym_requires_shift(self, keysym):
        """Does a keysym require that a shift key be held down?

        Args:
            keysym: keysym name as str

        Returns:
            True or False

        Raises:
            InvalidKeySymError: keysym name isn't an actual keycode
            RuntimeError: unable to map the keysym to a keycode (maybe it
                isn't present in the current keymap)
        """
        keysym_num = XK.string_to_keysym(keysym)
        if keysym_num == XK.NoSymbol:
            raise self.InvalidKeySymError(keysym)
        # This gives us a list of (keycode, index) tuples, sorted by index and
        # then by keycode.  Index 0 is without any modifiers, 1 is with Shift,
        # 2 is with Mode_switch, and 3 is Mode_switch and Shift.
        keycodes = self.__display.keysym_to_keycodes(keysym_num)
        if not keycodes:
            raise self.RuntimeError(
                'Unable to map keysym "%s" to a keycode' % keysym)
        # We don't use Mode_switch for anything, at least currently, so just
        # check if the first index is unshifted.
        return keycodes[0][1] != 0

    def __handle_key_command(keysym, key_press):
        """Looks up the keycode for a keysym and presses or releases it.

        Helper method for press_key() and release_key().

        Args:
            keysym: keysym name as str
            key_press: True to send key press; False to send release

        Raises:
            InputError: input was invalid; details in exception
            InvalidKeySymError, RuntimeError: see __get_keycode_for_keysym()
        """
        keycode = self.__get_keycode_for_keysym(keysym)
        if self.__keysym_requires_shift(keysym):
            raise self.InputError(
                'Keysym "%s" requires the Shift key to be held.  Either use '
                'send_text() or make separate calls to press/release_key(), '
                'one for Shift_L and then one for the keycode\'s non-shifted '
                'keysym' % keysym)

        type = X.KeyPress if key_press else X.KeyRelease
        xtest.fake_input(self.__display, type, detail=keycode)
        self.__display.sync()

    def __convert_escaped_string_to_keysym(self, escaped_string):
        """Read an escaped keysym name from the beginning of a string.

        Helper method called by send_text().

        Args:
            escaped_string: str prefixed with a backslash followed by a
                keysym name in parens, e.g. "\\(Return)more text"

        Returns:
            tuple consisting of the keysym name and the number of
            characters that should be skipped to get to the next character
            in the string (including the leading backslash).  For example,
            "\\(Space)blah" yields ("Space", 8).

        Raises:
            InputError: unable to find an escaped keysym-looking thing at
                the beginning of the string
        """
        if escaped_string[0] != '\\':
            raise self.InputError('Escaped string is missing backslash')
        if len(escaped_string) < 2:
            raise self.InputError('Escaped string is too short')
        if escaped_string[1] == '\\':
            return ('backslash', 2)
        if escaped_string[1] != '(':
            raise self.InputError('Escaped string is missing opening paren')

        end_index = escaped_string.find(')')
        if end_index == -1 or end_index == 2:
            raise self.InputError('Escaped string is missing closing paren')
        return (escaped_string[2:end_index], end_index + 1)

    def __convert_char_to_keysym(self, char):
        """Convert a character into its keysym name.

        Args:
            char: str of length 1 containing the character to be looked up

        Returns:
            keysym name as str

        Raises:
            InputError: received non-length-1 string
            InvalidKeySymError: character wasn't a keysym that we know about
                (this may just mean that it needs to be added to
                '__chars_to_keysyms')
        """
        if len(char) != 1:
            raise self.InputError('Got non-length-1 string "%s"' % char)
        if char.isalnum():
            # Letters and digits are easy.
            return char
        if char in AutoX.__chars_to_keysyms:
            return AutoX.__chars_to_keysyms[char]
        raise self.InvalidKeySymError(char)

    def await_condition(self, condition, desc='', timeout_sec=10.0):
        """Wait until a condition becomes true.

        We call the condition whenever we receive events from the X server
        and return when it becomes true.

        Args:
            condition: callable object taking no args and returning a bool
            desc: str describing the condition; just used in exception
            timeout_sec: maximum time to wait for the condition

        Raises:
            ConditionTimeoutError: condition didn't occur before timeout
        """
        end_time = time.time() + timeout_sec
        fd = self.__display.fileno()

        while True:
            self.sync()
            if condition():
                return

            remaining_time = end_time - time.time()
            if remaining_time <= 0:
                break

            (rfds, wfds, xfds) = select.select([fd], [], [], remaining_time)
            if not rfds:
                break

        raise AutoX.ConditionTimeoutError(desc)

    def sync(self):
        """Flush X request queue and process all pending events.
        """
        self.__display.sync()
        while self.__display.pending_events():
            event = self.__display.next_event()
            if event.type == X.ConfigureNotify:
                info = self.__windows[event.window.id]
                info.x = event.x
                info.y = event.y
                info.width = event.width
                info.height = event.height
            elif event.type == X.DestroyNotify:
                del self.__windows[event.window.id]
            elif event.type == X.Expose:
                info = self.__windows[event.window.id]
                info.was_exposed = True
                if info.expose_callback:
                    info.expose_callback(event)
                else:
                    event.window.clear_area(
                            event.x, event.y, event.width, event.height)
            elif event.type == X.FocusIn or event.type == X.FocusOut:
                self.__windows[event.window.id].is_focused = \
                    (event.type == X.FocusIn)


    def get_pointer_position(self):
        """Get the pointer's absolute position.

        Returns:
            (x, y) integer tuple
        """
        reply = Xlib.protocol.request.QueryPointer(
            display=self.__display.display, window=self.__root)
        return (reply.root_x, reply.root_y)

    def press_button(self, button):
        """Press a mouse button.

        Args:
            button: 1-indexed mouse button to press
        """
        xtest.fake_input(self.__display, X.ButtonPress, detail=button)
        self.__display.sync()

    def release_button(self, button):
        """Release a mouse button.

        Args:
            button: 1-indexed mouse button to release
        """
        xtest.fake_input(self.__display, X.ButtonRelease, detail=button)
        self.__display.sync()

    def move_pointer(self, x, y):
        """Move the mouse pointer to an absolute position.

        Args:
            x, y: integer position relative to the root window's origin
        """
        xtest.fake_input(self.__display, X.MotionNotify, x=x, y=y)
        self.__display.sync()

    def send_hotkey(self, hotkey):
        """Send a combination of keystrokes.

        Args:
            hotkey: str describing a '+' or '-'-separated sequence of
               keysyms, e.g. "Control_L+Alt_L+R" or "Ctrl-J".  Several
               aliases are accepted:

               Ctrl  -> Control_L
               Alt   -> Alt_L
               Shift -> Shift_L

               Whitespace is permitted around individual keysyms.

        Raises:
            InputError: hotkey sequence contained an error
            InvalidKeySymError, RuntimeError: see __get_keycode_for_keysym()
        """
        # Did the shift key occur in the combination?
        saw_shift = False
        keycodes = []

        regexp = re.compile('[-+]')
        for keysym in regexp.split(hotkey):
            keysym = keysym.strip()

            if keysym == 'Ctrl':
                keysym = 'Control_L'
            elif keysym == 'Alt':
                keysym = 'Alt_L'
            elif keysym == 'Shift':
                keysym = 'Shift_L'

            if keysym == 'Shift_L' or keysym == 'Shift_R':
                saw_shift = True

            keycode = self.__get_keycode_for_keysym(keysym)

            # Bail if we're being asked to press a key that requires Shift and
            # the Shift key wasn't pressed already (but let it slide if they're
            # just asking for an uppercase letter).
            if self.__keysym_requires_shift(keysym) and not saw_shift and \
                (len(keysym) != 1 or keysym < 'A' or keysym > 'Z'):
                    raise self.InputError(
                        'Keysym "%s" requires the Shift key to be held, '
                        'but it wasn\'t seen earlier in the key combo. '
                        'Either press Shift first or using the keycode\'s '
                        'non-shifted keysym instead' % keysym)

            keycodes.append(keycode)

        # Press the keys in the correct order and then reverse them in the
        # opposite order.
        for keycode in keycodes:
            xtest.fake_input(self.__display, X.KeyPress, detail=keycode)
        for keycode in reversed(keycodes):
            xtest.fake_input(self.__display, X.KeyRelease, detail=keycode)
        self.__display.sync()

    def press_key(self, keysym):
        """Press the key corresponding to a keysym.

        Args:
            keysym: keysym name as str
        """
        self.__handle_key_command(keysym, True)  # key_press=True

    def release_key(self, keysym):
        """Release the key corresponding to a keysym.

        Args:
            keysym: keysym name as str
        """
        self.__handle_key_command(keysym, False)  # key_press=False

    def send_text(self, text):
        """Type a sequence of characters.

        Args:
            text: sequence of characters to type.  Along with individual
                single-byte characters, keysyms can be embedded by
                preceding them with "\\(" and suffixing them with ")", e.g.
                "first line\\(Return)second line"

        Raises:
            InputError: text string contained invalid input
            InvalidKeySymError, RuntimeError: see __get_keycode_for_keysym()
        """
        shift_keycode = self.__get_keycode_for_keysym('Shift_L')
        shift_pressed = False

        i = 0
        while i < len(text):
            ch = text[i:i+1]
            keysym = None
            if ch == '\\':
                (keysym, num_chars_to_skip) = \
                    self.__convert_escaped_string_to_keysym(text[i:])
                i += num_chars_to_skip
            else:
                keysym = self.__convert_char_to_keysym(ch)
                i += 1

            keycode = self.__get_keycode_for_keysym(keysym)

            # Press or release the shift key as needed for this keysym.
            shift_required = self.__keysym_requires_shift(keysym)
            if shift_required and not shift_pressed:
                xtest.fake_input(
                    self.__display, X.KeyPress, detail=shift_keycode)
                shift_pressed = True
            elif not shift_required and shift_pressed:
                xtest.fake_input(
                    self.__display, X.KeyRelease, detail=shift_keycode)
                shift_pressed = False

            xtest.fake_input(self.__display, X.KeyPress, detail=keycode)
            xtest.fake_input(self.__display, X.KeyRelease, detail=keycode)

        if shift_pressed:
            xtest.fake_input(
                self.__display, X.KeyRelease, detail=shift_keycode)
        self.__display.sync()

    def create_and_map_window(self, x=0, y=0, width=200, height=200,
                              title=None, override_redirect=False,
                              expose_callback=None):
        """Create and map a window.

        Waits until the window has been exposed before returning.

        Args:
            x, y: int position of window
            width, height: int dimensions of window
            title: str containing the window's title
            override_redirect: whether this is an override-redirect
                ("popup", in GTK's parlance) window.  override-redirect
                windows are mapped, placed, and sized without any window
                manager involvement.

        Returns:
            python-xlib Window object
        """
        # Sync before creating the window.  It's possible that we're
        # reusing the ID from an already-destroyed window that we haven't
        # seen a DestroyNotify event about yet, and we want to make sure
        # that its WindowInfo object gets cleaned up before we register ours.
        self.sync()

        win = self.__root.create_window(
            x, y, width, height, border_width=0,
            depth=X.CopyFromParent,
            override_redirect=override_redirect,
            background_pixel=self.__display.screen().white_pixel,
            event_mask = (X.ExposureMask |
                          X.FocusChangeMask |
                          X.StructureNotifyMask))
        info = self.WindowInfo(
            x, y, width, height, expose_callback=expose_callback)
        self.__windows[win.id] = info

        if title:
            win.set_wm_name(title)
            utf8_atom = self.__display.get_atom('UTF8_STRING')
            win.change_property(
                    self.__display.get_atom('_NET_WM_NAME'),
                    utf8_atom, 8, data=title)

        win.map()
        self.await_condition(
            lambda: info.was_exposed,
            desc='Waiting for window 0x%x to be exposed' % win.id)

        return win

    def get_window_info(self, window_id):
        """Get an object containing information about a window.

        Args:
            window_id: int ID of the window

        Returns:
            WindowInfo object (should be treated as read-only)
        """
        return self.__windows[window_id]

    def get_top_window_id_at_point(self, x, y):
        """Get the ID of the topmost mapped toplevel window at a given point.

        Note that under reparenting window managers, this may not be the
        client window that you're expecting.

        Args:
            x, y: int position of point

        Returns:
            int containing the ID of the window at the point, or 0 if no
            window is there
        """
        reply = self.__root.query_tree()
        for win in reversed(reply.children):
            attr = win.get_attributes()
            if attr.map_state == X.IsViewable:
                geom = win.get_geometry()
                if (geom.x <= x and geom.y <= y and
                    geom.x + geom.width > x and geom.y + geom.height > y):
                    return win.id
        return 0

    def get_active_window_property(self):
        """Get the root window's _NET_ACTIVE_WINDOW property.

        Returns:
            int window ID from the property, or None if unset
        """
        reply = self.__root.get_property(
            self.__display.get_atom('_NET_ACTIVE_WINDOW'), Xatom.WINDOW, 0, 1)
        if not reply:
            return None
        return reply.value[0]

    def get_screen_size(self):
        """Get the current dimensions of the root window.

        Returns:
            tuple with two ints: (width, height)
        """
        reply = self.__root.get_geometry()
        return (reply.width, reply.height)
