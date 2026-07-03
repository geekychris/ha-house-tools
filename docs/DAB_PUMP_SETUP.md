# DAB Pumps (DConnect) integration

This tool package doesn't ship the DAB integration itself — it lives in
HACS as a community integration. Follow these steps to bring your DAB
pump into HA, then use the solar-surplus automation below to run it
opportunistically when there's spare PV.

## 1. Install the HACS integration

1. HA UI → **HACS** → three-dot menu (top-right) → **Custom repositories**
2. Paste:
   - Repository: `https://github.com/pandaquests/hass-dabpumps`
   - Type: **Integration**
3. Click **Add**. Find "DAB Pumps" in the HACS integrations list →
   **Download**.
4. Settings → System → **Restart Home Assistant**.

## 2. Configure the integration

Settings → Devices & Services → **Add Integration** → search
"DAB Pumps". Enter:

- DConnect **email**
- DConnect **password**
- (Optional) installation name if you have multiple

HA discovers every pump under that account. Each becomes a device with
sensors for pressure, flow, water delivered, current draw, power,
temperature; controls (on/off, setpoint, restart-after-alarm) for
models that expose them.

## 3. Rename entities

The default entity names look like `sensor.dabpumps_<serial>_flow` —
functional but ugly. Rename to something friendlier:

- `switch.well_pump` (the main relay if one exists)
- `sensor.well_pump_power` (current draw in W)
- `sensor.well_pump_pressure` (bar)
- `sensor.well_pump_flow` (L/min)
- `sensor.well_pump_delivered` (L or gallons)

Do this once via the entity settings page. All downstream automations
target these names.

## 4. Wire up solar-surplus opportunistic pumping

Once the pump's HA entity is named, run:

```bash
HA_URL=http://ha.example.local:8123 HA_TOKEN=eyJ... \\
  PUMP_ENTITY=switch.well_pump \\
  PUMP_POWER_W=1200 \\
  python3 create_solar_surplus_automation.py
```

This creates an automation that turns the pump on when:

- Battery SoC ≥ 100% (fully charged, PV would otherwise be curtailed)
- PV output > house load + `PUMP_POWER_W` (spare PV to run the pump for free)
- Time is between 09:00 and 15:00 (peak solar hours)
- Pump was OFF for at least 30 minutes (avoid short-cycling)

And turns it off when:

- SoC drops below 95% (started drawing from battery)
- OR PV drops below load + pump draw (nearly cloudy)
- OR duration exceeded 60 min (checkpoint check)

## 5. (Optional) surface pump state in the dashboard

Add to the "Right Now" section of `create_energy_dashboard.py`:

```python
{"entity": "sensor.well_pump_power", "name": "Pump"},
```

And re-run `python3 create_energy_dashboard.py`. Now the current pump
draw shows next to Solar / Battery / Home / Grid.

## Troubleshooting

- **HACS integration doesn't discover the pump**: confirm DConnect app
  works from your phone with the same credentials. If the app works
  but HA doesn't, likely a token endpoint change on DAB's side; check
  the pandaquests/hass-dabpumps issues.
- **Solar-surplus automation never fires**: verify
  `sensor.smart_ac_status.attributes.soc` reaches 100. If your bank
  is smaller than your PV array you may already be curtailing at
  95-98%; adjust the SoC threshold in the automation.
- **Pump cycles too often**: raise the "was OFF for ≥ 30 min"
  minimum. Or add a "keep running for at least 20 min once on"
  guard in the automation's off-trigger conditions.
