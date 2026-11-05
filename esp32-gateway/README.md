# ESP32 Bluetooth gateway (experimental)

An ESP32 that pairs with the Yealink MP50 **over Bluetooth**, as if it were the phone's mobile, and turns what you dial into SIP calls
on your PBX. No PC needed once it is set up.

Status: parked. Dialling a PBX extension, the built-in echo test and **incoming calls** (the PBX rings the phone, you answer on the
MP50) work on real hardware. Known problems: about 1.5 s of extra round-trip audio delay on top of the network, and the call sounds
choppy ("bad signal"). The board's own audio queues were measured and are fine (the Bluetooth link runs at the right rate), so the
delay is probably inside the MP50's Bluetooth handling or the network path; not yet isolated. Also not done: pairing polish, DTMF
forwarding, wideband audio. The board logs audio statistics at the end of each call to help.

## Hardware

A **classic ESP32** (WROOM / WROVER, `ESP32-D0WD...`). The S3, C3, C6 and H2 have no Bluetooth Classic and cannot work.

## Build and flash

```
pip install platformio
cd esp32-gateway
pio run -t upload --upload-port COM3
```

Build from a **short folder path**; the ESP-IDF compiler test fails when the project path is very long on Windows.

## First setup

1. The board starts a Wi-Fi network called **Yea-Line-Setup** (open) until it has joined your Wi-Fi.
2. Join it and open `http://192.168.4.1`. Pick your Wi-Fi from the list, enter its password (eye icon shows what you type), then your
   PBX address, extension and secret. Save: the board restarts and joins your network.
3. Open the page again at the address your router gave the board. Press **Pair**, put the MP50 in Bluetooth pairing mode and choose
   *Yea-Line Gateway*.
4. Dial `#878` on the phone for the echo test (a beep, then your voice about a second later), or any extension for a SIP call.

The settings page has no password: use it on a trusted network only. The ESP32 and Yea-Line must not both register the same extension.
