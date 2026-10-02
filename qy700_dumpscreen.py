#!/usr/bin/env python3
"""
qy700_dumpscreen - capture the Yamaha QY700's screen over MIDI and save it as a PNG.

Uses the QY700's hidden screen-dump feature:
  * Enable it by holding [OCT DOWN] + [OCT UP] + the lowest keyboard key (E2) while powering on.
  * This tool sends  F0 43 10 18 5A 01 F7  and the QY700 replies with the LCD contents
    (F0 43 30 <19200 nibbles> F7, about 19 KB - takes roughly 6-7 seconds at MIDI speed).

Requirements:  Python 3 and python-rtmidi       ->   pip3 install python-rtmidi

Usage:
    python3 qy700_dumpscreen.py                  interactive: pick ports, then SPACE to dump, 1/2/3 scale, Q quit
    python3 qy700_dumpscreen.py --in 1 --out 2   skip the port menus
    options: --scale N (starting PNG scale 1-3, default 1; keys 1/2/3 change it)   --syx (also save the raw SysEx)
             --timeout S (seconds to wait for a reply, default 12)   --dir PATH (where to save)
"""
import argparse, datetime, os, struct, sys, time, zlib

try:
    import rtmidi
except ImportError:
    sys.exit("This tool needs the python-rtmidi module.\nInstall it with:\n\n    pip3 install python-rtmidi\n")

REQUEST = [0xF0, 0x43, 0x10, 0x18, 0x5A, 0x01, 0xF7]
HEADER = (0xF0, 0x43, 0x30)
W, H = 320, 240
EXPECTED = 3 + W * H // 4 + 1          # F0 43 30 + 19200 nibbles + F7 = 19204 bytes

# ---------------------------------------------------------------------------------------------- UI helpers
BOLD, DIM, GREEN, YELLOW, RED, CYAN, RESET = ('\033[1m', '\033[2m', '\033[32m', '\033[33m',
                                              '\033[31m', '\033[36m', '\033[0m')
if not sys.stdout.isatty() or os.environ.get('NO_COLOR'):
    BOLD = DIM = GREEN = YELLOW = RED = CYAN = RESET = ''


def say(msg=''):
    print(msg, flush=True)


class KeyReader:
    """Read single key presses without needing Enter (macOS / Linux / Windows)."""

    def __enter__(self):
        if os.name == 'nt':
            import msvcrt
            self._get = lambda: msvcrt.getwch()
        else:
            import termios, tty
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self._get = lambda: sys.stdin.read(1)
        return self

    def __exit__(self, *exc):
        if os.name != 'nt':
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def get(self):
        return self._get()


def choose_port(kind, names, preset):
    if not names:
        sys.exit(f"{RED}No MIDI {kind} ports found.{RESET} Connect your MIDI interface and try again.")
    if preset is not None:
        if not 0 <= preset < len(names):
            sys.exit(f"{RED}MIDI {kind} port {preset} does not exist.{RESET}")
        say(f"MIDI {kind}: {names[preset]}")
        return preset
    say(f"{BOLD}Choose the MIDI {kind} port{RESET} "
        f"({'receives from the QY700 MIDI OUT' if kind == 'IN' else 'goes to the QY700 MIDI IN'}):")
    for i, n in enumerate(names):
        say(f"  {CYAN}{i}{RESET}  {n}")
    while True:
        try:
            raw = input(f"Port number [0-{len(names) - 1}]: ").strip()
        except EOFError:
            sys.exit(1)
        if raw.isdigit() and int(raw) < len(names):
            say()
            return int(raw)
        say(f"{YELLOW}Please type a number from the list.{RESET}")


