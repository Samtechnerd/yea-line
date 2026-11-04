# Research tools

Small read-only scripts used to work out how the MP50 talks over USB. They only query the device; they never write to it.

- `hid_caps.py`: lists the phone's HID collections and their report sizes.
- `hid_outputs.py`: lists the output (host to phone) indicator usages of the telephony collections.

What was found: the telephony collection (usage page `0x0B`, usage `0x05`) sends 3-byte input reports (report ID 2) for hook and
keypad, and takes 2-byte output reports (report ID 2) for the Off-Hook, Ring, Mute, Hold and Microphone indicators (LED page `0x08`).
The phone only switches to its in-call screen if the Off-Hook indicator is set within moments of pick-up.
`app/mp50_led.py` shows how the indicator reports are built and sent.
