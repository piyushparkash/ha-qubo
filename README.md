# Qubo Smart Home for Home Assistant

A [HACS](https://hacs.xyz/)-installable custom integration that connects your [Qubo](https://quboworld.com/) (Hero Electronix) smart home devices to Home Assistant via Qubo's own cloud API.

Qubo's app and devices are **not** built on Tuya - they run their own cloud platform (`quboworld.com`). This integration talks to it through the unofficial, reverse-engineered [`qubopy`](https://github.com/selvakk2k/qubopy) client library, so no LocalTuya/Tuya Local setup will ever work for these devices.

## Supported devices today

| Series | Devices | Platform(s) |
| :--- | :--- | :--- |
| `HPH` | Smart Air Purifiers (Q200 - Q1000, including the Q400) | `fan`, `sensor` |

Each purifier shows up as a **Fan** entity (on/off, 3-step speed, `auto`/`sleep`/`manual` preset modes, child-lock state) plus **PM2.5** and **filter life** sensors.

### Roadmap

`qubopy` already models several other Qubo device types (smart plugs, bulbs, security/climate sensors, cameras, door locks), so this integration can grow beyond air purifiers. Those platforms just haven't been wired up here yet - contributions welcome. See [Contributing](#contributing).

## ⚠️ Before you install

- This relies entirely on `qubopy`, an **unofficial** library built by reverse-engineering the Android app. It is not affiliated with or endorsed by Hero Electronix / Qubo.
- Air purifier support in `qubopy` is marked `API Modeled / Untested` upstream - the request/response shapes are inferred from the app, not yet confirmed against physical purifier hardware by the library author. It is expected to work (the purifier commands are simple), but if something misbehaves, enable debug logging (below) and open an issue with the details.
- You are using Qubo's cloud account credentials. Use a dedicated **email**-registered Qubo account (see [Authentication notes](#authentication-notes)) rather than your primary phone-based account if you'd rather keep sessions separate.

## Installation via HACS

1. In Home Assistant, open **HACS → Integrations → ⋮ (top right) → Custom repositories**.
2. Add this repository URL: `https://github.com/piyushparkash/ha-qubo`, category **Integration**.
3. Find **Qubo Smart Home** in HACS and click **Download**.
4. Restart Home Assistant.
5. Go to **Settings → Devices & Services → Add Integration**, search for **Qubo**, and follow the prompts.

### Manual installation (without HACS)

Copy `custom_components/qubo` into your Home Assistant `config/custom_components/` directory, then restart Home Assistant and add the integration as above.

## Configuration

The integration asks for your Qubo account **email** and **password** - the same ones used in the Qubo mobile app.

### Authentication notes

- **Use an email-registered account.** Qubo issues a refreshable 180-day session only to email accounts. Accounts created via phone number get a 1-hour session with no refresh token, which is unusable for a 24/7 Home Assistant connection, and Qubo rejects email/password login for phone-registered accounts outright (`HTTP 400: ERR_SM_002`).
- If your only Qubo account was created via phone number, open the Qubo app, go to your profile, add/verify an email address, then register that email as its own login (you can safely reuse the same phone number for SMS verification during that signup).
- Home Assistant will show up as a new entry under **Qubo app → Account → Logged In Devices** (labeled "Home Assistant"). Logging that entry out from the app will disconnect Home Assistant until it re-authenticates.

### Debug logging

Add this to `configuration.yaml` to see detailed request/response logs from both the integration and `qubopy`:

```yaml
logger:
  default: info
  logs:
    custom_components.qubo: debug
    qubopy: debug
```

## How it works

- `custom_components/qubo` is a thin Home Assistant wrapper: a config flow for login, a `DataUpdateCoordinator` that polls the Qubo cloud shadow every 60 seconds, and `fan`/`sensor` entities backed by `qubopy`'s `QuboAirPurifier` model.
- All actual cloud communication (login, token refresh, device discovery, shadow reads, command dispatch) is handled by the `qubopy` PyPI package, pinned via `manifest.json`.

## Contributing

Issues and PRs are welcome, especially:
- Confirmation (or bug reports) from real Q200/Q400/Q500/Q1000 hardware.
- Additional platforms (`switch` for `HSP` plugs, `light` for `HLB` bulbs, `binary_sensor`/`sensor` for `HDS`/`HGS`/`HDT`, `lock` for `HLM`/`HLW`) following the same coordinator pattern used for the fan/sensor platforms here.

## Disclaimer

This project is independent and not affiliated with, endorsed by, or sponsored by Hero Electronix Pvt. Ltd. or Qubo. All trademarks are property of their respective owners. Use of Qubo's cloud API is subject to Qubo's Terms of Service.

## License

[MIT](LICENSE)