# ---------------------------------------------------------------------------------------------- PNG writer
def save_png(path, data, scale):
    """data = the 9600 bytes as sent by the QY700 (already inverted): a set bit is a dark pixel."""
    rows = []
    for y in range(H):
        line = bytearray()
        for x in range(W):
            dark = data[y * 40 + x // 8] & (0x80 >> (x % 8))
            line += (b'\x00' if dark else b'\xff') * scale
        rows += [b'\x00' + bytes(line)] * scale

    def chunk(tag, body):
        return struct.pack('>I', len(body)) + tag + body + struct.pack('>I', zlib.crc32(tag + body) & 0xFFFFFFFF)

    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n'
                + chunk(b'IHDR', struct.pack('>IIBBBBB', W * scale, H * scale, 8, 0, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(b''.join(rows), 9))
                + chunk(b'IEND', b''))


# ---------------------------------------------------------------------------------------------- MIDI receive
def drain(midi_in):
    while midi_in.get_message():
        pass


TRANSFER_SECS = 6.3      # measured on a real QY700: 19204 bytes at MIDI speed


def bar(frac, label, t0):
    width = 30
    frac = max(0.0, min(1.0, frac))
    filled = int(frac * width)
    sys.stdout.write(f"\r  {label} [{'#' * filled}{'-' * (width - filled)}] {time.time() - t0:4.1f}s ")
    sys.stdout.flush()


def receive_dump(midi_in, timeout):
    """Wait for a complete F0 43 30 ... F7 message and return it (or None on timeout).

    rtmidi (CoreMIDI on macOS) normally hands over a SysEx message only once it is complete, so
    while waiting the bar is an estimate based on the usual ~6.3 s transfer time. If a MIDI driver
    delivers the SysEx in pieces instead, the bar switches to the real byte count."""
    buf = bytearray()
    t0 = last = time.time()
    chunked = False
    while True:
        msg = midi_in.get_message()
        now = time.time()
        if msg is None:
            if chunked and now - last > 3:                    # transfer started but stalled
                sys.stdout.write('\n')
                return None
            if not chunked and now - t0 > timeout:            # nothing arrived at all
                sys.stdout.write('\n')
                return None
            if not chunked:
                el = now - t0
                if el < TRANSFER_SECS:
                    bar(el / TRANSFER_SECS, 'Receiving (estimated)', t0)
                else:
                    bar(0.99, 'Still waiting...     ', t0)
            time.sleep(0.02)
            continue
        last = now
        for b in msg[0]:
            if b == 0xF0:                       # start of a SysEx message
                buf = bytearray([b])
                continue
            if not buf:
                continue                        # outside SysEx: ignore other MIDI messages
            buf.append(b)
            if len(buf) == 3 and tuple(buf) != HEADER:
                buf = bytearray()               # other SysEx, e.g. our own request echoed back
                continue
            if b == 0xF7:
                if tuple(buf[:3]) == HEADER:
                    bar(1.0, f'Received {len(buf)} bytes  ', t0)
                    sys.stdout.write('\n')
                    return bytes(buf)
                buf = bytearray()
        if len(buf) >= 3:                       # driver delivers SysEx in pieces: show real progress
            chunked = True
            bar(len(buf) / EXPECTED, f'Receiving {len(buf):5d}/{EXPECTED}', t0)


def decode(msg):
    nib = msg[3:-1]
    if len(nib) < W * H // 4:
        say(f"  {YELLOW}Warning: message is short ({len(nib)} of {W * H // 4} data bytes) - "
            f"image will be incomplete.{RESET}")
    nib = nib.ljust(W * H // 4, b'\x00')
    return bytes(((nib[i] & 0x0F) << 4) | (nib[i + 1] & 0x0F) for i in range(0, W * H // 4, 2))


# ---------------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description='Capture the Yamaha QY700 screen over MIDI.')
    ap.add_argument('--in', dest='port_in', type=int, help='MIDI input port number')
    ap.add_argument('--out', dest='port_out', type=int, help='MIDI output port number')
    ap.add_argument('--scale', type=int, choices=(1, 2, 3), default=1, help='starting PNG scale 1-3 (default 1 = 320x240); keys 1/2/3 change it')
    ap.add_argument('--timeout', type=float, default=12.0, help='seconds to wait for a reply (default 12)')
    ap.add_argument('--syx', action='store_true', help='also save the raw SysEx reply')
    ap.add_argument('--dir', default='.', help='folder to save captures in (default: current folder)')
    args = ap.parse_args()

    say(f"\n{BOLD}QY700 screen capture{RESET}\n")
    say(f"{BOLD}Before you start:{RESET}")
    say("  1. Connect  computer MIDI OUT -> QY700 MIDI IN   and   QY700 MIDI OUT -> computer MIDI IN.")
    say(f"  2. Switch the QY700 off. Hold {BOLD}[OCT DOWN] + [OCT UP] + the lowest key (E2){RESET},")
    say("     switch it on, and keep holding for a couple of seconds while it boots.")
    say(f"     {RED}Careful:{RESET} [OCT DOWN] + [OCT UP] + {BOLD}F#2{RESET} (the first black key) "
        f"wipes the QY700's memory!")
    say("  3. Make sure SysEx reception isn't filtered (UTILITY -> MIDI Filter).\n")

    midi_in, midi_out = rtmidi.MidiIn(), rtmidi.MidiOut()
    if hasattr(midi_in, 'set_buffer_size'):          # bigger SysEx buffers (matters on Windows)
        try:
            midi_in.set_buffer_size(32768, 4)
        except Exception:
            pass
    pin = choose_port('IN', midi_in.get_ports(), args.port_in)
    pout = choose_port('OUT', midi_out.get_ports(), args.port_out)
    midi_in.open_port(pin)
    midi_out.open_port(pout)
    midi_in.ignore_types(sysex=False, timing=True, active_sense=True)
    os.makedirs(args.dir, exist_ok=True)

    count = 0
    try:
        with KeyReader() as keys:
            scale = args.scale if args.scale in (1, 2, 3) else 1

            def scale_line():
                opts = []
                for n in (1, 2, 3):
                    label = f"[{n}] {n}x {W * n}x{H * n}"
                    opts.append(f"{GREEN}{BOLD}>{label}<{RESET}" if n == scale else f"{DIM} {label} {RESET}")
                sys.stdout.write("\r\033[K  Scale: " + "  ".join(opts))
                sys.stdout.flush()

            prompt = True
            while True:
                if prompt:
                    say(f"\n{BOLD}Press SPACE to dump the screen, 1/2/3 to change scale, Q to quit.{RESET}")
                    scale_line()
                    prompt = False
                k = keys.get()
                if k.lower() == 'q' or k in ('\x1b', '\x03', '\x04'):
                    say()
                    break
                if k in ('1', '2', '3'):
                    scale = int(k)
                    scale_line()
                    continue
                if k != ' ':
                    continue
                prompt = True
                say(f"\n  Capturing at {scale}x ({W * scale}x{H * scale})")
                drain(midi_in)
                say(f"  Sending screen dump request: {DIM}{' '.join(f'{b:02X}' for b in REQUEST)}{RESET}")
                midi_out.send_message(REQUEST)
                say(f"  Waiting for the QY700 (up to {args.timeout:g}s)...")
                msg = receive_dump(midi_in, args.timeout)
                if msg is None:
                    say(f"  {RED}No complete reply from the QY700 within {args.timeout:g}s.{RESET}")
                    say("  Check that:")
                    say("    - the QY700 was powered on while holding [OCT DOWN] + [OCT UP] + E2")
                    say("    - the MIDI cables go both ways and the right IN/OUT ports were chosen")
                    say("    - SysEx isn't blocked in UTILITY -> MIDI Filter")
                    continue
                stamp = datetime.datetime.now().strftime('%y%m%d_%H%M%S')
                base = os.path.join(args.dir, f'qy700_{stamp}')
                if os.path.exists(base + '.png'):
                    base += f'_{count}'
                save_png(base + '.png', decode(msg), scale)
                count += 1
                say(f"  {GREEN}Saved {base}.png{RESET}  ({len(msg)} bytes received)")
                if args.syx:
                    with open(base + '.syx', 'wb') as f:
                        f.write(msg)
                    say(f"  {GREEN}Saved {base}.syx{RESET}")
    except KeyboardInterrupt:
        pass
    finally:
        midi_in.close_port()
        midi_out.close_port()
    say(f"\nDone - {count} screen{'s' if count != 1 else ''} captured.")


if __name__ == '__main__':
    main()
