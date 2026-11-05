# Yea-Line

Turn a **Yealink MP50** (a USB phone that only speaks Microsoft Teams) into a normal **SIP phone**, using a small Windows app.

The MP50 is really a USB headset with buttons and indicator lights. Yea-Line reads its keypad and hook switch, drives its
"ringing" and "in call" lights, carries the call audio through the phone's handset and speaker, and bridges all of it to a SIP
PBX (tested with FreePBX / Asterisk). It can also talk to **Home Assistant Assist** by voice.

> Not affiliated with Yealink or Microsoft. Windows only.

## What it does

- Dial on the phone and pick up (or press the speaker key): Yea-Line places a SIP call. Incoming calls ring the phone.
- Registers as a SIP extension, answers the PBX's keep-alive pings, handles hang-up cleanly.
- Built-in **test call** (`#878` by default): a keypad voice menu with an echo test and microphone chooser.
- **Home Assistant Assist**: dial a number (`#999` by default) to talk to your voice assistant, or dial an extension on the PBX that
  forwards to Home Assistant's *Voice over IP* integration (see [pbx-examples](pbx-examples)).
- **Runs in the system tray**: close the window and it keeps taking calls, with a notification for incoming ones. Optional start with Windows (starts hidden).
- **Speed dial** (optional): turn it on in Settings, save numbers for slots 1 to 9, and choose the dial key: `*`, `#` or none (just the digit). Then dial e.g. `*1` and pick up.
- Call history, light/dark theme (follows Windows), live keypad and call state. Pages scroll, so it fits small laptop screens.
- Passwords (SIP secret, Home Assistant token) are kept in **Windows Credential Manager**, never in files.

## Install

Download `Yea-Line-Setup.exe` from the [latest release](../../releases/latest) and run it. It installs just for your user, needs no
administrator rights, and can add a desktop shortcut and start with Windows. Windows will ask once to allow Yea-Line through the
firewall: allow it (SIP replies need to come back to the app).

Then plug in the MP50 over USB, open **Settings**, enter your PBX address, extension and secret, and press *Save & connect*.

## Run from source

Python 3.13+ on Windows:

```
pip install -r requirements.txt
python app/mp50_ui.py
```

Build the exe and installer yourself (needs [Inno Setup 6](https://jrsoftware.org/isinfo.php)):

```
pip install pyinstaller
powershell -File packaging/build.ps1
```

## PBX and Home Assistant notes

These came from real debugging, and are the usual reasons a setup fails:

- **Audio only one way / no audio:** if your PBX sets `external_media_address` on its transport but no `local_net`, it tells LAN devices
  to send audio to its public IP. Set `local_net` (see `pbx-examples/pjsip.transports_custom_post.conf`).
- **Extension shows unavailable:** the PBX pings the extension with SIP `OPTIONS`; Yea-Line answers them.
- **Home Assistant "problem with the current voice assistant settings":** the Assist pipeline chosen for the VoIP device needs a
  **speech-to-text engine and no wake word**. A pipeline with a wake word makes HA wait forever; one without speech-to-text speaks the
  error. If a call is silent after changing pipelines, reload the *Voice over IP* integration.
- Home Assistant's VoIP integration only speaks **Opus**; allow `opus` on that endpoint (the PBX transcodes for your phone).

## Roadmap

- Several MP50 phones on one PC (each with its own extension).
- Dial on the phone and pick up with a saved name shown in call history.

## ESP32 gateway (experimental)

[`esp32-gateway`](esp32-gateway) is an unfinished experiment: an ESP32 that pairs with the MP50 **over Bluetooth** as if it were a mobile
phone, registers on the PBX itself, and has an echo test plus a configuration web page. It builds with PlatformIO (ESP-IDF) for the
classic ESP32 (not S3/C3). Calling and the echo test worked in testing; incoming calls and pairing polish are not done. See its README.

## How it works

The MP50 shows up as USB audio plus HID devices. Yea-Line listens to the telephony HID collection with Windows Raw Input, decodes
hook and keypad reports, and sends indicator reports back (off-hook, ring) so the phone's screen follows the call. Notes and the small
research scripts are in [tools](tools).

## License

GPL-3.0, see [LICENSE](LICENSE). Yea-Line uses [pyVoIP](https://github.com/tayler6000/pyVoIP) (GPL-3.0). Other libraries:
miniaudio (MIT), websocket-client (Apache-2.0), audioop-lts (PSF-2.0), Pillow (MIT-CMU). The ESP32 firmware uses Espressif's ESP-IDF
(Apache-2.0).
