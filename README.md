# Medisana Scale for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

> **Disclaimer:** unofficial, community-made integration. Not affiliated with, endorsed by, or supported by Medisana. Use at your own risk.

A Home Assistant custom integration for the **Medisana BS444 Connect** Bluetooth body composition scale (and its siblings BS410 / BS430 / BS440 — they share the same BLE protocol).

Reads weight, BMI and BMI category, body fat %, body water %, muscle %, bone, basal metabolism (kcal), and per-user profile data (age, height, gender, activity level) for up to 8 scale users — directly, over Bluetooth.

## Features

- Zero-configuration Bluetooth discovery — no MAC address to type
- "Step on your scale" flow if the scale isn't already in range
- Supports all 8 user slots the scale tracks internally
- **Per-user Home Assistant devices are created lazily** — no empty placeholder devices for unused slots
- Multiple scales supported — each adds as its own config entry
- Works with Home Assistant bluetooth proxies

## Requirements

- Home Assistant 2024.8 or later with a working Bluetooth stack (built-in adapter or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html))
- A Medisana BS-series scale with **at least one user profile configured on the scale itself** (see below)

### About the user profiles

Body composition (fat %, body water %, muscle %, bone, basal metabolism) is **only recorded when the scale recognises you as one of its eight user slots** — it needs your age, gender, height, and activity level to compute those values. Without a profile the scale still reads your weight but sends it as a "guest" weighing with every body-composition field set to zero.

**You configure profiles directly on the scale**, using its buttons (read instructions provided). Home Assistant does not (and cannot) create profiles — it only reads what the scale already knows. Follow the Medisana quick-start booklet that came with the scale (or search "Medisana BS444 set user profile" for the full procedure); the short version is:

1. Tap the scale to wake it up
2. Long press the **SET** button to enter setup mode
3. Pick a user slot (1–8) with the ▲ / ▼ arrows
4. Enter gender, birth date (year/month/day), height (cm), and activity level (normal / high) — pressing **SET** after each value
5. Step on the scale to let it associate your weight range with that slot
6. When weighing afterwards, wait on the scale until it shows your **user slot number** (1–8) next to the weight — that's the signal the scale recognised you and will transmit body composition data. If it only shows the weight with no user number, it logged you as a guest.

## Installation

### Via HACS (recommended)

> **Prerequisites:** [HACS](https://hacs.xyz/) installed in Home Assistant. If you don't have it yet, follow the [HACS installation guide](https://hacs.xyz/docs/use/download/download/).

**Step 1 — add the repository to HACS:**

[![Add Repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=massimiliano024&repository=ha-medisana-scale&category=integration)

Click the button above to add the repository. This opens HACS in your Home Assistant and adds the Medisana repository. If the button doesn't work, add it manually:

1. Open **HACS** → **Integrations** → click the three-dot menu (top right) → **Custom repositories**
2. Paste `https://github.com/massimiliano024/ha-medisana-scale/` as the URL
3. Select **Integration** as the category and click **Add**

**Step 2 — download the integration:**

1. In HACS, find **Medisana** in the list
2. Click **Download** (bottom right), pick the latest version, confirm
3. **Restart Home Assistant**

**Step 3 — add the integration:**

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=medisana)

Click the button above to start the setup, or go to **Settings → Devices & Services → + Add Integration** and search for **Medisana**.

### Manual

1. Copy the `custom_components/medisana/` folder into your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Setup

The scale only turns its Bluetooth radio on for a few seconds right after a weighing — so the setup flow is:

1. **Settings → Devices & Services → Add Integration → Medisana**
2. When the setup screen appears, **step on your scale and stand still** until the final weight is shown
3. Home Assistant will pick up the scale automatically within ~60 seconds and ask you to confirm

If Home Assistant's Bluetooth scanner already caught your scale in a previous weighing, you'll see a pre-populated "Discovered device" card under **Devices & Services** — click **Configure** and skip straight to the confirm step.

## What to expect after each weighing

Two things commonly trip people up on first use:

**1. Values take ~15–20 seconds to appear after you step off.** This is by design. The BS444 opens its Bluetooth window *before* it finishes computing body composition — if the integration connected immediately, it would get an empty reading. Instead it waits ~12 seconds for the scale to commit the weighing, then connects, syncs, and disconnects. Total latency from stepping off to the sensor updating is usually 15–20 seconds. Don't expect the reading to pop up the instant you step off.

**2. If the scale didn't recognise you, the reading goes to "Latest weight".** When the scale identifies you as one of its eight user slots, the full data (weight + body composition) lands on the matching per-user device (`Medisana scale user 1`, or `Alice`, etc.). When it *doesn't* — quick step-ons, shoes on, weight too far from the slot's baseline, no profile at all — the reading is tagged as guest. Body composition will be zero (the scale can't compute it without a profile), but the weight **is still captured** and lands on the top-level **Medisana scale** device under the `Latest weight` + `Last weighing` sensors. So you never lose a weigh-in, you just don't get body composition for it.

