# Foldtop workstation companion

Foldtop turns an iPhone into a small laptop for a Linux workstation: the
workstation's screen on the phone, with a keyboard and trackpad underneath. On an
iPhone Duo stood half-folded, the picture sits above the fold and the keyboard
below it.

This repository is the half that runs on the workstation. The other half is the
**Foldtop** app for iPhone.

- Setup guide and help: <https://think1-dev.github.io/foldtop-host/>
- Privacy policy: <https://think1-dev.github.io/foldtop-host/privacy.html>
- Problems and questions: <https://github.com/think1-dev/foldtop-host/issues>

## What you need

- A Linux workstation running an **X11 session**. Wayland is not supported.
  `echo $XDG_SESSION_TYPE` should print `x11`.
- Python 3.10 or newer and GStreamer 1.22 or newer from your distribution.
- An iPhone on iOS 27.1 or later with the Foldtop app.

It is developed and tested on Ubuntu 24.04 LTS (GNOME on Xorg, PipeWire). Other
distributions with the same pieces should work but have not been tried.

## Install

```sh
sudo apt install git python3-gi gir1.2-gstreamer-1.0 python3-cryptography \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly

git clone https://github.com/think1-dev/foldtop-host.git
cd foldtop-host
./install.sh            # shows what it would do and changes nothing
./install.sh --apply    # adds the foldtop-host command and a systemd user unit
systemctl --user enable --now foldtop-host
```

Nothing here runs as root. `install.sh --apply` writes two files: the command
`~/.local/bin/foldtop-host` and the unit `~/.config/systemd/user/foldtop-host.service`.
The unit starts the companion with your graphical session. If `foldtop-host` is
"not found" straight after installing, log out and in again so `~/.local/bin` is
on your `PATH`.

`foldtop-host doctor` checks the machine and says what is missing.

## Pair your iPhone

```sh
foldtop-host pair
```

This prints a QR code and an 8-character code. Both work once, for five minutes.
In the app choose **Pair a Workstation…** and scan the QR code, or pick the
workstation under **On This Network** and type the code.

## Firewall and other networks

The iPhone reaches the workstation on TCP port **47800**. If the workstation has a
firewall, allow that port for the networks you use, for example:

```sh
sudo ufw allow from 192.168.0.0/24 to any port 47800 proto tcp   # your home network
sudo ufw allow in on tailscale0 to any port 47800 proto tcp      # Tailscale
```

Workstations on the same Wi-Fi are found automatically (mDNS, through Avahi).
Away from home, a VPN such as Tailscale or WireGuard works: the app remembers
every address the workstation has, including its VPN address, after it has
connected once. Do not forward port 47800 from the internet to the workstation.

## Commands

| Command | What it does |
|---|---|
| `foldtop-host pair` | Show a pairing code and QR code |
| `foldtop-host status` | Say what the companion is doing |
| `foldtop-host devices` | List paired devices |
| `foldtop-host unpair <name or id>` | Stop accepting a device |
| `foldtop-host doctor` | Check this machine |
| `foldtop-host run` | Run in the foreground (the systemd unit does this for you) |

Settings live in `~/.config/foldtop/config.json` (optional). Keys: `port`, `bind`,
`name`, `advertise`, `encoder`, `maxFps`, `audio`, `micDefault`, `clipboard`,
`pasteKeys`. The same folder holds the workstation's TLS key and the list of paired
devices.

## Sound, microphone and clipboard

- **Sound** is captured from the default output and played on the phone. The
  workstation's own speakers are not muted.
- **Microphone**: while the phone's microphone is switched on in the app, the
  workstation has an input device called "Foldtop Microphone" and it becomes the
  default input. The previous default is restored afterwards.
- **Clipboard**: text copied on one device can be pasted on the other. Plain text
  only. `"clipboard": false` in `config.json` turns it off on the workstation.

Sound and microphone need PipeWire or PulseAudio with `pactl`.

## Security

- The connection is TLS with a certificate the workstation creates on first run.
  The app pins that certificate when it pairs and accepts no other.
- Pairing needs the one-time code from `foldtop-host pair`. The window closes after
  five minutes or five wrong attempts.
- Each paired device has its own key. `foldtop-host unpair` revokes one.
- A paired device can see the screen, hear the workstation, type and click as you,
  feed audio in as a microphone, and read and replace the clipboard while it is
  connected. Treat pairing like handing someone your keyboard.
- The companion talks only to paired devices. It sends nothing to the developer or
  to anyone else.

This design has not had an independent security review.

## Update or remove

```sh
cd foldtop-host && git pull && systemctl --user restart foldtop-host    # update

systemctl --user disable --now foldtop-host                             # remove
rm ~/.config/systemd/user/foldtop-host.service ~/.local/bin/foldtop-host
rm -r ~/.config/foldtop     # forgets every paired device
```

## Tests

```sh
./run_tests.sh
```

On a live desktop the tests read the screen, move the pointer and put it back.
Without a display they skip those and run the rest.

## Licence

MIT. See [LICENSE](LICENSE).

iPhone is a trademark of Apple Inc. Foldtop is not affiliated with Apple.
