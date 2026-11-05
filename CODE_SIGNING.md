# Code signing policy

Free code signing for Yea-Line is provided by [SignPath.io](https://signpath.io), with a certificate from the
[SignPath Foundation](https://signpath.org).

- **What is signed:** the Windows installer `Yea-Line-Setup.exe` (and the `Yea-Line.exe` inside it), only when built by the
  GitHub Actions workflow in `.github/workflows/build.yml` from a tagged release of this repository.
- **Who can approve a release:** the project owner, [@Samtechnerd](https://github.com/Samtechnerd).
- **Privacy:** Yea-Line does not send any data anywhere except to the servers you configure yourself (your SIP PBX and your
  Home Assistant). It has no telemetry and no update checks. Passwords are stored only in Windows Credential Manager.