## Entities

Each configured scale produces one top-level **Medisana scale** device (the hardware) and up to eight **per-user sub-devices** (one per scale user slot 1–8). The per-user sub-devices are **created lazily** — a user 1 device only appears the first time the scale attributes a weighing to slot 1. Unused slots never clutter your Devices page.

You can give each slot a friendly name via **Settings → Devices & Services → Medisana → Configure** — the name you enter becomes the device name in HA, so a slot you've named "Alice" will produce `sensor.alice_weight`, `sensor.alice_bmi`, etc. Unnamed slots fall back to "Medisana scale user N".

### On the top-level "Medisana scale" device

Two scale-wide sensors that update on **every** weighing, including anonymous/guest ones where the scale didn't attribute the reading to a specific user:

| Entity | Unit | Notes |
|--------|------|-------|
| Latest weight | kg | Most recent weight the scale transmitted, whoever it was. Useful for quick step-ons (with shoes, without the full body-comp cycle) where no user gets attributed. |
| Last weighing | — | Timestamp of the most recent weighing |

### On each per-user sub-device

| Entity | Unit | Notes |
|--------|------|-------|
| Weight | kg | |
| BMI | — | Derived from weight + height on the scale's profile |
| BMI category | enum | `Underweight` / `Normal` / `Slightly overweight` / `Overweight` / `Obese` / `Severely obese`. WHO standard bands with a 25–27 "slightly overweight" sub-bucket |
| Body fat | % | |
| Body water | % | |
| Muscle | % | |
| Bone | kg | |
| Basal metabolism | kcal | Estimated BMR, computed by the scale from the user profile |
| Height | cm | From the scale's user profile |
| Age | years | From the scale's user profile |
| Last measurement | — | Timestamp of the most recent weighing attributed to this user |
| Gender | enum | `male` / `female`, from the scale's user profile |
| Activity level | enum | `normal` / `high`, from the scale's user profile |

Values update whenever a completed weighing attributed to that user lands in the scale's sync buffer. Unused slots never materialise until a matching weighing arrives.

## Known limitations

Things the integration genuinely can't fix on its own:

- **The scale's firmware sometimes tags completed weighings as "guest"** even with a user profile configured and the scale displaying `P1`–`P8`. When it does, body-composition fields are zero and the reading only lands on `Latest weight` (not the per-user device). There's no pattern to when this happens — firmware quirk. The scale still stores the reading internally; on a later weighing it may re-emit the missed ones attributed to the right user.
- **The BS444's BLE window is short and weak.** If your Bluetooth adapter / ESPHome proxy is more than ~2–3 m from the scale, advertisements get dropped and weighings can be missed entirely. We recommend either keeping the scale close to an adapter or setting up a dedicated [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) near the scale.
- **Missed weighings aren't lost forever.** The scale keeps each reading flagged "unsynced" until it's been successfully transmitted once. A later, successful connect will dump everything that's still pending, so skipped weighings catch up on the next good sync.

## Troubleshooting

- **"No scale picked up during the scan"** — make sure the scale is within Bluetooth range of your HA host (or ESPHome proxy) and that you stood on it long enough for the final weight to lock in. Try again.
- **Values don't update** — the scale won't broadcast if the battery is low; replace the AAA cells and re-weigh.
- **Weight updates but body fat / water / muscle / bone / kcal all sit at zero** — the scale is logging you as a guest because it didn't recognise you as one of its eight user slots. See [About the user profiles](#about-the-user-profiles) above: configure a profile on the scale using its buttons, then make sure the scale displays your user number (1–8) next to the weight during a weighing.

## Credits

Protocol reverse engineering credit goes entirely to:

- [bwynants/weegschaal](https://github.com/bwynants/weegschaal) — ESPHome external component
- [keptenkurk/BS440](https://github.com/keptenkurk/BS440) — original Python work
- [oliexdev/openScale](https://github.com/oliexdev/openScale) — device wiki

## License

MIT
