# Ecowitt WS-2551 setup

Live weather data (outdoor temperature, solar W/m², humidity, wind, rain)
from the Ecowitt WS-2551 station — HP2551 console at `192.168.5.44` on the
IoT VLAN, same subnet as the AirTouch.

## How data gets out of the console

The HP2551 has **no pollable local API**. Its TCP port 45000 answers only
configuration commands (firmware: EasyWeather V1.5.9); the GW1000-style
live-data command is ignored — that API exists only on Ecowitt's headless
GW-series gateways.

The one local live-data path is **customized upload**: the console
HTTP-POSTs a form-encoded snapshot of all sensors to a server you configure,
on a fixed interval. `ecowitt_listener.py` (built on
[aioecowitt](https://pypi.org/project/aioecowitt/), the library Home
Assistant uses) is that server. The console always reports imperial units;
aioecowitt derives metric keys (`tempc`, `tempinc`, `solarradiation` in
W/m², `windspeedkmh`, `rainratemm`, …).

## One-time console configuration

In the **WS View Plus** app → your device → **Customized**, or on the
console itself: Setup → Weather Server → Customized Website:

| Setting         | Value                                    |
| --------------- | ---------------------------------------- |
| Protocol        | Ecowitt                                  |
| Server IP       | IP of the machine running the listener   |
| Path            | `/data/report/`                          |
| Port            | `8090`                                   |
| Upload interval | `60` seconds (minimum 16)                |
| Enabled         | on                                       |

As of 2026-07-05 customized upload was disabled on the console, so enabling
it doesn't clobber an existing setup.

**Firewall note:** unlike the AirTouch (which we poll), this is push — the
IoT VLAN initiates a connection *to* the listener's subnet. If IoT→LAN
traffic is blocked, allow TCP 8090 to the listener host, or run the
listener on a host in the IoT VLAN.

## Running the listener

```sh
source .venv/bin/activate
python ecowitt_listener.py                    # listen on port 8090
python ecowitt_listener.py --verbose          # also log every sensor key
```

One line per report:

```
19:05:46  out 10.7°C  in 20.1°C  sun 312.4W/m²  uv 2  hum 71%  wind 7.2km/h  rain 0.0mm/h
```

## Testing without the station

Simulate a console report:

```sh
curl -X POST http://127.0.0.1:8090/data/report/ \
  -d "PASSKEY=TEST&stationtype=EasyWeatherV1.5.9&dateutc=2026-07-05+02:10:00\
&tempinf=68.2&humidityin=55&baromrelin=29.92&tempf=51.3&humidity=71\
&winddir=180&windspeedmph=4.5&windgustmph=7.8&solarradiation=312.4&uv=2\
&rainratein=0.000&dailyrainin=0.110&model=HP2551_V1.5.9"
```
