# PBX examples (FreePBX / Asterisk PJSIP)

Example config snippets that made Yea-Line and Home Assistant work on a FreePBX box. **The IP addresses are examples**
(`192.168.1.10` = the PBX, `192.168.1.20` = Home Assistant, `192.168.1.0/24` = your LAN): change them to yours.

| File | What it does |
|---|---|
| `pjsip.transports_custom_post.conf` | Declares the LAN with `local_net`, so devices on it get LAN addresses in SIP and audio setup instead of the PBX's public IP. Fixes "no audio". |
| `pjsip.endpoint_custom_post.conf` | Pins the media address for the Yea-Line extension. |
| `pjsip_custom.conf` | A `homeassistant` endpoint toward Home Assistant's *Voice over IP* integration (Opus allowed, own transport on UDP 5062). |
| `extensions_custom.conf` | Extension `700` forwards to Home Assistant Assist; also makes the `*43` echo test and other feature codes reachable. |

Put them in `/etc/asterisk/` (FreePBX keeps `*_custom*.conf` files across updates) and restart Asterisk; some FreePBX builds do not
reload transport changes.
