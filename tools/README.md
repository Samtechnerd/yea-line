# Research tools

Small read-only scripts used to work out how the MP50 talks over USB. They only query the device; they never write to it.

- `hid_caps.py`: lists the phone's HID collections and their report sizes.
- `hid_outputs.py`: lists the output (host to phone) indicator usages of the telephony collections.

What was found: the telephony collection (usage page `0x0B`, usage `0x05`) sends 3-byte input reports (report ID 2) for hook and
keypad, and takes 2-byte output reports (report ID 2) for the Off-Hook, Ring, Mute, Hold and Microphone indicators (LED page `0x08`).
The phone only switches to its in-call screen if the Off-Hook indicator is set within moments of pick-up.
`app/mp50_led.py` shows how the indicator reports are built and sent.

## USB recordings of the vendor channel (screen settings)

`usb_capture.py`, `usb_decode.py`, `mp50_vendor.py`, `mp50_replay.py`, `mp50_time.py`, `mp50_listen.py`: how the MP50's light/dark mode,
custom screensaver picture and screensaver delay were worked out. `usb_capture.py` records one device's traffic through the USBPcap
driver (it needs an administrator prompt, and a scan first to find the phone's USB address, which changes when it is replugged).
`mp50_vendor.py` documents the message layout and rebuilds recorded messages byte for byte as a check
(`python mp50_vendor.py recording.pcap`). The app uses `app/mp50_phone.py`. Note: one message type runs a shell command on the phone
(`date -s`, `rm`), so the app only ever builds those two fixed commands. Setting the clock was acknowledged by the phone but did not
change the displayed time, so it is not in the app.
